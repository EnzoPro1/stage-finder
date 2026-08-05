"""Tests de l'app web : enchaînement automatique et verrouillage des tâches.

On ne teste PAS la collecte réelle ni Ollama (lents, réseau) : les deux étapes
lourdes sont remplacées par des doublures. Ce qui est vérifié ici, c'est
l'ORCHESTRATION — l'ordre des étapes, la remise à zéro de l'état, et le refus
d'une seconde tâche pendant qu'une autre tourne.
"""

from __future__ import annotations

import pytest

import app as webapp


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
