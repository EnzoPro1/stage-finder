"""Tests des normaliseurs par source (fixtures JSON figées)."""

from __future__ import annotations

import pytest

import normalize
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

# ---------------------------------------------------------------------------
# Échappements Unicode amputés (Adzuna)
#
# Adzuna livre « Hu002FF » là où l'annonce dit « H/F » : l'antislash de
# l'échappement JSON a disparu quelque part en amont. Ce n'est pas qu'un
# problème d'affichage — `dedup._normaliser_titre` retire les mentions de genre
# pour que « (H/F) » et « (M/F) » donnent UNE clé, et « Hu002FF » n'est pas
# reconnu comme telle. La même annonce occupait donc deux lignes.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brut, attendu", [
    ("Hu002FF", "H/F"),
    ("Data Scientist (Hu002FF) - Core platform", "Data Scientist (H/F) - Core platform"),
    ("Stage de césure u002F fin d'étude", "Stage de césure / fin d'étude"),
    ("Consultant.e Cybergouvernance u002F Cybersécurité",
     "Consultant.e Cybergouvernance / Cybersécurité"),
    ("pru00e9-embauche", "pré-embauche"),          # é
    ("compu00e8tences", "compètences"),            # è
    ("u00e0 pourvoir", "à pourvoir"),              # à
])
def test_les_echappements_ampute_sont_restaures(brut, attendu):
    assert normalize._texte(brut) == attendu


def test_un_mot_francais_ordinaire_n_est_pas_massacre():
    """Le motif large `uXXXX` mordrait sur « aubade » : « ubade » est composé
    de quatre chiffres hexadécimaux valides. C'est la raison pour laquelle
    seul `u00XX` est décodé."""
    for mot in ("aubade", "cubade", "adubade", "une facade", "aubaine"):
        assert normalize._texte(mot) == mot


def test_une_url_traverse_sans_dommage():
    url = "https://www.adzuna.fr/details/5870794318?utm_medium=api&utm_source=4d2c2076"
    assert normalize._texte(url) == url


def test_le_desechappement_atteint_le_titre_et_la_description():
    offres = normalize.normaliser("adzuna", [{
        "title": "Stage - Consultant cybersécurité SECOP - PKI - Hu002FF",
        "company": {"display_name": "Synetis"},
        "location": {"display_name": "Paris"},
        "description": "Mission de pru00e9-embauche u002F CDI.",
        "redirect_url": "http://x",
    }])
    assert offres[0].title.endswith("PKI - H/F")
    assert offres[0].description == "Mission de pré-embauche / CDI."


def test_la_mention_de_genre_redevient_deduplicable():
    """Le vrai enjeu : `dedup` doit retrouver la mention de genre après coup."""
    import dedup

    brut = normalize._texte("Data Scientist (Hu002FF)")
    assert dedup._normaliser_titre(brut) == dedup._normaliser_titre("Data Scientist (H/F)")


def test_france_travail_decode_les_entites_html_du_titre_et_de_la_description():
    """Titre réel du run du 2026-09-17, livré avec l'entité non décodée."""
    brut = {
        "id": "6942947",
        "intitule": "&#128663; Vendeur(se) itinérant(e) - Pièces de rechange automobiles - Secteur 94 / 78 et 91 H/F",
        "description": "Vous &amp; votre v&eacute;hicule : &lt; 2 ans d'exp&eacute;rience &gt; bienvenus.",
        "lieuTravail": {"libelle": "91 - Morangis"},
        "origineOffre": {"urlOrigine": "https://candidat.francetravail.fr/offres/recherche/detail/6942947"},
        "dateCreation": "2026-09-16T10:00:00.000Z",
    }
    (offre,) = normaliser("france_travail", [brut])
    assert offre.title.startswith("🚗 Vendeur(se) itinérant(e)")
    assert "&#" not in offre.title
    assert offre.description == "Vous & votre véhicule : < 2 ans d'expérience > bienvenus."

