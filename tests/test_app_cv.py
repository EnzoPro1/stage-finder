"""Couche HTTP de la file de génération de CV (CP3).

Rien de coûteux n'est appelé : ces quatre routes ne touchent NI Ollama, NI
Typst, NI ``generate_cv``. Elles lisent et écrivent la file, et servent un
fichier. Le seul morceau de cv_forge réellement exercé est le calcul du
hash d'idempotence (``normalize_for_hash`` + ``master_fingerprint`` +
``ForgeConfig.config_version``), qui est de la pure fonction sur des octets
— le mocker ferait perdre la seule vérification d'intégration qui reste.

## Couverture du catalogue d'erreurs

Chaque code doit être atteignable depuis HTTP, sinon il ne vaut rien :

  OFFER_NOT_FOUND ... `test_offer_not_found_est_le_meme_code_aux_deux_points`
  TEXT_MISSING ...... `test_une_offre_sans_texte_est_refusee_sans_creer_de_job`
  MASTER_INVALID .... `test_un_master_introuvable_est_refuse_sans_creer_de_job`
  JOB_NOT_FOUND ..... `test_un_job_inconnu_se_solde_par_un_404_propre`
  PDF_UNAVAILABLE ... les quatre `test_le_telechargement_*`
  INTERNAL_ERROR .... `test_aucune_route_ne_rend_un_500_nu`
  les 7 de cv_forge . `test_tout_code_de_cv_forge_remonte_tel_quel`
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app as webapp
import config
import jobs
import storage
import worker as worker_module


# =====================================================================
# Décor
# =====================================================================
@pytest.fixture
def base(tmp_path, monkeypatch):
    """Base, master et racine de sortie temporaires.

    Les trois sont monkeypatchés sur ``config`` — et non injectés — parce
    que c'est exactement ainsi que les routes les lisent : à l'appel, pas à
    l'import. Un test qui injecterait ne prouverait rien sur le chemin réel.
    """
    chemin = str(tmp_path / "cv.db")
    master = tmp_path / "master.yaml"
    master.write_text("identite:\n  nom: Test\n", encoding="utf-8")
    sortie = tmp_path / "out"
    sortie.mkdir()

    monkeypatch.setattr(config, "CHEMIN_BASE", chemin)
    monkeypatch.setattr(config, "CV_MASTER_PATH", master)
    monkeypatch.setattr(config, "CV_OUT_ROOT", sortie)

    conn = storage.ouvrir(chemin)
    jobs.ensure_schema(conn)
    yield SimpleNamespace(conn=conn, chemin=chemin, master=master,
                          sortie=sortie, tmp=tmp_path)
    conn.close()


@pytest.fixture
def client(base):
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _offre(conn, cle="cle-a", titre="Stage IA", texte="Le texte de l'annonce."):
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, nb_vues) "
        "VALUES (?,?,?,?,?,1)", (cle, titre, "ACME", "Paris", "http://x"))
    conn.commit()
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)


def _job_termine(base, cle="cle-a", nom="cv.pdf", contenu=b"%PDF-1.7\nfaux\n%%EOF"):
    """Un job `done` dont le PDF existe pour de bon sous la racine de sortie."""
    _offre(base.conn, cle)
    job, _ = jobs.enfiler(base.conn, cle, "h-" + cle)
    jobs.reclamer(base.conn)
    dossier = base.sortie / cle[:16]
    dossier.mkdir(parents=True, exist_ok=True)
    pdf = dossier / nom
    pdf.write_bytes(contenu)
    jobs.terminer(base.conn, job["id"], str(pdf))
    return job["id"], pdf


def _job_echoue(base, code, message="raison de l'échec", cle="cle-a"):
    _offre(base.conn, cle)
    job, _ = jobs.enfiler(base.conn, cle, "h-" + cle)
    jobs.reclamer(base.conn)
    jobs.echouer(base.conn, job["id"], error=message, error_code=code)
    return job["id"]


def _nb_jobs(conn) -> int:
    n, = conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()
    return n


# =====================================================================
# POST /api/offers/<id>/cv — idempotence
# =====================================================================
def test_deux_demandes_identiques_partagent_le_meme_job(client, base):
    """Deux clics, ou un rafraîchissement de page, ne relancent pas huit
    cents secondes d'extraction pour un PDF identique."""
    _offre(base.conn)

    premiere = client.post("/api/offers/cle-a/cv")
    seconde = client.post("/api/offers/cle-a/cv")

    assert premiere.status_code == 202 and seconde.status_code == 202
    assert premiere.get_json()["job_id"] == seconde.get_json()["job_id"]
    assert premiere.get_json()["reutilise"] is False
    assert seconde.get_json()["reutilise"] is True
    assert _nb_jobs(base.conn) == 1


