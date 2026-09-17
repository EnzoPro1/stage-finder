"""
jobs_etudiants.py — Collecte des jobs étudiants autour des origines.

    Sources (autour de chaque origine) -> Normalisation -> Fraîcheur (+ drapeau
    alternance) -> Trajet (< seuil depuis une origine) -> Déduplication
    (exacte + floue) -> Persistance (base séparée)

Ce qui DIFFÈRE des stages, et pourquoi :

- pas de filtre « stage » dans le titre, pas d'exclusion alternance/sénior, pas
  de filtre Île-de-France (`filters.filtrer_jobs_etudiants`) : le tri se fera
  sur le temps de trajet depuis n'importe quelle origine, jamais sur un
  département ;
- pas de ranking : il n'y a pas de profil de référence pour un job étudiant,
  et la liste se trie par trajet ; le score enregistré vaut 0 ;
- base séparée (`config.CHEMIN_BASE_JOBS`) : `stages.db` nourrit l'étiquetage
  et l'évaluation du ranking des stages.

La déduplication floue est gardée : son garde-fou d'identité (entreprise,
commune, volume horaire du titre) a justement été mesuré sur 447 offres de
job étudiant.

Utilisation :
    python main.py --jobs-etudiants             # collecte complète
    python main.py --jobs-etudiants --no-jobspy # sans Indeed
"""

from __future__ import annotations

import logging

import config
import dedup
import filters
import main
import observabilite
import ranker
import storage
import trajets
from normalize import Offre

logger = logging.getLogger(__name__)


def collecter_et_dedupliquer(utiliser_jobspy: bool = True,
                             calculateur: trajets.Calculateur | None = None) -> list[Offre]:
    """Collecte, filtre (fraîcheur, trajet) et déduplique. Le bilan est journalisé au passage.

    Le trajet est calculé AVANT la déduplication : une fusion garde la version
    la plus riche, et celle-ci doit déjà porter sa commune et ses trajets.
    """
    offres = main.collecter(utiliser_jobspy, perimetre=main.perimetre_jobs())
    offres = filters.filtrer_jobs_etudiants(offres)
    offres = trajets.filtrer(offres, calculateur or trajets.Calculateur.depuis_config())
    observabilite.journaliser()
    offres = dedup.dedupliquer(offres)
    if offres and config.DEDUP_FLOUE_ACTIVE:
        embeddings = ranker.encoder_offres(offres)
        offres, _ = dedup.dedupliquer_flou(offres, embeddings)
    logger.info("Jobs étudiants : %d offre(s) après déduplication.", len(offres))
    return offres


def afficher_cli(offres: list[Offre]) -> None:
    """Liste lisible : nouveauté, drapeaux, titre, entreprise, lieu, sources, étiquettes."""
    print(f"\n{'=' * 100}\n {len(offres)} job(s) étudiant(s)\n{'=' * 100}\n")
    for offre in offres:
        neuf = "🆕 " if getattr(offre, "nouvelle", False) else ""
        drapeaux = f"  ⚑ {' '.join(offre.drapeaux)}" if offre.drapeaux else ""
        print(f"{neuf}{offre.title}{drapeaux}")
        print(f"       {offre.company or '—'}  |  {offre.location or '—'}  |  "
              f"{', '.join(sorted({l.source for l in offre.liens})) or offre.source}")
        if offre.familles:
            print(f"       termes : {', '.join(offre.familles)}")
        if offre.trajets:
            print("       trajet : " + "  ".join(
                f"{o} {t['brut_min']:.0f}→{t['ajuste_min']:.0f} min"
                f"{' (estim.)' if t['estimation'] else ''}" for o, t in offre.trajets.items()))
        else:
            print("       trajet : inconnu (commune non reconnue)")
        for lien in offre.liens or []:
            print(f"       {lien.url}")
        print()


def executer(utiliser_jobspy: bool = True, chemin_base=config.CHEMIN_BASE_JOBS,
             calculateur: trajets.Calculateur | None = None) -> list[Offre]:
    """Run complet des jobs étudiants. ``chemin_base=None`` : rien n'est persisté."""
    offres = collecter_et_dedupliquer(utiliser_jobspy, calculateur)
    tableau = observabilite.rendre_tableau()
    if tableau:
        print(f"\n{'=' * 78}\n BILAN PAR SOURCE — JOBS ÉTUDIANTS\n{'=' * 78}\n{tableau}\n")
    if chemin_base is not None and offres:
        conn = storage.ouvrir(str(chemin_base))
        try:
            storage.enregistrer_run(conn, [(o, 0.0) for o in offres])
        finally:
            conn.close()
    afficher_cli(offres)
    return offres
