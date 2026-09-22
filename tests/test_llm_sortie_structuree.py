"""
Tests des appels Ollama de llm.py : sorties structurées, embeddings, présence
des modèles.

Ollama n'est JAMAIS contacté : ``llm.requests.post`` / ``.get`` sont remplacés
par un faux serveur qui rejoue des réponses scriptées et enregistre les
charges reçues.
"""

from __future__ import annotations

import pytest
import requests
from pydantic import BaseModel

import llm
import ollama_pool


class Avis(BaseModel):
    pertinent: bool
    raison: str
    score: float


VALIDE = '{"pertinent": true, "raison": "IA au cœur du poste", "score": 0.8}'


class _Reponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class FauxOllama:
    """Rejoue ``reponses`` dans l'ordre ; garde chaque (url, charge) reçue."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels: list[tuple[str, dict]] = []
        self.jeton_tenu: list[bool] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - signature de requests
        self.appels.append((url, json))
        self.jeton_tenu.append(ollama_pool.JETON.locked())
        suivante = self.reponses.pop(0)
        if isinstance(suivante, Exception):
            raise suivante
        return suivante


def _generation(texte, done_reason="stop"):
    return _Reponse({"response": texte, "done": True, "done_reason": done_reason})


@pytest.fixture
def reglages():
    return llm.charger_reglages(env={})


@pytest.fixture
def ollama(monkeypatch):
    def installer(*reponses):
        faux = FauxOllama(reponses)
        monkeypatch.setattr(llm.requests, "post", faux.post)
        return faux
    return installer


# ---------------------------------------------------------------------------
# Charge envoyée
# ---------------------------------------------------------------------------
def test_charge_complete(ollama, reglages):
    faux = ollama(_generation(VALIDE))
    llm.generer_structure("juge", Avis, reglages=reglages)
    url, charge = faux.appels[0]
    assert url == "http://localhost:11434/api/generate"
    assert charge["model"] == "qwen3:4b"
    assert charge["format"] == Avis.model_json_schema()
    assert charge["think"] is False
    assert charge["stream"] is False
    assert charge["keep_alive"] == "10m"
    assert charge["options"]["temperature"] == 0
    assert charge["options"]["num_ctx"] == 8192


def test_schema_garde_l_ordre_des_champs():
    # Le score est émis EN DERNIER par la génération contrainte : l'ordre du
    # modèle Pydantic doit se retrouver tel quel dans le schéma envoyé.
    assert list(Avis.model_json_schema()["properties"]) == ["pertinent", "raison", "score"]


def test_jeton_tenu_pendant_l_appel_et_relache_apres(ollama, reglages):
    faux = ollama(_generation(VALIDE))
    llm.generer_structure("juge", Avis, reglages=reglages)
    assert faux.jeton_tenu == [True]
    assert not ollama_pool.JETON.locked()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_reponse_valide(ollama, reglages):
    ollama(_generation(VALIDE))
    avis = llm.generer_structure("juge", Avis, reglages=reglages)
    assert avis == Avis(pertinent=True, raison="IA au cœur du poste", score=0.8)


def test_preambule_think_tolere(ollama, reglages):
    ollama(_generation("<think>hmm</think>\n" + VALIDE))
    assert llm.generer_structure("juge", Avis, reglages=reglages).score == 0.8


def test_non_conforme_puis_valide_reprompte_avec_l_erreur(ollama, reglages):
    faux = ollama(_generation('{"pertinent": true}'), _generation(VALIDE))
    avis = llm.generer_structure("juge", Avis, reglages=reglages)
    assert avis.score == 0.8
    assert len(faux.appels) == 2
    second_prompt = faux.appels[1][1]["prompt"]
    assert second_prompt.startswith("juge")
    assert "pas conforme" in second_prompt and "raison" in second_prompt


def test_tronquee_double_le_budget_sans_toucher_au_prompt(ollama, reglages):
    faux = ollama(_generation('{"pertinent": true, "rai', "length"), _generation(VALIDE))
    llm.generer_structure("juge", Avis, reglages=reglages, num_predict=300)
    assert faux.appels[0][1]["options"]["num_predict"] == 300
    assert faux.appels[1][1]["options"]["num_predict"] == 600
    assert faux.appels[1][1]["prompt"] == "juge"


def test_budget_plafonne_par_num_ctx(ollama):
    r = llm.charger_reglages(env={"SF_LLM_NUM_CTX": "1000"})
    faux = ollama(_generation("{", "length"), _generation(VALIDE))
    llm.generer_structure("juge", Avis, reglages=r, num_predict=800)
    assert faux.appels[1][1]["options"]["num_predict"] == 1000


def test_echec_persistant_leve_une_erreur_explicite(ollama, reglages):
    faux = ollama(*[_generation("pas du json")] * 3)
    with pytest.raises(llm.SortieStructureeInvalide) as exc:
        llm.generer_structure("juge", Avis, reglages=reglages)
    assert len(faux.appels) == 1 + reglages.nouvelles_tentatives
    err = exc.value
    assert err.modele == "qwen3:4b" and err.schema == "Avis"
    assert err.tentatives == 3 and err.derniere_reponse == "pas du json"
    assert "qwen3:4b" in str(err) and "Avis" in str(err) and "pas du json" in str(err)


def test_echec_tronque_suggere_le_reglage(ollama, reglages):
    ollama(*[_generation('{"pertinent": tr', "length")] * 3)
    with pytest.raises(llm.SortieStructureeInvalide) as exc:
        llm.generer_structure("juge", Avis, reglages=reglages)
    assert exc.value.tronquee
    assert "SF_LLM_NUM_PREDICT" in str(exc.value)


def test_reponse_vide(ollama, reglages):
    ollama(*[_generation("")] * 3)
    with pytest.raises(llm.SortieStructureeInvalide, match="réponse vide"):
        llm.generer_structure("juge", Avis, reglages=reglages)


def test_zero_nouvelle_tentative(ollama):
    r = llm.charger_reglages(env={"SF_LLM_NOUVELLES_TENTATIVES": "0"})
    faux = ollama(_generation("x"))
    with pytest.raises(llm.SortieStructureeInvalide):
        llm.generer_structure("juge", Avis, reglages=r)
    assert len(faux.appels) == 1


# ---------------------------------------------------------------------------
# Pannes : pas de nouvelle tentative
# ---------------------------------------------------------------------------
def test_panne_reseau_sans_nouvelle_tentative(ollama, reglages):
    faux = ollama(requests.exceptions.ConnectionError("refusé"))
    with pytest.raises(llm.OllamaIndisponible, match="ollama serve"):
        llm.generer_structure("juge", Avis, reglages=reglages)
    assert len(faux.appels) == 1
    assert not ollama_pool.JETON.locked()


def test_modele_absent(ollama, reglages):
    ollama(_Reponse({"error": "model 'qwen3:4b' not found"}, status=404))
    with pytest.raises(llm.ModeleAbsent, match="ollama pull qwen3:4b"):
        llm.generer_structure("juge", Avis, reglages=reglages)


def test_erreur_http_remonte_le_message_d_ollama(ollama):
    r = llm.charger_reglages(env={"SF_LLM_MODEL": "gemma3:4b", "SF_LLM_THINK": "true"})
    ollama(_Reponse({"error": '"gemma3:4b" does not support thinking'}, status=400))
    with pytest.raises(llm.OllamaIndisponible, match="does not support thinking"):
        llm.generer_structure("juge", Avis, reglages=r)


def test_reponse_http_non_json(ollama, reglages):
    ollama(_Reponse(ValueError("html")))
    with pytest.raises(llm.OllamaIndisponible, match="non-JSON"):
        llm.generer_structure("juge", Avis, reglages=reglages)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def test_embed_par_lots_dans_l_ordre(ollama, reglages, monkeypatch):
    monkeypatch.setattr(llm.config, "MODELE_EMBEDDING", "ollama:bge-m3")
    faux = ollama(_Reponse({"embeddings": [[1.0], [2.0]]}),
                  _Reponse({"embeddings": [[3.0]]}))
    vecteurs = llm.embed(["a", "b", "c"], reglages=reglages, taille_lot=2)
    assert vecteurs == [[1.0], [2.0], [3.0]]
    assert [c["input"] for _, c in faux.appels] == [["a", "b"], ["c"]]
    url, charge = faux.appels[0]
    assert url.endswith("/api/embed")
    assert charge["model"] == "bge-m3"
    assert charge["keep_alive"] == "10m"
    assert faux.jeton_tenu == [True, True]


def test_embed_sans_modele_ollama_configure(ollama, reglages):
    faux = ollama()
    # Défaut du projet : MiniLM, servi par sentence-transformers, pas Ollama.
    with pytest.raises(llm.ErreurLLM, match="ollama:bge-m3"):
        llm.embed(["a"], reglages=reglages)
    assert faux.appels == []


def test_embed_modele_explicite(ollama, reglages):
    faux = ollama(_Reponse({"embeddings": [[1.0]]}))
    llm.embed(["a"], reglages=reglages, modele="nomic-embed-text")
    assert faux.appels[0][1]["model"] == "nomic-embed-text"


def test_embed_vide_sans_appel(ollama, reglages):
    faux = ollama()
    assert llm.embed([], reglages=reglages) == []
    assert faux.appels == []


def test_embed_nombre_de_vecteurs_incoherent(ollama, reglages):
    ollama(_Reponse({"embeddings": [[1.0]]}))
    with pytest.raises(llm.OllamaIndisponible, match="2 texte"):
        llm.embed(["a", "b"], reglages=reglages, modele="bge-m3")


# ---------------------------------------------------------------------------
# Présence des modèles
# ---------------------------------------------------------------------------
def _tags(monkeypatch, noms=None, exc=None, digests=None):
    def faux_get(url, timeout=None):
        if exc is not None:
            raise exc
        digests_ = digests or {}
        return _Reponse({"models": [{"name": n, "digest": digests_.get(n, "d-" + n)}
                                    for n in noms]})
    monkeypatch.setattr(llm.requests, "get", faux_get)


def test_modeles_tous_presents(monkeypatch, reglages):
    _tags(monkeypatch, ["qwen3:4b", "bge-m3:latest"])
    assert llm.modeles_manquants(["qwen3:4b", "bge-m3"], reglages) == []


def test_modele_manquant_donne_la_commande(monkeypatch, reglages):
    _tags(monkeypatch, ["bge-m3:latest"])
    manquants = llm.modeles_manquants(["qwen3:4b", "bge-m3"], reglages)
    assert manquants == ["qwen3:4b"]
    assert "ollama pull qwen3:4b" in llm.message_modeles_manquants(manquants)


def test_defaut_verifie_le_modele_de_generation(monkeypatch, reglages):
    _tags(monkeypatch, [])
    assert llm.modeles_manquants(reglages=reglages) == ["qwen3:4b"]


def test_serveur_injoignable_n_est_pas_un_modele_absent(monkeypatch, reglages):
    _tags(monkeypatch, exc=requests.exceptions.ConnectionError("refusé"))
    with pytest.raises(llm.OllamaIndisponible):
        llm.modeles_manquants(reglages=reglages)


def test_digest_d_un_modele(monkeypatch, reglages):
    _tags(monkeypatch, ["bge-m3:latest"], digests={"bge-m3:latest": "7907646426"})
    assert llm.digest_modele("bge-m3", reglages) == "7907646426"


def test_digest_d_un_modele_absent(monkeypatch, reglages):
    _tags(monkeypatch, ["qwen3:4b"])
    with pytest.raises(llm.ModeleAbsent, match="ollama pull bge-m3"):
        llm.digest_modele("bge-m3", reglages)