def test_un_texte_different_donne_un_job_different(client, base):
    """L'idempotence porte sur le CONTENU, pas sur l'identifiant d'offre :
    une annonce ré-éditée doit bien produire un nouveau CV."""
    _offre(base.conn)
    premier = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    jobs.enregistrer_texte(base.conn, "cle-a", "Un texte entièrement réécrit.")
    second = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    assert second != premier
    assert _nb_jobs(base.conn) == 2


def test_une_mise_en_page_differente_ne_cree_pas_de_second_job(client, base):
    """Même texte replié différemment (espaces, retours à la ligne) : c'est
    tout l'objet de `normalize_for_hash`, et on vérifie qu'on le traverse
    bien plutôt que de hacher le texte brut."""
    _offre(base.conn, texte="Stage IA\n\nParis   —  6 mois")
    premier = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    jobs.enregistrer_texte(base.conn, "cle-a", "Stage IA\nParis — 6 mois")
    second = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    assert second == premier
    assert _nb_jobs(base.conn) == 1


# =====================================================================
# POST — les refus, qui ne créent AUCUN job
# =====================================================================
def test_offer_not_found_est_le_meme_code_aux_deux_points_d_emission(client, base):
    """Une offre absente au POST et une offre qui DISPARAÎT entre
    l'enfilement et le traitement sont la même situation, vue de deux
    endroits. Un second code aurait obligé l'interface à en gérer deux."""
    au_post = client.post("/api/offers/fantome/cv")
    assert au_post.status_code == 404
    assert au_post.get_json()["error_code"] == "OFFER_NOT_FOUND"
    assert _nb_jobs(base.conn) == 0

    job, _ = jobs.enfiler(base.conn, "fantome", "h")
    worker_module.Worker(db_path=base.chemin)._traiter_un(base.conn)

    dans_le_worker = client.get(f"/api/jobs/{job['id']}").get_json()
    assert dans_le_worker["error_code"] == "OFFER_NOT_FOUND"


def test_une_offre_sans_texte_est_refusee_sans_creer_de_job(client, base):
    """`TEXT_MISSING` est un code propre à stage_finder : une `OfferInput`
    porte toujours son texte, donc cv_forge ne peut pas le produire.

    Le refus est ICI et pas dans le worker : un job qu'on sait d'avance
    voué à échouer n'occuperait la file que pour bruiter l'historique."""
    _offre(base.conn, texte=None)

    reponse = client.post("/api/offers/cle-a/cv")

    assert reponse.status_code == 422
    assert reponse.get_json()["error_code"] == "TEXT_MISSING"
    assert "backfill_textes" in reponse.get_json()["error_message"]
    assert _nb_jobs(base.conn) == 0


def test_un_master_introuvable_est_refuse_sans_creer_de_job(client, base, monkeypatch):
    """Un master absent ferait échouer CHAQUE offre de la même façon : le
    découvrir après coup, job par job, serait une perte sèche."""
    _offre(base.conn)
    monkeypatch.setattr(config, "CV_MASTER_PATH", base.tmp / "nulle-part.yaml")

    reponse = client.post("/api/offers/cle-a/cv")

    assert reponse.status_code == 503
    assert reponse.get_json()["error_code"] == "MASTER_INVALID"
    assert _nb_jobs(base.conn) == 0


