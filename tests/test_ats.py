"""Pages carrières : liste d'entreprises, Greenhouse / Lever / Ashby, normalisation.

Les réponses factices reprennent les clés relevées sur les endpoints réels le
2026-09-17 (Doctolib et Datadog chez Greenhouse, Pigment chez Lever, Photoroom
chez Ashby). Aucun appel réseau.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

import filters
import observabilite
import recherche
from normalize import cle_native, normaliser
from sources import ats, provenance

_YAML = """
familles:
  ml:
    fr: ["machine learning", "data scientist"]
  cyber:
    en: ["cybersecurity"]
entreprises:
  - {nom: "Doctolib", plateforme: greenhouse, identifiant: doctolib}
  - {nom: "Inconnue", plateforme: greenhouse, identifiant: nexistepas}
  - {nom: "Pigment", plateforme: lever, identifiant: pigment}
  - {nom: "Mistral AI", plateforme: lever, identifiant: mistral}
  - {nom: "Photoroom", plateforme: ashby, identifiant: photoroom}
"""


@pytest.fixture
def liste(tmp_path, monkeypatch):
    chemin = tmp_path / "recherche.yaml"
    chemin.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", str(chemin))
    observabilite.arreter()
    yield recherche.charger()
    observabilite.arreter()


class _Reponse:
    def __init__(self, status, donnees=None):
        self.status_code, self._donnees = status, donnees

    def json(self):
        return self._donnees


def _aujourd_hui_iso():
    return f"{date.today().isoformat()}T09:00:00+00:00"


# Réponses modelées sur les endpoints réels
_GREENHOUSE = {"jobs": [
    {"id": 7194969, "title": "Stage - Data Scientist (x/f/m) - janvier 2027", "company_name": "Doctolib",
     "absolute_url": "https://careers.doctolib.com/job?gh_jid=7194969",
     "location": {"name": "Berlin, Berlin, Germany; Paris, France"},
     "offices": [{"name": "Paris", "location": "Paris, Paris, France"}],
     "first_published": _aujourd_hui_iso(), "updated_at": "2026-09-15T13:24:16-04:00",
     "content": "&lt;p&gt;Machine learning &amp;amp; donn&amp;eacute;es&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"},
]}
_LEVER = [
    {"id": "d025a757-a3df-4ef7-8344-bcd0de83933f", "text": "Cybersecurity (6-month Internship)",
     "hostedUrl": "https://jobs.lever.co/pigment/d025a757", "createdAt": 1789660000000,
     "categories": {"commitment": "Internship", "location": "New York", "allLocations": ["New York", "Paris"]},
     "descriptionPlain": "Join Pigment."},
]
_ASHBY = {"jobs": [
    {"id": "085eaa32-44fe-4928-8826-b48873823bfd", "title": "Machine Learning Intern", "location": "London",
     "secondaryLocations": [{"location": "Paris"}], "isListed": True, "publishedAt": _aujourd_hui_iso(),
     "jobUrl": "https://jobs.ashbyhq.com/photoroom/085eaa32",
     "address": {"postalAddress": {"addressLocality": "Paris", "addressCountry": "France"}},
     "descriptionPlain": "Train diffusion models."},
    {"id": "non-publiee", "title": "Brouillon", "location": "Paris", "isListed": False,
     "jobUrl": "u", "descriptionPlain": ""},
]}


def _faux_get(appels):
    def get(url, params, headers, timeout):
        appels.append((url, dict(params)))
        if "boards-api.greenhouse.io" in url:
            return _Reponse(404, {"status": 404}) if "nexistepas" in url else _Reponse(200, _GREENHOUSE)
        if "api.lever.co" in url:
            return _Reponse(200, []) if url.endswith("/mistral") else _Reponse(200, _LEVER)
        if "api.ashbyhq.com" in url:
            return _Reponse(200, _ASHBY)
        raise AssertionError(url)
    return get


# ---------------------------------------------------------------------------
# Liste d'entreprises (recherche.yaml)
# ---------------------------------------------------------------------------
def test_le_fichier_du_depot_porte_des_exemples_valides():
    entreprises = recherche.lire(recherche.CHEMIN_RECHERCHE).entreprises
    assert 3 <= len(entreprises) <= 5
    assert {e.plateforme for e in entreprises} == {"greenhouse", "lever", "ashby"}


def test_une_liste_vide_est_valide(tmp_path):
    chemin = tmp_path / "r.yaml"
    chemin.write_text('familles:\n  ml:\n    fr: ["x"]\nentreprises: []\n', encoding="utf-8")
    assert recherche.lire(str(chemin)).entreprises == []
    chemin.write_text('familles:\n  ml:\n    fr: ["x"]\n', encoding="utf-8")
    assert recherche.lire(str(chemin)).entreprises == [], "bloc absent = liste vide"


@pytest.mark.parametrize("ligne", [
    '{nom: "X", plateforme: workable, identifiant: x}',                  # plateforme inconnue
    '{nom: "X", plateforme: lever, identifiant: "a b"}',                 # identifiant invalide
    '{nom: "", plateforme: lever, identifiant: x}',                      # nom vide
    '{nom: "X", plateforme: lever, identifiant: x, pays: fr}',           # clé inconnue
])
def test_une_entreprise_mal_formee_est_rejetee(tmp_path, ligne):
    chemin = tmp_path / "r.yaml"
    chemin.write_text(f'familles:\n  ml:\n    fr: ["x"]\nentreprises:\n  - {ligne}\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        recherche.lire(str(chemin))


def test_une_entreprise_en_double_est_rejetee(tmp_path):
    chemin = tmp_path / "r.yaml"
    chemin.write_text('familles:\n  ml:\n    fr: ["x"]\nentreprises:\n'
                      '  - {nom: "A", plateforme: lever, identifiant: qonto}\n'
                      '  - {nom: "B", plateforme: lever, identifiant: Qonto}\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        recherche.lire(str(chemin))


# ---------------------------------------------------------------------------
# Lieu préféré
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lieux, attendu", [
    (["New York, NY", " Paris, France"], "Paris, France"),
    (["London", "", "Île-de-France"], "Île-de-France"),
    (["Berlin, Germany", "Munich"], "Berlin, Germany"),
    (["", None], ""),
])
def test_lieu_prefere(lieux, attendu):
    assert ats.lieu_prefere(lieux) == attendu


# ---------------------------------------------------------------------------
# Adaptateurs
# ---------------------------------------------------------------------------
def test_greenhouse_url_incident_404_lieu_et_familles(liste, monkeypatch):
    from sources import greenhouse

    appels = []
    monkeypatch.setattr(ats.requests, "get", _faux_get(appels))
    r = observabilite.demarrer(["greenhouse"])
    (annonce,) = greenhouse.recuperer_offres()

    assert [u for u, _ in appels] == ["https://boards-api.greenhouse.io/v1/boards/doctolib/jobs",
                                      "https://boards-api.greenhouse.io/v1/boards/nexistepas/jobs"]
    assert all(p == {"content": "true"} for _, p in appels)
    assert annonce["_entreprise"] == "Doctolib" and annonce["_lieu"] == "Paris, France"
    assert provenance.familles_de(annonce) == ["ml"]
    (incident,) = r.lignes["greenhouse"].incidents
    assert incident.categorie == "http" and "nexistepas" in incident.detail


def test_lever_tableau_vide_devient_un_conseil(liste, monkeypatch):
    from sources import lever

    appels = []
    monkeypatch.setattr(ats.requests, "get", _faux_get(appels))
    r = observabilite.demarrer(["lever"])
    (annonce,) = lever.recuperer_offres()

    assert [u for u, _ in appels] == ["https://api.lever.co/v0/postings/pigment",
                                      "https://api.lever.co/v0/postings/mistral"]
    assert annonce["_lieu"] == "Paris"
    assert provenance.familles_de(annonce) == ["cyber"]
    assert "VIDE" in r.conseils["lever"] and "Mistral AI (mistral)" in r.conseils["lever"]
    assert r.lignes["lever"].incidents == []


def test_ashby_ignore_les_annonces_non_publiees(liste, monkeypatch):
    from sources import ashby

    monkeypatch.setattr(ats.requests, "get", _faux_get([]))
    (annonce,) = ashby.recuperer_offres()
    assert annonce["id"] == "085eaa32-44fe-4928-8826-b48873823bfd"
    assert annonce["_lieu"] == "Paris, France", "adresse postale française avant le lieu secondaire"
    assert provenance.familles_de(annonce) == ["ml"]


def test_une_liste_vide_ne_fait_aucun_appel(tmp_path, monkeypatch):
    from sources import greenhouse

    chemin = tmp_path / "r.yaml"
    chemin.write_text('familles:\n  ml:\n    fr: ["x"]\n', encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", str(chemin))
    appels = []
    monkeypatch.setattr(ats.requests, "get", _faux_get(appels))
    assert greenhouse.recuperer_offres() == [] and appels == []


# ---------------------------------------------------------------------------
# Normalisation et passage des filtres de stage
# ---------------------------------------------------------------------------
def _brut(plateforme, annonce):
    return {**annonce, "_entreprise": "ACME", "_lieu": "Paris, France"}


def test_normaliser_greenhouse_decode_le_html_echappe():
    (offre,) = normaliser("greenhouse", [_brut("greenhouse", _GREENHOUSE["jobs"][0])])
    assert offre.description == "Machine learning & données Python"
    assert "<" not in offre.description
    assert offre.url.startswith("https://careers.doctolib.com")
    assert offre.company == "ACME" and offre.location == "Paris, France"
    assert offre.liens[0].cle == "id:7194969"


def test_normaliser_lever_convertit_les_millisecondes():
    (offre,) = normaliser("lever", [_brut("lever", _LEVER[0])])
    assert offre.posted_at.startswith("2026-09-17")
    assert offre.title == "Cybersecurity (6-month Internship)"
    assert offre.liens[0].cle == "id:d025a757-a3df-4ef7-8344-bcd0de83933f"


def test_normaliser_ashby():
    (offre,) = normaliser("ashby", [_brut("ashby", _ASHBY["jobs"][0])])
    assert (offre.title, offre.url, offre.source) == (
        "Machine Learning Intern", "https://jobs.ashbyhq.com/photoroom/085eaa32", "ashby")
    assert cle_native("ashby", _ASHBY["jobs"][0]) == "id:085eaa32-44fe-4928-8826-b48873823bfd"


def test_une_annonce_multi_sites_passe_le_filtre_ile_de_france_par_son_lieu_prefere(liste, monkeypatch):
    """« Berlin, Berlin, Germany; Paris, France » en lieu brut serait rejeté ;
    le lieu préféré le fait passer."""
    from sources import greenhouse

    monkeypatch.setattr(ats.requests, "get", _faux_get([]))
    offres = normaliser("greenhouse", greenhouse.recuperer_offres())
    assert [o.title for o in filters.filtrer(offres)] == ["Stage - Data Scientist (x/f/m) - janvier 2027"]
