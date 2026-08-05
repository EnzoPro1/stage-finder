"""jobs.py — File de génération de CV (SQLite, même base que les offres).

Une génération de CV coûte des minutes et monopolise Ollama. Elle ne peut
donc pas se faire dans le fil d'une requête HTTP : on enfile un JOB, le
client suit son avancement, un worker unique le consomme.

Ce module ne connaît QUE la file. Il n'importe ni ``cv_forge``, ni le
worker, ni Flask : il se teste sans rien lancer. Le worker (``worker.py``)
est le seul à faire le lien avec ``cv_forge.generate_cv``.

## Idempotence

``offer_hash = sha256(texte replié || empreinte du master || config_version)``

Les trois composantes viennent de l'API publique de cv_forge
(``normalize_for_hash``, ``master_fingerprint``, ``ForgeConfig``) : la
clé bouge exactement quand la sortie bougerait, et pas avant. Deux
demandes qui partagent un hash partagent un job — on ne relance pas huit
cents secondes d'extraction pour un PDF identique.

## Machine à états

    pending ──claim──> running ──┬──> done
       ▲                         └──> failed
       └──reprise (orphelin, si attempts < MAX_TENTATIVES)

Aucune autre transition n'est permise, et les refus sont explicites :
une transition invalide lève plutôt que de corrompre la file en silence.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATUTS = ("pending", "running", "done", "failed")
STATUTS_TERMINAUX = ("done", "failed")

# Au-delà, un job orphelin n'est plus repris mais marqué `failed`. Sans ce
# plafond, un job qui fait planter le worker le ferait planter à chaque
# redémarrage : il repasserait `pending`, serait réclamé, replanterait —
# une boucle qui ne s'arrête jamais et qui bloque toute la file derrière
# elle, puisque le claim est strictement FIFO.
MAX_TENTATIVES = 3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id    TEXT NOT NULL,          -- = offres.cle
    offer_hash  TEXT NOT NULL,          -- clé d'idempotence (cf. calculer_hash)
    status      TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    pdf_path    TEXT,
    error       TEXT,                   -- message lisible
    error_code  TEXT,                   -- ErrorCode de cv_forge
    traceback   TEXT,                   -- pile complète : le message seul ne suffit pas
                                        -- à diagnostiquer un plantage du worker
    attempts    INTEGER NOT NULL DEFAULT 0
);

-- Le claim FIFO lit `WHERE status='pending' ORDER BY created_at LIMIT 1`
-- à chaque tour de worker : sans cet index, un balayage complet de la
-- table à chaque itération.
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON generation_jobs(status, created_at);

-- Le cache d'idempotence cherche par hash à chaque clic.
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON generation_jobs(offer_hash);

-- Texte brut des offres. TABLE LATÉRALE et non colonne de `offres` :
-- `offres` est lue en entier à chaque run pour la dédup et le classement,
-- et y coller des descriptions de plusieurs kilo-octets alourdirait tous
-- les parcours pour une donnée que seule la génération de CV consomme.
CREATE TABLE IF NOT EXISTS offres_texte (
    cle         TEXT PRIMARY KEY,
    texte       TEXT NOT NULL,
    recupere_le TEXT NOT NULL
);
"""


class TransitionInvalide(RuntimeError):
    """Transition d'état refusée. Signale un bug d'appelant, pas un aléa."""