# =====================================================================
# GET /api/jobs/<id>
# =====================================================================
def test_la_lecture_d_un_job_expose_exactement_le_contrat(client, base):
    _offre(base.conn)
    job_id = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    vue = client.get(f"/api/jobs/{job_id}").get_json()

    assert set(vue) == {"job_id", "offer_id", "status", "position", "pdf_url",
                        "error_code", "error_message", "attempts"}
    assert vue["job_id"] == job_id
    assert vue["offer_id"] == "cle-a"
    assert vue["status"] == "pending"
    assert vue["position"] == 1
    assert vue["pdf_url"] is None
    assert vue["error_code"] is None and vue["error_message"] is None
    assert vue["attempts"] == 0


def test_la_position_reflete_l_ordre_de_service(client, base):
    """FIFO strict : la position affichée ne peut pas mentir sur le tour
    de passage, puisqu'elle compte exactement ce que compte le claim."""
    ids = []
    for i in range(3):
        _offre(base.conn, cle=f"cle-{i}", texte=f"Texte numéro {i}.")
        ids.append(client.post(f"/api/offers/cle-{i}/cv").get_json()["job_id"])

    positions = [client.get(f"/api/jobs/{j}").get_json()["position"] for j in ids]
    assert positions == [1, 2, 3]


def test_un_job_termine_ne_porte_plus_de_position(client, base):
    job_id, _ = _job_termine(base)
    vue = client.get(f"/api/jobs/{job_id}").get_json()
    assert vue["status"] == "done"
    assert vue["position"] is None, "un job terminé n'attend plus"
    assert vue["pdf_url"] == f"/api/jobs/{job_id}/download"


def test_un_job_inconnu_se_solde_par_un_404_propre(client, base):
    reponse = client.get("/api/jobs/4242")
    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize("code", sorted(webapp.CODES_CV_FORGE))
def test_tout_code_de_cv_forge_remonte_tel_quel(client, base, code):
    """Les sept codes du catalogue de cv_forge traversent la file et la
    couche HTTP sans être repliés sur un voisin ni maquillés en 500."""
    job_id = _job_echoue(base, code, message=f"message de {code}")

    vue = client.get(f"/api/jobs/{job_id}").get_json()
    assert vue["error_code"] == code
    assert vue["error_message"] == f"message de {code}"
    assert vue["pdf_url"] is None

    # Le téléchargement RÉPERCUTE le code du job : qui demande le PDF d'une
    # génération ratée veut savoir pourquoi, pas s'entendre dire « pas de
    # fichier ».
    refus = client.get(f"/api/jobs/{job_id}/download")
    assert refus.status_code == 409
    assert refus.get_json()["error_code"] == code


# =====================================================================
# Lecture sur offre disparue — ne lève jamais
# =====================================================================
def test_un_job_orphelin_se_lit_sans_lever(client, base):
    """Il n'y a pas de clé étrangère sur `offer_id` (décision actée) : un
    job survit à son offre, et son PDF aussi. Les deux routes de lecture ne
    consultent donc PAS `offres` — c'est ce qui les rend insensibles."""
    job_id, _ = _job_termine(base)
    base.conn.execute("DELETE FROM offres WHERE cle = 'cle-a'")
    base.conn.commit()
    assert jobs.orphelins(base.conn), "le décor ne fabrique pas d'orphelin"

    par_job = client.get(f"/api/jobs/{job_id}")
    assert par_job.status_code == 200
    assert par_job.get_json()["offer_id"] == "cle-a"

    par_offre = client.get("/api/offers/cle-a/cv/latest")
    assert par_offre.status_code == 200
    assert par_offre.get_json()["job_id"] == job_id


