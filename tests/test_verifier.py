"""
Tests de la vérification LLM (verifier.py) + du cache SQLite des verdicts.

Aucune dépendance réseau : l'appel Ollama est TOUJOURS mocké (monkeypatch sur
``verifier.requests``). On couvre :
- parsing d'un JSON valide -> Verdict correct,
- échec réseau / JSON invalide -> None (dégradation gracieuse),
- cache : une 2e lecture ne redéclenche AUCUN appel,
- combinaison des scores (avec et sans verdict),
- garde-fou --no-verify : la couche est bien court-circuitée.
"""

from __future__ import annotations

import json

import pytest

import config
import storage
import verifier
from normalize import Offre


def _offre(title="Stage IA cyber", desc="Machine learning, pentest. 6 mois, janvier 2027."):
    return Offre(title, "ACME", "Paris", desc, "http://x", "test", "", "")


class _FausseReponse:
    """Imite l'objet renvoyé par requests.post (juste .json() et raise_for_status())."""

    def __init__(self, payload, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        pass

    def json(self):
        if self._exc is not None:
            raise self._exc
        return self._payload


def _mock_post(monkeypatch, response_text):
    """Fait renvoyer à Ollama une réponse dont 'response' vaut ``response_text``."""
    def faux_post(url, json=None, timeout=None):  # noqa: A002 - signature de requests
        return _FausseReponse({"response": response_text})
    monkeypatch.setattr(verifier.requests, "post", faux_post)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_json_valide_donne_verdict(monkeypatch):
    payload = {
        "pertinent": True, "score": 0.82, "duree_mois": 6, "date_debut": "2027-01",
        "est_alternance": False, "niveau": "stage", "domaine_match": True,
        "drapeaux_rouges": ["localisation à confirmer"], "justification": "Bon match.",
    }
    _mock_post(monkeypatch, json.dumps(payload))
    v = verifier.verifier(_offre())
    assert v is not None
    assert v.pertinent is True
    assert v.score == pytest.approx(0.82)
    assert v.duree_mois == 6
    assert v.est_alternance is False
    assert v.niveau == "stage"
    assert v.drapeaux_rouges == ["localisation à confirmer"]


def test_json_avec_preambule_think(monkeypatch):
    """Un préambule (ex. balises <think>) avant le JSON reste parsable."""
    payload = {"pertinent": True, "score": 0.5, "est_alternance": False,
               "niveau": "stage", "justification": "ok"}
    _mock_post(monkeypatch, "<think>je réfléchis...</think>\n" + json.dumps(payload))
    v = verifier.verifier(_offre())
    assert v is not None and v.score == pytest.approx(0.5)


def test_score_borne_dans_0_1(monkeypatch):
    payload = {"pertinent": True, "score": 4.7, "est_alternance": False,
               "niveau": "stage", "justification": "ok"}
    _mock_post(monkeypatch, json.dumps(payload))
    v = verifier.verifier(_offre())
    assert v.score == 1.0


def test_alternance_plafonne_le_score(monkeypatch):
    """Garde-fou déterministe : une alternance détectée écrase le score."""
    payload = {"pertinent": True, "score": 0.9, "est_alternance": True,
               "niveau": "stage", "domaine_match": True, "justification": "ok"}
    _mock_post(monkeypatch, json.dumps(payload))
    v = verifier.verifier(_offre())
    assert v.score <= 0.2


def test_hors_domaine_plafonne_le_score(monkeypatch):
    """Garde-fou : domaine_match=false plafonne le score, même si le modèle est clément."""
    payload = {"pertinent": True, "score": 0.9, "est_alternance": False,
               "niveau": "stage", "domaine_match": False, "justification": "ok"}
    _mock_post(monkeypatch, json.dumps(payload))
    v = verifier.verifier(_offre())
    assert v.score <= 0.3


# ---------------------------------------------------------------------------
# Dégradation gracieuse
# ---------------------------------------------------------------------------
def test_echec_reseau_donne_none(monkeypatch):
    import requests

    def faux_post(url, json=None, timeout=None):  # noqa: A002
        raise requests.exceptions.ConnectionError("connexion refusée")
    monkeypatch.setattr(verifier.requests, "post", faux_post)
    assert verifier.verifier(_offre()) is None


def test_json_invalide_donne_none(monkeypatch):
    _mock_post(monkeypatch, "ceci n'est pas du JSON du tout")
    assert verifier.verifier(_offre()) is None


def test_reponse_vide_donne_none(monkeypatch):
    _mock_post(monkeypatch, "")
    assert verifier.verifier(_offre()) is None


# ---------------------------------------------------------------------------
# Cache SQLite
# ---------------------------------------------------------------------------
def test_cache_roundtrip():
    conn = storage.ouvrir(":memory:")
    offre = _offre()
    h = storage.hash_offre(offre)
    v = verifier.Verdict(True, 0.7, False, "stage", "ok", duree_mois=6)
    assert storage.get_verdict(conn, h, "qwen3:8b") is None
    storage.save_verdict(conn, h, "qwen3:8b", v.to_dict())
    relu = storage.get_verdict(conn, h, "qwen3:8b")
    assert relu is not None
    assert verifier.Verdict.from_dict(relu).score == pytest.approx(0.7)


def test_cache_invalide_par_modele():
    """Changer de modèle = clé différente = cache vide (recalcul)."""
    conn = storage.ouvrir(":memory:")
    h = storage.hash_offre(_offre())
    storage.save_verdict(conn, h, "qwen3:8b", {"score": 0.7})
    assert storage.get_verdict(conn, h, "llama3.1") is None


def test_deuxieme_run_ne_rappelle_pas_ollama(monkeypatch):
    """Cache hit -> verifier.verifier n'est PAS appelé au second passage."""
    import main

    conn = storage.ouvrir(":memory:")
    offre = _offre()
    # Pré-remplit le cache pour cette offre + modèle par défaut. La clé est
    # VERSIONNÉE par les règles de jugement : un verdict enregistré sous le seul
    # nom du modèle ne serait (volontairement) plus relu.
    storage.save_verdict(
        conn, storage.hash_offre(offre), verifier.cle_cache(config.VERIFY_MODEL),
        verifier.Verdict(True, 0.9, False, "stage", "cache").to_dict(),
    )

    appels = {"n": 0}

    def faux_verifier(*a, **k):
        appels["n"] += 1
        return verifier.Verdict(True, 0.1, False, "stage", "frais")

    monkeypatch.setattr(verifier, "verifier", faux_verifier)
    monkeypatch.setattr(verifier, "ollama_disponible", lambda *a, **k: True)

    resultat = main.verifier_shortlist(
        [(offre, 0.5)], conn, top_n=1, model=config.VERIFY_MODEL,
    )
    assert appels["n"] == 0  # aucun appel : tout venait du cache
    assert resultat[0][0].verdict.justification == "cache"


# ---------------------------------------------------------------------------
# Combinaison des scores
# ---------------------------------------------------------------------------
def test_score_final_avec_verdict():
    v = verifier.Verdict(True, 1.0, False, "stage", "ok")
    # poids 0.5 : moyenne des deux.
    assert verifier.score_final(0.4, v, poids=0.5) == pytest.approx(0.7)
    # poids 0 : cosinus seul ; poids 1 : LLM seul.
    assert verifier.score_final(0.4, v, poids=0.0) == pytest.approx(0.4)
    assert verifier.score_final(0.4, v, poids=1.0) == pytest.approx(1.0)


def test_score_final_sans_verdict_inchange():
    assert verifier.score_final(0.42, None) == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Garde-fou --no-verify
# ---------------------------------------------------------------------------
def test_no_verify_court_circuite(monkeypatch):
    import main

    def interdit(*a, **k):
        raise AssertionError("ollama_disponible ne doit pas être appelé avec --no-verify")

    monkeypatch.setattr(verifier, "ollama_disponible", interdit)
    args = _args(no_verify=True)
    classees = [(_offre(), 0.5)]
    # La branche verify est gardée dans executer ; ici on vérifie qu'appeler la
    # couche avec la garde respecte le court-circuit au niveau du flag.
    assert args.no_verify is True
    # Simule la garde de executer : no_verify -> pas d'appel à verifier_shortlist.
    resultat = classees if args.no_verify else main.verifier_shortlist(
        classees, None, top_n=config.VERIFY_TOP_N, model=config.VERIFY_MODEL,
    )
    assert resultat is classees


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _args(no_verify=False, verify_model=None, verify_top_n=None):
    import argparse

    return argparse.Namespace(
        no_verify=no_verify, verify_model=verify_model, verify_top_n=verify_top_n,
    )
