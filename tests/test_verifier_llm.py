"""
Tests du branchement de la vérification sur llm.py (étape 2).

Couvre ce que les tests historiques de test_verifier.py ne voient pas : les
options réellement envoyées (num_ctx, keep_alive, schéma Pydantic), les
nouvelles tentatives, le modèle absent dans la boucle de shortlist, l'URL de
déchargement et l'estampille des réglages effectifs. Ollama est toujours mocké.
"""

from __future__ import annotations

import json

import pytest
import requests

import llm
import ollama_pool
import reference
import storage
import verifier
from normalize import Offre

VALIDE = {
    "pertinent": True, "est_alternance": False, "niveau": "stage",
    "domaine_match": True, "drapeaux_rouges": [], "justification": "ok", "score": 0.8,
}


def _offre(title="Stage IA cyber"):
    return Offre(title, "ACME", "Paris", "Machine learning, pentest. 6 mois.",
                 "http://x", "test", "", "")


class _Reponse:
    status_code = 200

    def __init__(self, texte):
        self._texte = texte

    def raise_for_status(self):
        pass

    def json(self):
        return {"response": self._texte, "done_reason": "stop"}


@pytest.fixture
def ollama(monkeypatch):
    """Faux /api/generate : rejoue des textes, enregistre les charges."""
    def installer(*textes):
        charges = []
        file = list(textes)

        def faux_post(url, json=None, timeout=None):  # noqa: A002
            charges.append({"url": url, **json})
            return _Reponse(file.pop(0) if len(file) > 1 else file[0])
        monkeypatch.setattr(llm.requests, "post", faux_post)
        return charges
    return installer


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------
def test_schema_envoye_exige_les_champs_de_decision():
    schema = verifier.VerdictLLM.model_json_schema()
    assert schema["required"] == ["pertinent", "est_alternance", "niveau",
                                  "domaine_match", "justification", "score"]
    assert list(schema["properties"])[-1] == "score"  # décidé EN DERNIER


def test_validation_tolere_domaine_absent():
    # Le schéma l'exige de la génération ; la validation, elle, reste aussi
    # tolérante que le parsing d'avant (défaut None).
    sortie = verifier.VerdictLLM.model_validate(
        {k: v for k, v in VALIDE.items() if k != "domaine_match"})
    assert sortie.domaine_match is None


# ---------------------------------------------------------------------------
# Charge envoyée
# ---------------------------------------------------------------------------
def test_options_centralisees_envoyees(ollama):
    charges = ollama(json.dumps(VALIDE))
    assert verifier.verifier(_offre()) is not None
    c = charges[0]
    assert c["model"] == "qwen3:4b"
    assert c["options"]["num_ctx"] == 8192
    assert c["options"]["temperature"] == 0
    assert c["keep_alive"] == "10m"
    assert c["think"] is False
    assert c["format"] == verifier.VerdictLLM.model_json_schema()


def test_variables_sf_appliquees(ollama, monkeypatch):
    monkeypatch.setenv("SF_LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("SF_LLM_NUM_CTX", "4096")
    monkeypatch.setenv("SF_OLLAMA_URL", "http://gpu:11434")
    charges = ollama(json.dumps(VALIDE))
    verifier.verifier(_offre())
    assert charges[0]["model"] == "gemma3:4b"
    assert charges[0]["options"]["num_ctx"] == 4096
    assert charges[0]["url"] == "http://gpu:11434/api/generate"


def test_arguments_explicites_prioritaires(ollama, monkeypatch):
    monkeypatch.setenv("SF_LLM_MODEL", "gemma3:4b")
    charges = ollama(json.dumps(VALIDE))
    verifier.verifier(_offre(), model="qwen3:1.7b", url="http://autre:1/")
    assert charges[0]["model"] == "qwen3:1.7b"
    assert charges[0]["url"] == "http://autre:1/api/generate"


# ---------------------------------------------------------------------------
# Nouvelles tentatives et échec explicite
# ---------------------------------------------------------------------------
def test_sortie_non_conforme_puis_valide(ollama):
    charges = ollama('{"pertinent": true}', json.dumps(VALIDE))
    v = verifier.verifier(_offre())
    assert v is not None and v.score == pytest.approx(0.8)
    assert len(charges) == 2


def test_echec_persistant_logue_la_cause_et_rend_none(ollama, caplog):
    charges = ollama("pas du json")
    with caplog.at_level("WARNING"):
        assert verifier.verifier(_offre("Stage X")) is None
    assert len(charges) == 3
    assert "Stage X" in caplog.text and "VerdictLLM" in caplog.text


def test_modele_absent_rend_none_avec_la_commande(monkeypatch, caplog):
    class _Absent(_Reponse):
        status_code = 404

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404")

        def json(self):
            return {"error": "model 'qwen3:4b' not found"}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Absent(""))
    with caplog.at_level("WARNING"):
        assert verifier.verifier(_offre()) is None
    assert "ollama pull qwen3:4b" in caplog.text


