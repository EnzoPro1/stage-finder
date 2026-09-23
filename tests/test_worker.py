"""Worker : unicité, jeton Ollama, déchargement à la vidange, robustesse.

Rien de réel n'est appelé : ``generer`` et ``decharger`` sont injectés.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import jobs
import ollama_pool
import storage
import worker as worker_module


class FauxResultat:
    def __init__(self, status="done", pdf_path="/out/cv.pdf", error=None,
                 error_code=None, traceback=None):
        self.status = status
        self.pdf_path = pdf_path
        self.error = error
        self.error_code = error_code
        self.traceback = traceback


class FauxConfig:
    config_version = "v-test"

    def resolved(self):
        return {"model": "qwen3:8b"}


@pytest.fixture
def base(tmp_path):
    chemin = str(tmp_path / "w.db")
    conn = storage.ouvrir(chemin)
    jobs.ensure_schema(conn)
    yield SimpleNamespace(chemin=chemin, conn=conn)
    conn.close()


def _offre(conn, cle, titre="Stage IA", texte="Texte de l'offre."):
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, nb_vues) "
        "VALUES (?,?,?,?,?,1)", (cle, titre, "ACME", "Paris", "http://x"))
    conn.commit()
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)


def _worker(base, **kw):
    kw.setdefault("generer", lambda offre, texte: FauxResultat())
    kw.setdefault("decharger", lambda modele: True)
    kw.setdefault("forge_config", FauxConfig())
    return worker_module.Worker(db_path=base.chemin, intervalle=0.01, **kw)


# =====================================================================
# Unicité — testée, pas supposée
# =====================================================================
def test_un_second_demarrage_est_refuse(base):
    w = _worker(base)
    assert w.demarrer() is True
    try:
        assert w.demarrer() is False, "deux threads de worker dans le même processus"
    finally:
        w.arreter()


def test_un_lecteur_de_passage_ne_fait_pas_echouer_le_verrou(tmp_path):
    """Deux démarrages simultanés : le perdant tient un instant un verrou
    SHARED sur le fichier avant d'abandonner. Sans attente, le gagnant ne
    pouvait pas passer EXCLUSIVE et échouait AUSSI — personne ne consommait
    la file. Ce lecteur de passage joue le perdant, de façon déterministe."""
    import sqlite3

    chemin = str(tmp_path / "v.lock")
    sqlite3.connect(chemin).execute("CREATE TABLE t (x)").connection.close()
    lecteur = sqlite3.connect(chemin, isolation_level=None, check_same_thread=False)
    lecteur.execute("BEGIN")
    lecteur.execute("SELECT * FROM t").fetchall()            # SHARED tenu
    threading.Timer(0.05, lambda: lecteur.execute("ROLLBACK")).start()

    verrou = worker_module.VerrouWorker(chemin)
    try:
        assert verrou.acquerir() is True
    finally:
        verrou.relacher()
        lecteur.close()


def test_le_worker_est_autorise_hors_reloader():
    assert worker_module.worker_autorise(reloader_actif=False, env={}) is True


def test_sous_reloader_seul_l_enfant_porte_le_worker():
    """Le reloader lance DEUX interpréteurs. Ni le verrou de thread ni le
    drapeau d'instance ne les traversent : sans ce filtre, deux workers
    généreraient en même temps sur le même Ollama."""
    parent = {}                                    # variable absente
    enfant = {"WERKZEUG_RUN_MAIN": "true"}
    assert worker_module.worker_autorise(reloader_actif=True, env=parent) is False
    assert worker_module.worker_autorise(reloader_actif=True, env=enfant) is True


def test_deux_workers_ne_traitent_jamais_le_meme_job(base):
    """Ceinture ET bretelle : même si l'unicité cassait, le claim atomique
    empêche deux CV pour la même offre."""
    for i in range(6):
        _offre(base.conn, f"cle-{i}")
        jobs.enfiler(base.conn, f"cle-{i}", f"h{i}")

    vus, verrou = [], threading.Lock()

    def generer(offre, texte):
        with verrou:
            vus.append(offre["cle"])
        time.sleep(0.005)
        return FauxResultat()

    a, b = _worker(base, generer=generer), _worker(base, generer=generer)
    a.demarrer()
    b.demarrer()
    deadline = time.time() + 10
    while time.time() < deadline and jobs.en_attente(base.conn) > 0:
        time.sleep(0.02)
    a.arreter()
    b.arreter()

    assert sorted(vus) == sorted(set(vus)), "une offre a été générée deux fois"
    assert len(vus) == 6


# =====================================================================
# Jeton Ollama
# =====================================================================
def test_le_jeton_serialise_les_appels():
    """Deux appels concurrents ne se recouvrent jamais."""
    recouvrement = []
    dedans = []

    def appel():
        with ollama_pool.JETON:
            dedans.append(1)
            recouvrement.append(len(dedans))
            time.sleep(0.01)
            dedans.pop()

    fils = [threading.Thread(target=appel) for _ in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert max(recouvrement) == 1, "deux inférences en vol simultanément"


def test_le_jeton_est_relache_meme_sur_exception():
    with pytest.raises(ValueError):
        with ollama_pool.JETON:
            raise ValueError("boum")
    assert ollama_pool.JETON.acquire(blocking=False), "jeton resté pris"
    ollama_pool.JETON.release()


# =====================================================================
# Déchargement — à la TRANSITION, et une seule fois
# =====================================================================
def test_le_dechargement_a_lieu_quand_la_file_se_vide(base):
    _offre(base.conn, "cle-a")
    jobs.enfiler(base.conn, "cle-a", "h")
    decharges = []
    w = _worker(base, decharger=lambda m: decharges.append(m) or True)

    assert w._traiter_un(base.conn) is True        # un job traité
    assert decharges == [], "déchargé alors que la file tournait encore"
    assert w._traiter_un(base.conn) is False       # file vide
    w._au_repos()
    assert decharges == ["qwen3:8b"]


def test_le_dechargement_n_a_lieu_qu_UNE_fois(base):
    """La condition est une TRANSITION, pas un état : un worker au repos
    ne doit pas redemander le déchargement à chaque tour de sondage."""
    _offre(base.conn, "cle-a")
    jobs.enfiler(base.conn, "cle-a", "h")
    decharges = []
    w = _worker(base, decharger=lambda m: decharges.append(m) or True)

    w._traiter_un(base.conn)
    for _ in range(5):                             # cinq tours à vide
        w._traiter_un(base.conn)
        w._au_repos()
    assert decharges == ["qwen3:8b"], f"déchargé {len(decharges)} fois"


def test_pas_de_dechargement_si_rien_n_a_ete_traite(base):
    """Un worker qui démarre sur une file déjà vide n'a rien à décharger."""
    decharges = []
    w = _worker(base, decharger=lambda m: decharges.append(m) or True)
    for _ in range(3):
        w._traiter_un(base.conn)
        w._au_repos()
    assert decharges == []