# =====================================================================
# Pourquoi `offer_id` n'est PAS une clé étrangère vers `offres.cle`
#
# La contrainte serait tentante : `offer_id` référence bien `offres.cle`,
# qui est une clé primaire. Elle est pourtant refusée, pour trois raisons
# qui vont toutes dans le même sens.
#
# 1. LES DEUX ACTIONS POSSIBLES SONT MAUVAISES. `migration_recalc_cles.py`
#    supprime et réinsère légitimement des lignes de `offres` pour fusionner
#    des doublons — c'est déjà arrivé. Avec ON DELETE CASCADE, les jobs et
#    la trace des PDF déjà produits disparaîtraient EN SILENCE pendant une
#    migration ; avec RESTRICT, la migration échouerait purement et
#    simplement. Aucune des deux n'est le comportement voulu, qui est de
#    RE-POINTER le job vers la ligne survivante.
#
# 2. UN JOB SURVIT À SON OFFRE, ET SON ARTEFACT AUSSI. Le PDF est sur le
#    disque. Effacer la ligne qui le référence en laissant le fichier ne
#    supprime pas le CV, ça le rend introuvable.
#
# 3. `cle` EST DÉRIVÉE DU CONTENU. Une offre disparue peut revenir à
#    l'identique au prochain scrape, sous la même clé : son historique de
#    génération se rattache alors tout seul. CASCADE l'aurait détruit
#    définitivement, pour une absence temporaire.
#
# S'y ajoute que les FK de SQLite exigent `PRAGMA foreign_keys=ON` sur
# CHAQUE connexion : une garantie qui dépend de tous les appelants ne
# l'oublient jamais n'est pas une garantie.
#
# En échange, les orphelins doivent être DÉTECTABLES et PURGEABLES — c'est
# l'objet de `orphelins()` et `purger_orphelins()` ci-dessous — et les
# lectures ne doivent jamais échouer sur une offre disparue.
# =====================================================================


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée tables et index si besoin. Idempotente, sûre à chaque démarrage."""
    conn.executescript(_SCHEMA)
    conn.commit()


def _maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


# =====================================================================
# Idempotence
# =====================================================================
def calculer_hash(raw_text: str, *, master_path: Path, config) -> str:
    """Empreinte de TOUT ce qui déterminerait le PDF.

    Les trois composantes sont séparées par un caractère qui ne peut pas
    apparaître dans les deux premières (``\\x00``) : sans séparateur sûr,
    deux découpages différents des mêmes octets donneraient le même hash.
    """
    from cv_forge import master_fingerprint, normalize_for_hash

    base = "\x00".join((
        normalize_for_hash(raw_text),
        master_fingerprint(master_path),
        config.config_version,
    ))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# =====================================================================
# Enfilement
# =====================================================================
def enfiler(conn: sqlite3.Connection, offer_id: str, offer_hash: str) -> tuple[dict, bool]:
    """Enfile un job, ou rend celui qui existe déjà. Rend ``(job, reutilise)``.

    Idempotent par ``offer_hash`` : un job ``pending``, ``running`` ou
    ``done`` portant le même hash est RENDU TEL QUEL. Deux clics sur le
    même bouton ne produisent pas deux CV identiques, et un rafraîchissement
    de page ne relance rien.

    ``failed`` fait exception et n'est pas réutilisé : réessayer est
    précisément ce qu'on veut permettre. C'est le bouton « Réessayer ».
    """
    existant = conn.execute(
        """SELECT * FROM generation_jobs
           WHERE offer_hash = ? AND status IN ('pending','running','done')
           ORDER BY created_at DESC LIMIT 1""",
        (offer_hash,),
    ).fetchone()
    if existant is not None:
        return dict(existant), True

    with conn:
        curseur = conn.execute(
            """INSERT INTO generation_jobs
               (offer_id, offer_hash, status, created_at, attempts)
               VALUES (?, ?, 'pending', ?, 0)""",
            (offer_id, offer_hash, _maintenant()),
        )
    return lire(conn, curseur.lastrowid), False


def lire(conn: sqlite3.Connection, job_id: int) -> dict | None:
    ligne = conn.execute(
        "SELECT * FROM generation_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(ligne) if ligne is not None else None


def dernier_pour_offre(conn: sqlite3.Connection, offer_id: str) -> dict | None:
    """Dernier job connu d'une offre, quel que soit son statut."""
    ligne = conn.execute(
        """SELECT * FROM generation_jobs WHERE offer_id = ?
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (offer_id,),
    ).fetchone()
    return dict(ligne) if ligne is not None else None


def position(conn: sqlite3.Connection, job_id: int) -> int | None:
    """Rang dans la file d'attente, 1 = prochain servi. ``None`` si non pending.

    Compte les `pending` créés AVANT lui, ce qui est exactement l'ordre du
    claim — la position affichée ne peut donc pas mentir sur le tour de
    passage.
    """
    ligne = conn.execute(
        "SELECT status, created_at FROM generation_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if ligne is None or ligne["status"] != "pending":
        return None
    devant, = conn.execute(
        """SELECT COUNT(*) FROM generation_jobs
           WHERE status = 'pending' AND (created_at, id) < (?, ?)""",
        (ligne["created_at"], job_id),
    ).fetchone()
    return devant + 1


def en_attente(conn: sqlite3.Connection) -> int:
    n, = conn.execute(
        "SELECT COUNT(*) FROM generation_jobs WHERE status IN ('pending','running')"
    ).fetchone()
    return n


# =====================================================================
# Consommation — transitions
# =====================================================================
def reclamer(conn: sqlite3.Connection) -> dict | None:
    """Réserve le plus ancien job `pending`. FIFO strict. ``None`` si file vide.

    Le claim est ATOMIQUE : le ``UPDATE`` porte sa propre condition
    ``status='pending'``, et on vérifie ``rowcount``. Deux workers qui
    liraient le même id n'en verraient qu'un réussir. C'est une ceinture
    en plus de la bretelle — l'invariant reste « un seul worker » — mais
    elle ne coûte rien et rend la file correcte même si l'invariant casse.
    """
    while True:
        ligne = conn.execute(
            """SELECT id, attempts FROM generation_jobs
               WHERE status = 'pending'
               ORDER BY created_at, id LIMIT 1"""
        ).fetchone()
        if ligne is None:
            return None

        with conn:
            curseur = conn.execute(
                """UPDATE generation_jobs
                   SET status = 'running', started_at = ?, attempts = attempts + 1
                   WHERE id = ? AND status = 'pending'""",
                (_maintenant(), ligne["id"]),
            )
        if curseur.rowcount == 1:
            return lire(conn, ligne["id"])
        # Quelqu'un d'autre l'a pris entre le SELECT et l'UPDATE : on
        # reprend au suivant plutôt que de rendre None à tort.


def _exiger_running(conn: sqlite3.Connection, job_id: int) -> dict:
    job = lire(conn, job_id)
    if job is None:
        raise TransitionInvalide(f"job {job_id} inexistant")
    if job["status"] != "running":
        raise TransitionInvalide(
            f"job {job_id} : transition depuis '{job['status']}' refusée — "
            f"seul un job 'running' peut être terminé. Un job déjà terminal "
            f"ne doit jamais être réécrit : son résultat est ce que "
            f"l'utilisateur a sous les yeux."
        )
    return job


def terminer(conn: sqlite3.Connection, job_id: int, pdf_path: str | None) -> dict:
    """`running` -> `done`."""
    _exiger_running(conn, job_id)
    with conn:
        conn.execute(
            """UPDATE generation_jobs
               SET status = 'done', finished_at = ?, pdf_path = ?,
                   error = NULL, error_code = NULL, traceback = NULL
               WHERE id = ?""",
            (_maintenant(), pdf_path, job_id),
        )
    return lire(conn, job_id)


def echouer(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    error_code: str | None = None,
    trace: str | None = None,
) -> dict:
    """`running` -> `failed`, avec la pile complète si on l'a.

    La pile est persistée AVEC le message : un « INTERNAL_ERROR:
    KeyError » sans pile ne se diagnostique pas, et le worker tourne dans
    un thread dont personne ne lit la sortie.
    """
    _exiger_running(conn, job_id)
    with conn:
        conn.execute(
            """UPDATE generation_jobs
               SET status = 'failed', finished_at = ?, error = ?,
                   error_code = ?, traceback = ?
               WHERE id = ?""",
            (_maintenant(), error, error_code, trace, job_id),
        )
    return lire(conn, job_id)


def reprendre_orphelins(conn: sqlite3.Connection) -> tuple[int, int]:
    """Au démarrage : rattrape les jobs laissés `running` par un arrêt brutal.

    Rend ``(repris, abandonnes)``.

    Un job `running` alors qu'aucun worker ne tourne est forcément un
    résidu : le processus est mort entre le claim et la fin. Il repasse
    `pending` — SAUF s'il a déjà épuisé ``MAX_TENTATIVES``, auquel cas il
    part en `failed`. C'est ce plafond qui empêche la boucle : sans lui,
    un job qui tue le worker le tuerait à chaque redémarrage, et la file
    étant FIFO, plus rien ne passerait derrière.

    À appeler AU DÉMARRAGE DU WORKER, jamais à l'import : un import ne
    doit pas écrire en base, et les tests importent le module.
    """
    orphelins = conn.execute(
        "SELECT id, attempts FROM generation_jobs WHERE status = 'running'"
    ).fetchall()
    repris = abandonnes = 0
    with conn:
        for job in orphelins:
            if job["attempts"] >= MAX_TENTATIVES:
                conn.execute(
                    """UPDATE generation_jobs
                       SET status = 'failed', finished_at = ?, error = ?,
                           error_code = 'INTERNAL_ERROR'
                       WHERE id = ?""",
                    (_maintenant(),
                     f"abandonné après {job['attempts']} tentative(s) : le worker "
                     f"s'est interrompu à chaque fois sur ce job.",
                     job["id"]),
                )
                abandonnes += 1
            else:
                conn.execute(
                    "UPDATE generation_jobs SET status = 'pending', started_at = NULL "
                    "WHERE id = ?",
                    (job["id"],),
                )
                repris += 1
    if repris or abandonnes:
        logger.info("Jobs orphelins : %d repris, %d abandonné(s).", repris, abandonnes)
    return repris, abandonnes


# =====================================================================
# Orphelins — jobs dont l'offre a disparu
# =====================================================================
def orphelins(conn: sqlite3.Connection) -> list[dict]:
    """Jobs dont ``offer_id`` ne désigne plus aucune ligne de ``offres``.

    Cas nominal : une fusion de doublons a supprimé la ligne. Le job reste
    valide en tant qu'ARCHIVE — son PDF existe toujours — mais il n'est
    plus atteignable depuis la liste des offres.
    """
    return [dict(l) for l in conn.execute(
        """SELECT j.* FROM generation_jobs j
           LEFT JOIN offres o ON o.cle = j.offer_id
           WHERE o.cle IS NULL
           ORDER BY j.id"""
    )]


def repointer(conn: sqlite3.Connection, ancienne: str, nouvelle: str) -> int:
    """Rattache les jobs d'une offre fusionnée à la ligne survivante.

    À appeler par toute migration qui déplace ou fusionne des ``cle``.
    C'est le geste que ferait ON UPDATE CASCADE, mais déclenché quand on
    le décide et pas comme effet de bord d'un DELETE.
    """
    with conn:
        curseur = conn.execute(
            "UPDATE generation_jobs SET offer_id = ? WHERE offer_id = ?",
            (nouvelle, ancienne),
        )
    return curseur.rowcount


def purger_orphelins(conn: sqlite3.Connection, *, seulement_echoues: bool = False) -> int:
    """Supprime les jobs orphelins. Rend le nombre de lignes supprimées.

    ``seulement_echoues`` limite la purge aux jobs qui n'ont rien produit :
    c'est le défaut prudent à privilégier quand on ne veut pas perdre la
    trace d'un PDF encore présent sur le disque. La purge ne supprime
    AUCUN fichier — elle ne fait qu'oublier des lignes.
    """
    condition = "AND j.status = 'failed'" if seulement_echoues else ""
    with conn:
        curseur = conn.execute(
            f"""DELETE FROM generation_jobs WHERE id IN (
                    SELECT j.id FROM generation_jobs j
                    LEFT JOIN offres o ON o.cle = j.offer_id
                    WHERE o.cle IS NULL {condition})"""
        )
    return curseur.rowcount


# =====================================================================
# Texte des offres
# =====================================================================
def enregistrer_texte(conn: sqlite3.Connection, cle: str, texte: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO offres_texte (cle, texte, recupere_le) "
            "VALUES (?, ?, ?)",
            (cle, texte, _maintenant()),
        )


def lire_texte(conn: sqlite3.Connection, cle: str) -> str | None:
    ligne = conn.execute(
        "SELECT texte FROM offres_texte WHERE cle = ?", (cle,)
    ).fetchone()
    return ligne["texte"] if ligne is not None else None


def cles_avec_texte(conn: sqlite3.Connection) -> set[str]:
    return {l["cle"] for l in conn.execute("SELECT cle FROM offres_texte")}
