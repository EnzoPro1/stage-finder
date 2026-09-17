"""
sources/jooble.py — Source d'offres via l'API Jooble (France).

Doc API : https://jooble.org/api/about
Endpoint : POST https://jooble.org/api/{API_KEY}
Corps JSON : {"keywords": "...", "location": "Paris"}
Réponse   : {"totalCount": N, "jobs": [ {...}, ... ]}

Mêmes garanties que les autres sources :
- Clé absente => warning + [] (pas de crash).
- Toute erreur réseau/format est capturée par terme.
- Renvoie des dicts BRUTS ; la conversion se fait dans normalize.py.

Testable isolément :  python -m sources.jooble
"""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

import config
import observabilite
import recherche
from sources import masquer_secrets, provenance

load_dotenv()

logger = logging.getLogger(__name__)

NOM_SOURCE = "jooble"

_BASE_URL = "https://jooble.org/api/"


def _cle_disponible() -> str | None:
    return os.getenv("JOOBLE_API_KEY")


def _chercher_un_terme(cle: str, terme: str) -> list[dict]:
    """Interroge Jooble pour un seul terme de recherche."""
    url = _BASE_URL + cle
    corps = {"keywords": terme, "location": config.LIEU}
    try:
        reponse = requests.post(url, json=corps, timeout=config.TIMEOUT_HTTP)
        reponse.raise_for_status()
    except requests.exceptions.RequestException as err:
        categorie, detail = observabilite.categorie_requests(err)
        observabilite.signaler(NOM_SOURCE, categorie, f"{detail} sur « {terme} »")
        logger.warning(
            "Jooble : échec de la requête pour « %s » (%s)",
            terme,
            masquer_secrets(err),
        )
        return []

    try:
        donnees = reponse.json()
    except ValueError:
        observabilite.signaler(NOM_SOURCE, "format", f"réponse non-JSON sur « {terme} »")
        logger.warning("Jooble : réponse non-JSON pour « %s »", terme)
        return []

    jobs = donnees.get("jobs", [])
    if not isinstance(jobs, list):
        observabilite.signaler(NOM_SOURCE, "format", f"structure inattendue sur « {terme} »")
        logger.warning("Jooble : format inattendu pour « %s »", terme)
        return []

    logger.info("Jooble : %d offre(s) pour « %s »", len(jobs), terme)
    return jobs


def recuperer_offres() -> list[dict]:
    """Agrège les offres brutes Jooble : un appel par terme distinct de recherche.yaml.

    Source RETIRÉE de ``SOURCES_ACTIVES`` (elle résout « Paris » en Paris,
    Texas) : ce chemin n'est exercé que par ``python -m sources.jooble``. Sa
    grammaire de requête n'a pas été sondée, d'où la forme la plus prudente —
    un terme par appel, préfixé du mot « stage ».
    """
    cle = _cle_disponible()
    if not cle:
        observabilite.signaler(NOM_SOURCE, "cle_absente", "JOOBLE_API_KEY absente du .env")
        logger.warning("Jooble ignorée : JOOBLE_API_KEY absente du .env.")
        return []

    toutes: list[dict] = []
    for t in recherche.charger().termes():
        toutes.extend(provenance.marquer(_chercher_un_terme(cle, f"stage {t.terme}"), t.familles))

    toutes = provenance.fusionner(toutes, lambda o: o.get("id") or o.get("link"))
    logger.info("Jooble : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) Jooble récupérée(s) ===\n")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('company')}  |  {o.get('location')}")
