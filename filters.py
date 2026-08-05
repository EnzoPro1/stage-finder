"""
filters.py — Filtres durs.

Quatre filtres :
1) C'est bien un stage : un mot-clé "stage"/"internship"... dans le TITRE.
   (On cible le titre car c'est le seul signal fiable du type de contrat :
    un CDI senior peut contenir le mot « stage » dans sa description.)
2) Ce n'est PAS une alternance/apprentissage ni un poste sénior : aucun
   mot-clé d'exclusion dans le titre.
3) C'est bien en Île-de-France (et pas un "Paris" étranger type Paris, Texas).
4) C'est récent : publié dans les N derniers jours (config.JOURS_FRAICHEUR).

On NE filtre PAS sur la durée (6 mois) ni la date de début (janvier 2027) :
ces infos sont souvent seulement en texte libre — on les laisse au ranking.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from dateutil import parser as dateparser

import config
from normalize import Offre

logger = logging.getLogger(__name__)


def _motif_mots(mots: list[str]) -> re.Pattern:
    """Compile un motif « mot entier » insensible à la casse à partir d'une liste.

    Les frontières de mot (\\b) évitent les faux positifs (ex. « intern » ne
    doit pas matcher « international »).
    """
    alternatives = "|".join(re.escape(m) for m in mots)
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


_MOTIF_STAGE = _motif_mots(config.MOTS_CLES_STAGE)
_MOTIF_EXCLU = _motif_mots(config.MOTS_CLES_EXCLUS)
_MOTIF_ETRANGER_TITRE = _motif_mots(config.MARQUEURS_ETRANGERS_TITRE)


def est_un_stage(offre: Offre) -> bool:
    """Vrai si un mot-clé de stage apparaît dans le TITRE."""
    return bool(_MOTIF_STAGE.search(offre.title))


def est_exclu(offre: Offre) -> bool:
    """Vrai si le titre contient un mot d'exclusion (alternance, sénior...)."""
    return bool(_MOTIF_EXCLU.search(offre.title))


def est_en_idf(offre: Offre) -> bool:
    """Vrai si la localisation est en Île-de-France et pas manifestement à l'étranger.

    Le TITRE est examiné en plus du champ « lieu » : les cabinets de placement
    publient depuis Paris des postes qui sont ailleurs (« Stage Data Scientist -
    IT (H/F) - Canada »). Le champ « lieu » dit alors « Paris » et le seul indice
    fiable est dans l'intitulé.
    """
    if _MOTIF_ETRANGER_TITRE.search(offre.title):
        return False
    lieu = offre.location.lower()
    if not lieu:
        # Localisation vide : permissif, on laisse passer (le ranking jugera).
        return True
    if any(marqueur in lieu for marqueur in config.MARQUEURS_ETRANGERS):
        return False
    return any(zone in lieu for zone in config.LIEUX_ACCEPTES)


def _parse_date(texte: str) -> date | None:
    """Tente de parser une date de publication (formats variés selon la source)."""
    if not texte:
        return None
    try:
        # fuzzy=True tolère du texte autour ; on ne garde que la partie date.
        d = dateparser.parse(texte, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    return d.date() if d else None


def est_recente(offre: Offre) -> bool:
    """Vrai si l'offre a été publiée dans les config.JOURS_FRAICHEUR derniers jours."""
    d = _parse_date(offre.posted_at)
    if d is None:
        # Date inconnue : comportement piloté par la config.
        return config.GARDER_SI_DATE_INCONNUE
    age = (date.today() - d).days
    # age négatif possible (fuseaux/décalage) : on tolère un petit futur.
    return -1 <= age <= config.JOURS_FRAICHEUR


def filtrer(offres: list[Offre]) -> list[Offre]:
    """Applique tous les filtres durs et loggue le détail des rejets."""
    gardees: list[Offre] = []
    rejets = {"hors-stage": 0, "exclu": 0, "hors-IDF": 0, "trop-vieux": 0}

    for offre in offres:
        if not est_un_stage(offre):
            rejets["hors-stage"] += 1
            continue
        if est_exclu(offre):
            rejets["exclu"] += 1
            continue
        if not est_en_idf(offre):
            rejets["hors-IDF"] += 1
            continue
        if not est_recente(offre):
            rejets["trop-vieux"] += 1
            continue
        gardees.append(offre)

    total_rejets = sum(rejets.values())
    logger.info(
        "Filtres : %d gardée(s) / %d rejetée(s) "
        "(hors-stage : %d, alternance/sénior : %d, hors-IDF : %d, +7j : %d).",
        len(gardees), total_rejets,
        rejets["hors-stage"], rejets["exclu"], rejets["hors-IDF"], rejets["trop-vieux"],
    )
    return gardees
