"""
sources/jobspy_source.py — Source d'offres via la bibliothèque python-jobspy
(Indeed FR, LinkedIn). Aucune clé API requise.

Garde-fous demandés (scraping responsable) :
- JAMAIS authentifié : JobSpy est utilisé sans identifiants (comportement par
  défaut). On ne fournit aucun cookie/compte perso.
- Délais raisonnables : on interroge UN site à la fois, UNE famille à la fois,
  avec une pause (config.DELAI_ENTRE_REQUETES) entre chaque requête.
- Robustesse au blocage / rate-limit : chaque appel est isolé dans un
  try/except. Si un site bloque (429, captcha, timeout...), on loggue et on
  passe au suivant, sans jamais faire tomber le reste du pipeline.

## Un blocage que JobSpy ne lève pas

JobSpy n'émet PAS d'exception quand un site refuse : LinkedIn journalise
« 429 Response - Blocked by LinkedIn » en ERROR et rend ce qu'il a déjà, Indeed
journalise « responded with status code » en INFO. Ces messages partent sur
les journaux propres de JobSpy (`JobSpy:LinkedIn`, `JobSpy:Indeed`), qui ne
remontent pas aux nôtres. Un blocage ressemblait donc à un résultat maigre.
``_Temoin`` les écoute pendant chaque requête et en fait des incidents de la
ligne du site dans `observabilite`.

Google Jobs n'est plus interrogé (cf. `config.SOURCES_ECARTEES_STAGES`).

Renvoie des dicts BRUTS (une ligne du DataFrame JobSpy = un dict).
La conversion vers le schéma commun est faite dans normalize.py.

Testable isolément :  python -m sources.jobspy_source
"""

from __future__ import annotations

import logging
import math
import re
import time

import pandas as pd

import config
import observabilite
import recherche
from normalize import cle_native
from sources import provenance

# L'import de jobspy peut être lourd ; on le protège pour un message clair.
try:
    from jobspy import scrape_jobs
except Exception as err:  # noqa: BLE001
    scrape_jobs = None
    _ERREUR_IMPORT = err
else:
    _ERREUR_IMPORT = None

logger = logging.getLogger(__name__)

NOM_SOURCE = "jobspy"

# Localisation passée à JobSpy (Indeed exige un pays explicite via country_indeed).
_LOCALISATION = f"{config.LIEU}, France"

# Journal de chaque scraper, tel que JobSpy le nomme (`create_logger("LinkedIn")`).
_JOURNAUX = {"indeed": "JobSpy:Indeed", "linkedin": "JobSpy:LinkedIn", "google": "JobSpy:Google"}

_MOTIF_CODE_HTTP = re.compile(r"\b[1-5]\d\d\b")

_KM_PAR_MILE = 1.609344


def ligne(site: str) -> str:
    """Nom de la ligne du bilan pour un site : celui que `normalize` donne à ses offres."""
    return f"{NOM_SOURCE}:{site}"


def analyser_message(message: str, niveau: int) -> tuple[str, bool] | None:
    """(catégorie d'incident, blocage ?) si un message de JobSpy signale un refus, sinon None.

    Reconnu : tout message ERROR (LinkedIn journalise ses refus ainsi), et tout
    message qui cite un « status code » (Indeed le fait en INFO). Un blocage
    est un 429 ou un « Blocked » explicite. Les formulations sont celles de
    python-jobspy 1.1.82, épinglé dans requirements.txt ; des échantillons
    enregistrés (tests/fixtures/jobspy_messages.json) cassent les tests si
    une montée de version les change.
    """
    bas = message.lower()
    if niveau < logging.ERROR and "status code" not in bas:
        return None
    blocage = "429" in message or "blocked" in bas
    categorie = "http" if blocage or _MOTIF_CODE_HTTP.search(message) else "reseau"
    return categorie, blocage


