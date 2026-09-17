"""Provenance des offres : familles de requêtes, composition par source, rotation.

Aucun appel réseau : `requests.get` et `scrape_jobs` sont remplacés par des
doublures qui répondent selon la requête reçue, ce qui permet de vérifier à la
fois CE QUI est demandé à chaque moteur et CE QUI est étiqueté au retour.
"""

from __future__ import annotations

import json
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


def test_les_tranches_couvrent_tous_les_termes_une_fois():
    termes = recherche.lire(recherche.CHEMIN_RECHERCHE).termes()
    lots = recherche.tranches(termes, 2)
    assert sorted(t.terme for lot in lots for t in lot) == sorted(t.terme for t in termes)
    assert abs(len(lots[0]) - len(lots[1])) <= 1


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
# France Travail : un appel par terme, rotation
# ---------------------------------------------------------------------------
@pytest.fixture
def france_travail(petite_recherche, monkeypatch, tmp_path):
    from sources import france_travail as ft

    monkeypatch.setenv("FRANCE_TRAVAIL_ID", "x")
    monkeypatch.setenv("FRANCE_TRAVAIL_KEY", "y")
    monkeypatch.setattr(ft, "obtenir_jeton", lambda scope=None: "jeton")
    monkeypatch.setattr(ft, "_respecter_debit", lambda: None)
    monkeypatch.setattr(config, "CHEMIN_ROTATION", tmp_path / ".rotation.json")
    monkeypatch.setattr(config, "FRANCE_TRAVAIL_TRANCHES", 2)
    demandes = []

    def faux_get(url, params, headers, timeout):
        demandes.append(params)
        return _Reponse({"resultats": [{"id": "ft-" + params["motsCles"]}]})

    monkeypatch.setattr(ft.requests, "get", faux_get)
    return ft, demandes


def test_france_travail_alterne_les_tranches_et_couvre_tous_les_termes(france_travail):
    ft, demandes = france_travail
    ft.recuperer_offres()
    premiere = [p["motsCles"] for p in demandes]
    demandes.clear()
    ft.recuperer_offres()
    seconde = [p["motsCles"] for p in demandes]

    # Termes distincts : machine learning, NLP, LLM, MLOps, cybersécurité,
    # sécurité de l'IA — à tour de rôle dans deux tranches.
    assert premiere == ["machine learning", "LLM", "cybersécurité"]
    assert seconde == ["NLP", "MLOps", "sécurité de IA"]
    assert all(p["range"] == f"0-{config.FRANCE_TRAVAIL_RESULTATS - 1}" for p in demandes)


def test_france_travail_etiquette_un_terme_partage_des_deux_familles(france_travail):
    ft, _ = france_travail
    offres = {o["id"]: provenance.familles_de(o) for o in ft.recuperer_offres()}
    assert offres["ft-LLM"] == ["ml", "mlops"]
    assert offres["ft-machine learning"] == ["ml"]


def test_rotation_reprend_la_tranche_la_plus_ancienne(france_travail):
    ft, demandes = france_travail
    config.CHEMIN_ROTATION.write_text(json.dumps(
        {"tranches": 2, "passages": {"0": "2026-09-01", "1": "2026-08-01"}}), encoding="utf-8")
    ft.recuperer_offres()
    assert demandes[0]["motsCles"] == "NLP"


def test_rotation_perimee_si_le_nombre_de_tranches_change(tmp_path):
    from sources import france_travail as ft

    chemin = tmp_path / "r.json"
    ft.ecrire_rotation(chemin, 3, {0: "2026-09-01"})
    assert ft.lire_rotation(chemin, 2) == {}
    assert ft.lire_rotation(chemin, 3) == {0: "2026-09-01"}


def test_rotation_illisible_repart_de_zero(tmp_path):
    from sources import france_travail as ft

    chemin = tmp_path / "r.json"
    chemin.write_text("{pas du json", encoding="utf-8")
    assert ft.lire_rotation(chemin, 2) == {}
    assert ft.choisir_tranche(2, {}) == 0


def test_rotation_signale_une_tranche_sortie_de_la_fenetre(france_travail, caplog):
    ft, _ = france_travail
    config.CHEMIN_ROTATION.write_text(json.dumps(
        {"tranches": 2, "passages": {"0": "2020-01-01", "1": "2030-01-01"}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        ft.recuperer_offres()
    assert "n'avait pas été interrogée depuis" in caplog.text


def test_mots_cles_france_travail_retire_la_ponctuation():
    from sources import france_travail as ft

    assert ft.mots_cles("optimisation de l'inférence") == "optimisation de inférence"


# ---------------------------------------------------------------------------
# JobSpy
# ---------------------------------------------------------------------------
def test_jobspy_une_requete_par_site_et_famille(petite_recherche, monkeypatch):
    from sources import jobspy_source as js

    monkeypatch.setattr(js, "SITES", ["indeed", "linkedin"])
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
