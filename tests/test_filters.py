"""Tests des filtres durs (stage, exclusions, IDF, fraîcheur)."""

from __future__ import annotations

from datetime import date, timedelta

import config
import filters
from normalize import Offre


def _offre(title="Stage IA", location="Paris", posted_at=""):
    return Offre(title, "ACME", location, "desc", "http://x", "test", posted_at, "")


def test_est_un_stage_mot_entier():
    assert filters.est_un_stage(_offre("Stage développeur"))
    assert filters.est_un_stage(_offre("Internship in AI"))
    # "international" ne doit PAS matcher "intern" (frontière de mot).
    assert not filters.est_un_stage(_offre("International Business Manager"))


def test_exclusions_alternance_senior():
    assert filters.est_exclu(_offre("Alternance data"))
    assert filters.est_exclu(_offre("Stage senior manager"))
    assert not filters.est_exclu(_offre("Stage junior IA"))


def test_idf_accepte_et_rejette_etranger():
    assert filters.est_en_idf(_offre(location="Paris, Île-de-France"))
    assert filters.est_en_idf(_offre(location="Boulogne-Billancourt"))
    assert not filters.est_en_idf(_offre(location="Paris, TX"))
    assert not filters.est_en_idf(_offre(location="Lyon"))
    # localisation vide -> permissif (True)
    assert filters.est_en_idf(_offre(location=""))


def test_idf_rejette_un_pays_etranger_annonce_dans_le_titre():
    """Cabinet basé à Paris, poste ailleurs : seul le titre le dit."""
    assert not filters.est_en_idf(
        _offre(title="Stage Data Scientist - IT (H/F) - Canada", location="Paris")
    )
    assert not filters.est_en_idf(_offre(title="Internship AI - London", location="Paris"))
    # Pas de faux positif : un titre français ordinaire passe toujours.
    assert filters.est_en_idf(_offre(title="Stage Ingénieur IA", location="Paris"))


def test_fraicheur(monkeypatch):
    recente = (date.today() - timedelta(days=2)).isoformat()
    vieille = (date.today() - timedelta(days=30)).isoformat()
    assert filters.est_recente(_offre(posted_at=recente))
    assert not filters.est_recente(_offre(posted_at=vieille))


def test_fraicheur_date_inconnue(monkeypatch):
    monkeypatch.setattr(config, "GARDER_SI_DATE_INCONNUE", False)
    assert not filters.est_recente(_offre(posted_at=""))
    monkeypatch.setattr(config, "GARDER_SI_DATE_INCONNUE", True)
    assert filters.est_recente(_offre(posted_at=""))


def test_filtrer_pipeline():
    recente = (date.today() - timedelta(days=1)).isoformat()
    offres = [
        _offre("Stage IA", "Paris", recente),          # gardée
        _offre("Alternance IA", "Paris", recente),     # exclue
        _offre("Stage IA", "Lyon", recente),           # hors IDF
        _offre("Chef de projet", "Paris", recente),    # pas un stage
    ]
    gardees = filters.filtrer(offres)
    assert len(gardees) == 1
    assert gardees[0].title == "Stage IA"