def test_le_pdf_d_un_orphelin_reste_telechargeable(client, base):
    """« Effacer la ligne qui référence le PDF en laissant le fichier ne
    supprime pas le CV, ça le rend introuvable » — jobs.py. On vérifie que
    la couche HTTP tient cette promesse."""
    job_id, pdf = _job_termine(base)
    base.conn.execute("DELETE FROM offres WHERE cle = 'cle-a'")
    base.conn.commit()

    reponse = client.get(f"/api/jobs/{job_id}/download")
    assert reponse.status_code == 200
    assert reponse.data == pdf.read_bytes()


# =====================================================================
# GET /api/offers/<id>/cv/latest
# =====================================================================
def test_latest_rend_la_derniere_generation_connue(client, base):
    """Y compris `failed` : c'est ce que la liste d'offres du CP4 lira pour
    décider entre « Générer », « en cours » et « Réessayer »."""
    _offre(base.conn)
    ancien, _ = jobs.enfiler(base.conn, "cle-a", "h1")
    jobs.reclamer(base.conn)
    jobs.echouer(base.conn, ancien["id"], error="typst absent",
                 error_code="TYPST_FAILED")

    vue = client.get("/api/offers/cle-a/cv/latest").get_json()
    assert vue["job_id"] == ancien["id"]
    assert vue["status"] == "failed"
    assert vue["error_code"] == "TYPST_FAILED"


def test_latest_sans_aucune_generation_est_un_404_propre(client, base):
    _offre(base.conn)
    reponse = client.get("/api/offers/cle-a/cv/latest")
    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "JOB_NOT_FOUND"


def test_latest_sur_une_offre_inexistante_ne_leve_pas(client, base):
    """Même réponse que pour une offre sans génération : la route ne
    consulte pas `offres`, donc ne peut pas se casser dessus."""
    reponse = client.get("/api/offers/jamais-vue/cv/latest")
    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "JOB_NOT_FOUND"


# =====================================================================
# GET /api/jobs/<id>/download
# =====================================================================
def test_le_telechargement_sert_le_pdf_en_piece_jointe(client, base):
    job_id, pdf = _job_termine(base)

    reponse = client.get(f"/api/jobs/{job_id}/download")

    assert reponse.status_code == 200
    assert reponse.data == pdf.read_bytes()
    disposition = reponse.headers["Content-Disposition"]
    assert disposition.startswith("attachment")
    assert "cv.pdf" in disposition


def test_le_telechargement_d_un_job_inconnu_est_un_404_propre(client, base):
    reponse = client.get("/api/jobs/4242/download")
    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "JOB_NOT_FOUND"


def test_le_telechargement_avant_la_fin_annonce_l_attente(client, base):
    _offre(base.conn)
    job_id = client.post("/api/offers/cle-a/cv").get_json()["job_id"]

    reponse = client.get(f"/api/jobs/{job_id}/download")

    assert reponse.status_code == 409
    assert reponse.get_json()["error_code"] == "PDF_UNAVAILABLE"
    assert "pending" in reponse.get_json()["error_message"]


def test_le_telechargement_d_un_pdf_disparu_du_disque(client, base):
    """État RÉEL et pas une impossibilité : le dossier de sortie a pu être
    vidé, ou le PDF déplacé, longtemps après la génération."""
    job_id, pdf = _job_termine(base)
    pdf.unlink()

    reponse = client.get(f"/api/jobs/{job_id}/download")

    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "PDF_UNAVAILABLE"
    assert "disparu" in reponse.get_json()["error_message"]


