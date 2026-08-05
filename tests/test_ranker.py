"""Tests du ranking qui ne nécessitent PAS le modèle (boost, composition).

On évite volontairement de charger sentence-transformers ici : ces fonctions
sont pures (regex, numpy) et testables en isolation.
"""

from __future__ import annotations

import numpy as np

import config
import ranker
from normalize import Offre


def _offre(title, description=""):
    return Offre(title, "ACME", "Paris", description, "http://x", "test", "", "")


def test_boost_ia_dans_titre():
    boost, tags = ranker.calculer_boost(_offre("Stage machine learning"))
    assert "IA" in tags
    assert boost >= config.BOOST_IA - 1e-9


def test_boost_combo_exige_signal_titre(monkeypatch):
    monkeypatch.setattr(config, "COMBO_EXIGE_SIGNAL_TITRE", True)
    # IA + cyber tous deux seulement en description -> pas de combo.
    _, tags = ranker.calculer_boost(
        _offre("Stage généraliste", "machine learning et cybersécurité")
    )
    assert "★ IA+CYBER" not in tags
    # Au moins un signal dans le titre -> combo accordé.
    _, tags2 = ranker.calculer_boost(
        _offre("Stage IA sécurité", "machine learning et cybersécurité")
    )
    assert "★ IA+CYBER" in tags2


def test_boost_description_attenue():
    plein, _ = ranker.calculer_boost(_offre("Stage machine learning"))
    attenue, _ = ranker.calculer_boost(_offre("Stage généraliste", "machine learning"))
    assert attenue < plein


def test_normaliser_minmax():
    v = np.array([0.2, 0.4, 0.6])
    out = ranker._normaliser(v, "minmax")
    assert out.min() == 0.0 and out.max() == 1.0


def test_normaliser_constante_ne_divise_pas_par_zero():
    v = np.array([0.5, 0.5, 0.5])
    out = ranker._normaliser(v, "minmax")
    assert np.allclose(out, 0.0)


def test_composer_lexicographique_met_combo_en_tete(monkeypatch):
    monkeypatch.setattr(config, "MODE_COMPOSITION_SCORE", "lexicographique")
    o_combo = _offre("Stage IA cyber")
    o_sim = _offre("Stage autre")
    offres = [o_sim, o_combo]
    sims = np.array([0.9, 0.1])          # o_sim a une meilleure similarité brute
    boosts_mc = np.array([0.0, 0.25])
    tags = [[], ["IA", "Cyber", "★ IA+CYBER"]]
    boosts_soft = np.array([0.0, 0.0])
    classees = ranker.composer(offres, sims, boosts_mc, tags, boosts_soft)
    # Malgré une similarité plus faible, le combo passe devant.
    assert classees[0][0] is o_combo


def test_composer_multiplicatif(monkeypatch):
    monkeypatch.setattr(config, "MODE_COMPOSITION_SCORE", "multiplicatif")
    offres = [_offre("A"), _offre("B")]
    sims = np.array([0.5, 0.5])
    boosts_mc = np.array([0.2, 0.0])
    boosts_soft = np.array([0.0, 0.0])
    classees = ranker.composer(offres, sims, boosts_mc, [[], []], boosts_soft)
    # 0.5*(1+0.2)=0.6 > 0.5*(1+0)=0.5
    assert classees[0][1] > classees[1][1]
    assert abs(classees[0][1] - 0.6) < 1e-9
