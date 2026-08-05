"""Tests des indicateurs marché (market.py).

Aucun appel réseau : les comptages des sources sont remplacés par des doublures.
Ce qui est vérifié, c'est le CALCUL des indicateurs — parts, rythme, lecture
qualitative — et surtout leur comportement quand une mesure manque (``None``),
cas fréquent en vrai (clé absente, API en panne) où un faux zéro serait pire
qu'une absence de chiffre.
"""

from __future__ import annotations

import sqlite3

import pytest

import market
import storage
from normalize import Offre


def test_rythme_extrapole_la_fenetre_courte():
    # 31 j -> 31 offres, soit 1/jour ; 7 offres sur 7 j = rythme identique.
    assert market._rythme(7, 31) == pytest.approx(1.0, abs=0.01)
    # Deux fois plus vite que la moyenne du mois.
    assert market._rythme(14, 31) == pytest.approx(2.0, abs=0.02)


def test_rythme_et_part_absorbent_les_mesures_manquantes():
    """Une mesure absente ne doit JAMAIS devenir 0 (ni faire planter le calcul)."""
    assert market._rythme(None, 31) is None
    assert market._rythme(7, None) is None
    assert market._rythme(7, 0) is None
    assert market._part(None, 10) is None
    assert market._part(5, 0) is None
    assert market._part(5, 10) == 50.0


def _volumes(ia_court, cyber_court, tous_court=40, tous_long=100,
             tension_ia=0.06, tension_cyber=0.09):
    """Jeu de mesures : la ligne de référence n'a PAS de code ROME, les autres si."""
    return [
        {"theme": "tous stages (IdF)", "code_rome": None, "ft_court": tous_court,
         "ft_long": tous_long, "careerjet_total": 900,
         "tension": None, "tension_annee": None},
        {"theme": "Ingénieur IA", "code_rome": "M1889", "ft_court": ia_court,
         "ft_long": 20, "careerjet_total": 100,
         "tension": tension_ia, "tension_annee": "2025"},
        {"theme": "Expert cybersécurité", "code_rome": "M1856", "ft_court": cyber_court,
         "ft_long": 10, "careerjet_total": 50,
         "tension": tension_cyber, "tension_annee": "2025"},
    ]


@pytest.mark.parametrize("ia,cyber,attendu", [
    (10, 8, "confortable"),   # 18 offres/semaine sur la niche
    (3, 3, "correct"),        # 6
    (1, 0, "étroit"),         # 1
])
def test_lecture_qualitative_du_flux(ia, cyber, attendu):
    lecture = market._lecture(_volumes(ia, cyber), {"renouvellement": 20.0})
    assert lecture["tension"] == attendu
    assert lecture["verdict"]


def test_lecture_saisonnalite():
    # 40 offres sur 7 j alors que le mois n'en donne que 100/31*7 ≈ 22 : accélération.
    assert market._lecture(_volumes(2, 2), {})["saison"] == "en accélération"
    # 10 sur 7 j pour 100 sur 31 j (≈ 22 attendues) : ralentissement.
    assert market._lecture(_volumes(2, 2, tous_court=10), {})["saison"] == "en ralentissement"
    # Mesure absente -> on ne conclut pas.
    assert market._lecture(_volumes(2, 2, tous_court=None), {})["saison"] == "inconnue"


def test_lecture_ignore_les_themes_non_mesures():
    """Un thème à None ne doit pas être compté comme 0 dans le flux de niche."""
    vols = _volumes(None, 8)
    assert market._lecture(vols, {})["flux_niche_7j"] == 8


def test_lecture_ne_conclut_pas_sans_aucune_mesure():
    """Rien de mesuré => « inconnu », surtout PAS « marché étroit ».

    Sans ce garde-fou, une panne d'API produirait un flux de 0 et l'interface
    annoncerait un marché bouché alors qu'elle n'a rien pu mesurer.
    """
    lecture = market._lecture(_volumes(None, None), {})
    assert lecture["tension"] == "inconnu"
    assert "aucune conclusion" in lecture["verdict"].lower()