def test_le_telechargement_refuse_un_chemin_absolu_hors_racine(client, base):
    """Le fichier EXISTE et est parfaitement lisible : c'est le cas
    dangereux, pas celui du fichier absent. Seule la racine décide."""
    secret = base.tmp / "secret.pdf"
    secret.write_bytes(b"%PDF-1.7 contenu qui ne doit pas sortir")
    job_id, _ = _job_termine(base)
    base.conn.execute("UPDATE generation_jobs SET pdf_path = ? WHERE id = ?",
                      (str(secret), job_id))
    base.conn.commit()

    reponse = client.get(f"/api/jobs/{job_id}/download")

    assert reponse.status_code == 403
    assert reponse.get_json()["error_code"] == "PDF_UNAVAILABLE"
    assert b"ne doit pas sortir" not in reponse.data


def test_le_telechargement_refuse_une_traversee_par_points(client, base):
    """`..` sous la racine : la comparaison doit se faire sur les chemins
    RÉSOLUS, sinon la préfixation textuelle laisse tout passer."""
    secret = base.tmp / "secret.pdf"
    secret.write_bytes(b"%PDF-1.7 contenu qui ne doit pas sortir")
    job_id, _ = _job_termine(base)
    traversee = base.sortie / "cle-a" / ".." / ".." / "secret.pdf"
    base.conn.execute("UPDATE generation_jobs SET pdf_path = ? WHERE id = ?",
                      (str(traversee), job_id))
    base.conn.commit()

    reponse = client.get(f"/api/jobs/{job_id}/download")

    assert reponse.status_code == 403
    assert reponse.get_json()["error_code"] == "PDF_UNAVAILABLE"
    assert b"ne doit pas sortir" not in reponse.data


def test_le_telechargement_d_un_job_done_sans_chemin(client, base):
    """`terminer(..., pdf_path=None)` est permis par la file : la couche
    HTTP doit le dire proprement plutôt que de tomber sur un `None`."""
    _offre(base.conn)
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    jobs.reclamer(base.conn)
    jobs.terminer(base.conn, job["id"], None)

    reponse = client.get(f"/api/jobs/{job['id']}/download")

    assert reponse.status_code == 404
    assert reponse.get_json()["error_code"] == "PDF_UNAVAILABLE"


# =====================================================================
# Jamais de 500 nu, et un catalogue qui suit cv_forge
# =====================================================================
def test_aucune_route_ne_rend_un_500_nu(client, base, monkeypatch):
    """`generate_cv` promet de ne pas lever ; SQLite n'a rien promis. Sans
    le filet, une base verrouillée sortirait en page HTML de Werkzeug, que
    le front ne sait pas lire."""
    def verrouillee(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webapp.jobs, "lire", verrouillee)

    reponse = client.get("/api/jobs/1")

    assert reponse.status_code == 500
    assert reponse.is_json, "500 nu : le front n'a ni code ni message"
    assert reponse.get_json()["error_code"] == "INTERNAL_ERROR"
    assert "OperationalError" in reponse.get_json()["error_message"]


@pytest.mark.parametrize("route", [
    "/api/offers/cle-a/cv",
    "/api/jobs/1",
    "/api/offers/cle-a/cv/latest",
    "/api/jobs/1/download",
])
def test_chaque_route_est_protegee_par_le_filet(client, base, monkeypatch, route):
    """Le filet est posé vue par vue : il faut donc vérifier qu'aucune n'a
    été oubliée. On casse la couche que les quatre traversent."""
    def cassee(*args, **kwargs):
        raise RuntimeError("panne de base")

    monkeypatch.setattr(webapp, "_conn", cassee)

    reponse = client.open(route, method="POST" if route.endswith("/cv") else "GET")

    assert reponse.is_json, f"{route} rend un 500 nu"
    assert reponse.get_json()["error_code"] == "INTERNAL_ERROR"


def test_le_catalogue_suit_cv_forge():
    """Verrou : si cv_forge ajoute un code, ce test tombe et oblige à
    décider de son sort côté HTTP, plutôt que de le voir se replier
    silencieusement sur INTERNAL_ERROR."""
    from cv_forge import ErrorCode

    assert {code.value for code in ErrorCode} == set(webapp.CODES_CV_FORGE)