def test_le_modele_reste_chaud_entre_jobs_consecutifs(base):
    """Tout l'intérêt d'une file : ne pas payer 18,7 s de rechargement
    entre deux CV qui s'enchaînent."""
    for i in range(3):
        _offre(base.conn, f"cle-{i}")
        jobs.enfiler(base.conn, f"cle-{i}", f"h{i}")
    decharges = []
    w = _worker(base, decharger=lambda m: decharges.append(m) or True)

    while w._traiter_un(base.conn):
        assert decharges == [], "déchargé entre deux jobs consécutifs"
    w._au_repos()
    assert decharges == ["qwen3:8b"]


def test_un_dechargement_en_echec_ne_casse_rien(base):
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")

    def decharger_casse(modele):
        raise OSError("Ollama injoignable")

    w = _worker(base, decharger=decharger_casse)
    w._traiter_un(base.conn)
    w._au_repos()                                   # ne doit pas lever
    assert jobs.lire(base.conn, job["id"])["status"] == "done"


# =====================================================================
# Traitement d'un job
# =====================================================================
def test_un_job_reussi_passe_done_avec_son_pdf(base):
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    _worker(base)._traiter_un(base.conn)
    fini = jobs.lire(base.conn, job["id"])
    assert fini["status"] == "done" and fini["pdf_path"] == "/out/cv.pdf"
    assert fini["finished_at"] is not None


def test_un_echec_typé_conserve_son_code(base):
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    w = _worker(base, generer=lambda o, t: FauxResultat(
        status="failed", pdf_path=None, error="typst absent",
        error_code="TYPST_FAILED"))
    w._traiter_un(base.conn)
    fini = jobs.lire(base.conn, job["id"])
    assert fini["status"] == "failed" and fini["error_code"] == "TYPST_FAILED"


