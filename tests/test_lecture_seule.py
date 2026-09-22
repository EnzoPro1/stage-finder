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


# ---------------------------------------------------------------------------
# Les portes d'entrée de mesure, et l'étiquetage
# ---------------------------------------------------------------------------
def _instantane_ancien_schema(dossier):
    """Instantané figé AVANT les colonnes de storage._COLONNES_AJOUTEES."""
    chemin = dossier / "stages.reference-20260101-000000.db"
    conn = sqlite3.connect(chemin)
    conn.execute("CREATE TABLE offres (cle TEXT PRIMARY KEY, title TEXT, company TEXT, "
                 "location TEXT, url TEXT, source TEXT, posted_at TEXT, salary TEXT)")
    conn.execute("CREATE TABLE offres_texte (cle TEXT PRIMARY KEY, texte TEXT)")
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, horodatage TEXT, nb_offres INTEGER)")
    conn.execute("INSERT INTO offres VALUES ('k','Stage IA','ACME','Paris','u','s','','')")
    conn.commit()
    conn.close()
    return chemin


def _colonnes(chemin):
    conn = sqlite3.connect(chemin)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(offres)")]
    finally:
        conn.close()


def test_etiqueter_n_ajoute_aucune_colonne_a_l_instantane(tmp_path):
    import reference
    import etiqueter

    chemin = _instantane_ancien_schema(tmp_path)
    avant_colonnes = _colonnes(chemin)
    lecture = reference.ouvrir_pour_mesure(str(chemin))
    avant = reference.empreinte_donnees(lecture)
    lecture.close()

    conn = storage.ouvrir_sans_migration(str(chemin))
    etiqueter.ensure_schema(conn)  # ce que fait etiqueter.main
    conn.close()

    assert _colonnes(chemin) == avant_colonnes
    lecture = reference.ouvrir_pour_mesure(str(chemin))
    assert reference.empreinte_donnees(lecture) == avant
    lecture.close()


def test_storage_ouvrir_aurait_change_l_empreinte(tmp_path):
    """Le piège que les ouvertures dédiées évitent, constaté et figé ici."""
    import reference

    chemin = _instantane_ancien_schema(tmp_path)
    lecture = reference.ouvrir_pour_mesure(str(chemin))
    avant = reference.empreinte_donnees(lecture)
    lecture.close()
    storage.ouvrir(str(chemin)).close()
    lecture = reference.ouvrir_pour_mesure(str(chemin))
    assert reference.empreinte_donnees(lecture) != avant
    lecture.close()


def test_immuable_sur_instantane_seulement(monkeypatch):
    import reference

    vus = []
    monkeypatch.setattr(storage, "ouvrir_lecture_seule",
                        lambda chemin, immuable: vus.append((chemin, immuable)))
    reference.ouvrir_pour_mesure("stages.reference-20260812-110639.db")
    reference.ouvrir_pour_mesure("stages.db")
    assert vus == [("stages.reference-20260812-110639.db", True), ("stages.db", False)]


@pytest.mark.parametrize("module, argv", [
    ("evaluer_ranking", ["evaluer_ranking.py", "--db", "x.db"]),
    ("reference", ["reference.py", "--controler", "--db", "x.db"]),
    ("benchmark", ["benchmark.py", "--db", "x.db"]),
])
def test_les_scripts_de_mesure_ouvrent_en_lecture_seule(monkeypatch, module, argv):
    import importlib

    import reference

    def interdit(*a, **k):
        raise AssertionError("storage.ouvrir écrit dans la base")

    class Arret(Exception):
        pass

    ouvertures = []

    def lecture(chemin):
        ouvertures.append(chemin)
        raise Arret
    monkeypatch.setattr(storage, "ouvrir", interdit)
    monkeypatch.setattr(reference, "ouvrir_pour_mesure", lecture)
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(Arret):
        importlib.import_module(module).main()
    assert ouvertures == ["x.db"]
