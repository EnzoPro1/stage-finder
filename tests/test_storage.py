"""Tests de la persistance SQLite (cache, nouveautés, historique)."""

from __future__ import annotations

import storage
from normalize import Offre


def _offre(title, url):
    return Offre(title, "ACME", "Paris", "desc", url, "test", "", "")


def test_premier_run_tout_est_nouveau():
    conn = storage.ouvrir(":memory:")
    classees = [(_offre("Stage IA", "http://a"), 0.5),
                (_offre("Stage Cyber", "http://b"), 0.4)]
    nouvelles = storage.enregistrer_run(conn, classees)
    assert len(nouvelles) == 2


def test_deuxieme_run_ne_reannonce_pas_les_connues():
    conn = storage.ouvrir(":memory:")
    o1 = _offre("Stage IA", "http://a")
    storage.enregistrer_run(conn, [(o1, 0.5)])
    # Même offre (même clé d'identité) + une nouvelle.
    o1b = _offre("Stage IA", "http://a-mirror")  # même titre/entreprise/ville
    o2 = _offre("Stage Data", "http://c")
    nouvelles = storage.enregistrer_run(conn, [(o1b, 0.5), (o2, 0.6)])
    assert len(nouvelles) == 1
    assert storage.cle_identite(o2) in nouvelles


def test_filtrer_nouveautes():
    o1 = _offre("Stage IA", "http://a")
    o2 = _offre("Stage Data", "http://c")
    classees = [(o1, 0.5), (o2, 0.6)]
    nouvelles = {storage.cle_identite(o2)}
    filtrees = storage.filtrer_nouveautes(classees, nouvelles)
    assert len(filtrees) == 1
    assert filtrees[0][0] is o2


def test_historique_runs_enregistre():
    conn = storage.ouvrir(":memory:")
    storage.enregistrer_run(conn, [(_offre("S", "http://a"), 0.5)])
    storage.enregistrer_run(conn, [(_offre("S", "http://a"), 0.5)])
    (nb,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert nb == 2