def test_un_code_hors_catalogue_ne_fuit_jamais(base):
    """Défense en profondeur : un code inventé par erreur ne doit pas
    atteindre le client, qui ne saurait pas l'interpréter."""
    with webapp.app.test_request_context():
        reponse, statut = webapp._erreur("CODE_INVENTE", "peu importe", 400)

    assert reponse.get_json()["error_code"] == "INTERNAL_ERROR"
    assert statut == 400


# =====================================================================
# Worker unique au démarrage de Flask
# =====================================================================
class _FauxWorker:
    """Doublure : `demarrer()` ne lance aucun thread, mais compte."""

    demarrages: list = []

    def __init__(self, **_kwargs):
        self._vivant = False

    def demarrer(self) -> bool:
        _FauxWorker.demarrages.append(self)
        self._vivant = True
        return True

    @property
    def actif(self) -> bool:
        return self._vivant


@pytest.fixture
def faux_worker(monkeypatch):
    _FauxWorker.demarrages = []
    monkeypatch.setattr(webapp.worker_module, "Worker", _FauxWorker)
    monkeypatch.setattr(webapp, "WORKER", None)
    return _FauxWorker


def test_un_seul_worker_meme_si_on_demande_deux_fois(faux_worker):
    premier = webapp.demarrer_worker(reloader_actif=False)
    second = webapp.demarrer_worker(reloader_actif=False)

    assert premier is second
    assert len(faux_worker.demarrages) == 1, "deux workers dans le même processus"


def test_le_superviseur_du_reloader_ne_porte_pas_de_worker(faux_worker, monkeypatch):
    """Le reloader lance DEUX interpréteurs : ni le verrou de thread ni le
    drapeau de module ne les traversent. Seul l'enfant sert."""
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    assert webapp.demarrer_worker(reloader_actif=True) is None
    assert faux_worker.demarrages == []

    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    assert webapp.demarrer_worker(reloader_actif=True) is not None
    assert len(faux_worker.demarrages) == 1


def test_le_reloader_est_coupe_dans_l_app():
    """Si cette constante repassait à True, `demarrer_worker` resterait
    correct — mais autant constater qu'on ne l'a pas fait par mégarde."""
    assert webapp.UTILISER_RELOADER is False


def test_importer_l_app_ne_demarre_aucun_worker():
    """Le worker est lancé depuis `__main__`, jamais à l'import : sinon
    chaque test, et chaque `import app` du CLI, ouvrirait la vraie base et
    lancerait un thread de génération."""
    racine = Path(webapp.__file__).resolve().parent
    programme = (
        "import threading, app;"
        "print('|'.join(t.name for t in threading.enumerate()));"
        "print('WORKER=', app.WORKER)"
    )
    sortie = subprocess.run(
        [sys.executable, "-c", programme], cwd=racine,
        capture_output=True, text=True, timeout=120,
    )

    assert sortie.returncode == 0, sortie.stderr
    assert "cv-worker" not in sortie.stdout
    assert "WORKER= None" in sortie.stdout


# =====================================================================
# Les routes existantes sont intactes
# =====================================================================
def test_les_routes_existantes_repondent_toujours(client):
    """Le CP3 est ADDITIF : rien de ce qui existait ne change."""
    assert client.get("/").status_code == 200
    assert client.get("/api/etat").status_code == 200
    assert client.get("/api/marche").status_code == 200


def test_les_nouvelles_routes_n_ont_pas_recouvert_les_anciennes(client):
    regles = {str(r.rule) for r in webapp.app.url_map.iter_rules()}
    anciennes = {"/", "/api/etat", "/api/verifier", "/api/rafraichir",
                 "/api/marche", "/api/tout"}
    assert anciennes <= regles
    assert {"/api/offers/<offer_id>/cv", "/api/jobs/<int:job_id>",
            "/api/offers/<offer_id>/cv/latest",
            "/api/jobs/<int:job_id>/download"} <= regles
