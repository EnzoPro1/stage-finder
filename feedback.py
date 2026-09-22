"""
feedback.py — Journal APPEND-ONLY des retours portés sur les offres affichées.

Le corpus étiqueté (`etiqueter.py`) mesure le ranking ; ce journal-ci sert à
l'améliorer. Les deux ne doivent JAMAIS se mélanger, et c'est la raison d'être
de ce module séparé : deux pools de jugements, deux supports, deux provenances
(cf. `etiqueter.PROVENANCE_CORPUS` / `etiqueter.PROVENANCE_FEEDBACK`).

## Pourquoi un journal, et pas une table d'état

Un « avis courant par offre » se met à jour en place, donc s'écrase, donc perd
l'historique — et avec lui la seule chose qui permettra de rejouer la
distillation le jour où la représentation du profil changera. On écrit donc des
ÉVÉNEMENTS, jamais un état : changer d'avis produit une ligne de plus, et
l'état courant se DÉRIVE par lecture (``rejouer``).

L'append-only est garanti par deux TRIGGERS qui refusent tout `UPDATE` et tout
`DELETE` sur la table. Pas par la discipline d'appel : une garantie qui dépend
du fait que chaque appelant y ait pensé n'est pas une garantie, et ce module ne
sera pas le seul à écrire ici (l'app web vient ensuite).

## Chaque événement est un INSTANTANÉ COMPLET

Verdict, étiquettes et commentaire sont trois axes semi-indépendants. Les
fusionner entre événements successifs (« ce like garde les chips du précédent »)
produirait un état que personne ne peut prédire en relisant le journal. Un
événement porte donc l'opinion ENTIÈRE au moment où elle est posée, et le
dernier gagne EN BLOC.

Corollaire sur ``none`` : dans un journal, « jamais donné d'avis » s'écrit par
l'ABSENCE d'événement. ``none`` ne peut donc signifier que « je retire mon
avis ». C'est une rétractation, pas une abstention — et contrairement au ``?``
de `etiqueter.py`, elle est légitime ici parce que rien n'est détruit :
l'événement rétracté reste dans le journal, seul l'état dérivé change.

## Rang et score sont OBLIGATOIRES

``rank_at_feedback`` / ``score_at_feedback`` sont `NOT NULL` en base ET sans
valeur par défaut dans la signature. Ils servent à diagnostiquer plus tard le
biais d'exposition — « je n'ai liké que ce qui était déjà en tête » — et un
champ optionnel serait vide précisément dans les événements les plus anciens,
les seuls qui permettraient de mesurer une dérive.

``rank_at_feedback`` est le rang RÉELLEMENT AFFICHÉ (il tient donc le rôle du
« rang_affiché » ; une colonne de plus qui recopierait la même valeur ne
servirait qu'à les faire diverger). Les deux rangs canoniques (``cos_rang``,
``ia_rang``) l'accompagnent, ainsi que le contexte qui le rend interprétable :
``mode_tri``, ``filtre_actif`` — un rang 3 dans une liste filtrée par texte
n'est pas un rang 3 — et ``exploratoire``.

Utilisation :
    python feedback.py --etat              # état courant dérivé, par offre
    python feedback.py --journal           # le journal brut, dans l'ordre
    python feedback.py --journal --offre <cle>
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime

import reference

logger = logging.getLogger(__name__)

# Les trois verdicts. ``none`` = rétractation (cf. en-tête), pas « pas encore vu ».
VERDICTS = ("like", "dislike", "none")

# Modes de tri de la liste affichée. Le front trie au choix par rang cosinus ou
# par rang IA : sans cette information, ``rank_at_feedback`` ne se lit pas.
MODES_TRI = ("cos", "ia")

# Valeur de ``profile_version`` tant qu'aucun profil n'est promu. Sentinelle
# explicite plutôt que NULL : « aucun profil actif » et « on ne sait pas quel
# profil était actif » ne sont pas la même chose, et se confondraient dans un
# NULL au moment précis où on chercherait à expliquer un classement.
PROFIL_AUCUN = "aucun"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id          TEXT NOT NULL,          -- = offres.cle (dedup._cle)
    timestamp         TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (verdict IN ('like','dislike','none')),
    tags              TEXT NOT NULL,          -- liste JSON, '[]' si aucune
    comment           TEXT,                   -- texte libre, nullable

    -- Rang et score EFFECTIVEMENT SOUS LES YEUX au moment du retour.
    rank_at_feedback  INTEGER NOT NULL,
    score_at_feedback REAL NOT NULL,

    -- Les deux rangs canoniques, pour ne pas avoir à deviner lequel était
    -- affiché, et pour rester lisibles si le tri par défaut du front change.
    cos_rang          INTEGER NOT NULL,
    ia_rang           INTEGER,                -- NULL tant que l'IA n'a pas vérifié
    mode_tri          TEXT NOT NULL CHECK (mode_tri IN ('cos','ia')),
    filtre_actif      INTEGER NOT NULL CHECK (filtre_actif IN (0,1)),
    exploratoire      INTEGER NOT NULL CHECK (exploratoire IN (0,1)),

    profile_version   TEXT NOT NULL,
    ranking_version   TEXT NOT NULL,

    -- Recopiés au moment du retour, et non joints à la demande : `offer_id`
    -- dérive du titre et de l'entreprise, donc un titre retouché par la source
    -- produit une clé neuve et l'événement devient orphelin. Ces trois
    -- colonnes sont ce qui permet alors de le rattacher à la main. Même
    -- raison et même geste que `etiquettes` (cf. etiqueter._SCHEMA).
    title             TEXT NOT NULL,
    company           TEXT NOT NULL,
    url               TEXT NOT NULL
);

-- L'état courant se dérive par « dernier événement de chaque offre ».
CREATE INDEX IF NOT EXISTS idx_feedback_offre ON feedback_events(offer_id, id);

-- APPEND-ONLY, garanti par la base et non par la discipline d'appel. SQLite
-- n'a pas de table en lecture seule ; ces deux triggers en sont l'équivalent
-- le plus proche, et ils tiennent quel que soit le code qui ouvre la base —
-- y compris un `sqlite3 stages.db` tapé à la main un soir de ménage.
CREATE TRIGGER IF NOT EXISTS feedback_events_pas_de_maj
BEFORE UPDATE ON feedback_events
BEGIN
    SELECT RAISE(ABORT, 'feedback_events est append-only : UPDATE refuse');
END;

CREATE TRIGGER IF NOT EXISTS feedback_events_pas_de_suppression
BEFORE DELETE ON feedback_events
BEGIN
    SELECT RAISE(ABORT, 'feedback_events est append-only : DELETE refuse');
END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée table, index et triggers si besoin. Idempotente."""
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------
def _tags_normalises(tags) -> list[str]:
    """Étiquettes dédoublonnées, ORDRE D'ARRIVÉE conservé.

    Le dédoublonnage évite qu'un double clic sur une chip la compte deux fois
    dans la distillation. L'ordre est conservé plutôt que trié : il ne coûte
    rien à garder, et un journal se relit mieux quand il ressemble à ce qui a
    été cliqué.
    """
    vus: dict[str, None] = {}
    for tag in tags or []:
        texte = str(tag).strip()
        if texte:
            vus.setdefault(texte, None)
    return list(vus)


