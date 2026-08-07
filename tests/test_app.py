"""Tests de l'app web : enchaînement automatique et verrouillage des tâches.

On ne teste PAS la collecte réelle ni Ollama (lents, réseau) : les deux étapes
lourdes sont remplacées par des doublures. Ce qui est vérifié ici, c'est
l'ORCHESTRATION — l'ordre des étapes, la remise à zéro de l'état, et le refus
d'une seconde tâche pendant qu'une autre tourne.
"""

from __future__ import annotations

import pytest

import app as webapp
import jobs
import storage
from dedup import _cle as cle_identite
from normalize import Offre


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def etat_neuf():
    """Repart d'un état vierge avant chaque test (l'état est un global)."""
    webapp.ETAT["classees"] = []
    webapp.ETAT["ia_rang"] = {}
    webapp.ETAT["ia_score"] = {}
    webapp.ETAT["collecte"] = {"en_cours": False, "debut": None}
    webapp.ETAT["verif"] = {"en_cours": False, "fait": 0, "total": 0, "appels": 0,
                            "cache": 0, "indisponible": False, "modele": None}
    webapp.ETAT["auto"] = {"en_cours": False, "etape": 0}
    yield


def test_tout_enchaine_collecte_puis_verification(monkeypatch):
    """« Tout lancer » doit appeler la collecte PUIS la vérif, dans cet ordre."""
    appels = []

    def fausse_collecte(utiliser_jobspy):
        appels.append(("collecte", utiliser_jobspy))
        webapp.ETAT["classees"] = [("offre", 0.5)] * 7
        return 7

    def fausse_verif(n, modele):
        appels.append(("verif", n))

    monkeypatch.setattr(webapp, "_collecte", fausse_collecte)
    monkeypatch.setattr(webapp, "_verifier", fausse_verif)

    webapp._thread_tout(utiliser_jobspy=False, n_demande=3)

    assert appels == [("collecte", False), ("verif", 3)]
    # L'état est relâché : les boutons redeviennent cliquables.
    assert webapp.ETAT["auto"] == {"en_cours": False, "etape": 0}
    assert not webapp.ETAT["collecte"]["en_cours"]
    assert not webapp.ETAT["verif"]["en_cours"]


def test_tout_borne_la_shortlist_au_nombre_d_offres(monkeypatch):
    """Demander 50 vérifications sur 4 offres ne doit pas dépasser 4."""
    vus = []
    monkeypatch.setattr(webapp, "_collecte",
                        lambda utiliser_jobspy: webapp.ETAT.__setitem__("classees", [("o", 1)] * 4) or 4)
    monkeypatch.setattr(webapp, "_verifier", lambda n, modele: vus.append(n))

    webapp._thread_tout(utiliser_jobspy=True, n_demande=50)
    assert vus == [4]


def test_tout_saute_la_verification_si_aucune_offre(monkeypatch):
    """Collecte vide : on n'enchaîne pas sur une vérification qui n'a rien à lire."""
    vus = []
    monkeypatch.setattr(webapp, "_collecte", lambda utiliser_jobspy: 0)
    monkeypatch.setattr(webapp, "_verifier", lambda n, modele: vus.append(n))

    webapp._thread_tout(utiliser_jobspy=True, n_demande=10)
    assert vus == []
    assert webapp.ETAT["auto"]["en_cours"] is False


def test_tout_relache_l_etat_meme_en_cas_d_echec(monkeypatch):
    """Une collecte qui lève ne doit pas laisser l'interface bloquée."""
    def collecte_qui_casse(utiliser_jobspy):
        raise RuntimeError("réseau coupé")

    monkeypatch.setattr(webapp, "_collecte", collecte_qui_casse)
    with pytest.raises(RuntimeError):
        webapp._thread_tout(utiliser_jobspy=True, n_demande=5)
    assert webapp.ETAT["auto"]["en_cours"] is False
    assert webapp.ETAT["collecte"]["en_cours"] is False


def test_une_seule_tache_a_la_fois(client, monkeypatch):
    """Toute route de tâche répond 409 tant qu'une autre tâche tourne."""
    monkeypatch.setattr(webapp.threading, "Thread", lambda *a, **k: type(
        "FauxThread", (), {"start": lambda self: None})())

    assert client.post("/api/tout", json={"no_jobspy": True}).status_code == 200
    assert webapp.ETAT["auto"]["en_cours"] is True

    for route in ("/api/tout", "/api/rafraichir", "/api/verifier"):
        assert client.post(route, json={}).status_code == 409


