"""
conftest.py — Configuration partagée des tests.

Rend le dossier racine du projet importable (config, normalize, filters…) et
expose un helper pour charger les fixtures JSON figées par source.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Racine du projet = parent du dossier tests/. On l'ajoute au sys.path pour que
# `import config`, `import normalize`, etc. fonctionnent quel que soit le CWD.
RACINE = Path(__file__).resolve().parent.parent
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def charger_fixture():
    """Retourne une fonction qui charge tests/fixtures/<nom>.json."""
    def _charger(nom: str):
        with open(FIXTURES / f"{nom}.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return _charger


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "config_reelle: le test lit la configuration RÉELLE du poste (.env et "
        "variables SF_* compris) au lieu d'un environnement neutre.",
    )
    config.addinivalue_line(
        "markers",
        "cv_forge: le test a besoin du paquet cv_forge (dépôt privé, installé "
        "à part) ; sauté quand il est absent, comme sur un clone public ou en CI.",
    )


def pytest_collection_modifyitems(config, items):
    import importlib.util

    if importlib.util.find_spec("cv_forge") is not None:
        return
    saut = pytest.mark.skip(reason="cv_forge non installé : génération de CV indisponible")
    for item in items:
        if "cv_forge" in item.keywords:
            item.add_marker(saut)


@pytest.fixture(autouse=True)
def _environnement_neutre(request, monkeypatch):
    """Les variables SF_* du poste (shell ou .env) ne décident d'aucun test.

    Sans ce filet, poser ``SF_LLM_MODEL=gemma3:4b`` dans son .env ferait
    échouer les tests qui vérifient les défauts — ou pire, les ferait passer
    pour de mauvaises raisons. Les tests qui veulent une variable la posent
    eux-mêmes (``monkeypatch.setenv``). Exception : le marqueur
    ``config_reelle``, dont c'est précisément le sujet.
    """
    if request.node.get_closest_marker("config_reelle"):
        return
    import os

    import reglages_env

    monkeypatch.setattr(reglages_env, "_lire_dotenv", lambda: None)
    for nom in [n for n in os.environ if n.startswith("SF_")]:
        monkeypatch.delenv(nom)

    # Aucun test ne dépend d'un Ollama lancé : le classement par défaut
    # tourne donc sur le modèle CPU de repli. Les tests du chemin Ollama
    # posent « ollama:… » eux-mêmes, avec Ollama mocké.
    import config

    monkeypatch.setattr(config, "MODELE_EMBEDDING", config.MODELE_EMBEDDING_REPLI)


@pytest.fixture(autouse=True)
def _sans_surcharge_locale(tmp_path, monkeypatch):
    """``recherche.local.yaml`` porte les vraies origines du poste : aucun
    test ne doit en dépendre. Chacun voit un chemin de surcharge absent."""
    import recherche

    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE_LOCALE",
                        str(tmp_path / "recherche.local.yaml"))
