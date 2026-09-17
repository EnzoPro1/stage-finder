"""Provenance des offres : familles de requêtes, et requête composée par source.

Aucun appel réseau : `requests.get` et `scrape_jobs` sont remplacés par des
doublures qui répondent selon la requête reçue, ce qui permet de vérifier à la
fois CE QUI est demandé à chaque moteur et CE QUI est étiqueté au retour.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

import config
import recherche
from normalize import normaliser
from sources import provenance

_YAML = """
familles:
  ml:
    fr: ["machine learning", "NLP"]
    en: ["machine learning", "LLM"]
  mlops:
    en: ["MLOps", "LLM"]
  cyber:
    fr: ["cybersécurité", "sécurité de l'IA"]
"""


@pytest.fixture
def petite_recherche(tmp_path, monkeypatch):
    chemin = tmp_path / "recherche.yaml"
    chemin.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", str(chemin))
    return recherche.charger()


class _Reponse:
    def __init__(self, donnees=None, status=200):
        self._donnees = donnees
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self):
        return self._donnees

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(response=self)


# ---------------------------------------------------------------------------
# sources/provenance.py
# ---------------------------------------------------------------------------
def test_marquer_ajoute_sans_doublon_et_garde_l_ordre():
    items = provenance.marquer([{"id": 1}], ["ml"])
    provenance.marquer(items, ["mlops", "ml"])
    assert provenance.familles_de(items[0]) == ["ml", "mlops"]


def test_fusionner_reunit_les_familles_de_toutes_les_copies():
    lot = (provenance.marquer([{"id": "a", "t": 1}, {"id": "b"}], ["ml"])
           + provenance.marquer([{"id": "a", "t": 2}], ["mlops"]))
    fusion = provenance.fusionner(lot, lambda o: o["id"])
    assert [o["id"] for o in fusion] == ["a", "b"]
    assert fusion[0]["t"] == 1, "la première copie est gardée"
    assert provenance.familles_de(fusion[0]) == ["ml", "mlops"]


def test_fusionner_garde_les_items_sans_identifiant():
    lot = [{"id": None}, {"id": None}]
    assert len(provenance.fusionner(lot, lambda o: o["id"])) == 2


def test_normaliser_recopie_les_familles_sur_l_offre(charger_fixture):
    brut = provenance.marquer(charger_fixture("careerjet")[:1], ["ml", "cyber"])
    offre = normaliser("careerjet", brut)[0]
    assert offre.familles == ["ml", "cyber"]


def test_une_offre_sans_provenance_a_des_familles_vides(charger_fixture):
    offre = normaliser("free_work", charger_fixture("free_work"))[0]
    assert offre.familles == []


# ---------------------------------------------------------------------------
# Composition des requêtes (recherche.py)
# ---------------------------------------------------------------------------
def test_expression_ou_met_les_phrases_entre_guillemets():
    assert (recherche.expression_ou(["machine learning", "NLP", "sécurité de l'IA"])
            == '("machine learning" OR NLP OR "sécurité de l\'IA")')


def test_requete_stage_juxtapose_contrat_et_domaine():
    assert (recherche.requete_stage(["LLM"], ["stage", "internship"])
            == "(stage OR internship) (LLM)")


def test_mots_isoles_decoupe_et_retire_les_mots_generiques():
    mots = recherche.mots_isoles(
        ["ingénieur machine learning", "machine learning", "sécurité de l'IA", "vLLM"],
        ignores=["ingénieur", "de", "ia", "sécurité"],
    )
    assert mots == ["machine", "learning", "vllm"]


def test_chaque_famille_du_depot_garde_des_mots_pour_adzuna():
    """Une famille réduite à zéro mot serait silencieusement absente d'Adzuna."""
    r = recherche.lire(recherche.CHEMIN_RECHERCHE)
    for nom, famille in r.familles.items():
        assert recherche.mots_isoles(famille.termes(), config.ADZUNA_MOTS_IGNORES), nom


