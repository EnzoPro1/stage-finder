"""
sources/free_work.py — Source d'offres via l'API publique de Free-Work (France).

Endpoint : GET https://www.free-work.com/api/job_postings
Réponse  : une LISTE JSON d'annonces (API Platform : ``page`` + ``itemsPerPage``).

Pourquoi cette source : Free-Work (ex Carrière Info) est un job board 100 %
tech/IT français. Son gisement vient en bonne partie d'ESN et de cabinets qui ne
publient pas sur Indeed/LinkedIn — donc peu de recouvrement avec les autres
sources. Aucune clé API n'est nécessaire.

Particularité : le paramètre de mots-clés de cette API est ignoré côté serveur
(vérifié : mêmes résultats avec et sans). On filtre donc par TYPE DE CONTRAT
(``contracts=internship``, c'est-à-dire « stage ») et on laisse le pipeline
(filtres durs + ranking) faire le tri sur le contenu. Le volume est petit (le
site publie quelques dizaines de stages), donc c'est peu coûteux.

Renvoie des dicts BRUTS ; la conversion se fait dans normalize.py.

Testable isolément :  python -m sources.free_work
"""

from __future__ import annotations

import logging

import requests

import config
import observabilite

logger = logging.getLogger(__name__)

NOM_SOURCE = "free_work"

_BASE_URL = "https://www.free-work.com/api/job_postings"

# Valeur du filtre « type de contrat » côté Free-Work correspondant au stage.
_CONTRAT_STAGE = "internship"

_ENTETES = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _chercher_une_page(page: int) -> list[dict]:
    """Récupère UNE page d'annonces de stage. [] en cas d'échec."""
    params = {
        "contracts": _CONTRAT_STAGE,
        "itemsPerPage": config.RESULTATS_PAR_TERME,
        "page": page,
    }
    try:
        reponse = requests.get(
            _BASE_URL, params=params, headers=_ENTETES, timeout=config.TIMEOUT_HTTP
        )
        reponse.raise_for_status()
    except requests.exceptions.RequestException as err:
        categorie, detail = observabilite.categorie_requests(err)
        observabilite.signaler(NOM_SOURCE, categorie, f"{detail} p.{page}")
        logger.warning("Free-Work : échec de la requête (page %d) (%s)", page, err)
        return []

    try:
        donnees = reponse.json()
    except ValueError:
        observabilite.signaler(NOM_SOURCE, "format", f"réponse non-JSON p.{page}")
        logger.warning("Free-Work : réponse non-JSON (page %d).", page)
        return []

    # L'API renvoie une liste nue ; on tolère aussi un éventuel enveloppement
    # Hydra (``hydra:member``) si l'API venait à changer de format.
    if isinstance(donnees, dict):
        donnees = donnees.get("hydra:member") or donnees.get("member") or []
    if not isinstance(donnees, list):
        observabilite.signaler(NOM_SOURCE, "format", f"structure inattendue p.{page}")
        logger.warning("Free-Work : format inattendu (page %d).", page)
        return []
    return donnees


def recuperer_offres() -> list[dict]:
    """Agrège les annonces de stage Free-Work sur toutes les pages configurées."""
    toutes: list[dict] = []
    for page in range(1, config.FREE_WORK_PAGES + 1):
        lot = _chercher_une_page(page)
        toutes.extend(lot)
        # Page incomplète = dernière page : inutile d'en demander une de plus.
        if len(lot) < config.RESULTATS_PAR_TERME:
            break

    logger.info("Free-Work : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) Free-Work récupérée(s) ===\n")
    for o in offres[:5]:
        lieu = (o.get("location") or {}).get("label")
        print(f"- {o.get('title')}  |  {(o.get('company') or {}).get('name')}  |  {lieu}")
