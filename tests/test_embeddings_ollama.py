"""
Tests du backend d'embeddings Ollama (étape 3) : cache disque, encodeur du
ranker, séparation dédup / classement, enchaînement VRAM.

Ollama n'est jamais contacté : ``llm.embed`` et ``ollama_pool.decharger`` sont
remplacés par des doubles qui comptent leurs appels.
"""

from __future__ import annotations

import numpy as np
import pytest

import cache_embeddings
import config
import main
import ranker
import storage
import verifier
from normalize import Offre


@pytest.fixture
def faux_embed(monkeypatch):
    """Vecteur déterministe par texte ; garde la trace des lots envoyés."""
    lots = []

    def embed(textes, *, reglages=None, modele=None, taille_lot=16, apres_lot=None):
        lots.append((modele, list(textes)))
        vecteurs = [[float(len(t)), 1.0, float(sum(map(ord, t)) % 7)] for t in textes]
        if apres_lot is not None:  # comme le vrai : après chaque lot, cumul
            for fin in range(taille_lot, len(textes) + taille_lot, taille_lot):
                apres_lot(min(fin, len(textes)))
        return vecteurs
    monkeypatch.setattr(cache_embeddings.llm, "embed", embed)
    return lots


@pytest.fixture
def digests(monkeypatch):
    """Faux /api/tags : ``modele -> digest`` modifiable, lectures comptées."""
    etat = {"bge-m3": "poids-v1", "autre": "poids-a", "lectures": 0}

    def digest_modele(nom, reglages=None):
        etat["lectures"] += 1
        return etat[nom]
    monkeypatch.setattr(cache_embeddings.llm, "digest_modele", digest_modele)
    cache_embeddings.oublier_digests()
    yield etat
    cache_embeddings.oublier_digests()


@pytest.fixture
def cache(tmp_path, monkeypatch, digests):
    chemin = tmp_path / "emb.db"
    monkeypatch.setattr(config, "CHEMIN_CACHE_EMBEDDINGS", chemin)
    return chemin


def _offre(titre, desc="desc"):
    return Offre(titre, "ACME", "Paris", desc, f"http://{titre}", "test", "", "")


# ---------------------------------------------------------------------------
# Cache disque
# ---------------------------------------------------------------------------
def test_second_passage_sans_appel(faux_embed, cache):
    premier = cache_embeddings.encoder(["a", "bb"], "bge-m3")
    second = cache_embeddings.encoder(["a", "bb"], "bge-m3")
    assert len(faux_embed) == 1
    np.testing.assert_array_equal(premier, second)
    assert second.dtype == np.float32 and second.shape == (2, 3)


def test_seuls_les_manquants_partent_dedoublonnes(faux_embed, cache):
    cache_embeddings.encoder(["a"], "bge-m3")
    sortie = cache_embeddings.encoder(["b", "a", "b", "c"], "bge-m3")
    assert faux_embed[-1] == ("bge-m3", ["b", "c"])
    assert sortie.shape == (4, 3)
    np.testing.assert_array_equal(sortie[0], sortie[2])  # ordre d'entrée respecté


def test_le_modele_fait_partie_de_la_cle(faux_embed, cache):
    cache_embeddings.encoder(["a"], "bge-m3")
    cache_embeddings.encoder(["a"], "autre")
    assert [m for m, _ in faux_embed] == ["bge-m3", "autre"]


def test_le_cache_survit_au_processus(faux_embed, cache):
    cache_embeddings.encoder(["a"], "bge-m3")
    assert cache.is_file()
    faux_embed.clear()
    cache_embeddings.encoder(["a"], "bge-m3")
    assert faux_embed == []


def test_nouveaux_poids_invalident_le_cache(faux_embed, cache, digests):
    cache_embeddings.encoder(["a"], "bge-m3")
    digests["bge-m3"] = "poids-v2"          # `ollama pull` a changé les poids
    cache_embeddings.oublier_digests()      # run suivant
    cache_embeddings.encoder(["a"], "bge-m3")
    assert len(faux_embed) == 2


def test_digest_lu_une_fois_par_run(faux_embed, cache, digests):
    cache_embeddings.encoder(["a"], "bge-m3")
    cache_embeddings.encoder(["b"], "bge-m3")
    assert digests["lectures"] == 1
    cache_embeddings.oublier_digests()
    cache_embeddings.encoder(["a"], "bge-m3")
    assert digests["lectures"] == 2


def test_un_run_reouvre_la_lecture_des_digests(monkeypatch, digests):
    cache_embeddings.cle_modele("bge-m3")
    monkeypatch.setattr(main, "collecter", lambda utiliser_jobspy: [])
    main.collecter_et_classer(utiliser_jobspy=False)
    cache_embeddings.cle_modele("bge-m3")
    assert digests["lectures"] == 2


def test_liste_vide(faux_embed, cache):
    assert cache_embeddings.encoder([], "bge-m3").shape[0] == 0
    assert faux_embed == []


