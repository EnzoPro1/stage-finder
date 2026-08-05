"""
sources/registry.py — Catalogue des sources disponibles.

Avant, la liste des sources était codée en dur dans ``main.collecter()`` :
ajouter un site imposait de toucher l'orchestrateur. Ici, une source = UNE ligne
dans ``CATALOGUE``, et ``config.SOURCES_ACTIVES`` décide lesquelles tournent.

Deux propriétés portées par le catalogue :
- ``module`` : chemin d'import, chargé PARESSEUSEMENT (``import_module``). Les
  dépendances lourdes (JobSpy tire pandas + un moteur de scraping) ne sont donc
  payées que si la source est réellement activée.
- ``sequentiel`` : True pour les sources de SCRAPING, qu'on interroge une par
  une avec des délais (politesse). Les sources API sont parallélisables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import import_module
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    """Description d'une source : d'où l'importer et comment l'interroger."""

    nom: str
    module: str
    libelle: str
    sequentiel: bool = False   # True = scraping, à interroger sans parallélisme
    cle_requise: str = ""      # variable .env attendue ("" = aucune clé)

    def recuperer(self) -> Callable[[], list[dict]]:
        """Importe le module de la source et retourne son ``recuperer_offres``."""
        return getattr(import_module(self.module), "recuperer_offres")


# Une source = une ligne. L'ordre n'a pas d'importance (la collecte est parallèle).
CATALOGUE: dict[str, Source] = {
    s.nom: s
    for s in [
        Source("adzuna", "sources.adzuna", "Adzuna (API)",
               cle_requise="ADZUNA_APP_ID / ADZUNA_APP_KEY"),
        Source("jooble", "sources.jooble", "Jooble (API)",
               cle_requise="JOOBLE_API_KEY"),
        Source("france_travail", "sources.france_travail", "France Travail (API)",
               cle_requise="FRANCE_TRAVAIL_ID / FRANCE_TRAVAIL_KEY"),
        Source("careerjet", "sources.careerjet", "Careerjet (méta-moteur, API)"),
        Source("free_work", "sources.free_work", "Free-Work (job board tech, API)"),
        Source("jobspy", "sources.jobspy_source", "JobSpy (Indeed / LinkedIn / Google)",
               sequentiel=True),
    ]
}


def sources_actives(noms: list[str]) -> list[Source]:
    """Résout les noms de ``config.SOURCES_ACTIVES`` en objets ``Source``.

    Un nom inconnu est signalé et ignoré : une faute de frappe dans la config ne
    doit pas faire tomber toute la collecte.
    """
    resolues: list[Source] = []
    for nom in noms:
        source = CATALOGUE.get(nom)
        if source is None:
            logger.warning(
                "Source « %s » inconnue (ignorée). Disponibles : %s.",
                nom, ", ".join(sorted(CATALOGUE)),
            )
            continue
        resolues.append(source)
    return resolues