def test_un_error_code_enum_est_serialise_en_chaine(base):
    """`GenerationResult.error_code` est un ErrorCode, la colonne est TEXT."""
    from enum import Enum

    class Code(str, Enum):
        OLLAMA_UNAVAILABLE = "OLLAMA_UNAVAILABLE"

    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    w = _worker(base, generer=lambda o, t: FauxResultat(
        status="failed", pdf_path=None, error="absent",
        error_code=Code.OLLAMA_UNAVAILABLE))
    w._traiter_un(base.conn)
    assert jobs.lire(base.conn, job["id"])["error_code"] == "OLLAMA_UNAVAILABLE"


def test_la_pile_rendue_par_generate_cv_est_persistee(base):
    """Régression du premier run réel. `generate_cv` attrape les exceptions
    imprévues et ne les relève pas : sa pile partait dans SON logger, et la
    ligne de job n'avait que « RuntimeError: ... » — de quoi constater la
    panne, pas de quoi la situer."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    pile = "Traceback (most recent call last):\n  File ...\nRuntimeError: boum"
    w = _worker(base, generer=lambda o, t: FauxResultat(
        status="failed", pdf_path=None, error="RuntimeError: boum",
        error_code="INTERNAL_ERROR", traceback=pile))

    w._traiter_un(base.conn)

    assert jobs.lire(base.conn, job["id"])["traceback"] == pile


def test_un_echec_sans_pile_ne_casse_pas_l_enregistrement(base):
    """Les échecs catalogués n'en portent pas : le champ reste nul."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    w = _worker(base, generer=lambda o, t: FauxResultat(
        status="failed", pdf_path=None, error="typst absent",
        error_code="TYPST_FAILED"))
    w._traiter_un(base.conn)
    assert jobs.lire(base.conn, job["id"])["traceback"] is None


def test_le_pont_prepare_l_environnement_avant_de_charger_l_encodeur(
        base, tmp_path, monkeypatch):
    """L'ordre EST le propos : `truststore` doit être injecté AVANT que
    sentence_transformers ne touche au réseau, pas après."""
    import cv_forge

    import embeddings_env

    _offre(base.conn, "cle-a")
    ordre = []
    monkeypatch.setattr(embeddings_env, "preparer",
                        lambda *a, **k: ordre.append("preparer") or {})
    monkeypatch.setattr(cv_forge, "generate_cv",
                        lambda e, **kw: ordre.append("generate") or FauxResultat())

    w = worker_module.Worker(db_path=base.chemin, master_path=tmp_path / "m.yaml",
                             out_root=tmp_path / "out", forge_config=FauxConfig())
    offre = base.conn.execute(
        "SELECT cle, title, company, url FROM offres WHERE cle='cle-a'").fetchone()
    w._appel_generate_cv(offre, "texte")

    assert ordre == ["preparer", "generate"]


def test_une_offre_sans_texte_echoue_en_TEXT_MISSING(base):
    """L'absence de texte est un ÉTAT, pas un plantage : l'interface
    proposera « Récupérer » au lieu d'afficher une erreur."""
    _offre(base.conn, "cle-a", texte=None)
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    _worker(base)._traiter_un(base.conn)
    assert jobs.lire(base.conn, job["id"])["error_code"] == "TEXT_MISSING"


def test_une_offre_disparue_echoue_en_OFFER_NOT_FOUND(base):
    job, _ = jobs.enfiler(base.conn, "cle-fantome", "h")
    _worker(base)._traiter_un(base.conn)
    assert jobs.lire(base.conn, job["id"])["error_code"] == "OFFER_NOT_FOUND"


