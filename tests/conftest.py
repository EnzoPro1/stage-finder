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