class _Temoin(logging.Handler):
    """Recueille ce que JobSpy dit d'un refus sans le lever."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.refus: list[tuple[str, str, bool]] = []   # (message, catégorie, blocage)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        analyse = analyser_message(message, record.levelno)
        if analyse and all(message != m for m, _, _ in self.refus):
            self.refus.append((message, *analyse))


def _consoles_jobspy_au_niveau_erreur() -> None:
    """JobSpy écrit sur la console par ses propres handlers.

    On l'appelle en ``verbose=2`` pour que ``_Temoin`` reçoive les messages
    INFO d'Indeed ; ses consoles restent, elles, au niveau ERROR — ce que
    ``verbose=0`` affichait jusqu'ici. Le bruit à l'écran ne change pas.
    """
    for nom, journal in list(logging.root.manager.loggerDict.items()):
        if nom.startswith("JobSpy:") and isinstance(journal, logging.Logger):
            for handler in journal.handlers:
                if not isinstance(handler, _Temoin):
                    handler.setLevel(logging.ERROR)


def _scraper(site: str, requete: str, libelle: str, lieu: str | None = None,
             rayon_km: int | None = None, resultats: int | None = None) -> list[dict]:
    """Scrape UN site pour UNE requête. Retourne une liste de dicts (ou []).

    ``lieu`` vaut par défaut « Paris, France » (stages). ``rayon_km`` est
    converti en MILES, l'unité de JobSpy ; sans lui, JobSpy prend 50 miles.
    """
    rayon = {"distance": max(1, math.ceil(rayon_km / _KM_PAR_MILE))} if rayon_km else {}
    journal = logging.getLogger(_JOURNAUX.get(site, f"JobSpy:{site}"))
    temoin = _Temoin()
    journal.addHandler(temoin)
    _consoles_jobspy_au_niveau_erreur()
    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=requete,
            location=lieu or _LOCALISATION,
            **rayon,
            results_wanted=resultats or config.JOBSPY_RESULTATS_PAR_SITE.get(
                site, config.RESULTATS_PAR_TERME),
            country_indeed="France",
            # Fraîcheur poussée côté source : offres des N derniers jours.
            hours_old=config.JOURS_FRAICHEUR * 24,
            # Récupère la description complète sur LinkedIn (sinon vide) :
            linkedin_fetch_description=(site == "linkedin"),
            verbose=2,
        )
    except Exception as err:  # noqa: BLE001 - blocage, captcha, rate-limit, réseau...
        categorie, detail = observabilite.categorie_requests(err)
        observabilite.signaler(ligne(site), categorie, f"{detail} sur « {libelle} »")
        logger.warning("JobSpy[%s] : échec pour « %s » (%s)", site, libelle, err)
        return []
    finally:
        journal.removeHandler(temoin)

    for message, categorie, blocage in temoin.refus:
        observabilite.signaler(ligne(site), categorie, f"{message[:120]} sur « {libelle} »")
        logger.warning("JobSpy[%s] : %s (« %s »)", site, message, libelle)
        if blocage:
            plafond = resultats or config.JOBSPY_RESULTATS_PAR_SITE.get(site, config.RESULTATS_PAR_TERME)
            observabilite.conseiller(
                ligne(site),
                f"« {ligne(site)} » a été BLOQUÉ (429) pendant ce run, résultats partiels. "
                f"Plafond actuel : {plafond} résultats par requête — envisage de l'abaisser "
                f"(config.JOBSPY_RESULTATS_PAR_SITE[\"{site}\"]). Rien n'est ajusté automatiquement.",
            )

    if df is None or df.empty:
        logger.info("JobSpy[%s] : 0 offre pour « %s »", site, libelle)
        return []

    # Remplace les NaN pandas par None pour une normalisation propre.
    df = df.where(pd.notna(df), None)
    lignes = df.to_dict("records")
    logger.info("JobSpy[%s] : %d offre(s) pour « %s »", site, len(lignes), libelle)
    return lignes


def recuperer_offres() -> list[dict]:
    """Agrège les offres brutes JobSpy : une requête par SITE × FAMILLE.

    Indeed et LinkedIn comprennent OU, guillemets et parenthèses : une famille
    entière tient dans « (stage OR internship …) ("machine learning" OR …) ».
    Une offre trouvée par deux familles sur le même site n'est rendue qu'une
    fois (identifiant JobSpy, préfixé par le site : « in-… », « li-… »).
    """
    if scrape_jobs is None:
        for site in config.JOBSPY_SITES:
            observabilite.signaler(ligne(site), "import", str(_ERREUR_IMPORT))
        logger.warning("JobSpy indisponible (import impossible : %s).", _ERREUR_IMPORT)
        return []

    familles = recherche.charger().familles
    toutes: list[dict] = []
    premier_appel = True
    for site in config.JOBSPY_SITES:
        for nom, famille in familles.items():
            # Pause polie entre deux requêtes (sauf tout premier appel).
            if not premier_appel:
                time.sleep(config.DELAI_ENTRE_REQUETES)
            premier_appel = False
            requete = recherche.requete_stage(famille.termes(), config.MOTS_CLES_STAGE)
            toutes.extend(provenance.marquer(_scraper(site, requete, nom), [nom]))

    toutes = provenance.fusionner(toutes, lambda o: cle_native(NOM_SOURCE, o))
    logger.info("JobSpy : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


def texte_brut(item: dict) -> str:
    """Titre et description d'une ligne JobSpy, pour y retrouver les termes."""
    return f"{item.get('title') or ''} {item.get('description') or ''}"


def recuperer_jobs_etudiants() -> list[dict]:
    """Jobs étudiants : UNE requête OU des termes par SITE × ORIGINE, dans le rayon.

    Pas de `job_type` : chez Indeed, JobSpy ne peut pas le combiner à
    `hours_old`, et la fraîcheur prime. Chaque offre est étiquetée par les
    termes retrouvés dans son titre et sa description.
    """
    if scrape_jobs is None:
        for site in config.JOBSPY_SITES_JOBS:
            observabilite.signaler(ligne(site), "import", str(_ERREUR_IMPORT))
        logger.warning("JobSpy indisponible (import impossible : %s).", _ERREUR_IMPORT)
        return []

    jobs = recherche.charger().student_jobs
    etiquettes = list(jobs.familles())
    requete = recherche.expression_ou(jobs.termes)
    toutes: list[dict] = []
    premier_appel = True
    for site in config.JOBSPY_SITES_JOBS:
        for origine in jobs.origines.values():
            if not premier_appel:
                time.sleep(config.DELAI_ENTRE_REQUETES)
            premier_appel = False
            lot = _scraper(site, requete, f"jobs étudiants, {origine.libelle}",
                           lieu=f"{origine.commune}, France", rayon_km=jobs.rayon_km,
                           resultats=config.JOBSPY_RESULTATS_PAR_SITE_JOBS.get(site))
            toutes.extend(provenance.marquer_par_texte(lot, etiquettes, texte_brut))

    toutes = provenance.fusionner(toutes, lambda o: cle_native(NOM_SOURCE, o))
    logger.info("JobSpy (jobs étudiants) : %d offre(s) brute(s).", len(toutes))
    return toutes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) JobSpy récupérée(s) ===\n")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('company')}  |  {o.get('location')}  |  {o.get('site')}")
