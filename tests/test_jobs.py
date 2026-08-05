"""File de génération : hash, idempotence, transitions, reprise, worker.

Ollama, Typst et cv_forge sont MOCKÉS de bout en bout. Aucun test de ce
fichier ne charge un modèle, ne compile un PDF ni n'ouvre une socket.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import jobs
import storage
import worker as worker_module


@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    jobs.ensure_schema(c)
    yield c
    c.close()


class FauxConfig:
    """Double de ``ForgeConfig`` : seul ``config_version`` compte ici."""

    def __init__(self, version="v-test"):
        self.config_version = version

    def resolved(self):
        return {"model": "qwen3:8b"}


@pytest.fixture
def master(tmp_path):
    chemin = tmp_path / "master.yaml"
    chemin.write_text("nom: Test\n", encoding="utf-8")
    return chemin


def _offre(conn, cle="cle-a", titre="Stage IA", texte="Texte de l'offre."):
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, nb_vues) "
        "VALUES (?,?,?,?,?,1)",
        (cle, titre, "ACME", "Paris", "http://x"),
    )
    conn.commit()
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)
    return cle


# =====================================================================
# Schéma
# =====================================================================
def test_ensure_schema_est_idempotente(conn):
    jobs.ensure_schema(conn)
    jobs.ensure_schema(conn)          # ne doit pas lever
    tables = {l["name"] for l in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"generation_jobs", "offres_texte"} <= tables


def test_les_deux_index_existent(conn):
    """Le claim FIFO et le cache d'idempotence sont les deux seuls accès
    chauds : sans index, chaque tour de worker balaie la table."""
    index = {l["name"] for l in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_jobs_claim" in index
    assert "idx_jobs_hash" in index


def test_le_claim_fifo_utilise_bien_son_index(conn):
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM generation_jobs "
        "WHERE status='pending' ORDER BY created_at, id LIMIT 1"
    ).fetchall()
    assert any("idx_jobs_claim" in str(tuple(l)) for l in plan), plan


def test_un_statut_inconnu_est_refuse_par_la_base(conn):
    """La contrainte CHECK est la dernière ligne de défense : même un
    UPDATE écrit à la main ne peut pas inventer un état."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO generation_jobs (offer_id, offer_hash, status, created_at) "
            "VALUES ('x', 'h', 'en_cours', '2026-01-01')"
        )


def test_wal_et_busy_timeout_sont_poses(tmp_path):
    """Sur FICHIER, pas en mémoire : `:memory:` n'a pas de journal WAL."""
    c = storage.ouvrir(str(tmp_path / "t.db"))
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == storage.BUSY_TIMEOUT_MS
    c.close()


def test_ouvrir_en_memoire_ne_leve_pas(conn):
    """Le PRAGMA WAL rend « memory » sans lever : les tests tournent pareil."""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "memory"


# =====================================================================
# Hash d'idempotence
# =====================================================================
def test_le_hash_est_stable(master):
    a = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig())
    b = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig())
    assert a == b


def test_le_hash_ignore_la_mise_en_page(master):
    """Même annonce recopiée autrement = même CV : on ne relance rien."""
    a = jobs.calculer_hash("Stage IA chez ACME", master_path=master, config=FauxConfig())
    b = jobs.calculer_hash("Stage IA\r\n  chez   ACME  ",
                           master_path=master, config=FauxConfig())
    assert a == b


def test_le_hash_suit_le_texte(master):
    a = jobs.calculer_hash("Stage IA", master_path=master, config=FauxConfig())
    b = jobs.calculer_hash("Stage Cyber", master_path=master, config=FauxConfig())
    assert a != b


def test_le_hash_suit_le_master(master):
    a = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig())
    master.write_text("nom: Modifie\n", encoding="utf-8")
    b = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig())
    assert a != b, "un master modifié doit invalider les CV déjà produits"


def test_le_hash_suit_la_config(master):
    a = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig("v1"))
    b = jobs.calculer_hash("Texte", master_path=master, config=FauxConfig("v2"))
    assert a != b


def test_les_composantes_ne_peuvent_pas_se_chevaucher(master):
    """Sans séparateur sûr, deux découpages des mêmes octets collideraient."""
    a = jobs.calculer_hash("AB", master_path=master, config=FauxConfig("C"))
    b = jobs.calculer_hash("A", master_path=master, config=FauxConfig("BC"))
    assert a != b