def test_jeton_pris_pendant_la_verification(monkeypatch):
    vu = []
    texte = json.dumps(VALIDE)

    def faux_post(url, json=None, timeout=None):  # noqa: A002
        vu.append(ollama_pool.JETON.locked())
        return _Reponse(texte)
    monkeypatch.setattr(llm.requests, "post", faux_post)
    verifier.verifier(_offre())
    assert vu == [True] and not ollama_pool.JETON.locked()


# ---------------------------------------------------------------------------
# Boucle de shortlist : modèle absent
# ---------------------------------------------------------------------------
def test_shortlist_modele_absent_signale_une_fois_et_garde_le_cosinus(monkeypatch):
    import main

    conn = storage.ouvrir(":memory:")
    en_cache, a, b = _offre("En cache"), _offre("Offre A"), _offre("Offre B")
    storage.save_verdict(conn, storage.hash_offre(en_cache), verifier.cle_cache("qwen3:4b"),
                         verifier.Verdict(True, 0.9, False, "stage", "cache").to_dict())

    monkeypatch.setattr(verifier, "ollama_disponible", lambda *a, **k: True)
    monkeypatch.setattr(llm, "modeles_installes", lambda r=None: {"bge-m3:latest"})
    appels = []
    monkeypatch.setattr(verifier, "verifier", lambda *a, **k: appels.append(1))
    phases = []

    resultat = main.verifier_shortlist(
        [(en_cache, 0.5), (a, 0.4), (b, 0.3)], conn, top_n=3, model="qwen3:4b",
        on_progress=phases.append,
    )
    assert appels == []  # aucun appel voué à l'échec
    absents = [p for p in phases if p["phase"] == "modele_absent"]
    assert len(absents) == 1 and "ollama pull qwen3:4b" in absents[0]["message"]
    scores = {o.title: s for o, s in resultat}
    assert scores["Offre A"] == pytest.approx(0.4)  # cosinus inchangé
    assert en_cache.verdict.justification == "cache"  # le cache sert toujours


def test_shortlist_toute_en_cache_ne_controle_pas_le_modele(monkeypatch):
    import main

    conn = storage.ouvrir(":memory:")
    o = _offre()
    storage.save_verdict(conn, storage.hash_offre(o), verifier.cle_cache("qwen3:4b"),
                         verifier.Verdict(True, 0.9, False, "stage", "cache").to_dict())
    monkeypatch.setattr(verifier, "ollama_disponible", lambda *a, **k: True)

    def interdit(*a, **k):
        raise AssertionError("contrôle de présence inutile : tout vient du cache")
    monkeypatch.setattr(llm, "modeles_manquants", interdit)
    main.verifier_shortlist([(o, 0.5)], conn, top_n=1, model="qwen3:4b")


# ---------------------------------------------------------------------------
# Diagnostic de démarrage
# ---------------------------------------------------------------------------
def test_diagnostic_tout_est_pret(monkeypatch):
    monkeypatch.setattr(llm, "modeles_installes", lambda r=None: {"qwen3:4b"})
    assert llm.diagnostic_demarrage(reglages=llm.charger_reglages(env={})) is None


def test_diagnostic_modele_manquant(monkeypatch):
    monkeypatch.setattr(llm, "modeles_installes", lambda r=None: set())
    message = llm.diagnostic_demarrage(reglages=llm.charger_reglages(env={}))
    assert "ollama pull qwen3:4b" in message


def test_diagnostic_ollama_injoignable_ne_leve_pas(monkeypatch):
    def panne(r=None):
        raise llm.OllamaIndisponible("Ollama injoignable")
    monkeypatch.setattr(llm, "modeles_installes", panne)
    assert "injoignable" in llm.diagnostic_demarrage(reglages=llm.charger_reglages(env={}))


# ---------------------------------------------------------------------------
# URL de déchargement et estampille
# ---------------------------------------------------------------------------
def test_dechargement_suit_sf_ollama_url(monkeypatch):
    monkeypatch.setenv("SF_OLLAMA_URL", "http://gpu:11434/")
    assert ollama_pool._base_url() == "http://gpu:11434"
    assert ollama_pool._base_url("http://explicite:1/") == "http://explicite:1"


def test_reglages_effectifs_estampilles(monkeypatch):
    valeurs = reference.constantes_ranking()
    assert valeurs["llm.EFFECTIF_MODELE"] == "qwen3:4b"
    assert valeurs["llm.EFFECTIF_NUM_CTX"] == 8192
    avant = reference.empreinte_config()
    monkeypatch.setenv("SF_LLM_MODEL", "gemma3:4b")
    assert reference.empreinte_config() != avant


def test_url_et_keep_alive_hors_estampille(monkeypatch):
    avant = reference.empreinte_config()
    monkeypatch.setenv("SF_OLLAMA_URL", "http://gpu:11434")
    monkeypatch.setenv("SF_LLM_KEEP_ALIVE", "30m")
    assert reference.empreinte_config() == avant
