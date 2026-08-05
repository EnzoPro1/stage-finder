"""
storage.py — Persistance SQLite (cache des offres vues, nouveautés, historique).

CSV/HTML sont éphémères : chaque run écrase le précédent. Une petite base locale
débloque plusieurs pistes d'un coup, sans dépendance externe (sqlite est dans la
stdlib) :

  - **Cache des offres vues** → on peut n'afficher que les NOUVEAUTÉS.
  - **Dédup dans le temps** → une offre republiée chaque lundi n'est « nouvelle »
    qu'une fois (identité = même clé que la dédup exacte).
  - **Historique / tendances** → une table `runs` garde une trace de chaque
    exécution (combien d'offres, combien de nouveautés).

L'identité d'une offre à travers le temps réutilise la clé de déduplication
(`dedup._cle`) : titre normalisé + entreprise + ville. Deux relances de la même
annonce partagent donc la même ligne.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime

import config
from dedup import _cle as cle_identite
from normalize import Offre

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS offres (
    cle          TEXT PRIMARY KEY,
    title        TEXT,
    company      TEXT,
    location     TEXT,
    url          TEXT,
    source       TEXT,
    posted_at    TEXT,
    salary       TEXT,
    duree_mois   INTEGER,
    date_debut   TEXT,
    tags         TEXT,
    dernier_score REAL,
    premiere_vue TEXT,   -- date ISO du premier run où l'offre est apparue
    derniere_vue TEXT,   -- date ISO du dernier run
    nb_vues      INTEGER -- nombre de runs distincts ayant vu l'offre
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage  TEXT,
    nb_offres   INTEGER,
    nb_nouvelles INTEGER
);

-- Cache des verdicts de vérification LLM. Un verdict est calculé UNE seule fois
-- par offre (hash_offre) et par modèle : changer VERIFY_MODEL invalide
-- naturellement le cache (clé différente).
CREATE TABLE IF NOT EXISTS verdicts (
    hash_offre  TEXT NOT NULL,   -- sha256 de title|company|location|description
    model       TEXT NOT NULL,   -- tag Ollama du modèle
    verdict     TEXT NOT NULL,   -- verdict sérialisé en JSON
    created_at  TEXT,
    PRIMARY KEY (hash_offre, model)
);
"""


# Délai d'attente d'un verrou avant `database is locked`, en millisecondes.
# Le worker de génération écrit pendant que Flask lit à chaque tour de
# polling : sans ce délai, SQLite renvoie l'erreur IMMÉDIATEMENT au lieu
# d'attendre la fin d'une transaction qui dure quelques millisecondes.
BUSY_TIMEOUT_MS = 5000


def ouvrir(chemin: str | None = None) -> sqlite3.Connection:
    """Ouvre (et initialise si besoin) la base SQLite.

    Deux réglages posés à CHAQUE ouverture, et pas une seule fois à la
    création : ils sont attachés à la CONNEXION, pas au fichier. Flask et
    le worker en ouvrent chacun la leur.

    - **WAL** : lecteurs et écrivain cessent de se bloquer mutuellement.
      Sans lui, le worker qui écrit l'avancement d'un job fait échouer le
      polling de la page, et inversement. C'est une propriété du FICHIER,
      persistante une fois posée, mais la re-poser est sans coût.
    - **busy_timeout** : sur une base en WAL il reste UN écrivain à la
      fois. Deux écritures qui se croisent doivent attendre, pas échouer.

    ``:memory:`` n'a pas de journal WAL : le PRAGMA y rend « memory » sans
    lever. Les tests s'exécutent donc à l'identique.
    """
    conn = sqlite3.connect(chemin or config.CHEMIN_BASE)
    # Accès aux colonnes par NOM pour le code récent (jobs.py), sans rien
    # casser de l'ancien : `sqlite3.Row` accepte aussi l'index entier et le
    # dépaquetage, donc les `ligne[0]` existants continuent de fonctionner.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def cles_connues(conn: sqlite3.Connection) -> set[str]:
    """Ensemble des clés d'offres déjà enregistrées (runs précédents)."""
    return {ligne[0] for ligne in conn.execute("SELECT cle FROM offres")}


