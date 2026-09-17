"""Le témoin JobSpy reconnaît-il encore les refus que la bibliothèque journalise ?

JobSpy ne LÈVE pas quand LinkedIn ou Indeed refuse : il journalise, puis rend
un résultat partiel. `sources/jobspy_source.py` n'en sait quelque chose qu'en
lisant ces messages. Si une version de JobSpy les reformule, le témoin
devient sourd sans que rien ne casse — le défaut exact que l'observabilité
existe pour empêcher.

Deux garde-fous, sur des échantillons enregistrés (fixtures/jobspy_messages.json) :
- les échantillons sont reconnus avec la bonne catégorie et le bon drapeau ;
- la bibliothèque INSTALLÉE contient toujours leur gabarit, sous le nom de
  journal attendu, et c'est bien la version épinglée dans requirements.txt.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import re
from pathlib import Path

import pandas as pd
import pytest

import observabilite
from sources import jobspy_source as js

RACINE = Path(__file__).resolve().parent.parent
ECHANTILLONS = json.loads(
    (Path(__file__).parent / "fixtures" / "jobspy_messages.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def releve_propre():
    observabilite.arreter()
    yield
    observabilite.arreter()


def _niveau(nom: str) -> int:
    return getattr(logging, nom)


@pytest.mark.parametrize("ech", ECHANTILLONS["refus"], ids=lambda e: e["nom"])
def test_chaque_refus_enregistre_est_reconnu(ech):
    assert js.analyser_message(ech["message"], _niveau(ech["niveau"])) == (
        ech["categorie"], ech["blocage"])


@pytest.mark.parametrize("ech", ECHANTILLONS["ordinaires"], ids=lambda e: e["message"])
def test_un_message_ordinaire_n_est_pas_un_refus(ech):
    assert js.analyser_message(ech["message"], _niveau(ech["niveau"])) is None


@pytest.mark.parametrize("ech", ECHANTILLONS["refus"], ids=lambda e: e["nom"])
def test_le_temoin_capte_l_echantillon_sur_le_bon_journal(ech, monkeypatch):
    """Bout en bout : message émis sur le journal de JobSpy -> incident de la ligne."""
    site = "linkedin" if "LinkedIn" in ech["journal"] else "indeed"
    assert js._JOURNAUX[site] == ech["journal"]

    def faux_scrape(**_):
        logging.getLogger(ech["journal"]).log(_niveau(ech["niveau"]), ech["message"])
        return pd.DataFrame()

    monkeypatch.setattr(js, "scrape_jobs", faux_scrape)
    r = observabilite.demarrer([js.ligne(site)])
    js._scraper(site, "(stage) (NLP)", "ml")
    (incident,) = r.lignes[js.ligne(site)].incidents
    assert incident.categorie == ech["categorie"]
    assert (js.ligne(site) in r.conseils) is ech["blocage"]


def test_un_blocage_linkedin_est_annonce_en_tete_avec_le_plafond(monkeypatch):
    import config

    (blocage,) = [e for e in ECHANTILLONS["refus"] if e["nom"] == "linkedin_blocage"]
    monkeypatch.setattr(config, "JOBSPY_RESULTATS_PAR_SITE", {"linkedin": 60})
    monkeypatch.setattr(js, "scrape_jobs", lambda **_: (
        logging.getLogger(blocage["journal"]).error(blocage["message"]), pd.DataFrame())[1])
    r = observabilite.demarrer(["jobspy:linkedin"])
    js._scraper("linkedin", "(stage) (NLP)", "ml")
    js._scraper("linkedin", "(stage) (GPU)", "inference")

    tete = observabilite.rendre_tableau(r).splitlines()[0]
    assert tete.startswith("⚠ « jobspy:linkedin » a été BLOQUÉ (429)")
    assert "Plafond actuel : 60" in tete and "abaisser" in tete
    assert len(r.conseils) == 1, "un seul conseil par ligne, même si deux requêtes bloquent"
    assert config.JOBSPY_RESULTATS_PAR_SITE["linkedin"] == 60, "rien n'est ajusté"


# ---------------------------------------------------------------------------
# La bibliothèque installée parle toujours comme les échantillons
# ---------------------------------------------------------------------------
def _dossier_jobspy() -> Path:
    import jobspy

    return Path(jobspy.__file__).parent


def test_la_version_installee_est_celle_epinglee_et_celle_des_echantillons():
    installee = importlib.metadata.version("python-jobspy")
    exigences = (RACINE / "requirements.txt").read_text(encoding="utf-8")
    (epinglee,) = re.findall(r"^python-jobspy==([\d.]+)", exigences, re.MULTILINE)
    assert installee == epinglee == ECHANTILLONS["version"]


@pytest.mark.parametrize("ech", ECHANTILLONS["refus"], ids=lambda e: e["nom"])
def test_le_gabarit_est_toujours_dans_la_bibliotheque(ech):
    source = (_dossier_jobspy() / ech["fichier_source"]).read_text(encoding="utf-8")
    assert ech["gabarit_source"] in source, (
        f"JobSpy ne journalise plus « {ech['gabarit_source']} » : le témoin de "
        f"sources/jobspy_source.py ne verra plus ce refus.")


@pytest.mark.parametrize("site, fichier", [("linkedin", "linkedin/__init__.py"),
                                           ("indeed", "indeed/__init__.py")])
def test_le_nom_de_journal_est_toujours_celui_ecoute(site, fichier):
    source = (_dossier_jobspy() / fichier).read_text(encoding="utf-8")
    nom = js._JOURNAUX[site].split(":", 1)[1]
    assert f'create_logger("{nom}")' in source
