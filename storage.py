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

## Les liens, et ce qui fait une nouveauté

Chaque offre porte ses LIENS : une annonce par source, identifiée par une clé
stable chez cette source (`normalize.Lien`) — l'identifiant natif, jamais
l'URL. Ils vivent dans `offres_liens`, une ligne par (source, clé) :

- un lien déjà connu qui revient avec une autre URL met à jour l'URL EN PLACE ;
  il n'y a jamais deux lignes pour la même (source, clé). Adzuna ajoute un jeton
  `se=` différent à chaque requête, Careerjet réchiffre l'URL entière : sans
  cette règle, chaque run doublerait les liens ;
- une offre est NOUVELLE seulement si AUCUN de ses liens n'a déjà été vu, ET que
  sa clé d'offre n'est pas déjà en base. La seconde condition couvre les lignes
  antérieures aux liens (aucun lien enregistré pour elles) et une annonce qui
  change de source d'un run à l'autre.

Une annonce dont le titre change garde ses liens — elle n'est donc pas
annoncée nouvelle — mais occupe une nouvelle ligne `offres`, sous sa nouvelle
clé, comme avant : les jobs de CV et les textes restent rattachés à la clé
calculée, et les liens sont repointés vers elle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime

import config
import jobs
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
    familles     TEXT,   -- familles de requêtes, séparées par des espaces
    familles_titre TEXT, -- parmi elles, celles dont un terme est dans le titre
    drapeaux     TEXT,   -- signalements non excluants (« alternance »)
    commune_trajet TEXT, -- jobs étudiants : code de la commune résolue
    trajets      TEXT,   -- jobs étudiants : JSON {origine: {brut_min, ajuste_min, …}}
    dernier_score REAL,
    premiere_vue TEXT,   -- date ISO du premier run où l'offre est apparue
    derniere_vue TEXT,   -- date ISO du dernier run
    nb_vues      INTEGER -- nombre de runs distincts ayant vu l'offre
);

-- Une annonce chez une source. (source, cle) est l'identité : `cle` est
-- l'identifiant natif (« id:… »), une empreinte de contenu (« contenu:… »,
-- Careerjet) ou, en dernier recours, l'URL normalisée (« url:… »). `url` est
-- la dernière adresse vue, mise à jour en place.
CREATE TABLE IF NOT EXISTS offres_liens (
    source       TEXT NOT NULL,
    cle          TEXT NOT NULL,
    cle_offre    TEXT NOT NULL,   -- offres.cle de la ligne à laquelle il est rattaché
    url          TEXT,
    premiere_vue TEXT,
    derniere_vue TEXT,
    PRIMARY KEY (source, cle)
);
CREATE INDEX IF NOT EXISTS idx_offres_liens_offre ON offres_liens (cle_offre);

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
    _ajouter_colonnes_manquantes(conn)
    conn.commit()
    return conn


# Colonnes ajoutées à `offres` après sa création. `CREATE TABLE IF NOT EXISTS`
# ne touche pas une table existante : une base d'avant CP3 doit les recevoir.
_COLONNES_AJOUTEES = (("familles", "TEXT"), ("familles_titre", "TEXT"), ("drapeaux", "TEXT"),
                      ("commune_trajet", "TEXT"), ("trajets", "TEXT"))


def _ajouter_colonnes_manquantes(conn: sqlite3.Connection) -> None:
    presentes = {ligne[1] for ligne in conn.execute("PRAGMA table_info(offres)")}
    for nom, type_sql in _COLONNES_AJOUTEES:
        if nom not in presentes:
            conn.execute(f"ALTER TABLE offres ADD COLUMN {nom} {type_sql}")


