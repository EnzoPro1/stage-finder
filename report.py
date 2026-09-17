"""
report.py — Export CSV des stages classés.

Ouvrable dans Excel/LibreOffice (encodage utf-8-sig pour les accents). Prend
la liste classée : [(Offre, score), ...].

Le rapport HTML n'est plus ici : `rapport.py` écrit UNE page, stages et jobs
étudiants ensemble, lue dans les deux bases.
"""

from __future__ import annotations

import csv
import logging

from normalize import Offre

logger = logging.getLogger(__name__)

# Colonnes du CSV (ordre stable). duree_mois / date_debut sont les colonnes
# filtrables issues de l'extraction ; nouvelle indique une offre jamais vue.
_COLONNES = [
    "rang", "score", "match", "nouvelle", "title", "company", "location",
    "source", "posted_at", "duree_mois", "date_debut", "salary",
    # Verdict de la vérification LLM (vides pour les offres non vérifiées).
    "score_llm", "pertinent", "niveau", "est_alternance", "drapeaux_rouges",
    "justification", "url", "description",
]


def _champs_verdict(offre: Offre) -> dict:
    """Colonnes issues du verdict LLM (vides si l'offre n'a pas été vérifiée)."""
    verdict = getattr(offre, "verdict", None)
    if verdict is None:
        return {c: "" for c in (
            "score_llm", "pertinent", "niveau", "est_alternance",
            "drapeaux_rouges", "justification",
        )}
    return {
        "score_llm": round(verdict.score, 4),
        "pertinent": "oui" if verdict.pertinent else "non",
        "niveau": verdict.niveau,
        "est_alternance": "oui" if verdict.est_alternance else "non",
        "drapeaux_rouges": " | ".join(verdict.drapeaux_rouges),
        "justification": verdict.justification,
    }


def exporter_csv(classees: list[tuple[Offre, float]], chemin: str) -> None:
    """Écrit les offres classées dans un fichier CSV."""
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        # extrasaction="ignore" : tout champ hors _COLONNES est simplement ignoré.
        writer = csv.DictWriter(f, fieldnames=_COLONNES, extrasaction="ignore")
        writer.writeheader()
        for rang, (offre, score) in enumerate(classees, 1):
            ligne = offre.as_dict()
            ligne["rang"] = rang
            ligne["score"] = round(score, 4)
            ligne["match"] = " ".join(offre.tags)
            ligne["nouvelle"] = "oui" if getattr(offre, "nouvelle", False) else ""
            ligne.update(_champs_verdict(offre))
            writer.writerow(ligne)
    logger.info("CSV écrit : %s (%d offre(s)).", chemin, len(classees))
