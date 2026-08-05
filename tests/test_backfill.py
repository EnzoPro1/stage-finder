"""Backfill de `offres_texte` depuis le cache de classement."""

from __future__ import annotations

import pytest

import backfill_textes
import jobs
import storage
from dedup import _cle as cle_identite
from normalize import Offre


@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    jobs.ensure_schema(c)
    yield c
    c.close()


def _row(title="Stage IA", company="ACME", location="Paris", description="Le texte."):
    return {"title": title, "company": company, "location": location,
            "description": description}


def _inserer_offre(conn, row):
    cle = cle_identite(Offre(row["title"], row["company"], row["location"],
                             "", "", "", "", ""))
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, nb_vues) "
        "VALUES (?,?,?,?,?,1)",
        (cle, row["title"], row["company"], row["location"], "http://x"),
    )
    conn.commit()
    return cle


def test_le_texte_est_rattache_a_la_bonne_offre(conn):
    row = _row()
    cle = _inserer_offre(conn, row)
    rapport = backfill_textes.backfill(conn, [row])
    assert rapport["ecrites"] == 1
    assert jobs.lire_texte(conn, cle) == "Le texte."


def test_la_simulation_n_ecrit_RIEN(conn):
    """Régression. Envelopper l'appel dans une transaction annulée ne
    suffisait pas : `jobs.enregistrer_texte` ouvre la sienne et la valide
    en sortie, donc la « simulation » écrivait pour de bon."""
    row = _row()
    cle = _inserer_offre(conn, row)

    rapport = backfill_textes.backfill(conn, [row], ecrire=False)

    assert rapport["ecrites"] == 1, "le rapport doit annoncer ce qui SERAIT écrit"
    assert jobs.lire_texte(conn, cle) is None, "la simulation a écrit en base"
    assert conn.execute("SELECT COUNT(*) FROM offres_texte").fetchone()[0] == 0


def test_relancer_ne_reecrit_pas(conn):
    row = _row()
    _inserer_offre(conn, row)
    backfill_textes.backfill(conn, [row])
    rapport = backfill_textes.backfill(conn, [row])
    assert (rapport["ecrites"], rapport["deja"]) == (0, 1)


def test_forcer_reecrit(conn):
    row = _row()
    cle = _inserer_offre(conn, row)
    backfill_textes.backfill(conn, [row])
    rapport = backfill_textes.backfill(conn, [_row(description="Nouveau.")],
                                       forcer=True)
    assert rapport["ecrites"] == 1
    assert jobs.lire_texte(conn, cle) == "Nouveau."


def test_une_offre_absente_de_la_base_n_est_PAS_creee(conn):
    """Ce script peuple une table latérale ; il n'invente pas d'offres."""
    rapport = backfill_textes.backfill(conn, [_row()])
    assert (rapport["ecrites"], rapport["hors_base"]) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0] == 0


def test_une_entree_sans_description_est_comptee_a_part(conn):
    row = _row(description="")
    _inserer_offre(conn, row)
    rapport = backfill_textes.backfill(conn, [row])
    assert (rapport["ecrites"], rapport["sans_texte"]) == (0, 1)


def test_une_description_blanche_compte_comme_absente(conn):
    row = _row(description="   \n  ")
    _inserer_offre(conn, row)
    assert backfill_textes.backfill(conn, [row])["sans_texte"] == 1


def test_la_cle_est_calculee_par_dedup_jamais_reimplementee(conn):
    """Deux calculs concurrents finiraient par diverger, et le symptôme
    serait un texte rattaché à la mauvaise offre."""
    row = _row(title="Stage Ingenieur IA (M/F)")
    cle = _inserer_offre(conn, row)
    backfill_textes.backfill(conn, [row])
    # La mention de genre est retirée des deux côtés, donc les clés
    # coïncident et le texte atterrit bien sur l'offre.
    assert jobs.lire_texte(conn, cle) == "Le texte."
