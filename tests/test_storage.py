"""Tests de la persistance SQLite (cache, nouveautés, historique)."""

from __future__ import annotations

import jobs
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


# ---------------------------------------------------------------------------
# Persistance du TEXTE au scrape
#
# Le défaut qu'ils verrouillent : pendant tout le CP4, AUCUN chemin de scrape
# n'écrivait `offres_texte`. 220 offres sur 294 affichaient un bouton
# « Générer CV » inutilisable, et seul un backfill ponctuel depuis
# `.rank_cache.json` en avait sauvé 74. Retirer l'écriture de
# `storage.enregistrer_run` fait tomber ces tests.
# ---------------------------------------------------------------------------
def _offre_avec_texte(title, texte):
    return Offre(title, "ACME", "Paris", texte, "http://a", "test", "", "")


def test_le_run_persiste_le_texte_d_une_offre_neuve():
    conn = storage.ouvrir(":memory:")
    offre = _offre_avec_texte("Stage IA", "Missions : entraîner des modèles.")
    storage.enregistrer_run(conn, [(offre, 0.5)])
    assert jobs.lire_texte(conn, storage.cle_identite(offre)) == (
        "Missions : entraîner des modèles."
    )


def test_le_run_persiste_le_texte_d_une_offre_deja_connue():
    """La branche UPDATE compte autant que la branche INSERT.

    Une offre vue au run précédent (donc déjà dans `offres`) mais dépourvue de
    texte est le cas MAJORITAIRE de la base réelle : 220 lignes sur 294. Si
    seule la branche « nouvelle » écrivait, elles resteraient bloquées à vie.
    """
    conn = storage.ouvrir(":memory:")
    sans_texte = Offre("Stage IA", "ACME", "Paris", "", "http://a", "test", "", "")
    storage.enregistrer_run(conn, [(sans_texte, 0.5)])
    cle = storage.cle_identite(sans_texte)
    assert jobs.lire_texte(conn, cle) is None

    storage.enregistrer_run(conn, [(_offre_avec_texte("Stage IA", "Le texte."), 0.5)])
    assert jobs.lire_texte(conn, cle) == "Le texte."
    # Toujours une seule ligne d'offre : le texte n'a pas créé de doublon.
    (nb,) = conn.execute("SELECT COUNT(*) FROM offres").fetchone()
    assert nb == 1


def test_un_texte_vide_n_ecrase_pas_le_texte_en_base():
    """Le cas qui ferait régresser des offres déjà générables.

    Une source qui cesse de livrer la description ne doit pas effacer ce
    qu'on avait : `INSERT OR REPLACE` avec une chaîne vide rendrait
    `TEXT_MISSING` une offre qui marchait la veille.
    """
    conn = storage.ouvrir(":memory:")
    avec = _offre_avec_texte("Stage IA", "Le texte complet de l'annonce.")
    storage.enregistrer_run(conn, [(avec, 0.5)])
    cle = storage.cle_identite(avec)

    for description in ("", "   ", "\n\t "):
        muette = Offre("Stage IA", "ACME", "Paris", description,
                       "http://a", "test", "", "")
        storage.enregistrer_run(conn, [(muette, 0.5)])
        assert jobs.lire_texte(conn, cle) == "Le texte complet de l'annonce."


def test_un_texte_modifie_remplace_l_ancien():
    """L'annonce réécrite doit gagner : `offer_hash` en dépend."""
    conn = storage.ouvrir(":memory:")
    storage.enregistrer_run(conn, [(_offre_avec_texte("Stage IA", "v1"), 0.5)])
    storage.enregistrer_run(conn, [(_offre_avec_texte("Stage IA", "v2"), 0.5)])
    cle = storage.cle_identite(_offre_avec_texte("Stage IA", "v2"))
    assert jobs.lire_texte(conn, cle) == "v2"
    (nb,) = conn.execute("SELECT COUNT(*) FROM offres_texte").fetchone()
    assert nb == 1


def test_le_run_cree_le_schema_du_texte_sans_qu_on_le_demande():
    """`main.executer` n'appelait pas `jobs.ensure_schema` : il ne doit pas avoir à.

    `storage.ouvrir` seul ne crée pas `offres_texte`. Un appelant qui oublie
    l'appel obtiendrait « no such table » — c'est-à-dire un run perdu.
    """
    conn = storage.ouvrir(":memory:")
    tables = {l[0] for l in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "offres_texte" not in tables      # état de départ, sans ensure_schema

    storage.enregistrer_run(conn, [(_offre_avec_texte("Stage IA", "texte"), 0.5)])
    tables = {l[0] for l in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "offres_texte" in tables


def test_aucune_offre_avec_description_ne_reste_text_missing():
    """Formulation « produit » de l'invariant, telle que l'UI la voit.

    `/api/cv/states` marque `text_missing` toute offre de `offres` absente de
    `offres_texte`. Après un run où chaque offre porte sa description, cet
    ensemble doit être VIDE.
    """
    conn = storage.ouvrir(":memory:")
    classees = [(_offre_avec_texte(f"Stage {i}", f"Description n°{i}."), 0.5)
                for i in range(12)]
    storage.enregistrer_run(conn, classees)

    manquantes = conn.execute(
        "SELECT o.cle FROM offres o LEFT JOIN offres_texte t ON t.cle = o.cle "
        "WHERE t.cle IS NULL"
    ).fetchall()
    assert manquantes == []
