"""
sources/adzuna.py — Source d'offres via l'API Adzuna (pays = France).

Doc API : https://developer.adzuna.com/overview
Endpoint : GET https://api.adzuna.com/v1/api/jobs/fr/search/{page}

Principe :
- Lit les clés ADZUNA_APP_ID / ADZUNA_APP_KEY depuis le .env.
- Si une clé manque -> on loggue un warning et on renvoie [] (jamais de crash).
- Interroge chaque famille de recherche.yaml, agrège les résultats bruts.
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
import recherche
from normalize import cle_native
from sources import masquer_secrets, provenance

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


def _chercher_une_page(
    app_id: str, app_key: str, titre: str, mots: list[str], libelle: str, page: int
) -> list[dict]:
    """Interroge UNE page Adzuna : ``titre`` exigé dans l'intitulé, ``mots`` en OU.

    ## Pourquoi cette forme de requête

    Le paramètre ``what`` d'Adzuna est CONJONCTIF : il exige tous les mots.
    Mesuré le 2026-09-05, à Paris, sur « stage intelligence artificielle » :
    0 offre avec la fenêtre de 7 jours, 4 sans la fenêtre, 43 sans ``where``.

    On demande donc la même chose autrement : ``title_only`` pour exiger le mot
    de contrat DANS LE TITRE — ce que `filters.est_un_stage` exigera de toute
    façon — et ``what_or`` pour les mots de la famille, en OU au lieu d'un ET
    impossible. Adzuna ne sait pas faire un OU de PHRASES : les termes de
    recherche.yaml y arrivent découpés en mots (`recherche.mots_isoles`).

    L'API répond **400 (et non 429)** quand elle étrangle, avec une page HTML.
    ``raise_for_status`` en fait une ``RequestException``, tracée en
    ``http: HTTP 400`` par ``observabilite`` au lieu de disparaître dans un
    ``[]`` muet.
    """
    params = {
        "results_per_page": config.RESULTATS_PAR_TERME,
        "title_only": titre,
        "what_or": " ".join(mots),
        "where": config.LIEU,
    }
    return _interroger(app_id, app_key, params, f"{titre} + {libelle} (page {page})", page)[0]


def _interroger(
    app_id: str, app_key: str, specifiques: dict, terme: str, page: int
) -> tuple[list[dict], int | None]:
    """UNE page Adzuna. Rend (offres, `count` annoncé par l'API ou None)."""
    params = {
        "app_id": app_id,
        "app_key": app_key,
        # Fraîcheur poussée côté API : offres publiées dans les N derniers jours.
        "max_days_old": config.JOURS_FRAICHEUR,
        "content-type": "application/json",
        **specifiques,
    }
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
        return [], None

    try:
        donnees = reponse.json()
    except ValueError:
        observabilite.signaler(NOM_SOURCE, "format", f"réponse non-JSON sur « {terme} »")
        logger.warning("Adzuna : réponse non-JSON pour « %s »", terme)
        return [], None

    resultats = donnees.get("results", [])
    if not isinstance(resultats, list):
        observabilite.signaler(NOM_SOURCE, "format", f"structure inattendue sur « {terme} »")
        logger.warning("Adzuna : format inattendu pour « %s »", terme)
        return [], None

    logger.info("Adzuna : %d offre(s) pour « %s »", len(resultats), terme)
    try:
        count = int(donnees.get("count"))
    except (TypeError, ValueError):
        count = None
    return resultats, count


def recuperer_offres() -> list[dict]:
    """
    Point d'entrée de la source.

    Une chaîne de pages par FAMILLE × MOT DE CONTRAT (``ADZUNA_TITRES_EXIGES``).
    Chaque offre porte les familles qui l'ont trouvée ; une offre trouvée par
    deux chaînes n'est rendue qu'une fois (identifiant Adzuna).
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
    for nom, famille in recherche.charger().familles.items():
        mots = recherche.mots_isoles(famille.termes(), config.ADZUNA_MOTS_IGNORES)
        if not mots:
            # Tous les termes de la famille sont faits de mots ignorés : la
            # famille n'existe pas pour Adzuna. On le dit plutôt que d'envoyer
            # un `what_or` vide, qui ramènerait TOUS les stages sous son nom.
            logger.warning("Adzuna : famille « %s » sans mot interrogeable, ignorée.", nom)
            continue
        for titre in config.ADZUNA_TITRES_EXIGES:
            for page in range(1, config.ADZUNA_PAGES + 1):
                lot = _chercher_une_page(app_id, app_key, titre, mots, nom, page)
                toutes.extend(provenance.marquer(lot, [nom]))
                # Page incomplète = dernière page : inutile d'en demander une
                # de plus. C'est aussi le comportement en cas d'étranglement
                # (lot vide), ce qui évite d'insister sur une API qui refuse.
                if len(lot) < config.RESULTATS_PAR_TERME:
                    break

    toutes = provenance.fusionner(toutes, lambda o: cle_native(NOM_SOURCE, o))
    logger.info("Adzuna : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


def texte_brut(item: dict) -> str:
    """Titre et description d'une offre Adzuna, pour y retrouver les termes."""
    return f"{item.get('title') or ''} {item.get('description') or ''}"


def recuperer_jobs_etudiants() -> list[dict]:
    """Jobs étudiants : UNE requête par origine, par code postal et rayon explicite.

    - `where` = code postal : « Meaux » et « Évry-Courcouronnes »
      rendent 0 offre, leurs codes postaux en rendent (sondé le 2026-09-17) ;
    - `distance` = rayon de recherche.yaml ; sans lui, Adzuna prend 10 km ;
    - `what_or` = ADZUNA_MOTS_JOBS, les seuls termes d'un mot sans ambiguïté :
      Adzuna ne sait pas faire de OU de phrases, et les mots isolés des autres
      termes (« partiel », « caisse », « extra ») ramenaient n'importe quoi ;
    - pas de `part_time` : le filtre structuré n'est tenu pour fiable que chez
      France Travail.

    Chaque offre est étiquetée par les termes retrouvés — en PHRASE — dans son
    titre et sa description. Si `count` dépasse ce que les pages lues ont
    rapporté, un CONSEIL le dit en tête du bilan.
    """
    app_id, app_key = _cles_disponibles()
    if not app_id or not app_key:
        observabilite.signaler(NOM_SOURCE, "cle_absente",
                               "ADZUNA_APP_ID / ADZUNA_APP_KEY absents du .env")
        logger.warning("Adzuna ignorée : ADZUNA_APP_ID / ADZUNA_APP_KEY absents du .env.")
        return []

    jobs = recherche.charger().student_jobs
    etiquettes = list(jobs.familles())
    mots = config.ADZUNA_MOTS_JOBS
    taille = config.ADZUNA_RESULTATS_PAR_PAGE_JOBS
    toutes: list[dict] = []
    for origine in jobs.origines.values():
        lues, count = 0, None
        for page in range(1, config.ADZUNA_PAGES_JOBS + 1):
            specifiques = {"results_per_page": taille, "what_or": " ".join(mots),
                           "where": origine.code_postal, "distance": jobs.rayon_km,
                           "sort_by": "date"}
            lot, count_page = _interroger(app_id, app_key, specifiques,
                                          f"jobs étudiants, {origine.libelle} (page {page})", page)
            count = count_page if count_page is not None else count
            toutes.extend(provenance.marquer_par_texte(lot, etiquettes, texte_brut))
            lues += len(lot)
            if len(lot) < taille:
                break
        if count is not None and count > lues:
            observabilite.conseiller(
                NOM_SOURCE,
                f"« {NOM_SOURCE} » TRONQUÉE autour de {origine.libelle} : {count} offres "
                f"annoncées, {lues} rapatriées (ADZUNA_PAGES_JOBS = {config.ADZUNA_PAGES_JOBS} "
                f"pages de {taille}).",
            )

    toutes = provenance.fusionner(toutes, lambda o: cle_native(NOM_SOURCE, o))
    logger.info("Adzuna (jobs étudiants) : %d offre(s) brute(s).", len(toutes))
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
