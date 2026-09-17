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


def test_liste_mots_de_contrat_adzuna_vide_rejetee(monkeypatch):
    monkeypatch.setattr(config, "ADZUNA_TITRES_EXIGES", [])
    with pytest.raises(ValidationError):
        valider_config()


def test_coherence_duree_min_vs_cible(monkeypatch):
    monkeypatch.setattr(config, "DUREE_MIN_ACCEPTABLE", 12)
    monkeypatch.setattr(config, "DUREE_CIBLE_MOIS", 6)
    with pytest.raises(ValidationError):
        valider_config()


def test_france_travail_au_dela_du_plafond_api_rejete(monkeypatch):
    monkeypatch.setattr(config, "FRANCE_TRAVAIL_RESULTATS", 151)
    with pytest.raises(ValidationError):
        valider_config()


def test_source_active_et_ecartee_rejetee(monkeypatch):
    monkeypatch.setattr(config, "SOURCES_ECARTEES_STAGES", {"adzuna": "raison"})
    with pytest.raises(ValidationError):
        valider_config()


def test_source_ecartee_inconnue_rejetee(monkeypatch):
    monkeypatch.setattr(config, "SOURCES_ECARTEES_STAGES", {"monster": "raison"})
    with pytest.raises(ValidationError):
        valider_config()


def test_source_ecartee_sans_raison_rejetee(monkeypatch):
    monkeypatch.setattr(config, "SOURCES_ECARTEES_STAGES", {"jooble": "  "})
    with pytest.raises(ValidationError):
        valider_config()


def test_un_site_jobspy_ecarte_est_accepte():
    valider_config()  # « jobspy:google » figure dans la config du dépôt


def test_un_site_jobspy_a_la_fois_interroge_et_ecarte_est_rejete(monkeypatch):
    monkeypatch.setattr(config, "SOURCES_ECARTEES_STAGES", {"jobspy:indeed": "raison"})
    with pytest.raises(ValidationError):
        valider_config()


def test_un_site_d_une_source_inconnue_est_rejete(monkeypatch):
    monkeypatch.setattr(config, "SOURCES_ECARTEES_STAGES", {"monster:fr": "raison"})
    with pytest.raises(ValidationError):
        valider_config()


def test_un_mot_adzuna_jobs_qui_n_est_pas_un_terme_est_rejete(monkeypatch):
    monkeypatch.setattr(config, "ADZUNA_MOTS_JOBS", ["vendeur", "caisse"])
    with pytest.raises(ValueError, match="caisse"):
        valider_config()


def test_les_mots_adzuna_jobs_du_depot_sont_des_termes():
    import recherche

    termes = {t.casefold() for t in recherche.lire(recherche.CHEMIN_RECHERCHE).student_jobs.termes}
    assert all(m.casefold() in termes for m in config.ADZUNA_MOTS_JOBS)

