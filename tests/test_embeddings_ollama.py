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

    def embed(textes, *, reglages=None, modele=None, taille_lot=16):
        lots.append((modele, list(textes)))
        return [[float(len(t)), 1.0, float(sum(map(ord, t)) % 7)] for t in textes]
    monkeypatch.setattr(cache_embeddings.llm, "embed", embed)
    return lots


@pytest.fixture
def cache(tmp_path, monkeypatch):
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
    """Neutralise tout ce qui précède la dédup floue ; trace les encodages."""
    offres = [_offre("A"), _offre("B")]
    monkeypatch.setattr(main, "collecter", lambda utiliser_jobspy: list(offres))
    monkeypatch.setattr(main, "filtrer", lambda o: o)
    monkeypatch.setattr(main.observabilite, "journaliser", lambda: None)
    monkeypatch.setattr(main.extract, "annoter_toutes", lambda o: None)
    monkeypatch.setattr(main.dedup, "dedupliquer", lambda o: o)
    monkeypatch.setattr(main.dedup, "dedupliquer_flou", lambda o, e: (o, e))
    trace = {"encodages": [], "classer": None, "liberes": 0}

    def encoder(o, modele=None):
        trace["encodages"].append(modele)
        return np.eye(len(o))

    def classer(o, embeddings=None):
        trace["classer"] = embeddings
        return [(x, 0.5) for x in o]

    monkeypatch.setattr(main.ranker, "encoder_offres", encoder)
    monkeypatch.setattr(main.ranker, "classer", classer)
    monkeypatch.setattr(main.ranker, "liberer_modele",
                        lambda nom=None: trace.__setitem__("liberes", trace["liberes"] + 1))
    return trace


def test_meme_modele_vecteurs_partages(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", True)
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == [config.MODELE_EMBEDDING_DEDUP]
    assert pipeline["classer"] is not None  # réutilisés, pas ré-encodés
    assert pipeline["liberes"] == 1


def test_modeles_differents_la_dedup_garde_minilm(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", True)
    monkeypatch.setattr(config, "MODELE_EMBEDDING", "ollama:bge-m3")
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == ["paraphrase-multilingual-MiniLM-L12-v2"]
    assert pipeline["classer"] is None  # le classement encode avec SON modèle
    assert pipeline["liberes"] == 1


def test_sans_dedup_floue_pas_d_encodage_pour_rien(pipeline, monkeypatch):
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", False)
    main.collecter_et_classer(utiliser_jobspy=False)
    assert pipeline["encodages"] == []
    assert pipeline["classer"] is None


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
