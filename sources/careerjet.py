"""
sources/careerjet.py — Source d'offres via l'API publique Careerjet (France).

Doc API : https://www.careerjet.fr/partners/api/
Endpoint : GET http://public.api.careerjet.net/search

Pourquoi cette source : Careerjet est un MÉTA-moteur qui indexe des centaines de
sites d'emploi français (sites d'entreprises, cabinets, job boards de niche) que
ni Indeed ni LinkedIn ne couvrent complètement. C'est le meilleur rapport
« nouvelles offres / effort » pour élargir le gisement sans clé payante.

Deux particularités de cette API, apprises à ses dépens :
- elle EXIGE un en-tête ``Referer`` (sinon 403 « Undeclared referrer ») ;
- elle attend ``user_ip`` / ``user_agent``, qui identifient l'appelant final.
  On tourne en local : on envoie donc une IP de bouclage et notre propre UA.

L'``affid`` est un identifiant d'affiliation. Celui par défaut est l'identifiant
de démonstration publié dans la doc Careerjet ; il fonctionne mais il est
partagé (donc soumis à un quota commun). Pour un usage régulier, crée le tien
sur https://www.careerjet.fr/partners/ et renseigne ``CAREERJET_AFFID`` au .env.

Renvoie des dicts BRUTS ; la conversion se fait dans normalize.py.

Testable isolément :  python -m sources.careerjet
"""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

import config
from sources import masquer_secrets

load_dotenv()

logger = logging.getLogger(__name__)

NOM_SOURCE = "careerjet"

_BASE_URL = "http://public.api.careerjet.net/search"

# Identifiant d'affiliation de démonstration publié dans la doc Careerjet.
# Surchargeable par CAREERJET_AFFID dans le .env (recommandé, voir docstring).
_AFFID_DEMO = "213e213hd127e01ab5abcd0e5d6c5d43"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _chercher_une_page(terme: str, page: int) -> tuple[list[dict], int]:
    """Interroge Careerjet pour UN terme et UNE page.

    Retourne ``(offres, nb_pages_total)``. ``nb_pages_total`` vaut 0 en cas
    d'échec, ce qui arrête proprement la pagination côté appelant.
    """
    params = {
        "keywords": terme,
        "location": config.LIEU,
        "locale_code": config.CAREERJET_LOCALE,
        "affid": os.getenv("CAREERJET_AFFID") or _AFFID_DEMO,
        "pagesize": config.RESULTATS_PAR_TERME,
        "page": page,
        "sort": "date",  # les plus récentes d'abord (cohérent avec JOURS_FRAICHEUR)
        # Identité de l'appelant final exigée par l'API (on tourne en local).
        "user_ip": "127.0.0.1",
        "user_agent": _UA,
    }
    # Sans Referer, l'API répond 403 « Undeclared referrer ».
    entetes = {"User-Agent": _UA, "Referer": "http://localhost/", "Accept": "application/json"}

    try:
        reponse = requests.get(
            _BASE_URL, params=params, headers=entetes, timeout=config.TIMEOUT_HTTP
        )
        reponse.raise_for_status()
    except requests.exceptions.RequestException as err:
        logger.warning(
            "Careerjet : échec de la requête pour « %s » (page %d) (%s)",
            terme, page, masquer_secrets(err),
        )
        return [], 0

    try:
        donnees = reponse.json()
    except ValueError:
        logger.warning("Careerjet : réponse non-JSON pour « %s »", terme)
        return [], 0

    # L'API répond aussi "LOCATIONS" (lieu ambigu) ou "ERROR" : ce ne sont pas
    # des offres, on ne les confond pas avec un résultat vide.
    type_reponse = donnees.get("type")
    if type_reponse != "JOBS":
        logger.warning(
            "Careerjet : réponse « %s » pour « %s » (%s)",
            type_reponse, terme, donnees.get("error", "sans détail"),
        )
        return [], 0

    jobs = donnees.get("jobs") or []
    if not isinstance(jobs, list):
        logger.warning("Careerjet : format inattendu pour « %s »", terme)
        return [], 0

    return jobs, int(donnees.get("pages") or 1)


def compter_offres(mots_cles: str) -> int | None:
    """Nombre TOTAL d'annonces correspondant (champ ``hits``), sans pagination.

    Sert de SECONDE mesure de volume au tableau de bord marché (``market.py``),
    indépendante de France Travail : Careerjet indexe le privé, France Travail
    le gisement public. Les deux ensemble donnent une image moins biaisée.
    Retourne ``None`` si la mesure échoue.
    """
    params = {
        "keywords": mots_cles,
        "location": config.LIEU,
        "locale_code": config.CAREERJET_LOCALE,
        "affid": os.getenv("CAREERJET_AFFID") or _AFFID_DEMO,
        "pagesize": 1,
        "page": 1,
        "user_ip": "127.0.0.1",
        "user_agent": _UA,
    }
    entetes = {"User-Agent": _UA, "Referer": "http://localhost/", "Accept": "application/json"}
    try:
        reponse = requests.get(
            _BASE_URL, params=params, headers=entetes, timeout=config.TIMEOUT_HTTP
        )
        reponse.raise_for_status()
        donnees = reponse.json()
    except (requests.exceptions.RequestException, ValueError) as err:
        logger.warning("Careerjet : comptage impossible (%s).", masquer_secrets(err))
        return None

    if donnees.get("type") != "JOBS":
        return None
    try:
        return int(donnees.get("hits") or 0)
    except (TypeError, ValueError):
        return None


def recuperer_offres() -> list[dict]:
    """Agrège les offres brutes Careerjet sur tous les termes et toutes les pages."""
    toutes: list[dict] = []
    for terme in config.TERMES_RECHERCHE:
        n_terme = 0
        for page in range(1, config.CAREERJET_PAGES + 1):
            jobs, pages_total = _chercher_une_page(terme, page)
            toutes.extend(jobs)
            n_terme += len(jobs)
            # Plus rien à paginer : dernière page atteinte ou requête en échec.
            if not jobs or page >= pages_total:
                break
        logger.info("Careerjet : %d offre(s) pour « %s »", n_terme, terme)

    logger.info("Careerjet : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) Careerjet récupérée(s) ===\n")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('company')}  |  {o.get('locations')}")
