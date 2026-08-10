"""Tests des deux métriques ajoutées : rang médian, négatives dans le top-k."""

from __future__ import annotations

import evaluation


# ---------------------------------------------------------------------------
# rang_median_positives
# ---------------------------------------------------------------------------
def test_rang_median_sur_nombre_impair_de_positives():
    # positives aux rangs 1, 3, 5 -> médiane 3
    assert evaluation.rang_median_positives([1, 0, 1, 0, 1]) == 3


def test_rang_median_sur_nombre_pair_reste_une_demi_position():
    # positives aux rangs 2 et 5 -> 3.5, et surtout PAS arrondi vers un rang réel
    assert evaluation.rang_median_positives([0, 1, 0, 0, 1]) == 3.5


def test_rang_median_sans_positive_vaut_none():
    assert evaluation.rang_median_positives([0, 0, 0]) is None
    assert evaluation.rang_median_positives([]) is None


def test_rang_median_bouge_quand_precision_et_ndcg_ne_bougent_pas():
    """La raison d'être de cette métrique : voir ce que le top-k ignore."""
    tete = [0] * 10
    avant = tete + [0] * 26 + [1]      # positive au rang 37
    apres = tete + [1] + [0] * 26      # positive au rang 11

    assert evaluation.precision_at_k(avant, 10) == evaluation.precision_at_k(apres, 10)
    assert evaluation.ndcg_at_k(avant, 10) == evaluation.ndcg_at_k(apres, 10)
    assert evaluation.rang_median_positives(avant) == 37
    assert evaluation.rang_median_positives(apres) == 11


# ---------------------------------------------------------------------------
# negatives_dans_top_k
# ---------------------------------------------------------------------------
def test_negatives_dans_top_k_compte_les_zeros():
    assert evaluation.negatives_dans_top_k([1, 0, 0, 1, 0], 4) == 2


def test_negatives_dans_top_k_ne_compte_pas_les_places_manquantes():
    """Une liste de 2 items n'a pas 8 négatives dans son top-10."""
    assert evaluation.negatives_dans_top_k([1, 1], 10) == 0


def test_negatives_dans_top_k_classement_parfait_vaut_zero():
    assert evaluation.negatives_dans_top_k([1] * 10 + [0] * 20, 10) == 0


def test_negatives_dans_top_k_avec_k_nul():
    assert evaluation.negatives_dans_top_k([0, 0], 0) == 0