def enregistrer_run(
    conn: sqlite3.Connection, classees: list[tuple[Offre, float]]
) -> set[str]:
    """Enregistre un run : upsert des offres, trace dans `runs`.

    Retourne l'ensemble des clés NOUVELLES (jamais vues avant ce run), pour
    permettre à l'appelant de mettre en avant / filtrer les nouveautés.
    """
    connues_avant = cles_connues(conn)
    aujourd_hui = date.today().isoformat()
    nouvelles: set[str] = set()

    for offre, score in classees:
        cle = cle_identite(offre)
        est_nouvelle = cle not in connues_avant
        if est_nouvelle:
            nouvelles.add(cle)

        if est_nouvelle:
            conn.execute(
                """INSERT INTO offres
                   (cle, title, company, location, url, source, posted_at, salary,
                    duree_mois, date_debut, tags, dernier_score,
                    premiere_vue, derniere_vue, nb_vues)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    cle, offre.title, offre.company, offre.location, offre.url,
                    offre.source, offre.posted_at, offre.salary, offre.duree_mois,
                    offre.date_debut, " ".join(offre.tags), round(float(score), 4),
                    aujourd_hui, aujourd_hui,
                ),
            )
        else:
            # Offre déjà connue : on rafraîchit et on incrémente le compteur de vues
            # (une seule incrémentation par jour de run, pour ne pas gonfler).
            conn.execute(
                """UPDATE offres
                   SET title=?, company=?, location=?, url=?, source=?, posted_at=?,
                       salary=?, duree_mois=?, date_debut=?, tags=?, dernier_score=?,
                       derniere_vue=?,
                       nb_vues = nb_vues + (CASE WHEN derniere_vue <> ? THEN 1 ELSE 0 END)
                   WHERE cle=?""",
                (
                    offre.title, offre.company, offre.location, offre.url,
                    offre.source, offre.posted_at, offre.salary, offre.duree_mois,
                    offre.date_debut, " ".join(offre.tags), round(float(score), 4),
                    aujourd_hui, aujourd_hui, cle,
                ),
            )

    conn.execute(
        "INSERT INTO runs (horodatage, nb_offres, nb_nouvelles) VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), len(classees), len(nouvelles)),
    )
    conn.commit()
    logger.info(
        "SQLite : run enregistré (%d offre(s), %d nouveauté(s)) dans %s.",
        len(classees), len(nouvelles), config.CHEMIN_BASE,
    )
    return nouvelles


def filtrer_nouveautes(
    classees: list[tuple[Offre, float]], nouvelles: set[str]
) -> list[tuple[Offre, float]]:
    """Ne conserve que les offres dont la clé est dans l'ensemble `nouvelles`."""
    return [(o, s) for (o, s) in classees if cle_identite(o) in nouvelles]


# ---------------------------------------------------------------------------
# Cache des verdicts de vérification LLM
# ---------------------------------------------------------------------------
def hash_offre(offre: Offre) -> str:
    """Empreinte sha256 du CONTENU de l'offre (title|company|location|description).

    Distincte de la clé d'identité de dédup (titre+entreprise+ville) : ici on
    inclut la description, car un même intitulé dont le texte change mérite une
    re-vérification. Champs normalisés (trim + minuscules) pour la stabilité.
    """
    base = "|".join(
        (getattr(offre, champ, "") or "").strip().lower()
        for champ in ("title", "company", "location", "description")
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def get_verdict(
    conn: sqlite3.Connection, hash_offre: str, model: str
) -> dict | None:
    """Verdict en cache pour (offre, modèle), ou None si jamais vérifié.

    Retourne le dict désérialisé (à repasser à ``verifier.Verdict.from_dict``).
    Un JSON corrompu en base est traité comme une absence de cache (défensif).
    """
    ligne = conn.execute(
        "SELECT verdict FROM verdicts WHERE hash_offre=? AND model=?",
        (hash_offre, model),
    ).fetchone()
    if ligne is None:
        return None
    try:
        return json.loads(ligne[0])
    except (ValueError, TypeError):
        return None


def save_verdict(
    conn: sqlite3.Connection, hash_offre: str, model: str, verdict: dict
) -> None:
    """Enregistre (ou remplace) le verdict d'une offre pour un modèle donné."""
    conn.execute(
        """INSERT OR REPLACE INTO verdicts (hash_offre, model, verdict, created_at)
           VALUES (?,?,?,?)""",
        (
            hash_offre, model,
            json.dumps(verdict, ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    # Démo : deux runs sur la même base temporaire.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    conn = ouvrir(":memory:")
    o1 = Offre("Stage IA", "ACME", "Paris", "desc", "http://a", "demo", "", "")
    o2 = Offre("Stage Cyber", "BetaCorp", "Paris", "desc", "http://b", "demo", "", "")
    n1 = enregistrer_run(conn, [(o1, 0.5), (o2, 0.4)])
    print("Run 1, nouvelles :", len(n1))
    o3 = Offre("Stage Data", "Gamma", "Paris", "desc", "http://c", "demo", "", "")
    n2 = enregistrer_run(conn, [(o1, 0.5), (o3, 0.6)])
    print("Run 2, nouvelles :", len(n2), "(attendu 1 : seule 'Stage Data')")
