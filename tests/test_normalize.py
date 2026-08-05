"""Tests des normaliseurs par source (fixtures JSON figées)."""

from __future__ import annotations

from normalize import Offre, normaliser


def test_normaliser_adzuna(charger_fixture):
    offres = normaliser("adzuna", charger_fixture("adzuna"))
    assert len(offres) == 2
    o = offres[0]
    assert isinstance(o, Offre)
    assert o.title == "Stage Intelligence Artificielle (H/F)"
    assert o.company == "ACME"
    assert o.location == "Paris, Île-de-France"
    assert o.url == "https://adzuna.example/1"
    assert o.source == "adzuna"
    # Bornes de salaire présentes -> chaîne lisible.
    assert "1200" in o.salary and "1500" in o.salary
    # Salaire absent (null/null) -> chaîne vide.
    assert offres[1].salary == ""


def test_normaliser_jooble(charger_fixture):
    offres = normaliser("jooble", charger_fixture("jooble"))
    assert len(offres) == 2
    assert offres[0].description.startswith("Analyse de données")
    assert offres[0].source == "jooble"
    assert offres[1].location == "Paris, TX"  # conservé tel quel (filtré plus tard)


def test_normaliser_jobspy_prefixe_site(charger_fixture):
    offres = normaliser("jobspy", charger_fixture("jobspy"))
    assert offres[0].source == "jobspy:indeed"
    assert offres[1].source == "jobspy:linkedin"
    assert "EUR" in offres[0].salary


def test_normaliser_france_travail(charger_fixture):
    offres = normaliser("france_travail", charger_fixture("france_travail"))
    assert len(offres) == 2
    o = offres[0]
    assert o.title == "Stage Ingénieur IA / Cybersécurité"
    assert o.company == "Eta"
    assert o.location == "Paris (75)"
    assert o.url == "https://francetravail.example/1"
    assert o.salary == "Selon profil"
    # salaire au libellé absent -> vide
    assert offres[1].salary == ""


def test_normaliser_careerjet(charger_fixture):
    offres = normaliser("careerjet", charger_fixture("careerjet"))
    # La 3e offre n'a pas d'URL : elle est écartée comme non exploitable.
    assert len(offres) == 2
    o = offres[0]
    # Balises <b> retirées et entités décodées, dans le titre comme dans le texte.
    assert o.title == "Stage - Intelligence artificielle & Cybersécurité"
    assert "<b>" not in o.description
    assert "intelligence artificielle" in o.description
    assert o.location == "Paris"
    assert o.source == "careerjet"
    assert o.posted_at.startswith("Thu, 30 Jul 2026")
    assert offres[1].company == ""


def test_normaliser_free_work(charger_fixture):
    offres = normaliser("free_work", charger_fixture("free_work"))
    assert len(offres) == 2
    o = offres[0]
    assert o.title == "Stage Machine Learning (6 mois)"
    assert o.company == "Zenith"
    assert o.location == "Paris, France"
    assert o.source == "free_work"
    # URL reconstruite depuis les slugs (l'API n'en fournit aucune).
    assert o.url == ("https://www.free-work.com/fr/tech-it/data-scientist"
                     "/job-mission/stage-machine-learning-6-mois")
    # HTML nettoyé, et <br /> remplacé par une espace (pas de mots recollés).
    assert "<p>" not in o.description
    assert "modèles. Stack Python & PyTorch." in o.description
    assert "18000" in o.salary and "22000" in o.salary
    # 2e offre : applicationUrl prioritaire, lieu de repli sur la région,
    # publishedAt absent -> createdAt, salaire absent -> chaîne vide.
    assert offres[1].url == "https://carriere.example/stage-cyber"
    assert offres[1].location == "Île-de-France"
    assert offres[1].posted_at.startswith("2026-07-27")
    assert offres[1].salary == ""


def test_sans_html_ne_recolle_pas_les_phrases():
    from normalize import _sans_html

    assert _sans_html("<p>Sécurité<br/>Vous serez</p>") == "Sécurité Vous serez"
    assert _sans_html("R&amp;D <b>IA</b>") == "R&D IA"
    assert _sans_html(None) == ""


def test_item_sans_titre_ou_url_est_ignore():
    # title vide -> non exploitable, ignoré (pas d'exception).
    brut = [{"title": "", "redirect_url": "http://x"},
            {"title": "Stage", "redirect_url": ""}]
    assert normaliser("adzuna", brut) == []


def test_source_inconnue_renvoie_vide():
    assert normaliser("inconnue", [{"title": "x"}]) == []