# ---------------------------------------------------------------------------
# Encodeur du ranker
# ---------------------------------------------------------------------------
def test_charger_un_modele_ollama_ne_charge_pas_sentence_transformers(monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("sentence-transformers ne doit pas être chargé")
    monkeypatch.setattr(ranker.embeddings_env, "preparer", interdit)
    encodeur = ranker._charger_modele("ollama:bge-m3")
    assert isinstance(encodeur, ranker.EncodeurOllama) and encodeur.nom == "bge-m3"


def test_encodeur_ollama_normalise(faux_embed, cache):
    vecteurs = ranker.EncodeurOllama("ollama:bge-m3").encode(["a", "bbb"],
                                                            normalize_embeddings=True)
    np.testing.assert_allclose(np.linalg.norm(vecteurs, axis=1), 1.0, rtol=1e-6)


def test_classement_complet_sur_ollama(faux_embed, cache, monkeypatch):
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    offres = [_offre("Stage IA"), _offre("Stage vente")]
    classees = ranker.classer(offres)
    assert len(classees) == 2
    assert all(m == "bge-m3" for m, _ in faux_embed)


def test_changer_de_modele_st_le_recharge(monkeypatch):
    charges = []

    class FauxST:
        max_seq_length = 128

        def __init__(self, nom):
            charges.append(nom)

    import sys
    from types import ModuleType

    faux = ModuleType("sentence_transformers")
    faux.SentenceTransformer = FauxST
    monkeypatch.setitem(sys.modules, "sentence_transformers", faux)
    monkeypatch.setattr(ranker.embeddings_env, "preparer", lambda nom=None: {"hors_ligne": True})
    monkeypatch.setattr(ranker, "_modele", None)
    monkeypatch.setattr(ranker, "_nom_modele", None)

    ranker._charger_modele("modele-a")
    ranker._charger_modele("modele-a")
    ranker._charger_modele("modele-b")
    assert charges == ["modele-a", "modele-b"]


def test_modeles_ollama_requis(monkeypatch):
    assert ranker.modeles_ollama_requis() == []
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    assert ranker.modeles_ollama_requis() == ["bge-m3"]


def test_liberer_ne_decharge_qu_un_modele_ollama(monkeypatch):
    decharges = []
    import ollama_pool

    monkeypatch.setattr(ollama_pool, "decharger", lambda m, **k: decharges.append(m))
    ranker.liberer_modele("paraphrase-multilingual-MiniLM-L12-v2")
    ranker.liberer_modele("ollama:bge-m3")
    assert decharges == ["bge-m3"]


# ---------------------------------------------------------------------------
# Pipeline : dédup sur SON modèle, classement sur le sien, puis libération
# ---------------------------------------------------------------------------
@pytest.fixture
def pipeline(monkeypatch):
    """Neutralise tout ce qui précède la dédup floue ; trace encodages et classements.

    Ollama est présent par défaut ; ``trace["ollama"]`` règle son état :
    "ok", "injoignable", "sans_modele", ou "panne_en_cours" (tombe pendant
    l'encodage).
    """
    offres = [_offre("A"), _offre("B")]
    monkeypatch.setattr(main, "collecter", lambda utiliser_jobspy: list(offres))
    monkeypatch.setattr(main, "filtrer", lambda o: o)
    monkeypatch.setattr(main.observabilite, "journaliser", lambda: None)
    monkeypatch.setattr(main.extract, "annoter_toutes", lambda o: None)
    monkeypatch.setattr(main.dedup, "dedupliquer", lambda o: o)
    monkeypatch.setattr(main.dedup, "dedupliquer_flou", lambda o, e: (o, e))
    trace = {"encodages": [], "classements": [], "liberes": [], "ollama": "ok"}

    def modeles_manquants(modeles, reglages=None):
        if trace["ollama"] == "injoignable":
            raise ranker.llm.OllamaIndisponible("Ollama injoignable sur http://localhost:11434")
        return list(modeles) if trace["ollama"] == "sans_modele" else []

    def encoder(o, modele=None):
        trace["encodages"].append(modele)
        return np.eye(len(o))

    def classer(o, embeddings=None, modele=None):
        trace["classements"].append((modele, embeddings is not None))
        if trace["ollama"] == "panne_en_cours" and ranker.est_ollama(modele):
            raise ranker.llm.OllamaIndisponible("connexion perdue")
        return [(x, 0.5) for x in o]

    monkeypatch.setattr(ranker.llm, "modeles_manquants", modeles_manquants)
    monkeypatch.setattr(main.ranker, "encoder_offres", encoder)
    monkeypatch.setattr(main.ranker, "classer", classer)
    monkeypatch.setattr(main.ranker, "liberer_modele",
                        lambda nom=None: trace["liberes"].append(nom))
    return trace


MINILM = "paraphrase-multilingual-MiniLM-L12-v2"


def test_meme_modele_vecteurs_partages(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", True)
    monkeypatch.setattr(config, "MODELE_EMBEDDING", MINILM)
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == [MINILM]
    assert pipeline["classements"] == [(MINILM, True)]  # réutilisés, pas ré-encodés
    assert pipeline["liberes"] == [MINILM]


def test_modeles_differents_la_dedup_garde_minilm(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", True)
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == [MINILM]
    assert pipeline["classements"] == [("ollama:bge-m3", False)]  # encode avec SON modèle
    assert pipeline["liberes"] == ["ollama:bge-m3"]


def test_sans_dedup_floue_pas_d_encodage_pour_rien(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", False)
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == []
    assert pipeline["classements"][0][1] is False


# ---------------------------------------------------------------------------
# Repli sur MiniLM quand Ollama manque
# ---------------------------------------------------------------------------
@pytest.fixture
def releve():
    r = main.observabilite.demarrer(sources=[])
    yield r
    main.observabilite.arreter()


@pytest.mark.parametrize("etat, fragment", [
    ("injoignable", "injoignable"),
    ("sans_modele", "ollama pull bge-m3"),
    ("panne_en_cours", "pendant l'encodage"),
])
def test_repli_sur_minilm(pipeline, monkeypatch, caplog, releve, etat, fragment):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", True)
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    pipeline["ollama"] = etat
    with caplog.at_level("WARNING"):
        classees = main.collecter_et_classer(utiliser_jobspy=False)
    assert len(classees) == 2  # le run se classe quand même
    # Le DERNIER classement, celui qui compte, est entièrement sur le repli,
    # et réutilise les vecteurs de la dédup (même modèle).
    assert pipeline["classements"][-1] == (MINILM, True)
    assert "REPLI" in caplog.text and fragment in caplog.text
    assert "pas comparables" in caplog.text
    assert releve.modele_embedding == MINILM and fragment in releve.repli_embedding


def test_sans_repli_le_releve_porte_le_modele_voulu(pipeline, monkeypatch, releve):
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    main.collecter_et_classer(utiliser_jobspy=False)
    assert releve.modele_embedding == "ollama:bge-m3"
    assert releve.repli_embedding is None
    assert releve.resume()["classement"] == {"modele_embedding": "ollama:bge-m3",
                                             "repli": None}


def test_modele_sentence_transformers_ne_consulte_pas_ollama(monkeypatch):
    def interdit(*a, **k):
        raise AssertionError("Ollama consulté pour un modèle CPU")
    monkeypatch.setattr(ranker.llm, "modeles_manquants", interdit)
    assert ranker.resoudre_modele(MINILM) == (MINILM, None)


def test_le_run_enregistre_le_modele_qui_a_servi():
    conn = storage.ouvrir(":memory:")
    bilan = {"classement": {"modele_embedding": MINILM, "repli": "Ollama injoignable"}}
    storage.enregistrer_run(conn, [(_offre("A"), 0.5)], bilan=bilan)
    run = storage.dernier_run(conn)
    assert run["modele_embedding"] == MINILM
    assert run["bilan"]["classement"]["repli"] == "Ollama injoignable"


def test_run_sans_bilan_reste_enregistrable():
    conn = storage.ouvrir(":memory:")
    storage.enregistrer_run(conn, [(_offre("A"), 0.5)])
    assert storage.dernier_run(conn)["modele_embedding"] is None


# ---------------------------------------------------------------------------
# Fin de lot de vérification : déchargement du LLM
# ---------------------------------------------------------------------------
def _shortlist(monkeypatch, conn, offres, appels_verifier):
    decharges = []
    monkeypatch.setattr(verifier, "ollama_disponible", lambda *a, **k: True)
    monkeypatch.setattr(main.llm, "modeles_manquants", lambda *a, **k: [])
    monkeypatch.setattr(main.ollama_pool, "decharger", lambda m, **k: decharges.append(m))

    def faux_verifier(*a, **k):
        appels_verifier.append(1)
        return verifier.Verdict(True, 0.5, False, "stage", "frais")
    monkeypatch.setattr(verifier, "verifier", faux_verifier)
    main.verifier_shortlist([(o, 0.5) for o in offres], conn, top_n=len(offres),
                            model="qwen3:4b")
    return decharges


def test_le_llm_est_decharge_en_fin_de_lot(monkeypatch):
    appels = []
    decharges = _shortlist(monkeypatch, storage.ouvrir(":memory:"),
                           [_offre("A"), _offre("B")], appels)
    assert len(appels) == 2
    assert decharges == ["qwen3:4b"]  # une fois, après le lot, pas entre les offres


def test_lot_servi_par_le_cache_ne_decharge_rien(monkeypatch):
    conn = storage.ouvrir(":memory:")
    o = _offre("A")
    storage.save_verdict(conn, storage.hash_offre(o), verifier.cle_cache("qwen3:4b"),
                         verifier.Verdict(True, 0.9, False, "stage", "cache").to_dict())
    appels = []
    assert _shortlist(monkeypatch, conn, [o], appels) == []
    assert appels == []