def _commentaire_normalise(comment) -> str | None:
    """``None`` pour un commentaire absent OU vide.

    Un champ replié qu'on n'a pas rempli ne doit pas produire une chaîne vide
    qui ressemblerait, en base, à un avis écrit.
    """
    if comment is None:
        return None
    texte = str(comment).strip()
    return texte or None


def enregistrer(
    conn: sqlite3.Connection,
    *,
    offer_id: str,
    verdict: str,
    rank_at_feedback: int,
    score_at_feedback: float,
    cos_rang: int,
    mode_tri: str,
    title: str,
    company: str,
    url: str,
    ia_rang: int | None = None,
    tags=None,
    comment=None,
    filtre_actif: bool = False,
    exploratoire: bool = False,
    profile_version: str = PROFIL_AUCUN,
    ranking_version: str | None = None,
    horodatage: str | None = None,
) -> int:
    """Ajoute UN événement au journal. Rend son ``id``.

    Tout passe par MOT-CLÉ : l'événement porte trois rangs et un score, et un
    appel positionnel qui intervertirait ``cos_rang`` et ``rank_at_feedback``
    serait indétectable à la relecture comme à l'exécution.

    ``rank_at_feedback``, ``score_at_feedback``, ``cos_rang`` et ``mode_tri``
    n'ont PAS de valeur par défaut : ils sont le garde-fou du biais
    d'exposition, et un défaut les rendrait facultatifs dans les faits.

    ``ranking_version`` vaut par défaut l'empreinte des constantes de ranking
    (``reference.empreinte_config``) — la même que celle qui estampille les
    rapports de mesure, pour qu'un événement et un rapport se rapportent au
    même vocabulaire sans table de correspondance.
    """
    if verdict not in VERDICTS:
        raise ValueError(
            f"verdict inconnu : {verdict!r} (attendu : {', '.join(VERDICTS)})")
    if mode_tri not in MODES_TRI:
        raise ValueError(
            f"mode_tri inconnu : {mode_tri!r} (attendu : {', '.join(MODES_TRI)})")
    if not offer_id:
        raise ValueError("offer_id vide : l'événement ne désignerait aucune offre.")

    ensure_schema(conn)
    curseur = conn.execute(
        """INSERT INTO feedback_events
           (offer_id, timestamp, verdict, tags, comment,
            rank_at_feedback, score_at_feedback, cos_rang, ia_rang,
            mode_tri, filtre_actif, exploratoire,
            profile_version, ranking_version, title, company, url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            offer_id,
            horodatage or datetime.now().isoformat(timespec="seconds"),
            verdict,
            json.dumps(_tags_normalises(tags), ensure_ascii=False),
            _commentaire_normalise(comment),
            int(rank_at_feedback),
            float(score_at_feedback),
            int(cos_rang),
            None if ia_rang is None else int(ia_rang),
            mode_tri,
            int(bool(filtre_actif)),
            int(bool(exploratoire)),
            profile_version,
            ranking_version if ranking_version is not None
            else reference.empreinte_config(),
            title or "",
            company or "",
            url or "",
        ),
    )
    conn.commit()
    return int(curseur.lastrowid)


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
def _decoder_tags(brut: str, event_id: int) -> list[str]:
    """Décodage DÉFENSIF des étiquettes : un JSON cassé ne bloque pas le rejeu.

    Le journal doit rester rejouable de bout en bout ; une ligne abîmée à la
    main en base ne doit pas faire échouer la relecture des mille autres. On
    perd les étiquettes de CETTE ligne, et on le dit.
    """
    try:
        valeur = json.loads(brut)
    except (TypeError, ValueError):
        logger.warning(
            "Événement %s : étiquettes illisibles (%r), ignorées.", event_id, brut)
        return []
    return [str(t) for t in valeur] if isinstance(valeur, list) else []


def _vue(ligne: sqlite3.Row) -> dict:
    """Une ligne de journal en dict, avec ses VRAIS types (bool, list, None)."""
    return {
        "id": int(ligne["id"]),
        "offer_id": ligne["offer_id"],
        "timestamp": ligne["timestamp"],
        "verdict": ligne["verdict"],
        "tags": _decoder_tags(ligne["tags"], int(ligne["id"])),
        "comment": ligne["comment"],
        "rank_at_feedback": int(ligne["rank_at_feedback"]),
        "score_at_feedback": float(ligne["score_at_feedback"]),
        "cos_rang": int(ligne["cos_rang"]),
        "ia_rang": None if ligne["ia_rang"] is None else int(ligne["ia_rang"]),
        "mode_tri": ligne["mode_tri"],
        "filtre_actif": bool(ligne["filtre_actif"]),
        "exploratoire": bool(ligne["exploratoire"]),
        "profile_version": ligne["profile_version"],
        "ranking_version": ligne["ranking_version"],
        "title": ligne["title"],
        "company": ligne["company"],
        "url": ligne["url"],
    }


def evenements(conn: sqlite3.Connection, offer_id: str | None = None) -> list[dict]:
    """Le journal, DANS L'ORDRE D'ÉCRITURE (``id`` croissant).

    C'est l'ordre du rejeu, et il ne se déduit pas de ``timestamp`` : deux
    retours posés dans la même seconde y sont indiscernables, alors que ``id``
    les ordonne. Le journal est une séquence, pas un horodatage.
    """
    ensure_schema(conn)
    if offer_id is None:
        lignes = conn.execute("SELECT * FROM feedback_events ORDER BY id")
    else:
        lignes = conn.execute(
            "SELECT * FROM feedback_events WHERE offer_id = ? ORDER BY id", (offer_id,)
        )
    return [_vue(ligne) for ligne in lignes]


def rejouer(journal) -> dict[str, dict]:
    """Rejoue une séquence d'événements et rend l'état courant par offre.

    FONCTION PURE, sur une liste de dicts — pas sur une connexion. C'est
    délibéré : c'est elle qui porte la promesse « l'historique permet de
    rejouer intégralement la séquence a posteriori », et une promesse qui
    exige une base pour être vérifiée se vérifie mal. La distillation
    (étape D) consommera cette fonction, jamais la table.

    Dernier événement gagnant EN BLOC : l'événement EST l'état, on n'en
    fusionne jamais deux (cf. en-tête). ``n_evenements`` est ajouté — il ne
    fait pas partie de l'opinion, il dit combien de fois elle a bougé, ce qui
    distingue un avis posé une fois d'un avis retourné cinq fois.
    """
    etat: dict[str, dict] = {}
    for evenement in journal:
        cle = evenement["offer_id"]
        precedent = etat.get(cle)
        etat[cle] = {
            **evenement,
            "n_evenements": (precedent["n_evenements"] + 1) if precedent else 1,
        }
    return etat


def etat_courant(conn: sqlite3.Connection,
                 offer_id: str | None = None) -> dict[str, dict]:
    """État courant dérivé du journal. AUCUN état n'est stocké séparément.

    Simple composition de ``evenements`` et ``rejouer`` : il n'existe qu'UNE
    dérivation de l'état. La version SQL qu'on serait tenté d'écrire ici
    (``GROUP BY offer_id HAVING MAX(id)``) en serait une seconde, à maintenir
    d'accord avec la première.
    """
    return rejouer(evenements(conn, offer_id))


def opinions(etat: dict[str, dict]) -> dict[str, dict]:
    """Ne garde que les offres portant un avis EXPRIMÉ (``like`` / ``dislike``).

    C'est ce que la distillation consomme. Une offre rétractée (``none``) n'a
    pas d'opinion courante : elle sort d'ici, et reste dans le journal.
    """
    return {c: e for c, e in etat.items() if e["verdict"] != "none"}


# ---------------------------------------------------------------------------
# Rapports console
# ---------------------------------------------------------------------------
_MARQUE = {"like": "👍 like", "dislike": "👎 dislike", "none": "∅ rétracté"}


def afficher_etat(conn: sqlite3.Connection) -> dict:
    """État courant par offre, et ce que la distillation en retiendrait."""
    etat = etat_courant(conn)
    exprimees = opinions(etat)
    journal = evenements(conn)

    likes = sum(1 for e in exprimees.values() if e["verdict"] == "like")
    print(f"\nJournal : {len(journal)} événement(s) sur {len(etat)} offre(s).")
    print(f"Opinions exprimées : {len(exprimees)} ({likes} like / "
          f"{len(exprimees) - likes} dislike) — "
          f"{len(etat) - len(exprimees)} rétractée(s).")
    for cle in sorted(etat):
        e = etat[cle]
        tags = " ".join(f"[{t}]" for t in e["tags"])
        churn = f"×{e['n_evenements']}" if e["n_evenements"] > 1 else ""
        contexte = e["mode_tri"] + (", filtré" if e["filtre_actif"] else "")
        print(f"  {_MARQUE[e['verdict']]:<12} {churn:<4} rang {e['rank_at_feedback']:>3}"
              f" ({contexte}){' 🔭' if e['exploratoire'] else ''}"
              f"  {e['title'][:44]:44} {tags}")
        if e["comment"]:
            print(f"                    « {e['comment'][:70]} »")
    return {"evenements": len(journal), "offres": len(etat),
            "opinions": len(exprimees)}


def afficher_journal(conn: sqlite3.Connection, offer_id: str | None = None) -> int:
    """Le journal brut, dans l'ordre — la séquence telle qu'elle sera rejouée."""
    journal = evenements(conn, offer_id)
    print(f"\n{len(journal)} événement(s)"
          + (f" pour l'offre {offer_id}" if offer_id else "") + " :")
    for e in journal:
        ia = e["ia_rang"] if e["ia_rang"] is not None else "—"
        print(f"  #{e['id']:<5} {e['timestamp']}  {e['verdict']:<8} "
              f"rang {e['rank_at_feedback']:>3}/{e['mode_tri']:<3} "
              f"cos={e['cos_rang']:>3} ia={ia:>3} "
              f"score={e['score_at_feedback']:.3f}  {e['offer_id'][:12]}  "
              f"{e['title'][:36]}")
        if e["tags"] or e["comment"]:
            commentaire = f"  « {e['comment'][:60]} »" if e["comment"] else ""
            print(f"         {' '.join('[' + t + ']' for t in e['tags'])}{commentaire}")
    return len(journal)


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    import storage

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Journal de feedback sur les offres.")
    p.add_argument("--db", default=None,
                   help="Base SQLite (défaut : config.CHEMIN_BASE, la base VIVE).")
    p.add_argument("--etat", action="store_true", help="État courant dérivé, par offre.")
    p.add_argument("--journal", action="store_true", help="Le journal brut, dans l'ordre.")
    p.add_argument("--offre", default=None, help="Restreint --journal à une clé d'offre.")
    args = p.parse_args()

    # Le feedback vit sur la base VIVE, jamais sur l'instantané de référence :
    # il porte sur les offres qu'on a réellement eues sous les yeux. C'est
    # aussi, accessoirement, ce qui le tient à l'écart du corpus de mesure.
    conn = storage.ouvrir(args.db)
    try:
        ensure_schema(conn)
        if args.journal:
            afficher_journal(conn, args.offre)
        else:
            afficher_etat(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
