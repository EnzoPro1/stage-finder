"""Tests de l'extraction durée / date de début et des signaux souples."""

from __future__ import annotations

import config
import extract
from normalize import Offre


def test_extraire_duree_mois():
    assert extract.extraire_duree_mois("Stage de 6 mois") == 6
    assert extract.extraire_duree_mois("Stage 4 à 6 mois") == 6  # borne haute
    assert extract.extraire_duree_mois("6-month internship") == 6
    assert extract.extraire_duree_mois("Stage de 3 mois") == 3
    assert extract.extraire_duree_mois("Aucune durée") is None


def test_extraire_duree_semaines_repli():
    # 26 semaines ~ 6 mois.
    assert extract.extraire_duree_mois("Stage de 26 semaines") == 6


def test_extraire_date_debut():
    assert extract.extraire_date_debut("à partir de janvier 2027") == (2027, 1)
    assert extract.extraire_date_debut("starting September 2026") == (2026, 9)
    assert extract.extraire_date_debut("début 03/2027") == (2027, 3)
    assert extract.extraire_date_debut("2027-01") == (2027, 1)
    assert extract.extraire_date_debut("pas de date") is None


def test_annoter_remplit_colonnes():
    o = Offre("Stage IA", "ACME", "Paris",
              "Stage de 6 mois à partir de janvier 2027.", "http://x", "t", "", "")
    extract.annoter(o)
    assert o.duree_mois == 6
    assert o.date_debut == "2027-01"


def test_boost_signaux_bonus_et_malus():
    cible = Offre("S", "", "", "Stage de 6 mois à partir de janvier 2027.",
                  "http://x", "t", "", "")
    extract.annoter(cible)
    # Durée cible + date cible -> bonus positif.
    assert extract.boost_signaux(cible) == config.BONUS_DUREE_CIBLE + config.BONUS_DATE_DEBUT

    courte = Offre("S", "", "", "Stage de 2 mois.", "http://y", "t", "", "")
    extract.annoter(courte)
    assert extract.boost_signaux(courte) == -config.MALUS_DUREE_COURTE

    inconnue = Offre("S", "", "", "Pas d'info.", "http://z", "t", "", "")
    extract.annoter(inconnue)
    assert extract.boost_signaux(inconnue) == 0.0
