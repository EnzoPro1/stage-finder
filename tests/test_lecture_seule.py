"""
storage.ouvrir_lecture_seule : mesurer sur un instantané sans jamais l'écrire.

`storage.ouvrir` pose WAL et crée le schéma — il écrit. Sur l'instantané de
référence, ce serait modifier l'objet qu'on mesure. On vérifie ici que la
variante lecture seule ne change pas un octet et ne crée aucun fichier annexe.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

import storage


def _instantane(tmp_path):
    chemin = tmp_path / "instantane.db"
    conn = storage.ouvrir(str(chemin))  # base en WAL, comme la vraie
    conn.execute("INSERT INTO offres (cle, title, company, location, url, nb_vues) "
                 "VALUES ('k', 'Stage IA', 'ACME', 'Paris', 'http://x', 1)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for annexe in tmp_path.glob("instantane.db-*"):
        annexe.unlink()
    return chemin


def test_ni_octet_modifie_ni_fichier_annexe(tmp_path):
    chemin = _instantane(tmp_path)
    avant = hashlib.sha256(chemin.read_bytes()).hexdigest()
    conn = storage.ouvrir_lecture_seule(str(chemin))
    assert conn.execute("SELECT title FROM offres").fetchone()["title"] == "Stage IA"
    conn.close()
    assert hashlib.sha256(chemin.read_bytes()).hexdigest() == avant
    assert sorted(p.name for p in tmp_path.iterdir()) == ["instantane.db"]


def test_ecriture_refusee(tmp_path):
    conn = storage.ouvrir_lecture_seule(str(_instantane(tmp_path)))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM offres")


def test_fichier_absent_leve_au_lieu_de_creer_une_base(tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.ouvrir_lecture_seule(str(tmp_path / "absent.db"))
    assert not (tmp_path / "absent.db").exists()
