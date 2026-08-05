"""
sources/jobspy_source.py — Source d'offres via la bibliothèque python-jobspy
(Indeed FR, LinkedIn, Google Jobs). Aucune clé API requise.

Garde-fous demandés (scraping responsable) :
- JAMAIS authentifié : JobSpy est utilisé sans identifiants (comportement par
  défaut). On ne fournit aucun cookie/compte perso.
- Délais raisonnables : on interroge UN site à la fois, UN terme à la fois,
  avec une pause (config.DELAI_ENTRE_REQUETES) entre chaque requête.
- Robustesse au blocage / rate-limit : chaque appel est isolé dans un
  try/except. Si un site bloque (429, captcha, timeout...), on loggue et on
  passe au suivant, sans jamais faire tomber le reste du pipeline.

Renvoie des dicts BRUTS (une ligne du DataFrame JobSpy = un dict).
La conversion vers le schéma commun est faite dans normalize.py.

Testable isolément :  python -m sources.jobspy_source
"""

from __future__ import annotations

import logging
import time

import pandas as pd

import config

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

# Sites interrogés. LinkedIn bloque agressivement : on le garde mais son échec
# éventuel est sans conséquence grâce à l'isolation par appel.
SITES = ["indeed", "google", "linkedin"]

# Localisation passée à JobSpy (Indeed exige un pays explicite via country_indeed).
_LOCALISATION = f"{config.LIEU}, France"


def _scraper(site: str, terme: str) -> list[dict]:
    """Scrape UN site pour UN terme. Retourne une liste de dicts (ou [])."""
    # Google Jobs fonctionne mieux avec une requête en langage naturel.
    google_terme = f"{terme} {config.LIEU}" if site == "google" else None
    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=terme,
            google_search_term=google_terme,
            location=_LOCALISATION,
            results_wanted=config.RESULTATS_PAR_TERME,
            country_indeed="France",
            # Fraîcheur poussée côté source : offres des N derniers jours.
            hours_old=config.JOURS_FRAICHEUR * 24,
            # Récupère la description complète sur LinkedIn (sinon vide) :
            linkedin_fetch_description=(site == "linkedin"),
            verbose=0,
        )
    except Exception as err:  # noqa: BLE001 - blocage, captcha, rate-limit, réseau...
        logger.warning("JobSpy[%s] : échec pour « %s » (%s)", site, terme, err)
        return []

    if df is None or df.empty:
        logger.info("JobSpy[%s] : 0 offre pour « %s »", site, terme)
        return []

    # Remplace les NaN pandas par None pour une normalisation propre.
    df = df.where(pd.notna(df), None)
    lignes = df.to_dict("records")
    logger.info("JobSpy[%s] : %d offre(s) pour « %s »", site, len(lignes), terme)
    return lignes


def recuperer_offres() -> list[dict]:
    """Agrège les offres brutes JobSpy sur tous les sites et tous les termes."""
    if scrape_jobs is None:
        logger.warning("JobSpy indisponible (import impossible : %s).", _ERREUR_IMPORT)
        return []

    toutes: list[dict] = []
    premier_appel = True
    for site in SITES:
        for terme in config.TERMES_RECHERCHE:
            # Pause polie entre deux requêtes (sauf tout premier appel).
            if not premier_appel:
                time.sleep(config.DELAI_ENTRE_REQUETES)
            premier_appel = False
            toutes.extend(_scraper(site, terme))

    logger.info("JobSpy : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) JobSpy récupérée(s) ===\n")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('company')}  |  {o.get('location')}  |  {o.get('site')}")
