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


# =====================================================================
# Hors ligne : le classement ne doit RIEN demander à Hugging Face
#
# Le projet revendique « 100 % local ». Le worker de génération posait
# déjà `HF_HUB_OFFLINE` (`embeddings_env.preparer`), mais le CLASSEMENT —
# le chemin de loin le plus emprunté — ne passait par aucun des deux :
# chaque chargement allait demander à HF si le modèle avait changé, d'où
# le message réclamant un `HF_TOKEN`. Sur une machine sans réseau, le
# chargement ÉCHOUAIT alors que le modèle était en cache.
# =====================================================================
def test_le_chargement_prepare_l_environnement_avant_d_importer(monkeypatch):
    """L'ORDRE est tout : `preparer` après l'import ne servirait à rien.

    `huggingface_hub` recopie `HF_HUB_OFFLINE` dans une constante à son
    import et ne la relit jamais.
    """
    import embeddings_env

    ordre = []
    monkeypatch.setattr(ranker, "_modele", None)
    monkeypatch.setattr(
        embeddings_env, "preparer",
        lambda nom=None: ordre.append(("preparer", nom)) or {"hors_ligne": True},
    )

    class FauxST:
        max_seq_length = 128

        def __init__(self, nom):
            ordre.append(("SentenceTransformer", nom))

    import sys
    from types import ModuleType

    faux = ModuleType("sentence_transformers")
    faux.SentenceTransformer = FauxST
    monkeypatch.setitem(sys.modules, "sentence_transformers", faux)

    ranker._charger_modele()
    monkeypatch.setattr(ranker, "_modele", None)

    assert [nom for nom, _ in ordre] == ["preparer", "SentenceTransformer"]
    # Et sur LE modèle du projet, pas sur le défaut de cv_forge : les deux
    # se trouvent identiques aujourd'hui, ce n'est pas une garantie.
    assert ordre[0][1] == config.MODELE_EMBEDDING


def test_le_module_active_truststore_a_l_import():
    """Le bloc `try: import truststore` recopié a été remplacé par l'appel
    partagé : il doit rester ÉQUIVALENT, pas disparaître."""
    import ssl

    import embeddings_env

    assert embeddings_env.activer_truststore() is True
    assert ssl.SSLContext is not None      # l'injection n'a rien cassé
