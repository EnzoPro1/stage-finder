"""Tests de l'instantané de référence et de la détection de dérive."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

import etiqueter
import jobs
import reference
import storage


def _offre(conn, cle, title="Stage IA", texte="Vos missions : coder.", vue="2026-08-09"):
    conn.execute(
        """INSERT INTO offres (cle,title,company,location,url,source,posted_at,salary,
                               duree_mois,date_debut,tags,dernier_score,
                               premiere_vue,derniere_vue,nb_vues)
           VALUES (?,?,'ACME','Paris',?,'careerjet','','',NULL,'','',0.5,?,?,1)""",
        (cle, title, f"http://x/{cle}", vue, vue),
    )
    jobs.enregistrer_texte(conn, cle, texte)
    conn.execute("INSERT INTO runs (horodatage, nb_offres, nb_nouvelles) VALUES (?,1,1)",
                 (f"{vue}T10:00:00",))
    conn.commit()


@pytest.fixture
def base(tmp_path):
    chemin = str(tmp_path / "stages.db")
    conn = storage.ouvrir(chemin)
    jobs.ensure_schema(conn)
    etiqueter.ensure_schema(conn)
    _offre(conn, "aaa")
    _offre(conn, "bbb", title="Stage Cyber")
    yield chemin, conn
    conn.close()


# ---------------------------------------------------------------------------
# Empreinte
# ---------------------------------------------------------------------------
def test_empreinte_est_stable(base):
    _chemin, conn = base
    assert reference.empreinte_donnees(conn) == reference.empreinte_donnees(conn)


def test_empreinte_ignore_les_etiquettes(base):
    """L'étiquetage écrit dans la même base : il ne doit pas crier à la dérive."""
    _chemin, conn = base
    avant = reference.empreinte_donnees(conn)
    etiqueter.enregistrer_etiquette(
        conn, {"cle": "aaa", "title": "T", "company": "C", "url": "U"}, 1)
    assert reference.empreinte_donnees(conn) == avant


def test_empreinte_bouge_quand_une_offre_change(base):
    _chemin, conn = base
    avant = reference.empreinte_donnees(conn)
    conn.execute("UPDATE offres SET dernier_score = 0.9 WHERE cle = 'aaa'")
    conn.commit()
    assert reference.empreinte_donnees(conn) != avant


def test_empreinte_bouge_quand_un_texte_change(base):
    _chemin, conn = base
    avant = reference.empreinte_donnees(conn)
    jobs.enregistrer_texte(conn, "aaa", "Vos missions : tout autre chose.")
    assert reference.empreinte_donnees(conn) != avant


def test_empreinte_distingue_null_de_chaine_vide(base):
    _chemin, conn = base
    conn.execute("UPDATE offres SET salary = '' WHERE cle = 'aaa'")
    conn.commit()
    vide = reference.empreinte_donnees(conn)
    conn.execute("UPDATE offres SET salary = NULL WHERE cle = 'aaa'")
    conn.commit()
    assert reference.empreinte_donnees(conn) != vide


# ---------------------------------------------------------------------------
# Geler
# ---------------------------------------------------------------------------
def test_geler_produit_un_fichier_autonome_et_un_manifeste(base, tmp_path):
    chemin, _conn = base
    manifeste_path = str(tmp_path / "reference.json")
    m = reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste_path)

    fige = tmp_path / m["chemin"]
    assert fige.is_file()
    assert not (tmp_path / (m["chemin"] + "-wal")).exists(), "VACUUM INTO : aucun WAL"
    assert m["nb_offres"] == 2 and m["nb_textes"] == 2
    assert json.loads(open(manifeste_path, encoding="utf-8").read())["empreinte"] == \
        m["empreinte"]


def test_instantane_a_la_meme_empreinte_que_la_source(base, tmp_path):
    chemin, conn = base
    m = reference.geler(chemin, dossier=str(tmp_path),
                        chemin_manifeste=str(tmp_path / "reference.json"))
    fige = sqlite3.connect(str(tmp_path / m["chemin"]))
    try:
        assert reference.empreinte_donnees(fige) == reference.empreinte_donnees(conn)
    finally:
        fige.close()