# ---------------------------------------------------------------------------
# Adzuna
# ---------------------------------------------------------------------------
def test_adzuna_une_chaine_par_famille_et_mot_de_contrat(petite_recherche, monkeypatch):
    from sources import adzuna

    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")
    monkeypatch.setattr(config, "ADZUNA_TITRES_EXIGES", ["stage"])
    appels = []

    def faux_get(url, params, timeout):
        appels.append(params)
        mots = params["what_or"].split()
        # L'offre 1 répond à « llm », présent dans ml ET mlops.
        resultats = [{"id": "1", "title": "Stage LLM"}] if "llm" in mots else []
        if "cybersécurité" in mots:
            resultats = [{"id": "2", "title": "Stage cyber"}]
        return _Reponse({"results": resultats})

    monkeypatch.setattr(adzuna.requests, "get", faux_get)
    offres = adzuna.recuperer_offres()

    assert [p["what_or"] for p in appels] == ["machine learning nlp llm", "mlops llm",
                                              "cybersécurité"]
    assert all(p["title_only"] == "stage" for p in appels)
    par_id = {o["id"]: provenance.familles_de(o) for o in offres}
    assert par_id == {"1": ["ml", "mlops"], "2": ["cyber"]}


# ---------------------------------------------------------------------------
# Careerjet
# ---------------------------------------------------------------------------
def test_careerjet_une_requete_ou_par_famille(petite_recherche, monkeypatch):
    from sources import careerjet

    monkeypatch.setattr(config, "MOTS_CLES_STAGE", ["stage", "intern"])
    demandes = []

    def faux_get(url, params, headers, timeout):
        demandes.append(params["keywords"])
        jobs = [{"url": "u1", "title": "Stage LLM"}] if "LLM" in params["keywords"] else []
        return _Reponse({"type": "JOBS", "jobs": jobs, "pages": 1})

    monkeypatch.setattr(careerjet.requests, "get", faux_get)
    offres = careerjet.recuperer_offres()

    assert demandes == [
        '(stage OR intern) ("machine learning" OR NLP OR LLM)',
        "(stage OR intern) (MLOps OR LLM)",
        '(stage OR intern) (cybersécurité OR "sécurité de l\'IA")',
    ]
    assert len(offres) == 1
    assert provenance.familles_de(offres[0]) == ["ml", "mlops"]


# ---------------------------------------------------------------------------
# France Travail : un appel par terme (hors mode stage, adaptateur conservé)
# ---------------------------------------------------------------------------
def test_france_travail_un_appel_par_terme_distinct(petite_recherche, monkeypatch):
    from sources import france_travail as ft

    monkeypatch.setenv("FRANCE_TRAVAIL_ID", "x")
    monkeypatch.setenv("FRANCE_TRAVAIL_KEY", "y")
    monkeypatch.setattr(ft, "obtenir_jeton", lambda scope=None: "jeton")
    monkeypatch.setattr(ft, "_respecter_debit", lambda: None)
    demandes = []

    def faux_get(url, params, headers, timeout):
        demandes.append(params)
        return _Reponse({"resultats": [{"id": "ft-" + params["motsCles"]}]})

    monkeypatch.setattr(ft.requests, "get", faux_get)
    offres = {o["id"]: provenance.familles_de(o) for o in ft.recuperer_offres()}

    # « LLM » est écrit deux fois (ml, mlops) : interrogé UNE fois, deux familles.
    assert [p["motsCles"] for p in demandes] == [
        "machine learning", "NLP", "LLM", "MLOps", "cybersécurité", "sécurité de IA"]
    assert all(p["range"] == f"0-{config.FRANCE_TRAVAIL_RESULTATS - 1}" for p in demandes)
    assert offres["ft-LLM"] == ["ml", "mlops"]
    assert offres["ft-machine learning"] == ["ml"]


def test_france_travail_hors_des_sources_actives_pour_les_stages():
    assert "france_travail" not in config.SOURCES_ACTIVES
    assert "france_travail" in config.SOURCES_ECARTEES_STAGES


def test_mots_cles_france_travail_retire_la_ponctuation():
    from sources import france_travail as ft

    assert ft.mots_cles("optimisation de l'inférence") == "optimisation de inférence"