def test_etat_expose_la_progression_auto_et_les_sources(client):
    etat = client.get("/api/etat").get_json()
    assert "auto" in etat and etat["auto"] == {"en_cours": False, "etape": 0}
    assert etat["sources"]  # la page affiche les sources réellement interrogées


# ---------------------------------------------------------------------------
# Persistance de la collecte WEB
#
# La collecte lancée depuis la page n'écrivait QUE `.rank_cache.json`. Les
# offres découvertes depuis l'interface n'entraient jamais dans `offres` :
# `/api/offers/<id>/cv` répondait 404 dessus, et `/api/cv/states` — qui itère
# sur `offres` — ne pouvait même pas les signaler `text_missing`.
# ---------------------------------------------------------------------------
def _offre(titre, texte):
    return Offre(titre, "ACME", "Paris", texte, f"http://{titre}", "test", "", "")


def test_la_collecte_web_persiste_offres_et_textes(tmp_path, monkeypatch):
    base = tmp_path / "t.db"
    monkeypatch.setattr(webapp.config, "CHEMIN_BASE", str(base))
    monkeypatch.setattr(webapp, "_sauver_cache", lambda: None)

    collectees = [(_offre("Stage IA", "Le texte de l'annonce IA."), 0.9),
                  (_offre("Stage Cyber", "Le texte de l'annonce cyber."), 0.7)]
    monkeypatch.setattr(webapp.main, "collecter_et_classer",
                        lambda utiliser_jobspy: collectees)

    assert webapp._collecte(utiliser_jobspy=False) == 2

    conn = storage.ouvrir(str(base))
    try:
        cles = {l["cle"] for l in conn.execute("SELECT cle FROM offres")}
        assert len(cles) == 2
        for cle in cles:
            assert jobs.lire_texte(conn, cle)
        # Formulation UI : plus aucune offre en `text_missing`.
        assert conn.execute(
            "SELECT COUNT(*) FROM offres o LEFT JOIN offres_texte t "
            "ON t.cle = o.cle WHERE t.cle IS NULL").fetchone()[0] == 0
    finally:
        conn.close()


def test_le_bouton_generer_cv_marche_sur_une_offre_tout_juste_collectee(
        tmp_path, monkeypatch, client):
    """Le bout en bout du défaut : collecter puis cliquer.

    Avant correction, ce POST renvoyait 404 OFFER_NOT_FOUND — l'offre venait
    d'apparaître à l'écran mais n'existait dans aucune table.
    """
    base = tmp_path / "t.db"
    master = tmp_path / "master.yaml"
    master.write_text("nom: Test\n", encoding="utf-8")
    monkeypatch.setattr(webapp.config, "CHEMIN_BASE", str(base))
    monkeypatch.setattr(webapp.config, "CV_MASTER_PATH", str(master))
    monkeypatch.setattr(webapp, "_sauver_cache", lambda: None)

    offre = _offre("Stage IA", "Missions : entraîner des modèles pendant 6 mois.")
    monkeypatch.setattr(webapp.main, "collecter_et_classer",
                        lambda utiliser_jobspy: [(offre, 0.9)])
    webapp._collecte(utiliser_jobspy=False)

    cle = cle_identite(offre)
    reponse = client.post(f"/api/offers/{cle}/cv")
    assert reponse.status_code == 202, reponse.get_json()
    assert reponse.get_json()["status"] == "pending"


def test_une_base_indisponible_ne_perd_pas_la_collecte(tmp_path, monkeypatch):
    """Dégradation : la page continue de servir le classement en mémoire."""
    monkeypatch.setattr(webapp.config, "CHEMIN_BASE",
                        str(tmp_path / "introuvable" / "t.db"))
    monkeypatch.setattr(webapp, "_sauver_cache", lambda: None)
    monkeypatch.setattr(webapp.main, "collecter_et_classer",
                        lambda utiliser_jobspy: [(_offre("Stage IA", "texte"), 0.9)])

    assert webapp._collecte(utiliser_jobspy=False) == 1
    assert len(webapp.ETAT["classees"]) == 1
