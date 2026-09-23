"""
Étape en cours d'une recherche (progression.py) : écrite par le pipeline,
lue par l'app. Sans elle, l'encodage de centaines d'offres par bge-m3 laissait
la console muette et la page vide pendant des minutes.
"""

from __future__ import annotations

import logging

import pytest

import cache_embeddings
import config
import llm
import main
import progression
import ranker
from normalize import Offre


@pytest.fixture(autouse=True)
def _propre():
    progression.effacer()
    yield
    progression.effacer()


def _offre(titre):
    return Offre(titre, "ACME", "Paris", "desc", f"http://{titre}", "test", "", "")


def test_signaler_lire_effacer():
    assert progression.lire() == {}
    progression.signaler("Classement (bge-m3)", 16, 353)
    etat = progression.lire()
    assert (etat["libelle"], etat["fait"], etat["total"]) == ("Classement (bge-m3)", 16, 353)
    progression.effacer()
    assert progression.lire() == {}


def test_lire_rend_une_copie():
    progression.signaler("x", 1, 2)
    progression.lire()["fait"] = 99
    assert progression.lire()["fait"] == 1


# ---------------------------------------------------------------------------
# llm.embed : un rappel par lot
# ---------------------------------------------------------------------------
class _Reponse:
    status_code = 200

    def __init__(self, n):
        self.n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {"embeddings": [[1.0]] * self.n}


def test_embed_rappelle_apres_chaque_lot(monkeypatch):
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: _Reponse(len(json["input"])))
    vus = []
    llm.embed(["t"] * 35, reglages=llm.charger_reglages(env={}), modele="bge-m3",
              taille_lot=16, apres_lot=vus.append)
    assert vus == [16, 32, 35]


# ---------------------------------------------------------------------------
# cache_embeddings : avancement, textes en cache compris, log tous les 10 %
# ---------------------------------------------------------------------------
@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHEMIN_CACHE_EMBEDDINGS", tmp_path / "emb.db")
    monkeypatch.setattr(cache_embeddings.llm, "digest_modele", lambda nom, r=None: "d1")
    cache_embeddings.oublier_digests()

    def embed(textes, *, reglages=None, modele=None, taille_lot=16, apres_lot=None):
        for fin in range(taille_lot, len(textes) + taille_lot, taille_lot):
            apres_lot(min(fin, len(textes)))
        return [[float(i), 1.0] for i, _ in enumerate(textes)]
    monkeypatch.setattr(cache_embeddings.llm, "embed", embed)
    yield
    cache_embeddings.oublier_digests()


def test_progression_compte_les_textes_deja_en_cache(cache):
    cache_embeddings.encoder([f"a{i}" for i in range(10)], "bge-m3")
    vus = []
    textes = [f"a{i}" for i in range(10)] + [f"b{i}" for i in range(20)]
    cache_embeddings.encoder(textes, "bge-m3", progression=lambda f, t: vus.append((f, t)))
    assert vus == [(26, 30), (30, 30)]  # 10 en cache + 16, puis + 4


def test_log_d_avancement_tous_les_10_pourcent(cache, caplog):
    with caplog.at_level(logging.INFO, logger="cache_embeddings"):
        cache_embeddings.encoder([f"t{i}" for i in range(160)], "bge-m3")
    lignes = [r.getMessage() for r in caplog.records if "…" in r.getMessage()]
    assert len(lignes) == 10          # un palier par lot de 16 sur 160
    assert lignes[-1].endswith("160/160…")


def test_encodage_des_offres_publie_son_avancement(cache, monkeypatch):
    vus = []
    monkeypatch.setattr(ranker.progression, "signaler",
                        lambda libelle, fait=None, total=None: vus.append((libelle, fait, total)))
    ranker.encoder_offres([_offre(f"O{i}") for i in range(20)], modele="ollama:bge-m3")
    assert vus[-1] == ("Classement (bge-m3)", 20, 20)


# ---------------------------------------------------------------------------
# Pipeline : étapes nommées, effacées à la fin même en cas d'erreur
# ---------------------------------------------------------------------------
def test_la_collecte_publie_ses_etapes_et_s_efface(monkeypatch):
    vus = []
    reel = progression.signaler
    monkeypatch.setattr(progression, "signaler",
                        lambda libelle, fait=None, total=None: (vus.append(libelle),
                                                                reel(libelle, fait, total)))
    monkeypatch.setattr(main, "collecter", lambda utiliser_jobspy: [_offre("A"), _offre("B")])
    monkeypatch.setattr(main, "filtrer", lambda o: o)
    monkeypatch.setattr(main.observabilite, "journaliser", lambda: None)
    monkeypatch.setattr(main.extract, "annoter_toutes", lambda o: None)
    monkeypatch.setattr(main.dedup, "dedupliquer", lambda o: o)
    monkeypatch.setattr(config, "DEDUP_FLOUE_ACTIVE", False)
    monkeypatch.setattr(main.ranker, "classer_avec_repli",
                        lambda o, connus: ranker.Classement([(x, 0.5) for x in o], "m"))
    monkeypatch.setattr(main.ranker, "liberer_modele", lambda nom=None: None)
    main.collecter_et_classer(utiliser_jobspy=False)
    assert vus == ["Filtres et déduplication", "Classement (2 offres)"]
    assert progression.lire() == {}


def test_l_etape_est_effacee_meme_sur_erreur(monkeypatch):
    def collecte_qui_plante(utiliser_jobspy):
        progression.signaler("Collecte des sources", 1, 7)
        raise RuntimeError("panne réseau")
    monkeypatch.setattr(main, "collecter", collecte_qui_plante)
    with pytest.raises(RuntimeError):
        main.collecter_et_classer(utiliser_jobspy=False)
    assert progression.lire() == {}


def test_l_app_expose_l_etape_en_cours():
    import app as webapp

    progression.signaler("Classement (bge-m3)", 160, 353)
    etat = webapp.app.test_client().get("/api/etat").get_json()
    assert etat["progression"]["fait"] == 160 and etat["progression"]["total"] == 353