# ---------------------------------------------------------------------------
# Contrôle de dérive
# ---------------------------------------------------------------------------
def test_controle_conforme_sur_l_instantane(base, tmp_path):
    chemin, _conn = base
    manifeste = str(tmp_path / "reference.json")
    m = reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    fige_path = str(tmp_path / m["chemin"])
    fige = sqlite3.connect(fige_path)
    try:
        controle = reference.controler(fige, fige_path, manifeste)
    finally:
        fige.close()
    assert controle["conforme"] is True and controle["alertes"] == []


def test_controle_detecte_un_scrape_dans_l_instantane(base, tmp_path):
    """LA dérive : la bonne base, mais son contenu a bougé."""
    chemin, _conn = base
    manifeste = str(tmp_path / "reference.json")
    m = reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    fige_path = str(tmp_path / m["chemin"])

    fige = storage.ouvrir(fige_path)
    jobs.ensure_schema(fige)
    _offre(fige, "ccc", title="Offre arrivée après le gel")
    try:
        controle = reference.controler(fige, fige_path, manifeste)
    finally:
        fige.close()

    assert controle["conforme"] is False
    assert any("empreinte" in a for a in controle["alertes"])


def test_controle_detecte_une_base_qui_n_est_pas_l_instantane(base, tmp_path):
    chemin, conn = base
    manifeste = str(tmp_path / "reference.json")
    reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    controle = reference.controler(conn, chemin, manifeste)
    assert controle["conforme"] is False
    assert any("≠ instantané" in a for a in controle["alertes"])


def test_sans_manifeste_le_controle_le_dit_au_lieu_de_valider(base, tmp_path):
    chemin, conn = base
    controle = reference.controler(conn, chemin, str(tmp_path / "absent.json"))
    assert controle["conforme"] is None, (
        "aucune référence n'est PAS la même chose qu'une référence respectée"
    )


def test_entete_crie_sur_derive(base, tmp_path, capsys):
    chemin, conn = base
    manifeste = str(tmp_path / "reference.json")
    reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    reference.afficher_entete(reference.controler(conn, chemin, manifeste), chemin)
    sortie = capsys.readouterr().out
    assert "DÉRIVE DE LA RÉFÉRENCE" in sortie
    assert "n'est PAS comparable" in sortie


# ---------------------------------------------------------------------------
# Base par défaut
# ---------------------------------------------------------------------------
def test_base_par_defaut_prefere_l_instantane(base, tmp_path, monkeypatch):
    chemin, _conn = base
    manifeste = str(tmp_path / "reference.json")
    m = reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    monkeypatch.chdir(tmp_path)
    assert reference.base_par_defaut(manifeste) == m["chemin"]


def test_base_par_defaut_retombe_sur_la_base_vive_sans_manifeste(tmp_path):
    import config
    assert reference.base_par_defaut(str(tmp_path / "absent.json")) == config.CHEMIN_BASE


def test_base_par_defaut_retombe_si_l_instantane_a_ete_efface(base, tmp_path, monkeypatch):
    import config
    chemin, _conn = base
    manifeste = str(tmp_path / "reference.json")
    m = reference.geler(chemin, dossier=str(tmp_path), chemin_manifeste=manifeste)
    os.remove(tmp_path / m["chemin"])
    monkeypatch.chdir(tmp_path)
    assert reference.base_par_defaut(manifeste) == config.CHEMIN_BASE


# ---------------------------------------------------------------------------
# Écriture atomique
# ---------------------------------------------------------------------------
def test_ecriture_atomique_ne_laisse_pas_de_temporaire(tmp_path):
    cible = tmp_path / "m.json"
    reference.ecrire_atomique(str(cible), "contenu\n")
    assert cible.read_text(encoding="utf-8") == "contenu\n"
    assert list(tmp_path.iterdir()) == [cible]