def test_lecture_conclut_sur_un_vrai_zero():
    """Un zéro MESURÉ reste un signal : le marché est bien étroit."""
    assert market._lecture(_volumes(0, 0), {})["tension"] == "étroit"


def test_lecture_exclut_la_reference_du_flux_de_niche():
    """La ligne « tous stages » est un baromètre, pas une mesure de ta niche.

    Sans l'exclusion, ses 40 offres seraient comptées dans le flux de niche et
    le verdict passerait à « confortable » quel que soit ton domaine.
    """
    lecture = market._lecture(_volumes(2, 1), {})
    assert lecture["flux_niche_7j"] == 3
    assert lecture["tension"] == "étroit"
    # Ta niche pèse 3 offres sur les 40 du marché du stage francilien.
    assert lecture["part_niche"] == 7.5


def test_tension_officielle_moyennee_et_interpretee():
    """Tension positive = employeurs en difficulté = favorable au candidat."""
    lecture = market._lecture(_volumes(2, 2, tension_ia=0.06, tension_cyber=0.10), {})
    assert lecture["tension_officielle"] == 0.08
    assert "favorable au candidat" in lecture["lecture_tension"]
    assert lecture["tension_annee"] == "2025"


def test_tension_negative_signale_la_concurrence():
    lecture = market._lecture(_volumes(2, 2, tension_ia=-0.2, tension_cyber=-0.1), {})
    assert lecture["tension_officielle"] == -0.15
    assert "concurrence" in lecture["lecture_tension"]


def test_tension_absente_ne_produit_aucune_lecture():
    """API tension indisponible : on n'invente pas un « marché équilibré »."""
    lecture = market._lecture(_volumes(2, 2, tension_ia=None, tension_cyber=None), {})
    assert lecture["tension_officielle"] is None
    assert lecture["lecture_tension"] is None


def test_tension_ignore_la_ligne_de_reference():
    """La ligne « tous stages » n'a pas de tension : elle ne doit pas peser 0."""
    lecture = market._lecture(_volumes(2, 2, tension_ia=0.10, tension_cyber=0.10), {})
    assert lecture["tension_officielle"] == 0.1


def test_metiers_cibles_replie_sur_la_config(monkeypatch):
    """ROMEO indisponible => codes ROME de repli, pas de tableau vide."""
    from sources import romeo

    monkeypatch.setattr(romeo, "predire_metiers", lambda *a, **k: {})
    metiers = market.metiers_cibles()
    codes = [m["code"] for m in metiers]
    assert codes, "le repli doit fournir des métiers"
    assert all(c.startswith("M") for c in codes)
    # Aucun doublon : un même code ROME ne doit pas produire deux lignes.
    assert len(codes) == len(set(codes))


@pytest.fixture
def base():
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    yield conn
    conn.close()


def test_historique_sur_deux_runs(base):
    a = Offre("Stage IA", "ACME", "Paris", "d", "http://a", "s", "", "")
    a.tags = ["IA", "★ IA+CYBER"]
    b = Offre("Stage Cyber", "BetaCorp", "Paris", "d", "http://b", "s", "", "")
    storage.enregistrer_run(base, [(a, 0.9), (b, 0.5)])
    c = Offre("Stage Data", "ACME", "Paris", "d", "http://c", "s", "", "")
    storage.enregistrer_run(base, [(a, 0.9), (c, 0.7)])

    h = market.historique(base)
    assert h["total_offres_connues"] == 3
    assert h["combos_ia_cyber"] == 1
    assert len(h["runs"]) == 2
    # Les runs sont rendus en ordre chronologique (le plus ancien d'abord).
    assert h["runs"][0]["nb_nouvelles"] == 2 and h["runs"][1]["nb_nouvelles"] == 1
    # Dernier run : 1 nouveauté sur 2 offres.
    assert h["renouvellement"] == 50.0
    assert h["top_entreprises"][0] == {"nom": "ACME", "n": 2}


def test_historique_sur_base_vide(base):
    h = market.historique(base)
    assert h["total_offres_connues"] == 0
    assert h["runs"] == []
    assert h["renouvellement"] is None
    assert h["part_revues"] is None
