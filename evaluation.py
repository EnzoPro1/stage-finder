"""
evaluation.py — Harnais d'évaluation du ranking (precision@k / nDCG).

Aujourd'hui les poids (BOOST_IA, BOOST_CYBER…) et le mode de composition du
score sont réglés à la main, à l'aveugle. Ce module transforme ce réglage en
**optimisation mesurée** :

  1. On labellise un petit jeu d'offres (pertinent = 1 / non pertinent = 0) dans
     un fichier JSON (voir labels.example.json).
  2. On fait classer ces offres par le ranking réel.
  3. On mesure la qualité du classement avec deux métriques standard de
     l'information retrieval :
        - precision@k : proportion d'offres pertinentes dans le top-k.
        - nDCG@k      : gain cumulé actualisé normalisé (récompense les
                        pertinents placés HAUT dans la liste).

On peut alors comparer objectivement deux réglages (modèle, poids, mode de
composition) au lieu de juger « à l'œil ».

Le module s'appelle `evaluation` (et non `eval`) pour ne pas masquer la builtin
`eval` de Python.

Utilisation :
    python evaluation.py labels.example.json --k 10
"""

from __future__ import annotations

import argparse
import json
import logging
import math

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Métriques pures (facilement testables, sans dépendance au modèle)
# ---------------------------------------------------------------------------
def precision_at_k(pertinences: list[float], k: int) -> float:
    """Proportion d'items pertinents (relevance > 0) dans les k premiers.

    ``pertinences`` est la liste des labels DANS L'ORDRE DU CLASSEMENT produit.
    """
    if k <= 0:
        return 0.0
    tete = pertinences[:k]
    if not tete:
        return 0.0
    return sum(1 for p in tete if p > 0) / min(k, len(tete))


def dcg_at_k(pertinences: list[float], k: int) -> float:
    """Discounted Cumulative Gain sur les k premiers items."""
    return sum(
        rel / math.log2(i + 2)  # position 0 -> log2(2)=1
        for i, rel in enumerate(pertinences[:k])
    )


def ndcg_at_k(pertinences: list[float], k: int) -> float:
    """nDCG@k : DCG du classement divisé par le DCG idéal (labels triés desc)."""
    dcg = dcg_at_k(pertinences, k)
    ideal = dcg_at_k(sorted(pertinences, reverse=True), k)
    return dcg / ideal if ideal > 0 else 0.0


# ---------------------------------------------------------------------------
# Harnais : charge un jeu labellisé, classe, mesure
# ---------------------------------------------------------------------------
def _charger_labels(chemin: str) -> list[dict]:
    """Charge un fichier JSON = liste d'offres avec un champ `pertinent`."""
    with open(chemin, "r", encoding="utf-8") as f:
        donnees = json.load(f)
    if not isinstance(donnees, list):
        raise ValueError("Le fichier de labels doit être une liste JSON d'offres.")
    return donnees


def evaluer(chemin_labels: str, k: int = 10) -> dict:
    """Classe le jeu labellisé et retourne {precision@k, ndcg@k, n}.

    Import tardif de `ranker`/`normalize`/`extract` : on ne paie le coût du
    modèle que si on lance vraiment une évaluation.
    """
    import extract
    import ranker
    from normalize import Offre

    brut = _charger_labels(chemin_labels)

    offres, labels = [], {}
    for item in brut:
        offre = Offre(
            title=item.get("title", ""),
            company=item.get("company", ""),
            location=item.get("location", ""),
            description=item.get("description", ""),
            url=item.get("url", ""),
            source=item.get("source", "eval"),
            posted_at=item.get("posted_at", ""),
            salary=item.get("salary", ""),
        )
        offres.append(offre)
        # Clé d'alignement label ↔ offre = URL (unique dans un jeu propre).
        labels[offre.url] = float(item.get("pertinent", 0))

    extract.annoter_toutes(offres)
    classees = ranker.classer(offres)

    pertinences_classees = [labels.get(o.url, 0.0) for (o, _s) in classees]
    resultat = {
        "n": len(offres),
        "k": k,
        "precision@k": round(precision_at_k(pertinences_classees, k), 4),
        "ndcg@k": round(ndcg_at_k(pertinences_classees, k), 4),
    }
    return resultat


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Évaluation du ranking (precision@k / nDCG).")
    parser.add_argument("labels", nargs="?", default="labels.example.json",
                        help="Fichier JSON d'offres labellisées.")
    parser.add_argument("--k", type=int, default=10, help="Rang de coupure des métriques.")
    args = parser.parse_args()

    res = evaluer(args.labels, args.k)
    print("\n=== Évaluation du ranking ===")
    print(f"  Jeu           : {res['n']} offre(s) labellisée(s)")
    print(f"  Mode de score : (voir config.MODE_COMPOSITION_SCORE)")
    print(f"  precision@{res['k']:<3} : {res['precision@k']}")
    print(f"  nDCG@{res['k']:<7} : {res['ndcg@k']}")


if __name__ == "__main__":
    main()
