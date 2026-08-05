"""Tests de la validation de configuration (pydantic)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import config
from config_schema import ConfigModel, valider_config


def test_config_reelle_est_valide():
    # La config du dépôt doit passer la validation.
    valider_config()


def test_poids_negatif_rejete(monkeypatch):
    monkeypatch.setattr(config, "BOOST_IA", -0.1)
    with pytest.raises(ValidationError):
        valider_config()


def test_seuil_dedup_hors_bornes(monkeypatch):
    monkeypatch.setattr(config, "SEUIL_DEDUP_FLOU", 1.5)
    with pytest.raises(ValidationError):
        valider_config()


def test_mode_composition_inconnu(monkeypatch):
    monkeypatch.setattr(config, "MODE_COMPOSITION_SCORE", "magique")
    with pytest.raises(ValidationError):
        valider_config()


def test_liste_termes_vide_rejetee(monkeypatch):
    monkeypatch.setattr(config, "TERMES_RECHERCHE", [])
    with pytest.raises(ValidationError):
        valider_config()


def test_coherence_duree_min_vs_cible(monkeypatch):
    monkeypatch.setattr(config, "DUREE_MIN_ACCEPTABLE", 12)
    monkeypatch.setattr(config, "DUREE_CIBLE_MOIS", 6)
    with pytest.raises(ValidationError):
        valider_config()
