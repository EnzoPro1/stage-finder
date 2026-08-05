"""Tests des garde-fous contre les FAUX POSITIFS de la vérification LLM.

Cas de référence, observé en production : « Legal intern » publié par un éditeur
de cybersécurité, noté 0.90 avec ``domaine_match=true``. Le modèle ne voyait que
la plaquette de la société (saturée de vocabulaire cyber) parce que les 600
premiers caractères de l'annonce ne contiennent jamais les missions.
"""

from __future__ import annotations

import pytest

import verifier
from verifier import Verdict

PLAQUETTE = (
    "Notre mission au quotidien est de protéger les données et les actifs critiques "
    "des entreprises à travers le monde, en découvrant les vulnérabilités cachées "
    "avant les cybercriminels. Chez CybelAngel, nous allons au-delà des périmètres "
    "traditionnels pour protéger les entreprises contre les menaces cyber les plus "
    "critiques. Des entreprises du Fortune 500 aux sociétés de taille moyenne font "
    "confiance à nos 130 collaborateurs basés aux États-Unis et en France. "
) * 3


def _verdict(**kw):
    base = dict(pertinent=True, score=0.9, est_alternance=False, niveau="stage",
                justification="", domaine_match=True, drapeaux_rouges=[])
    base.update(kw)
    return Verdict(**base)


# --- Extraction de la description ------------------------------------------
def test_extrait_saute_la_plaquette_et_vise_les_missions():
    description = PLAQUETTE + "Vos missions : rédaction de contrats, conformité RGPD."
    extrait = verifier._extrait_pertinent(description)
    assert extrait.startswith("Vos missions")
    assert "contrats" in extrait
    # La plaquette ne doit plus occuper la fenêtre.
    assert "Fortune 500" not in extrait


def test_extrait_repli_sur_le_debut_sans_marqueur():
    description = "Texte sans section identifiable. " * 40
    extrait = verifier._extrait_pertinent(description)
    assert extrait == description[: verifier._MAX_DESC_CHARS]


def test_extrait_tolere_une_description_vide():
    assert verifier._extrait_pertinent("") == ""
    assert verifier._extrait_pertinent(None or "") == ""


@pytest.mark.parametrize("marqueur", [
    "Vos missions", "Votre rôle", "Responsabilités", "Responsibilities",
    "Descriptif du poste", "Au sein de l'équipe",
])
def test_extrait_reconnait_les_marqueurs_courants(marqueur):
    extrait = verifier._extrait_pertinent(PLAQUETTE + marqueur + " : développer des modèles.")
    assert extrait.lower().startswith(marqueur.lower()[:8])


# --- Détection du métier support -------------------------------------------
@pytest.mark.parametrize("titre", [
    "Legal intern",
    "Stage - Talent acquisition Specialist (H/F)",
    "Chargé(e) de Mission RH Généraliste – Stage (F/H)",
    "Stage Juriste droit des affaires",
    "Stage Chargé de Communication & Marketing",
    "Stage – Commercial (cabinet de recrutement tech)",
])
def test_metier_support_detecte(titre):
    assert verifier.est_metier_support(titre)


@pytest.mark.parametrize("titre", [
    # Un signal technique dans le titre ANNULE la règle : ces postes restent des
    # postes techniques même s'ils touchent un domaine fonctionnel.
    "Stage Data Scientist - Marketing Analytics",
    "Stage Machine Learning pour la détection de fraude financière",
    "Stagiaire IA appliquée au recrutement",
    # Postes techniques ordinaires : aucun mot support.
    "Stage Ingénieur IA / Cybersécurité",
    "Stage - Active Directory, protéger et se préparer au pire",
])
def test_metier_technique_non_disqualifie(titre):
    assert not verifier.est_metier_support(titre)


@pytest.mark.parametrize("titre", [
    # « talent » isolé attrapait ce nom d'ÉVÉNEMENT sur un poste technique.
    "Data Engineer (H/F) STAGE TALENT DAY",
    # « communication » isolé attrapait cette recherche en apprentissage par
    # renforcement multi-agents (MARL).
    "Semantic Communication MARL Internship",
])
def test_termes_ambigus_ne_declassent_pas_un_poste_technique(titre):
    """Régression : un garde-fou trop large déclassait de vraies offres."""
    assert not verifier.est_metier_support(titre)


def test_plafonnement_metier_support_ecrase_le_score():
    """Le cas « Legal intern » : 0.90 et domaine_match=true -> plafonné."""
    v = _verdict(score=0.9, domaine_match=True)
    verifier._plafonner_metier_support(v, "Legal intern")
    verifier._plafonner_violations(v)
    assert v.domaine_match is False
    assert v.score <= 0.3
    # La raison doit être VISIBLE dans l'interface, pas silencieuse.
    assert any("métier support" in d for d in v.drapeaux_rouges)


def test_plafonnement_n_ajoute_pas_deux_fois_le_drapeau():
    """Le modèle avait déjà vu juste : on ne redouble pas l'explication."""
    v = _verdict(score=0.2, domaine_match=False)
    verifier._plafonner_metier_support(v, "Legal intern")
    assert v.drapeaux_rouges == []


def test_poste_technique_intact():
    v = _verdict(score=0.95, domaine_match=True)
    verifier._plafonner_metier_support(v, "Stage Ingénieur IA / Cybersécurité")
    verifier._plafonner_violations(v)
    assert v.domaine_match is True
    assert v.score == 0.95


# --- Versionnement du cache -------------------------------------------------
def test_cle_cache_versionnee():
    """Changer les règles doit produire une clé différente du modèle seul."""
    cle = verifier.cle_cache("qwen3:1.7b")
    assert cle != "qwen3:1.7b"
    assert cle.startswith("qwen3:1.7b@v")
    assert str(verifier.VERSION_REGLES) in cle
