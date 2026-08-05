"""Tests du catalogue de sources (sources/registry.py)."""

from __future__ import annotations

import pytest

import config
import normalize
from sources import registry


def test_toutes_les_sources_actives_sont_au_catalogue():
    """Garde-fou : une faute de frappe dans config.SOURCES_ACTIVES se voit ici."""
    inconnues = [n for n in config.SOURCES_ACTIVES if n not in registry.CATALOGUE]
    assert inconnues == []


def test_chaque_source_du_catalogue_a_un_normaliseur():
    """Une source sans normaliseur collecterait dans le vide (offres jetées)."""
    manquants = [n for n in registry.CATALOGUE if n not in normalize._NORMALISEURS]
    assert manquants == []


def test_sources_actives_ignore_les_noms_inconnus():
    resolues = registry.sources_actives(["adzuna", "n_existe_pas", "careerjet"])
    assert [s.nom for s in resolues] == ["adzuna", "careerjet"]


def test_jobspy_est_la_seule_source_sequentielle():
    """Le scraping doit rester hors du pool parallèle (politesse envers les sites)."""
    sequentielles = [n for n, s in registry.CATALOGUE.items() if s.sequentiel]
    assert sequentielles == ["jobspy"]


@pytest.mark.parametrize("nom", sorted(registry.CATALOGUE))
def test_le_module_de_chaque_source_est_importable(nom):
    """L'import paresseux doit aboutir sur un ``recuperer_offres`` appelable."""
    source = registry.CATALOGUE[nom]
    if nom == "jobspy":
        pytest.skip("JobSpy tire des dépendances lourdes : hors périmètre du test unitaire.")
    assert callable(source.recuperer())
