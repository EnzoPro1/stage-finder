"""Tests des métriques d'évaluation (precision@k, nDCG@k)."""

from __future__ import annotations

import math

import evaluation


def test_precision_at_k():
    # 3 pertinents sur les 5 premiers.
    rels = [1, 0, 1, 1, 0, 0]
    assert evaluation.precision_at_k(rels, 5) == 3 / 5


def test_precision_at_k_liste_plus_courte_que_k():
    assert evaluation.precision_at_k([1, 1], 5) == 1.0


def test_ndcg_classement_parfait_vaut_1():
    rels = [1, 1, 1, 0, 0]
    assert evaluation.ndcg_at_k(rels, 5) == 1.0


def test_ndcg_classement_inverse_est_penalise():
    parfait = evaluation.ndcg_at_k([1, 1, 0, 0], 4)
    mauvais = evaluation.ndcg_at_k([0, 0, 1, 1], 4)
    assert parfait == 1.0
    assert mauvais < parfait


def test_dcg_valeurs_connues():
    # DCG de [1, 1] = 1/log2(2) + 1/log2(3) = 1 + 0.6309...
    dcg = evaluation.dcg_at_k([1, 1], 2)
    assert abs(dcg - (1 + 1 / math.log2(3))) < 1e-9


def test_ndcg_tout_zero_vaut_zero():
    assert evaluation.ndcg_at_k([0, 0, 0], 3) == 0.0
