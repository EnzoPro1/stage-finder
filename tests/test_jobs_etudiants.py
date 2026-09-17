"""Jobs étudiants : origines, requêtes par source, étiquettes, filtres, collecte.

Aucun appel réseau. Chaque doublure enregistre ce qu'on lui demande : ce qui
est vérifié, c'est la question posée à chaque moteur (lieu, rayon, mots) autant
que l'étiquetage de ce qu'il rend.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

import config
import filters
import observabilite
import recherche
from normalize import Offre, normaliser
from sources import provenance

_YAML = """
familles:
  ml:
    fr: ["machine learning"]
student_jobs:
  rayon_km: 15
  origines:
    ville_a: {libelle: "Meaux", commune: "Meaux", insee: "77284", code_postal: "77100"}
    campus: {libelle: "ESIEE Paris", commune: "Noisy-le-Grand", insee: "93051", code_postal: "93160"}
  termes: ["vendeur", "hôte de caisse", "week-end"]
"""


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    chemin = tmp_path / "recherche.yaml"
    chemin.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", str(chemin))
    observabilite.arreter()
    yield recherche.charger().student_jobs
    observabilite.arreter()


class _Reponse:
    def __init__(self, donnees=None, status=200, entetes=None):
        self._donnees, self.status_code = donnees, status
        self.headers, self.text = entetes or {}, ""

    def json(self):
        return self._donnees

    def raise_for_status(self):
        pass


def _aujourd_hui():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# recherche.yaml : bloc student_jobs
# ---------------------------------------------------------------------------
def test_le_fichier_du_depot_porte_les_trois_origines_et_les_seize_termes():
    j = recherche.lire(recherche.CHEMIN_RECHERCHE).student_jobs
    assert {o.libelle for o in j.origines.values()} == {
        "Meaux", "Évry-Courcouronnes", "ESIEE Paris (Noisy-le-Grand)"}
    assert {(o.insee, o.code_postal) for o in j.origines.values()} == {
        ("77284", "77100"), ("91228", "91000"), ("93051", "93160")}
    assert len(j.termes) == 16 and "cours particuliers" in j.termes
    assert j.rayon_km == 15


@pytest.mark.parametrize("terme, etiquette", [
    ("hôte de caisse", "hote_de_caisse"), ("week-end", "week_end"),
    ("job étudiant", "job_etudiant"), ("Soirée", "soiree"),
])
def test_chaque_terme_devient_une_etiquette(terme, etiquette):
    assert recherche.slug(terme) == etiquette


@pytest.mark.parametrize("modif", [
    'termes: ["week-end", "week end"]',                       # même étiquette
    'termes: ["temps partiel sans mot clé"]',                 # étiquette réservée
    'termes: ["terme non retrouvé"]',                         # étiquette réservée
    'termes: ["ML"]',                                         # étiquette « ml » = une famille
])
def test_termes_invalides_rejetes(tmp_path, modif):
    contenu = _YAML.replace('termes: ["vendeur", "hôte de caisse", "week-end"]', modif)
    chemin = tmp_path / "r.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    with pytest.raises(ValidationError):
        recherche.lire(str(chemin))


def test_une_origine_mal_formee_est_rejetee(tmp_path):
    chemin = tmp_path / "r.yaml"
    chemin.write_text(_YAML.replace('code_postal: "77100"', 'code_postal: "Meaux"'),
                      encoding="utf-8")
    with pytest.raises(ValidationError):
        recherche.lire(str(chemin))


# ---------------------------------------------------------------------------
# Étiquetage par le texte (requêtes OU)
# ---------------------------------------------------------------------------
def test_les_termes_sont_retrouves_en_phrase_dans_le_html(jobs):
    etiquettes = list(jobs.familles())
    assert recherche.familles_dans_texte(
        "Hôte de caisse WEEK-END (H/F) - magasin <b>vendeur</b>", etiquettes) == [
        "vendeur", "hote_de_caisse", "week_end"]
    assert recherche.familles_dans_texte("Hôtesse de caisse", etiquettes) == []


def test_sans_terme_retrouve_l_item_porte_l_etiquette_de_repli(jobs):
    lot = provenance.marquer_par_texte(
        [{"t": "Vendeuse (H/F)"}, {"t": "Vendeur week-end"}], list(jobs.familles()),
        lambda i: i["t"])
    assert provenance.familles_de(lot[0]) == [recherche.ETIQUETTE_NON_RETROUVE]
    assert provenance.familles_de(lot[1]) == ["vendeur", "week_end"]


# ---------------------------------------------------------------------------
# France Travail : un appel par terme sur toutes les origines, + sans mot-clé
# ---------------------------------------------------------------------------
@pytest.fixture
def ft(jobs, monkeypatch):
    from sources import france_travail as ft

    monkeypatch.setenv("FRANCE_TRAVAIL_ID", "x")
    monkeypatch.setenv("FRANCE_TRAVAIL_KEY", "y")
    monkeypatch.setattr(ft, "obtenir_jeton", lambda scope=None: "jeton")
    monkeypatch.setattr(ft, "_respecter_debit", lambda: None)
    return ft


def test_france_travail_jobs_requetes_et_etiquettes(ft, monkeypatch):
    demandes = []

    def faux_get(url, params, headers, timeout):
        demandes.append(dict(params))
        cle = params.get("motsCles") or f"partiel-{params['commune']}"
        return _Reponse({"resultats": [{"id": f"ft-{cle}"}]}, entetes={"Content-Range": "offres 0-0/1"})

    monkeypatch.setattr(ft.requests, "get", faux_get)
    offres = {o["id"]: provenance.familles_de(o) for o in ft.recuperer_jobs_etudiants()}

    par_terme = [d for d in demandes if "motsCles" in d]
    assert [d["motsCles"] for d in par_terme] == ["vendeur", "hôte de caisse", "week end"]
    assert all(d["commune"] == "77284,93051" and d["distance"] == 15 for d in par_terme)
    assert all("region" not in d and "tempsPlein" not in d for d in par_terme)

    sans_mot = [d for d in demandes if "motsCles" not in d]
    assert [(d["commune"], d["tempsPlein"]) for d in sans_mot] == [("77284", "false"),
                                                                  ("93051", "false")]
    assert offres["ft-vendeur"] == ["vendeur"]
    assert offres["ft-partiel-77284"] == [recherche.ETIQUETTE_SANS_MOT_CLE]


def test_france_travail_jobs_pagine_et_signale_la_troncature(ft, monkeypatch):
    monkeypatch.setattr(config, "FRANCE_TRAVAIL_RESULTATS", 2)
    monkeypatch.setattr(config, "FRANCE_TRAVAIL_PAGES_JOBS", 2)
    ranges = []

    def faux_get(url, params, headers, timeout):
        ranges.append(params["range"])
        debut = int(params["range"].split("-")[0])
        cle = params.get("motsCles") or params["commune"]
        return _Reponse({"resultats": [{"id": f"{cle}-{debut}"}, {"id": f"{cle}-{debut + 1}"}]},
                        entetes={"Content-Range": f"offres {params['range']}/9"})

    monkeypatch.setattr(ft.requests, "get", faux_get)
    r = observabilite.demarrer(["france_travail"])
    ft.recuperer_jobs_etudiants()
    assert ranges[:2] == ["0-1", "2-3"], "deux pages, puis le plafond"
    assert "TRONQUÉE" in r.conseils["france_travail"]


# ---------------------------------------------------------------------------
# Careerjet : une requête OU par origine, sans filtre de durée
# ---------------------------------------------------------------------------
def test_careerjet_jobs_une_requete_ou_par_origine(jobs, monkeypatch):
    from sources import careerjet

    demandes = []

    def faux_get(url, params, headers, timeout):
        demandes.append(dict(params))
        return _Reponse({"type": "JOBS", "pages": 1, "jobs": [
            {"title": "Vendeur (H/F)", "company": "A", "locations": params["location"],
             "site": "s", "description": "", "url": "u1"},
            {"title": "Vendeuse", "company": "B", "locations": params["location"],
             "site": "s", "description": "Magasin", "url": "u2"},
        ]})

    monkeypatch.setattr(careerjet.requests, "get", faux_get)
    offres = careerjet.recuperer_jobs_etudiants()

    assert [d["location"] for d in demandes] == ["Meaux", "Noisy-le-Grand"]
    assert all(d["keywords"] == '(vendeur OR "hôte de caisse" OR "week-end")' for d in demandes)
    assert all("contractperiod" not in d for d in demandes)
    assert all(d["pagesize"] == config.CAREERJET_TAILLE_PAGE_JOBS for d in demandes)
    etiquettes = sorted(tuple(provenance.familles_de(o)) for o in offres)
    assert etiquettes == [(recherche.ETIQUETTE_NON_RETROUVE,)] * 2 + [("vendeur",)] * 2


# ---------------------------------------------------------------------------
# Adzuna : code postal, rayon explicite, mots utiles
# ---------------------------------------------------------------------------
def test_adzuna_jobs_code_postal_rayon_et_troncature(jobs, monkeypatch):
    from sources import adzuna

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")
    monkeypatch.setattr(config, "ADZUNA_RESULTATS_PAR_PAGE_JOBS", 1)
    monkeypatch.setattr(config, "ADZUNA_PAGES_JOBS", 1)
    demandes = []

    def faux_get(url, params, timeout):
        demandes.append(dict(params))
        return _Reponse({"count": 40, "results": [
            {"id": f"a-{params['where']}", "title": "Hôte de caisse week-end", "description": ""}]})

    monkeypatch.setattr(adzuna.requests, "get", faux_get)
    r = observabilite.demarrer(["adzuna"])
    offres = adzuna.recuperer_jobs_etudiants()

    assert [(d["where"], d["distance"]) for d in demandes] == [("77100", 15), ("93160", 15)]
    assert all(d["what_or"] == " ".join(config.ADZUNA_MOTS_JOBS) for d in demandes), \
        "liste fermée de mots, pas les mots des termes"
    assert all("part_time" not in d and "title_only" not in d for d in demandes)
    assert provenance.familles_de(offres[0]) == ["hote_de_caisse", "week_end"]
    assert "TRONQUÉE" in r.conseils["adzuna"]


# ---------------------------------------------------------------------------
# JobSpy : Indeed, par origine, rayon en miles
# ---------------------------------------------------------------------------
def test_jobspy_jobs_par_origine(jobs, monkeypatch):
    from sources import jobspy_source as js

    monkeypatch.setattr(config, "JOBSPY_SITES_JOBS", ["indeed"])
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    appels = []

    def faux(site_name, search_term, location, **kw):
        appels.append((site_name[0], search_term, location, kw.get("distance"), kw.get("hours_old"),
                       kw.get("job_type")))
        return pd.DataFrame([{"id": f"in-{location}", "title": "Serveur week-end", "job_url": "u",
                              "site": "indeed", "description": "Restaurant"}])

    monkeypatch.setattr(js, "scrape_jobs", faux)
    offres = js.recuperer_jobs_etudiants()

    assert [a[2] for a in appels] == ["Meaux, France", "Noisy-le-Grand, France"]
    assert {a[3] for a in appels} == {10}, "15 km -> 10 miles, arrondi au-dessus"
    assert all(a[4] == config.JOURS_FRAICHEUR * 24 and a[5] is None for a in appels)
    assert all(provenance.familles_de(o) == ["week_end"] for o in offres)


# ---------------------------------------------------------------------------
# Filtres : fraîcheur seule, alternance signalée et gardée
# ---------------------------------------------------------------------------
def _offre(titre, posted=None, source="careerjet"):
    return Offre(titre, "ACME", "Lagny-sur-Marne", "d", f"http://x/{titre}", source,
                 posted or _aujourd_hui(), "")


def test_filtres_jobs_ni_stage_ni_idf_ni_exclusion():
    gardees = filters.filtrer_jobs_etudiants([
        _offre("Vendeur week-end"),                         # pas « stage » : gardé
        _offre("Équipier senior"),                          # « senior » : gardé
        _offre("Alternance - Vendeur (H/F)"),               # gardé, signalé
        _offre("Vendeur", posted="2020-01-01"),             # trop vieux : rejeté
    ])
    assert [o.title for o in gardees] == ["Vendeur week-end", "Équipier senior",
                                          "Alternance - Vendeur (H/F)"]
    assert [o.drapeaux for o in gardees] == [[], [], ["alternance"]]


def test_filtres_jobs_alimentent_le_releve():
    r = observabilite.demarrer(["careerjet"])
    o = _offre("Vendeur")
    o.familles = ["vendeur"]
    filters.filtrer_jobs_etudiants([o, _offre("Vieux", posted="2020-01-01")])
    assert r.lignes["careerjet"].survivantes == 1
    assert r.lignes["careerjet"].rejets == {"trop-vieux": 1}
    assert r.familles["vendeur"].survivantes == 1


# ---------------------------------------------------------------------------
# Collecte : périmètre, bilan, base séparée
# ---------------------------------------------------------------------------
class _SourceFactice:
    def __init__(self, nom, lot, sequentiel=False):
        self.nom, self._lot, self.sequentiel = nom, lot, sequentiel
        self.points_entree = []

    def recuperer(self, point_entree="recuperer_offres"):
        self.points_entree.append(point_entree)
        return lambda: self._lot


def test_la_collecte_jobs_appelle_le_bon_point_d_entree_et_annonce_son_perimetre(jobs, monkeypatch):
    import main

    brut = provenance.marquer([{
        "title": "Vendeur", "company": "A", "locations": "Lagny", "site": "s",
        "description": "", "url": "u", "date": _aujourd_hui()}], ["vendeur"])
    source = _SourceFactice("careerjet", brut)
    monkeypatch.setattr(main.registry, "sources_actives", lambda noms: [source])
    main.collecter(utiliser_jobspy=False, perimetre=main.perimetre_jobs())
    r = observabilite.actif()

    assert source.points_entree == ["recuperer_jobs_etudiants"]
    assert r.perimetre == "jobs étudiants"
    assert r.ecartees == config.SOURCES_ECARTEES_JOBS
    assert set(r.familles) == {"vendeur", "hote_de_caisse", "week_end",
                               recherche.ETIQUETTE_SANS_MOT_CLE}
    assert r.familles["vendeur"].brut == 1
    tete = observabilite.rendre_tableau(r).splitlines()[0]
    assert tete.startswith("⊘ Source « free_work » désactivée pour les jobs étudiants")


def test_pipeline_jobs_bout_en_bout_sans_modele(jobs, monkeypatch, tmp_path):
    import jobs_etudiants
    import main
    import storage

    brut = [
        {"title": "Alternance vendeur", "company": "A", "locations": "Lagny", "site": "s",
         "description": "", "url": "u1", "date": _aujourd_hui()},
        {"title": "Vendeur week-end", "company": "B", "locations": "Meaux", "site": "s",
         "description": "", "url": "u2", "date": _aujourd_hui()},
    ]
    provenance.marquer_par_texte(brut, list(jobs.familles()), lambda i: i["title"])
    monkeypatch.setattr(main.registry, "sources_actives",
                        lambda noms: [_SourceFactice("careerjet", brut)])
    monkeypatch.setattr(jobs_etudiants.ranker, "encoder_offres",
                        lambda offres: np.eye(len(offres)))

    base = tmp_path / "jobs.db"
    offres = jobs_etudiants.executer(utiliser_jobspy=False, chemin_base=base)
    assert len(offres) == 2 and all(o.nouvelle for o in offres)

    conn = storage.ouvrir(str(base))
    lignes = {l["title"]: (l["familles"], l["drapeaux"]) for l in conn.execute(
        "SELECT title, familles, drapeaux FROM offres")}
    assert lignes == {"Alternance vendeur": ("vendeur", "alternance"),
                      "Vendeur week-end": ("vendeur week_end", "")}


def test_la_base_des_jobs_n_est_pas_celle_des_stages():
    assert str(config.CHEMIN_BASE_JOBS) != str(config.CHEMIN_BASE)