def liens_connus(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(source, clé) -> date ISO de première vue, pour tous les liens en base."""
    return {
        (ligne[0], ligne[1]): ligne[2]
        for ligne in conn.execute("SELECT source, cle, premiere_vue FROM offres_liens")
    }


def liens_de(conn: sqlite3.Connection, cle_offre: str) -> list[dict]:
    """Les liens rattachés à une ligne `offres`, du plus ancien au plus récent."""
    return [
        dict(ligne) for ligne in conn.execute(
            "SELECT source, cle, url, premiere_vue, derniere_vue FROM offres_liens "
            "WHERE cle_offre = ? ORDER BY premiere_vue, source, cle", (cle_offre,))
    ]


def cles_connues(conn: sqlite3.Connection) -> set[str]:
    """Ensemble des clés d'offres déjà enregistrées (runs précédents)."""
    return {ligne[0] for ligne in conn.execute("SELECT cle FROM offres")}


def enregistrer_run(
    conn: sqlite3.Connection, classees: list[tuple[Offre, float]]
) -> set[str]:
    """Enregistre un run : upsert des offres, texte brut, trace dans `runs`.

    Retourne l'ensemble des clés NOUVELLES (jamais vues avant ce run), pour
    permettre à l'appelant de mettre en avant / filtrer les nouveautés. Pose
    aussi sur chaque offre ``nouvelle`` et ``premiere_vue`` — la date la plus
    ancienne à laquelle l'un de ses liens, ou sa ligne, a été vu.

    Règle de nouveauté et traitement des liens : cf. l'en-tête du module.

    ## Pourquoi le TEXTE est écrit ICI

    `offres` ne porte pas de colonne `description` — délibérément : elle est
    lue en entier à chaque run, et y coller des kilo-octets alourdirait tous
    les parcours. Le texte va donc dans la table latérale `offres_texte`.

    Mais il doit y aller AU MOMENT DU RUN, et depuis cette fonction. C'est le
    seul endroit que traversent les deux chemins de scrape (`main.executer` en
    CLI, `app._collecte` côté web) : l'écrire ailleurs voudrait dire l'écrire
    deux fois, et l'un des deux finirait par l'oublier. C'est exactement ce
    qui s'était produit — le texte n'était persisté par AUCUN des deux, et
    seul un backfill ponctuel depuis `.rank_cache.json` avait pourvu 74 offres
    sur 294. Les 220 autres affichaient un bouton « Générer CV » inutilisable.
    """
    # Le texte vit dans `offres_texte`, dont le schéma appartient à `jobs`.
    # Idempotente et sans coût mesurable une fois par run : la garantie que
    # la table existe ne peut pas dépendre du fait que chaque appelant y ait
    # pensé (`main.executer` n'y pensait pas).
    jobs.ensure_schema(conn)

    connues_avant = cles_connues(conn)
    premieres_vues = dict(conn.execute("SELECT cle, premiere_vue FROM offres").fetchall())
    liens_avant = liens_connus(conn)
    aujourd_hui = date.today().isoformat()
    nouvelles: set[str] = set()
    # Les textes sont accumulés puis écrits APRÈS le commit des offres.
    # `jobs.enregistrer_texte` ouvre sa propre transaction (`with conn:`) qui
    # valide en sortie : l'appeler dans la boucle validerait un upsert
    # d'offres à moitié fait à chaque tour, là où la fonction ne validait
    # jusqu'ici qu'une fois, tout ou rien.
    a_ecrire: list[tuple[str, str]] = []

    for offre, score in classees:
        cle = cle_identite(offre)
        ligne_connue = cle in connues_avant
        vus = [liens_avant[(l.source, l.cle)] for l in offre.liens
               if (l.source, l.cle) in liens_avant]
        est_nouvelle = not vus and not ligne_connue
        if est_nouvelle:
            nouvelles.add(cle)
        dates = [d for d in vus if d] + (
            [premieres_vues[cle]] if ligne_connue and premieres_vues.get(cle) else [])
        offre.nouvelle = est_nouvelle
        offre.premiere_vue = min(dates) if dates else aujourd_hui

        # Un texte VIDE n'écrase rien. Une source qui cesse de livrer la
        # description (ou une offre reconstruite sans elle, comme le fait
        # `backfill_textes._offre_depuis_row`) ne doit pas faire disparaître
        # un texte déjà en base : la génération de CV s'arrêterait net sur
        # des offres qui marchaient la veille.
        texte = (offre.description or "").strip()
        if texte:
            a_ecrire.append((cle, texte))

        familles = " ".join(offre.familles)
        familles_titre = " ".join(offre.familles_titre)
        trajets = json.dumps(offre.trajets, ensure_ascii=False) if offre.trajets else ""
        # Insertion selon la LIGNE, pas selon la nouveauté : une annonce dont le
        # titre a changé n'est pas nouvelle (ses liens sont connus) mais n'a pas
        # encore de ligne sous sa nouvelle clé.
        if not ligne_connue:
            conn.execute(
                """INSERT INTO offres
                   (cle, title, company, location, url, source, posted_at, salary,
                    duree_mois, date_debut, tags, familles, familles_titre, drapeaux,
                    commune_trajet, trajets, dernier_score, premiere_vue, derniere_vue, nb_vues)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    cle, offre.title, offre.company, offre.location, offre.url,
                    offre.source, offre.posted_at, offre.salary, offre.duree_mois,
                    offre.date_debut, " ".join(offre.tags), familles, familles_titre,
                    " ".join(offre.drapeaux), offre.commune_trajet, trajets,
                    round(float(score), 4), offre.premiere_vue, aujourd_hui,
                ),
            )
        else:
            # Offre déjà connue : on rafraîchit et on incrémente le compteur de vues
            # (une seule incrémentation par jour de run, pour ne pas gonfler).
            conn.execute(
                """UPDATE offres
                   SET title=?, company=?, location=?, url=?, source=?, posted_at=?,
                       salary=?, duree_mois=?, date_debut=?, tags=?, familles=?,
                       familles_titre=?, drapeaux=?, commune_trajet=?, trajets=?,
                       dernier_score=?,
                       derniere_vue=?,
                       nb_vues = nb_vues + (CASE WHEN derniere_vue <> ? THEN 1 ELSE 0 END)
                   WHERE cle=?""",
                (
                    offre.title, offre.company, offre.location, offre.url,
                    offre.source, offre.posted_at, offre.salary, offre.duree_mois,
                    offre.date_debut, " ".join(offre.tags), familles, familles_titre,
                    " ".join(offre.drapeaux), offre.commune_trajet, trajets,
                    round(float(score), 4), aujourd_hui, aujourd_hui, cle,
                ),
            )

        # Un lien par (source, clé), JAMAIS deux : un lien connu garde sa
        # première vue et prend l'URL et le rattachement de ce run.
        for lien in offre.liens:
            conn.execute(
                """INSERT INTO offres_liens
                   (source, cle, cle_offre, url, premiere_vue, derniere_vue)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (source, cle) DO UPDATE SET
                       cle_offre = excluded.cle_offre,
                       url = excluded.url,
                       derniere_vue = excluded.derniere_vue""",
                (lien.source, lien.cle, cle, lien.url, aujourd_hui, aujourd_hui),
            )

    conn.execute(
        "INSERT INTO runs (horodatage, nb_offres, nb_nouvelles) VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), len(classees), len(nouvelles)),
    )
    conn.commit()

    # Après le commit, donc : une interruption ici laisse des offres sans
    # texte, ce que le run suivant répare tout seul. L'ordre inverse
    # laisserait des textes rattachés à des offres inexistantes.
    for cle, texte in a_ecrire:
        jobs.enregistrer_texte(conn, cle, texte)

    logger.info(
        "SQLite : run enregistré (%d offre(s), %d nouveauté(s), %d texte(s)) dans %s.",
        len(classees), len(nouvelles), len(a_ecrire),
        next((l[2] for l in conn.execute("PRAGMA database_list") if l[1] == "main"), "?"),
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
