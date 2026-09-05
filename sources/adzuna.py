"""
sources/adzuna.py — Source d'offres via l'API Adzuna (pays = France).

Doc API : https://developer.adzuna.com/overview
Endpoint : GET https://api.adzuna.com/v1/api/jobs/fr/search/{page}

Principe :
- Lit les clés ADZUNA_APP_ID / ADZUNA_APP_KEY depuis le .env.
- Si une clé manque -> on loggue un warning et on renvoie [] (jamais de crash).
- Interroge chaque terme de recherche, agrège les résultats bruts.
- Renvoie une liste de dictionnaires BRUTS (tels que fournis par Adzuna).
  La conversion vers le schéma commun est faite dans normalize.py.

Testable isolément :  python -m sources.adzuna
"""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

import config
import observabilite
from sources import masquer_secrets

# Charge le .env dès l'import (idempotent).
load_dotenv()

logger = logging.getLogger(__name__)

# Nom canonique de la source (utilisé partout dans le pipeline).
NOM_SOURCE = "adzuna"

# La PAGE est ajoutée au chemin : .../search/{page}.
_BASE_URL = "https://api.adzuna.com/v1/api/jobs/fr/search"


def _cles_disponibles() -> tuple[str | None, str | None]:
    """Retourne (app_id, app_key) lues dans l'environnement (ou None)."""
    return os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")


def _chercher_une_page(app_id: str, app_key: str, page: int) -> list[dict]:
    """Interroge UNE page Adzuna, sur la requête « stage + domaine ».

    ## Pourquoi cette source n'utilise pas ``config.TERMES_RECHERCHE``

    Le paramètre ``what`` d'Adzuna est CONJONCTIF : il exige tous les mots. Les
    termes du projet en font 3 à 4 (« stage intelligence artificielle »), et
    aucune annonce française ne les contient tous. Mesuré le 2026-09-05, à
    Paris, sur ce terme : 0 offre avec la fenêtre de 7 jours, 4 sans la
    fenêtre, 43 sans ``where``. Les cinq termes réunis rapportaient 2 offres,
    et la source pesait 6 lignes dans `stages.db` en sept semaines.

    On demande donc la même chose autrement : ``title_only`` pour exiger le mot
    « stage » DANS LE TITRE — ce que `filters.est_un_stage` exigera de toute
    façon, si bien que la requête et le filtre cessent de se contredire — et
    ``what_or`` pour les termes de domaine, en OU au lieu d'un ET impossible.

    L'API répond **400 (et non 429)** quand elle étrangle, avec une page HTML.
    ``raise_for_status`` en fait une ``RequestException``, désormais tracée en
    ``http: HTTP 400`` par ``observabilite`` au lieu de disparaître dans un
    ``[]`` muet.
    """
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": config.RESULTATS_PAR_TERME,
        "title_only": config.ADZUNA_TITRE_EXIGE,
        "what_or": config.ADZUNA_TERMES_DOMAINE,
        "where": config.LIEU,
        # Fraîcheur poussée côté API : offres publiées dans les N derniers jours.
        "max_days_old": config.JOURS_FRAICHEUR,
        "content-type": "application/json",
    }
    terme = f"{config.ADZUNA_TITRE_EXIGE} + domaine (page {page})"
    try:
        reponse = requests.get(
            f"{_BASE_URL}/{page}", params=params, timeout=config.TIMEOUT_HTTP
        )
        reponse.raise_for_status()
    except requests.exceptions.RequestException as err:
        # Timeout, DNS, 4xx/5xx, réseau coupé... on loggue et on continue.
        # masquer_secrets() évite que la clé API n'apparaisse dans l'URL loggée.
        categorie, detail = observabilite.categorie_requests(err)
        observabilite.signaler(NOM_SOURCE, categorie, f"{detail} sur « {terme} »")
        logger.warning(
            "Adzuna : échec de la requête pour « %s » (%s)",
            terme,
            masquer_secrets(err),
        )
        return []

    try:
        donnees = reponse.json()
    except ValueError:
        observabilite.signaler(NOM_SOURCE, "format", f"réponse non-JSON sur « {terme} »")
        logger.warning("Adzuna : réponse non-JSON pour « %s »", terme)
        return []

    resultats = donnees.get("results", [])
    if not isinstance(resultats, list):
        observabilite.signaler(NOM_SOURCE, "format", f"structure inattendue sur « {terme} »")
        logger.warning("Adzuna : format inattendu pour « %s »", terme)
        return []

    logger.info("Adzuna : %d offre(s) pour « %s »", len(resultats), terme)
    return resultats


def recuperer_offres() -> list[dict]:
    """
    Point d'entrée de la source.

    Renvoie la liste (potentiellement vide) des offres brutes Adzuna,
    agrégées sur tous les termes de recherche de la config.
    """
    app_id, app_key = _cles_disponibles()
    if not app_id or not app_key:
        observabilite.signaler(NOM_SOURCE, "cle_absente",
                               "ADZUNA_APP_ID / ADZUNA_APP_KEY absents du .env")
        logger.warning(
            "Adzuna ignorée : ADZUNA_APP_ID / ADZUNA_APP_KEY absents du .env."
        )
        return []

    toutes: list[dict] = []
    for page in range(1, config.ADZUNA_PAGES + 1):
        lot = _chercher_une_page(app_id, app_key, page)
        toutes.extend(lot)
        # Page incomplète = dernière page : inutile d'en demander une de plus.
        # C'est aussi le comportement en cas d'étranglement (lot vide), ce qui
        # évite d'insister sur une API qui vient de refuser.
        if len(lot) < config.RESULTATS_PAR_TERME:
            break

    logger.info("Adzuna : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    # Test isolé de la source : affiche un petit récapitulatif lisible.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) Adzuna récupérée(s) ===\n")
    for o in offres[:5]:
        titre = o.get("title", "(sans titre)")
        entreprise = (o.get("company") or {}).get("display_name", "?")
        lieu = (o.get("location") or {}).get("display_name", "?")
        print(f"- {titre}  |  {entreprise}  |  {lieu}")