# ---------------------------------------------------------------------------
# JobSpy
# ---------------------------------------------------------------------------
def test_jobspy_une_requete_par_site_et_famille(petite_recherche, monkeypatch):
    from sources import jobspy_source as js

    monkeypatch.setattr(config, "JOBSPY_SITES", ["indeed", "linkedin"])
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "JOBSPY_RESULTATS_PAR_SITE", {"indeed": 100})
    appels = []

    def faux_scrape(site_name, search_term, results_wanted, **_):
        appels.append((site_name[0], search_term, results_wanted))
        if "LLM" in search_term:
            return pd.DataFrame([{"id": f"{site_name[0]}-1", "title": "Stage LLM",
                                  "job_url": "u", "site": site_name[0]}])
        return pd.DataFrame()

    monkeypatch.setattr(js, "scrape_jobs", faux_scrape)
    offres = js.recuperer_offres()

    assert len(appels) == 6  # 2 sites × 3 familles
    assert {a[2] for a in appels if a[0] == "indeed"} == {100}
    assert {a[2] for a in appels if a[0] == "linkedin"} == {config.RESULTATS_PAR_TERME}
    par_id = {o["id"]: provenance.familles_de(o) for o in offres}
    assert par_id == {"indeed-1": ["ml", "mlops"], "linkedin-1": ["ml", "mlops"]}


# ---------------------------------------------------------------------------
# JobSpy : un blocage journalisé mais non levé devient un incident
# ---------------------------------------------------------------------------
def _faux_scrape_qui_journalise(journal, niveau, message, lignes):
    def faux(site_name, **_):
        logging.getLogger(journal).log(niveau, message)
        return pd.DataFrame(lignes)
    return faux


def test_jobspy_un_429_linkedin_journalise_devient_un_incident(petite_recherche, monkeypatch):
    import observabilite
    from sources import jobspy_source as js

    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    monkeypatch.setattr(js, "scrape_jobs", _faux_scrape_qui_journalise(
        "JobSpy:LinkedIn", logging.ERROR,
        "429 Response - Blocked by LinkedIn for too many requests",
        [{"id": "li-1", "title": "Stage NLP", "job_url": "u", "site": "linkedin"}]))
    r = observabilite.demarrer(["jobspy:linkedin"])
    js._scraper("linkedin", "(stage) (NLP)", "ml")

    (incident,) = r.lignes["jobspy:linkedin"].incidents
    assert incident.categorie == "http" and "Blocked by LinkedIn" in incident.detail
    assert "« ml »" in incident.detail


def test_jobspy_un_statut_indeed_en_info_devient_un_incident(petite_recherche, monkeypatch):
    import observabilite
    from sources import jobspy_source as js

    monkeypatch.setattr(js, "scrape_jobs", _faux_scrape_qui_journalise(
        "JobSpy:Indeed", logging.INFO,
        "responded with status code: 403 (submit GitHub issue if this appears to be a bug)", []))
    r = observabilite.demarrer(["jobspy:indeed"])
    assert js._scraper("indeed", "(stage) (NLP)", "ml") == []
    (incident,) = r.lignes["jobspy:indeed"].incidents
    assert incident.categorie == "http"


def test_jobspy_un_message_ordinaire_n_est_pas_un_incident(petite_recherche, monkeypatch):
    import observabilite
    from sources import jobspy_source as js

    monkeypatch.setattr(js, "scrape_jobs", _faux_scrape_qui_journalise(
        "JobSpy:Indeed", logging.INFO, "search page: 1 / 2",
        [{"id": "in-1", "title": "Stage NLP", "job_url": "u", "site": "indeed"}]))
    r = observabilite.demarrer(["jobspy:indeed"])
    js._scraper("indeed", "(stage) (NLP)", "ml")
    assert r.lignes["jobspy:indeed"].incidents == []


def test_jobspy_le_temoin_est_retire_apres_la_requete(petite_recherche, monkeypatch):
    from sources import jobspy_source as js

    monkeypatch.setattr(js, "scrape_jobs", lambda **_: pd.DataFrame())
    avant = list(logging.getLogger("JobSpy:Indeed").handlers)
    js._scraper("indeed", "(stage) (NLP)", "ml")
    assert logging.getLogger("JobSpy:Indeed").handlers == avant

