"""
evaluation.py — Métriques PURES de qualité d'un classement.

Ce module ne connaît ni les offres, ni le modèle, ni la base : il ne manipule
que des listes de pertinences **dans l'ordre du classement produit**. C'est ce
qui le rend testable sans rien charger, et réutilisable par n'importe quel
harnais.

Le harnais lui-même — charger le corpus étiqueté, classer, mesurer — vit dans
``evaluer_ranking.py``. Il vivait ici, sur un jeu de dix offres INVENTÉES
(`labels.example.json`) alignées sur l'URL ; ni l'un ni l'autre ne tenait :
des offres fictives ne mesurent pas un ranking, et les URLs de Careerjet sont
des redirections opaques qui ne survivent pas à un re-scrape.

Quatre métriques, qui disent quatre choses différentes :
  - precision@k        : proportion de pertinents dans le top-k.
  - nDCG@k             : récompense les pertinents placés HAUT.
  - rang médian des positives : parle du symptôme « ma bonne offre est 37e ».
  - négatives dans le top-k   : le critère d'acceptation, directement.

Le module s'appelle `evaluation` (et non `eval`) pour ne pas masquer la builtin
`eval` de Python.
"""

from __future__ import annotations

import logging
import math
import statistics

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


def rang_median_positives(pertinences: list[float]) -> float | None:
    """Rang médian (1-based) des items pertinents. ``None`` s'il n'y en a aucun.

    C'est la métrique qui parle du symptôme observé — « l'offre qui m'intéresse
    est 37e » — là où precision@k et nDCG@k ne regardent que la tête de liste
    et ne bougeraient pas d'un iota si cette offre passait de la 37e à la 30e
    place. Sur un nombre pair de positives, la médiane est une demi-position :
    c'est voulu, on ne l'arrondit pas vers un rang qui n'existe pas.
    """
    rangs = [i for i, rel in enumerate(pertinences, 1) if rel > 0]
    return statistics.median(rangs) if rangs else None


def negatives_dans_top_k(pertinences: list[float], k: int) -> int:
    """Nombre d'items NON pertinents dans les k premiers.

    Redondant avec precision@k tant que le corpus n'a que deux classes — et
    délibérément conservé : c'est la formulation du critère d'acceptation
    (« aucune offre étiquetée "ne m'intéresse pas" dans le top-10 »), donc un
    chiffre qui se lit sans conversion mentale. Une liste plus courte que k ne
    compte pas les places manquantes comme des négatives.
    """
    if k <= 0:
        return 0
    return sum(1 for rel in pertinences[:k] if rel <= 0)