# =====================================================================
# Enfilement et idempotence
# =====================================================================
def test_deux_demandes_identiques_partagent_un_job(conn):
    premier, reutilise1 = jobs.enfiler(conn, "cle-a", "hash-1")
    second, reutilise2 = jobs.enfiler(conn, "cle-a", "hash-1")
    assert reutilise1 is False and reutilise2 is True
    assert premier["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0] == 1


def test_un_hash_different_cree_un_job_different(conn):
    a, _ = jobs.enfiler(conn, "cle-a", "hash-1")
    b, reutilise = jobs.enfiler(conn, "cle-a", "hash-2")
    assert reutilise is False and a["id"] != b["id"]


def test_un_job_done_est_reutilise_sans_relancer(conn):
    """C'est le cache : le PDF existe, on le rend, on ne recalcule pas."""
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    jobs.terminer(conn, job["id"], "/out/cv.pdf")
    reprise, reutilise = jobs.enfiler(conn, "cle-a", "h")
    assert reutilise is True
    assert reprise["status"] == "done" and reprise["pdf_path"] == "/out/cv.pdf"


def test_un_job_failed_n_est_PAS_reutilise(conn):
    """« Réessayer » doit pouvoir réessayer."""
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    jobs.echouer(conn, job["id"], error="Ollama absent", error_code="OLLAMA_UNAVAILABLE")
    neuf, reutilise = jobs.enfiler(conn, "cle-a", "h")
    assert reutilise is False and neuf["id"] != job["id"]
    assert neuf["status"] == "pending"


# =====================================================================
# Claim FIFO
# =====================================================================
def test_le_claim_respecte_l_ordre_de_creation(conn):
    for i in range(3):
        jobs.enfiler(conn, f"cle-{i}", f"hash-{i}")
    assert [jobs.reclamer(conn)["offer_id"] for _ in range(3)] == \
        ["cle-0", "cle-1", "cle-2"]


def test_le_claim_rend_none_sur_file_vide(conn):
    assert jobs.reclamer(conn) is None


def test_le_claim_incremente_les_tentatives(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    assert jobs.reclamer(conn)["attempts"] == 1


def test_un_job_reclame_ne_l_est_pas_deux_fois(conn):
    jobs.enfiler(conn, "cle-a", "h")
    assert jobs.reclamer(conn) is not None
    assert jobs.reclamer(conn) is None


def test_le_claim_est_atomique_sous_concurrence(tmp_path):
    """Deux threads, dix jobs : chacun servi UNE fois, jamais deux.

    L'invariant reste « un seul worker », mais le claim doit rester
    correct même s'il casse — sans quoi un bug de démarrage produirait
    deux CV pour la même offre, en concurrence sur Ollama.
    """
    chemin = str(tmp_path / "concurrence.db")
    base = storage.ouvrir(chemin)
    jobs.ensure_schema(base)
    for i in range(10):
        jobs.enfiler(base, f"cle-{i}", f"hash-{i}")
    base.close()

    obtenus, verrou = [], threading.Lock()

    def consommer():
        c = storage.ouvrir(chemin)
        while True:
            job = jobs.reclamer(c)
            if job is None:
                break
            with verrou:
                obtenus.append(job["id"])
            time.sleep(0.001)
        c.close()

    fils = [threading.Thread(target=consommer) for _ in range(2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join(timeout=10)

    assert sorted(obtenus) == sorted(set(obtenus)), "un job a été réclamé deux fois"
    assert len(obtenus) == 10


# =====================================================================
# Transitions invalides
# =====================================================================
def test_terminer_un_job_pending_est_refuse(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    with pytest.raises(jobs.TransitionInvalide):
        jobs.terminer(conn, job["id"], "/out/cv.pdf")


def test_terminer_deux_fois_est_refuse(conn):
    """Le résultat d'un job terminal est ce que l'utilisateur a sous les
    yeux : le réécrire changerait un affichage déjà rendu."""
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    jobs.terminer(conn, job["id"], "/out/cv.pdf")
    with pytest.raises(jobs.TransitionInvalide):
        jobs.terminer(conn, job["id"], "/out/autre.pdf")


def test_echouer_un_job_done_est_refuse(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    jobs.terminer(conn, job["id"], "/out/cv.pdf")
    with pytest.raises(jobs.TransitionInvalide):
        jobs.echouer(conn, job["id"], error="trop tard")


def test_terminer_un_job_inexistant_est_refuse(conn):
    with pytest.raises(jobs.TransitionInvalide):
        jobs.terminer(conn, 4242, None)


def test_terminer_efface_l_erreur_d_une_tentative_precedente(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    jobs.echouer(conn, job["id"], error="raté", error_code="TYPST_FAILED",
                 trace="pile")
    conn.execute("UPDATE generation_jobs SET status='running' WHERE id=?", (job["id"],))
    conn.commit()
    fini = jobs.terminer(conn, job["id"], "/out/cv.pdf")
    assert fini["error"] is None and fini["error_code"] is None
    assert fini["traceback"] is None


# =====================================================================
# Traceback persistée
# =====================================================================
def test_la_pile_complete_est_persistee(conn):
    """Le worker tourne dans un thread dont personne ne lit la sortie :
    un « INTERNAL_ERROR: KeyError » sans pile ne se diagnostique pas."""
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    pile = "Traceback (most recent call last):\n  File ...\nKeyError: 'x'"
    fini = jobs.echouer(conn, job["id"], error="KeyError: 'x'",
                        error_code="INTERNAL_ERROR", trace=pile)
    assert fini["traceback"] == pile
    assert fini["error"] == "KeyError: 'x'"


# =====================================================================
# Reprise des orphelins
# =====================================================================
def test_un_running_orphelin_repasse_pending(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)                       # laissé running : worker tué
    repris, abandonnes = jobs.reprendre_orphelins(conn)
    assert (repris, abandonnes) == (1, 0)
    assert jobs.lire(conn, job["id"])["status"] == "pending"
    assert jobs.lire(conn, job["id"])["started_at"] is None


def test_la_reprise_ne_touche_pas_les_jobs_termines(conn):
    a, _ = jobs.enfiler(conn, "cle-a", "h1")
    jobs.reclamer(conn)
    jobs.terminer(conn, a["id"], "/out/a.pdf")
    b, _ = jobs.enfiler(conn, "cle-b", "h2")
    jobs.reprendre_orphelins(conn)
    assert jobs.lire(conn, a["id"])["status"] == "done"
    assert jobs.lire(conn, b["id"])["status"] == "pending"


def test_un_job_qui_tue_le_worker_est_abandonne_au_plafond(conn):
    """Sans plafond : il repasse pending, replante, à l'infini — et la
    file étant FIFO, plus rien ne passe derrière lui."""
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    for tour in range(jobs.MAX_TENTATIVES):
        assert jobs.reclamer(conn) is not None, f"tour {tour}"
        repris, abandonnes = jobs.reprendre_orphelins(conn)

    assert (repris, abandonnes) == (0, 1)
    final = jobs.lire(conn, job["id"])
    assert final["status"] == "failed"
    assert final["attempts"] == jobs.MAX_TENTATIVES
    assert "tentative" in final["error"]
    assert jobs.reclamer(conn) is None, "un job abandonné ne doit plus bloquer la file"


def test_un_job_abandonne_ne_bloque_pas_les_suivants(conn):
    bloquant, _ = jobs.enfiler(conn, "cle-a", "h1")
    suivant, _ = jobs.enfiler(conn, "cle-b", "h2")
    for _ in range(jobs.MAX_TENTATIVES):
        jobs.reclamer(conn)
        jobs.reprendre_orphelins(conn)
    assert jobs.lire(conn, bloquant["id"])["status"] == "failed"
    assert jobs.reclamer(conn)["offer_id"] == "cle-b"


# =====================================================================
# Position dans la file
# =====================================================================
def test_la_position_suit_l_ordre_du_claim(conn):
    ids = [jobs.enfiler(conn, f"cle-{i}", f"h{i}")[0]["id"] for i in range(3)]
    assert [jobs.position(conn, i) for i in ids] == [1, 2, 3]


def test_un_job_non_pending_n_a_pas_de_position(conn):
    job, _ = jobs.enfiler(conn, "cle-a", "h")
    jobs.reclamer(conn)
    assert jobs.position(conn, job["id"]) is None


def test_la_position_avance_quand_la_file_se_vide(conn):
    a, _ = jobs.enfiler(conn, "cle-a", "h1")
    b, _ = jobs.enfiler(conn, "cle-b", "h2")
    assert jobs.position(conn, b["id"]) == 2
    jobs.reclamer(conn)                       # a passe en running
    assert jobs.position(conn, b["id"]) == 1


# =====================================================================
# Texte des offres
# =====================================================================
def test_le_texte_se_relit(conn):
    jobs.enregistrer_texte(conn, "cle-a", "Le texte.")
    assert jobs.lire_texte(conn, "cle-a") == "Le texte."


def test_une_offre_sans_texte_rend_none(conn):
    assert jobs.lire_texte(conn, "inconnue") is None


def test_reenregistrer_remplace_sans_dupliquer(conn):
    jobs.enregistrer_texte(conn, "cle-a", "v1")
    jobs.enregistrer_texte(conn, "cle-a", "v2")
    assert jobs.lire_texte(conn, "cle-a") == "v2"
    assert conn.execute("SELECT COUNT(*) FROM offres_texte").fetchone()[0] == 1