def test_une_exception_de_generate_cv_ne_tue_pas_le_worker(base):
    """`generate_cv` promet de ne pas lever. Si la promesse casse, c'est
    CE thread qui tomberait, et la file entière avec lui."""
    _offre(base.conn, "cle-a")
    _offre(base.conn, "cle-b")
    a, _ = jobs.enfiler(base.conn, "cle-a", "h1")
    b, _ = jobs.enfiler(base.conn, "cle-b", "h2")

    def generer(offre, texte):
        if offre["cle"] == "cle-a":
            raise RuntimeError("boum imprévu")
        return FauxResultat()

    w = _worker(base, generer=generer)
    w._traiter_un(base.conn)
    w._traiter_un(base.conn)

    casse = jobs.lire(base.conn, a["id"])
    assert casse["status"] == "failed"
    assert casse["error_code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in casse["traceback"], "pile non persistée"
    assert jobs.lire(base.conn, b["id"])["status"] == "done", "la file s'est arrêtée"


# =====================================================================
# Le pont réel vers cv_forge
#
# Les tests ci-dessus injectent `generer` et court-circuitent donc le
# seul endroit où stage_finder touche cv_forge. Ceux-ci l'exercent, en
# ne remplaçant que `generate_cv` elle-même.
# =====================================================================
def test_le_pont_construit_un_OfferInput_conforme(base, tmp_path, monkeypatch):
    import cv_forge

    _offre(base.conn, "cle-a", titre="Stage Vision")
    vus = {}

    def faux_generate_cv(entree, *, master_path, out_dir, config):
        vus.update(entree=entree, master_path=master_path,
                   out_dir=out_dir, config=config)
        return FauxResultat()

    monkeypatch.setattr(cv_forge, "generate_cv", faux_generate_cv)

    w = worker_module.Worker(
        db_path=base.chemin, master_path=tmp_path / "master.yaml",
        out_root=tmp_path / "out", forge_config=FauxConfig(),
    )
    offre = base.conn.execute(
        "SELECT cle, title, company, url FROM offres WHERE cle='cle-a'").fetchone()
    resultat = w._appel_generate_cv(offre, "Le texte de l'annonce.")

    assert resultat.status == "done"
    entree = vus["entree"]
    assert isinstance(entree, cv_forge.OfferInput)
    assert entree.offer_id == "cle-a"          # = offres.cle, décision D3
    assert entree.title == "Stage Vision"
    assert entree.company == "ACME"
    assert entree.raw_text == "Le texte de l'annonce."
    assert entree.url == "http://x"
    assert vus["master_path"] == tmp_path / "master.yaml"
    assert vus["out_dir"].parent == tmp_path / "out"


def test_le_pont_prend_le_jeton_pendant_l_appel(base, tmp_path, monkeypatch):
    """Le jeton entoure l'APPEL, pas le job : c'est le seul moment où une
    inférence est réellement en vol."""
    import cv_forge

    _offre(base.conn, "cle-a")
    pris = {}

    def faux_generate_cv(entree, **kw):
        pris["pendant"] = not ollama_pool.JETON.acquire(blocking=False)
        return FauxResultat()

    monkeypatch.setattr(cv_forge, "generate_cv", faux_generate_cv)
    w = worker_module.Worker(db_path=base.chemin, master_path=tmp_path / "m.yaml",
                             out_root=tmp_path / "out", forge_config=FauxConfig())
    offre = base.conn.execute(
        "SELECT cle, title, company, url FROM offres WHERE cle='cle-a'").fetchone()
    w._appel_generate_cv(offre, "texte")

    assert pris["pendant"] is True, "le jeton n'était pas tenu pendant l'appel"
    assert ollama_pool.JETON.acquire(blocking=False), "jeton non relâché après"
    ollama_pool.JETON.release()


def test_la_config_forge_par_defaut_est_celle_de_cv_forge(base):
    """Sans injection, le worker doit construire une vraie `ForgeConfig` —
    c'est elle qui porte le `config_version` de la clé d'idempotence."""
    from cv_forge import ForgeConfig

    w = worker_module.Worker(db_path=base.chemin)
    assert isinstance(w.config_forge(), ForgeConfig)


def test_le_worker_reprend_les_orphelins_au_demarrage(base):
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    jobs.reclamer(base.conn)                        # laissé running

    w = _worker(base)
    w.demarrer()
    deadline = time.time() + 5
    while time.time() < deadline:
        if jobs.lire(base.conn, job["id"])["status"] == "done":
            break
        time.sleep(0.02)
    w.arreter()
    assert jobs.lire(base.conn, job["id"])["status"] == "done"


# =====================================================================
# Verrou inter-processus : un seul worker par base
#
# Le troisième niveau d'unicité, et le seul qui tienne face à deux
# INTERPRÉTEURS — `python app.py` d'un côté, `python cv_cli.py batch` de
# l'autre. Ni le verrou de thread ni le drapeau d'instance ne traversent
# un processus, et le claim atomique de `jobs.reclamer` empêche bien deux
# workers de prendre le même job, mais pas de se disputer Ollama.
# =====================================================================
def test_le_verrou_est_exclusif(tmp_path):
    chemin = str(tmp_path / "v.lock")
    premier = worker_module.VerrouWorker(chemin)
    second = worker_module.VerrouWorker(chemin)
    assert premier.acquerir() is True
    assert second.acquerir() is False
    assert second.detenu is False


def test_le_verrou_relache_se_reprend(tmp_path):
    chemin = str(tmp_path / "v.lock")
    premier = worker_module.VerrouWorker(chemin)
    premier.acquerir()
    premier.relacher()
    assert worker_module.VerrouWorker(chemin).acquerir() is True


def test_relacher_est_idempotente_et_ne_leve_pas(tmp_path):
    verrou = worker_module.VerrouWorker(str(tmp_path / "v.lock"))
    verrou.acquerir()
    verrou.relacher()
    verrou.relacher()                               # ne doit pas lever
    assert verrou.detenu is False


def test_une_base_en_memoire_n_a_pas_de_verrou():
    """Rien à partager entre processus : inventer un fichier commun ferait
    s'exclure mutuellement des tests sans rapport."""
    assert worker_module.chemin_verrou(":memory:") is None
    assert worker_module.VerrouWorker(None).acquerir() is True


def test_le_verrou_vit_a_cote_de_la_base(tmp_path):
    chemin = str(tmp_path / "stages.db")
    assert worker_module.chemin_verrou(chemin) == chemin + ".worker-lock"


def test_un_second_worker_sur_la_meme_base_ne_traite_rien(base):
    """La panne qu'on empêche : deux générations Ollama en parallèle."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")

    tenu = worker_module.VerrouWorker(worker_module.chemin_verrou(base.chemin))
    assert tenu.acquerir() is True
    try:
        second = _worker(base)
        assert second.vider() is None
        assert second.refuse_faute_de_verrou is True
        # Le job n'a pas bougé : ni traité, ni réclamé, ni échoué.
        assert jobs.lire(base.conn, job["id"])["status"] == "pending"
        assert jobs.lire(base.conn, job["id"])["attempts"] == 0
    finally:
        tenu.relacher()


def test_un_second_worker_ne_reprend_pas_les_orphelins_de_l_autre(base):
    """Le pire cas : `reprendre_orphelins` repasserait `pending` le job que
    l'autre processus est EN TRAIN de traiter, et le ferait produire deux
    fois. C'est pour ça que le verrou est pris AVANT la reprise."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    jobs.reclamer(base.conn)                        # l'autre worker le traite

    tenu = worker_module.VerrouWorker(worker_module.chemin_verrou(base.chemin))
    tenu.acquerir()
    try:
        intrus = _worker(base)
        intrus.demarrer()
        time.sleep(0.15)
        intrus.arreter()
        assert intrus.refuse_faute_de_verrou is True
        assert jobs.lire(base.conn, job["id"])["status"] == "running"
    finally:
        tenu.relacher()


def test_le_verrou_est_rendu_a_la_fin_de_la_boucle(base):
    w = _worker(base)
    w.demarrer()
    time.sleep(0.05)
    w.arreter()
    # Un autre worker doit pouvoir prendre la relève immédiatement.
    suivant = worker_module.VerrouWorker(worker_module.chemin_verrou(base.chemin))
    assert suivant.acquerir() is True
    suivant.relacher()


def test_vider_consomme_toute_la_file_puis_rend_la_main(base):
    for i in range(3):
        _offre(base.conn, f"cle-{i}")
        jobs.enfiler(base.conn, f"cle-{i}", f"h{i}")

    w = _worker(base)
    assert w.vider() == 3
    restants, = base.conn.execute(
        "SELECT COUNT(*) FROM generation_jobs WHERE status != 'done'").fetchone()
    assert restants == 0


def test_vider_decharge_le_modele_une_seule_fois(base):
    """Même politique que la boucle continue : le modèle reste chaud entre
    deux jobs, et n'est déchargé qu'à la vidange."""
    for i in range(3):
        _offre(base.conn, f"cle-{i}")
        jobs.enfiler(base.conn, f"cle-{i}", f"h{i}")

    w = _worker(base)
    w.vider()
    assert w.decharges == 1


def test_vider_sur_une_file_vide_ne_decharge_rien(base):
    w = _worker(base)
    assert w.vider() == 0
    assert w.decharges == 0
