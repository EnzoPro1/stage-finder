"""
app.py — Petite app web LOCALE pour explorer les stages de façon interactive.

Pourquoi une app en plus du CLI : la vérification LLM est LENTE (Ollama sur CPU).
On veut donc :
  1. afficher le classement cosinus IMMÉDIATEMENT (rechargé d'un cache disque),
     sans attendre l'IA ;
  2. déclencher la vérification IA À LA DEMANDE, sur un nombre d'offres saisi
     dans la page, avec une barre de progression ;
  3. comparer le classement AVANT (cosinus) et APRÈS (IA) côte à côte
     (colonnes « cos | IA | Δ »).

Architecture (volontairement simple, mono-utilisateur, 100 % local) :
  - un état en mémoire ``ETAT`` (le dernier classement + verdicts), protégé par
    un verrou ; persisté dans ``.rank_cache.json`` pour un affichage instantané
    au prochain démarrage ;
  - les tâches lourdes (collecte, vérif IA) tournent dans un THREAD de fond ;
    le front interroge ``/api/etat`` en polling pour suivre l'avancement ;
  - toute la logique métier est RÉUTILISÉE depuis main.py / verifier.py (aucune
    duplication) : ``main.collecter_et_classer`` et ``main.verifier_shortlist``.

Lancement :  python app.py   puis ouvrir http://localhost:5000
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file, url_for

import console  # noqa: F401 - force UTF-8 sur la console Windows
import config
import jobs
import main
import market
import storage
import verifier
import worker as worker_module
from dedup import _cle as cle_identite
from normalize import Offre
from sources import RedactingFilter

logger = logging.getLogger("job-finder.app")

CHEMIN_CACHE = ".rank_cache.json"

# --- État partagé ----------------------------------------------------------
# ``classees`` est la SOURCE DE VÉRITÉ : la liste (Offre, score_cosinus) en
# ordre cosinus. Les verdicts vivent sur les objets Offre (offre.verdict). Les
# rangs/scores IA sont indexés par empreinte d'offre.
ETAT: dict = {
    "classees": [],            # list[tuple[Offre, float]] en ordre cosinus
    "ia_rang": {},             # hash_offre -> rang après tri IA
    "ia_score": {},            # hash_offre -> score final (cosinus+LLM)
    "genere_le": None,         # ISO du dernier classement
    "collecte": {"en_cours": False, "debut": None},
    "verif": {"en_cours": False, "fait": 0, "total": 0,
              "appels": 0, "cache": 0, "indisponible": False, "modele": None},
    # Enchaînement automatique « recherche puis vérification IA » (bouton unique).
    # ``etape`` sert uniquement à l'affichage ("1/2" pendant la collecte).
    "auto": {"en_cours": False, "etape": 0},
    # Dernier tableau de bord marché calculé (market.py) + son horodatage.
    "marche": None,
    "marche_le": None,
}

# Durée de validité du tableau de bord marché. Les volumes d'offres bougent à
# l'échelle de la journée, pas de la minute : recalculer à chaque affichage
# gaspillerait une quinzaine d'appels d'API pour un chiffre identique.
MARCHE_TTL_S = 900
VERROU = threading.Lock()

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Cache disque du classement (affichage instantané au redémarrage)
# ---------------------------------------------------------------------------
def _sauver_cache() -> None:
    """Écrit le classement courant (offres + verdicts + rangs IA) sur disque."""
    with VERROU:
        rows = [_row(offre, cos, rang, inclure_desc=True)
                for rang, (offre, cos) in enumerate(ETAT["classees"], 1)]
        paquet = {"genere_le": ETAT["genere_le"], "modele": config.VERIFY_MODEL, "offres": rows}
    try:
        with open(CHEMIN_CACHE, "w", encoding="utf-8") as f:
            json.dump(paquet, f, ensure_ascii=False)
    except OSError as err:
        logger.warning("Cache non écrit (%s).", err)


def _charger_cache() -> None:
    """Recharge le dernier classement depuis le disque, s'il existe."""
    if not os.path.exists(CHEMIN_CACHE):
        return
    try:
        with open(CHEMIN_CACHE, "r", encoding="utf-8") as f:
            paquet = json.load(f)
    except (OSError, ValueError) as err:
        logger.warning("Cache illisible (%s) : ignoré.", err)
        return

    classees, ia_rang, ia_score = [], {}, {}
    for row in paquet.get("offres", []):
        offre = Offre(
            row["title"], row["company"], row["location"], row.get("description", ""),
            row["url"], row["source"], row.get("posted_at", ""), row.get("salary", ""),
        )
        offre.tags = row.get("tags", [])
        offre.duree_mois = row.get("duree_mois")
        offre.date_debut = row.get("date_debut", "")
        if row.get("verdict"):
            offre.verdict = verifier.Verdict.from_dict(row["verdict"])
        classees.append((offre, float(row.get("cos_score", 0.0))))
        h = storage.hash_offre(offre)
        if row.get("ia_rang") is not None:
            ia_rang[h] = row["ia_rang"]
            ia_score[h] = row.get("ia_score")

    with VERROU:
        ETAT["classees"] = classees
        ETAT["ia_rang"] = ia_rang
        ETAT["ia_score"] = ia_score
        ETAT["genere_le"] = paquet.get("genere_le")
    logger.info("Cache chargé : %d offre(s).", len(classees))


def _row(offre: Offre, cos_score: float, cos_rang: int, inclure_desc: bool = False) -> dict:
    """Sérialise une offre en dict JSON.

    ``inclure_desc`` : la description (souvent > 3 Ko) n'est utile qu'au CACHE
    disque (pour re-vérifier plus tard). On l'EXCLUT de la réponse API : sinon la
    charge JSON gonfle et le serveur de dev Werkzeug finit par couper des
    connexions sous Windows (ERR_CONNECTION_RESET).
    """
    h = storage.hash_offre(offre)
    verdict = getattr(offre, "verdict", None)
    ia_score = ETAT["ia_score"].get(h)
    row = {
        # AJOUT du CP4, rien d'autre ne bouge dans cette fonction. Sans la
        # clé, le front ne peut désigner aucune offre : c'est elle que
        # portent `/api/offers/<id>/cv` et `/api/cv/states`.
        #
        # `dedup._cle` et non `storage.hash_offre` : la première EST la
        # clé primaire de `offres`, la seconde est une empreinte de
        # contenu qui sert au cache des verdicts. Les confondre donnerait
        # un identifiant qui ne désigne aucune ligne.
        "cle": cle_identite(offre),
        "cos_rang": cos_rang,
        "cos_score": round(cos_score, 3),
        "ia_rang": ETAT["ia_rang"].get(h),
        "ia_score": round(ia_score, 3) if ia_score is not None else None,
        "title": offre.title, "company": offre.company, "location": offre.location,
        "url": offre.url, "source": offre.source,
        "duree_mois": offre.duree_mois, "date_debut": offre.date_debut,
        "tags": offre.tags,
        "verdict": verdict.to_dict() if verdict else None,
    }
    if inclure_desc:
        row["description"] = offre.description
        row["posted_at"] = offre.posted_at
        row["salary"] = offre.salary
    return row


# ---------------------------------------------------------------------------
# Tâches de fond : collecte (ranking cosinus) et vérification IA
# ---------------------------------------------------------------------------
def _collecte(utiliser_jobspy: bool) -> int:
    """Collecte + ranking cosinus, remplace l'état. Retourne le nb d'offres.

    Lancée telle quelle dans un thread par ``/api/rafraichir``, et réutilisée
    comme PREMIÈRE étape de l'enchaînement automatique (« Tout lancer ») : une
    seule implémentation pour les deux chemins.
    """
    try:
        classees = main.collecter_et_classer(utiliser_jobspy=utiliser_jobspy)
    except Exception as err:  # noqa: BLE001 - une collecte ratée ne doit pas tuer le serveur
        logger.warning("Collecte en échec : %s", err)
        classees = []
    with VERROU:
        if classees:
            ETAT["classees"] = classees
            ETAT["ia_rang"] = {}      # nouveau classement -> anciens verdicts caducs
            ETAT["ia_score"] = {}
            ETAT["genere_le"] = datetime.now().isoformat(timespec="seconds")
        ETAT["collecte"] = {"en_cours": False, "debut": None}
    if classees:
        _sauver_cache()
    return len(classees)


def _verifier(n: int, modele: str) -> None:
    """Vérifie les N premières offres par LLM : état, cache disque et progression.

    Lancée telle quelle dans un thread par ``/api/verifier``, et réutilisée
    comme SECONDE étape de l'enchaînement automatique.
    """
    conn = storage.ouvrir(config.CHEMIN_BASE)

    def prog(info: dict) -> None:
        with VERROU:
            v = ETAT["verif"]
            if info["phase"] == "indisponible":
                v["indisponible"] = True
            elif info["phase"] == "verif":
                v.update(fait=info["fait"], total=info["total"],
                         cache=info["cache"], appels=info["appels"])

    with VERROU:
        classees = ETAT["classees"]
    try:
        fusion = main.verifier_shortlist(
            classees, conn, top_n=n, model=modele, on_progress=prog,
        )
    except Exception as err:  # noqa: BLE001 - dégradation : on n'écroule pas le serveur
        logger.warning("Vérification IA en échec : %s", err)
        fusion = classees
    finally:
        conn.close()

    with VERROU:
        ETAT["ia_rang"] = {}
        ETAT["ia_score"] = {}
        for rang, (offre, score) in enumerate(fusion, 1):
            h = storage.hash_offre(offre)
            ETAT["ia_rang"][h] = rang
            ETAT["ia_score"][h] = score
        ETAT["verif"]["en_cours"] = False
    _sauver_cache()


def _thread_tout(utiliser_jobspy: bool, n_demande: int | None) -> None:
    """Enchaîne AUTOMATIQUEMENT la recherche puis la vérification IA.

    C'est le bouton « Tout lancer » : les deux étapes coûteuses s'enchaînent
    sans qu'on ait à revenir cliquer entre les deux (la collecte prend plusieurs
    minutes — autant ne pas avoir à la surveiller).

    La shortlist à vérifier est décidée APRÈS la collecte : on ne connaît le
    nombre d'offres qu'à ce moment-là. Si la collecte ne ramène rien, on
    n'enchaîne pas sur une vérification vide.
    """
    try:
        n_offres = _collecte(utiliser_jobspy)
        if not n_offres:
            logger.warning("Tout lancer : aucune offre collectée, vérification IA sautée.")
            return

        n = max(1, min(n_demande or config.VERIFY_TOP_N, n_offres))
        with VERROU:
            ETAT["auto"]["etape"] = 2
            ETAT["verif"] = {"en_cours": True, "fait": 0, "total": n, "appels": 0,
                             "cache": 0, "indisponible": False, "modele": config.VERIFY_MODEL}
        _verifier(n, config.VERIFY_MODEL)
    finally:
        # Quoi qu'il arrive, on relâche les verrous d'interface : sans ça, un
        # échec laisserait les boutons grisés jusqu'au redémarrage du serveur.
        with VERROU:
            ETAT["auto"] = {"en_cours": False, "etape": 0}
            ETAT["collecte"] = {"en_cours": False, "debut": None}
            ETAT["verif"]["en_cours"] = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return PAGE


@app.get("/api/etat")
def api_etat():
    """Snapshot complet : offres (ordre cosinus) + progression des tâches."""
    with VERROU:
        rows = [_row(offre, cos, rang)
                for rang, (offre, cos) in enumerate(ETAT["classees"], 1)]
        etat = {
            "offres": rows,
            "n_total": len(rows),
            "genere_le": ETAT["genere_le"],
            "modele": config.VERIFY_MODEL,
            "collecte": dict(ETAT["collecte"]),
            "verif": dict(ETAT["verif"]),
            "auto": dict(ETAT["auto"]),
            "profil": config.REQUETE_REFERENCE,
            "top_n_defaut": config.VERIFY_TOP_N,
            "sources": config.SOURCES_ACTIVES,
        }
    return jsonify(etat)


def _tache_en_cours() -> bool:
    """Vrai si une tâche de fond occupe déjà le serveur (appel sous VERROU)."""
    return (ETAT["verif"]["en_cours"] or ETAT["collecte"]["en_cours"]
            or ETAT["auto"]["en_cours"])


@app.post("/api/verifier")
def api_verifier():
    """Déclenche la vérification IA des N premières offres (tâche de fond)."""
    with VERROU:
        if _tache_en_cours():
            return jsonify({"erreur": "Une tâche est déjà en cours."}), 409
        n_total = len(ETAT["classees"])
    if not n_total:
        return jsonify({"erreur": "Aucun classement. Lance d'abord une recherche."}), 400

    n = (request.get_json(silent=True) or {}).get("n", config.VERIFY_TOP_N)
    try:
        n = max(1, min(int(n), n_total))
    except (TypeError, ValueError):
        return jsonify({"erreur": "Nombre invalide."}), 400

    with VERROU:
        ETAT["verif"] = {"en_cours": True, "fait": 0, "total": n, "appels": 0,
                         "cache": 0, "indisponible": False, "modele": config.VERIFY_MODEL}
    threading.Thread(target=_verifier, args=(n, config.VERIFY_MODEL), daemon=True).start()
    return jsonify({"ok": True, "n": n})


@app.post("/api/rafraichir")
def api_rafraichir():
    """Relance une recherche complète (collecte + ranking) en tâche de fond."""
    with VERROU:
        if _tache_en_cours():
            return jsonify({"erreur": "Une tâche est déjà en cours."}), 409
        ETAT["collecte"] = {"en_cours": True, "debut": datetime.now().isoformat(timespec="seconds")}
    utiliser_jobspy = not (request.get_json(silent=True) or {}).get("no_jobspy", False)
    threading.Thread(target=_collecte, args=(utiliser_jobspy,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/marche")
def api_marche():
    """Tableau de bord « le marché est-il favorable ? » (market.py).

    Deux modes VOLONTAIREMENT distincts :
    - sans paramètre : consultation du cache SEULEMENT. La page peut donc
      l'appeler à chaque ouverture sans jamais déclencher d'appels d'API ni
      faire attendre l'utilisateur (``vide: true`` s'il n'y a rien).
    - ``?force=1`` : mesure réelle (une quinzaine d'appels, quelques secondes),
      mise en cache pour ``MARCHE_TTL_S``.
    """
    force = request.args.get("force") == "1"
    with VERROU:
        cache, calcule_le = ETAT["marche"], ETAT["marche_le"]
    frais = (
        cache is not None
        and calcule_le is not None
        and (datetime.now() - datetime.fromisoformat(calcule_le)).total_seconds() < MARCHE_TTL_S
    )
    if cache is not None and (frais or not force):
        return jsonify({**cache, "calcule_le": calcule_le, "depuis_cache": True})
    if not force:
        return jsonify({"vide": True})

    try:
        tableau = market.tableau_de_bord()
    except Exception as err:  # noqa: BLE001 - un indicateur ne doit pas casser la page
        logger.warning("Tableau de bord marché en échec : %s", err)
        return jsonify({"erreur": "Indicateurs indisponibles (voir les logs)."}), 503

    calcule_le = datetime.now().isoformat(timespec="seconds")
    with VERROU:
        ETAT["marche"], ETAT["marche_le"] = tableau, calcule_le
    return jsonify({**tableau, "calcule_le": calcule_le, "depuis_cache": False})


@app.post("/api/tout")
def api_tout():
    """Bouton « Tout lancer » : recherche PUIS vérification IA, sans intervention.

    Un seul appel HTTP déclenche les deux étapes ; le front n'a rien à
    ré-armer, il suit la progression par le polling habituel.
    """
    with VERROU:
        if _tache_en_cours():
            return jsonify({"erreur": "Une tâche est déjà en cours."}), 409
        ETAT["auto"] = {"en_cours": True, "etape": 1}
        ETAT["collecte"] = {"en_cours": True, "debut": datetime.now().isoformat(timespec="seconds")}

    corps = request.get_json(silent=True) or {}
    utiliser_jobspy = not corps.get("no_jobspy", False)
    try:
        n = int(corps.get("n", config.VERIFY_TOP_N))
    except (TypeError, ValueError):
        n = config.VERIFY_TOP_N

    threading.Thread(target=_thread_tout, args=(utiliser_jobspy, n), daemon=True).start()
    return jsonify({"ok": True})


# ===========================================================================
# Génération de CV — couche HTTP au-dessus de la file (jobs.py / worker.py)
#
# Routes ADDITIVES : rien au-dessus de cette ligne n'est modifié. L'état en
# mémoire ``ETAT`` et son verrou ne sont pas touchés non plus — la file vit
# en base, pas en RAM, précisément pour survivre à un redémarrage et pour
# être lisible par le worker, qui est dans un autre thread.
#
# Pas d'UI ici : le CP4 la posera par-dessus ces quatre routes.
# ===========================================================================

# --- Catalogue d'erreurs ---------------------------------------------------
# Recopie LITTÉRALE de ``cv_forge.ErrorCode``, et non un import : le chemin
# d'erreur ne doit pas dépendre du chargement d'un paquet tiers — un
# ``ImportError`` pendant qu'on formate une erreur rendrait le 500 nu que
# tout ceci existe pour empêcher. ``test_le_catalogue_suit_cv_forge``
# compare les deux ensembles et tombe si cv_forge en ajoute un.
CODES_CV_FORGE = frozenset({
    "OFFER_NOT_FOUND", "MASTER_INVALID", "OLLAMA_UNAVAILABLE",
    "EXTRACTION_FAILED", "MATCHING_EMPTY", "TYPST_FAILED", "INTERNAL_ERROR",
})

# Codes PROPRES à stage_finder : cv_forge ne peut structurellement pas les
# produire, parce qu'ils décrivent des états qui n'existent qu'en amont de
# ``generate_cv``.
#
#   TEXT_MISSING    une ``OfferInput`` porte toujours son texte ; c'est
#                   stage_finder qui doit d'abord le trouver en base.
#   JOB_NOT_FOUND   cv_forge ne connaît pas la notion de job — la file est
#                   entièrement à nous.
#   PDF_UNAVAILABLE « il n'y a pas de PDF à te servir ». Quatre causes (job
#                   inachevé, aucun chemin enregistré, fichier disparu du
#                   disque, chemin hors de la racine de sortie) pour une
#                   seule situation côté appelant ; c'est le MESSAGE qui
#                   nomme la cause, pas le code. Garder le catalogue court
#                   et fermé vaut mieux qu'un code par nuance.
CODES_STAGE_FINDER = frozenset({"TEXT_MISSING", "JOB_NOT_FOUND", "PDF_UNAVAILABLE"})

CATALOGUE_ERREURS = CODES_CV_FORGE | CODES_STAGE_FINDER


def _erreur(code: str, message: str, http: int):
    """Réponse d'erreur JSON. **Toujours** porteuse d'un code du catalogue.

    Un code hors catalogue est un bug d'appelant : on le journalise et on
    rend ``INTERNAL_ERROR`` plutôt que de laisser fuiter un code que le
    client ne saura pas interpréter.
    """
    if code not in CATALOGUE_ERREURS:
        logger.error("Code d'erreur hors catalogue : %r (message : %s)", code, message)
        code = "INTERNAL_ERROR"
    return jsonify({"error_code": code, "error_message": message}), http


def _json_api(vue):
    """Garantit qu'une vue ne rend JAMAIS un 500 nu.

    ``generate_cv`` promet de ne pas lever ; le reste du chemin (SQLite,
    lecture de fichier, sérialisation) n'a fait aucune promesse. Sans ce
    filet, une ``sqlite3.OperationalError`` sortirait en page HTML de
    Werkzeug, que le front ne sait pas lire — il afficherait « erreur »
    sans code ni message exploitable.

    Décoré vue par vue, et non posé en ``errorhandler`` global : le
    comportement des routes existantes doit rester rigoureusement
    inchangé.
    """
    @functools.wraps(vue)
    def enveloppe(*args, **kwargs):
        try:
            return vue(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - c'est précisément le propos
            logger.exception("Erreur non prévue dans %s.", vue.__name__)
            return _erreur("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", 500)
    return enveloppe


# --- Connexion SQLite, une par requête -------------------------------------
def _conn():
    """Connexion à la base pour la requête en cours.

    Une par requête, et non une partagée : un objet ``sqlite3.Connection``
    n'est pas fait pour traverser les threads, et Flask sert en mode
    ``threaded=True``. Le worker a la sienne, de son côté — c'est ce que
    le mode WAL posé par ``storage.ouvrir`` rend indolore.
    """
    if "conn_cv" not in g:
        g.conn_cv = storage.ouvrir(config.CHEMIN_BASE)
        jobs.ensure_schema(g.conn_cv)
    return g.conn_cv


@app.teardown_appcontext
def _fermer_conn_cv(_exception=None) -> None:
    conn = g.pop("conn_cv", None)
    if conn is not None:
        conn.close()


# --- Sérialisation d'un job ------------------------------------------------
def _vue_job(conn, job: dict) -> dict:
    """Représentation publique d'un job. Ne touche JAMAIS la table `offres`.

    C'est ce qui rend la lecture insensible aux orphelins : un job dont
    l'offre a été fusionnée ou purgée se lit exactement comme les autres.
    Il n'y a pas de clé étrangère (cf. le commentaire de `jobs.py`), donc
    pas de jointure à faire — et surtout aucune à faire par mégarde.
    """
    termine = job["status"] == "done" and job["pdf_path"]
    return {
        "job_id": job["id"],
        "offer_id": job["offer_id"],
        "status": job["status"],
        # `None` dès que le job n'est plus `pending` : il n'attend plus.
        "position": jobs.position(conn, job["id"]),
        # Le lien reflète ce que dit la LIGNE, pas le disque : on ne va pas
        # stat() un fichier à chaque tour de polling. Si le PDF a disparu,
        # c'est `download` qui le dira, avec son code.
        "pdf_url": url_for("api_cv_telecharger", job_id=job["id"]) if termine else None,
        "error_code": job["error_code"],
        "error_message": job["error"],
        "attempts": job["attempts"],
    }


# --- Le PDF est-il servable ? ----------------------------------------------
def _resoudre_pdf(pdf_path: str) -> tuple[Path | None, str | None]:
    """Rend ``(chemin, motif_de_refus)``. Le refus est ``None`` si tout va bien.

    Le chemin vient de la BASE, donc d'un worker, donc a priori de nous.
    On le vérifie quand même contre la racine de sortie : « a priori de
    nous » n'est pas une garantie, et servir un fichier arbitraire du
    disque parce qu'une ligne de la base le désigne est exactement la
    faille qu'on ne veut pas offrir à un front qui, au CP4, passera des
    identifiants de job venus de l'utilisateur.

    ``resolve()`` des DEUX côtés avant comparaison : sans lui, un `..`
    ou un lien symbolique passerait la comparaison textuelle tout en
    désignant un fichier hors racine.
    """
    racine = Path(config.CV_OUT_ROOT).resolve()
    chemin = Path(pdf_path).resolve()
    if not chemin.is_relative_to(racine):
        return None, "hors_racine"
    if not chemin.is_file():
        return None, "absent"
    return chemin, None


# --- Routes ----------------------------------------------------------------
@app.post("/api/offers/<offer_id>/cv")
@_json_api
def api_cv_demander(offer_id: str):
    """Demande un CV pour une offre. Rend 202 + le job (créé ou réutilisé).

    Idempotent par ``offer_hash`` : deux clics, ou un rafraîchissement de
    page, rendent le MÊME job. ``reutilise`` dit lequel des deux cas s'est
    produit — le front en a besoin pour ne pas annoncer « lancé ! » sur une
    génération qui date d'hier.

    Aucun job n'est créé si l'offre n'est pas générable : un job qu'on sait
    d'avance voué à échouer ne fait qu'occuper la file et bruiter
    l'historique. `TEXT_MISSING` est donc refusé ICI, alors que le worker
    sait aussi le produire — pour l'offre dont le texte disparaît entre
    l'enfilement et le traitement.
    """
    conn = _conn()
    offre = conn.execute(
        "SELECT cle, title FROM offres WHERE cle = ?", (offer_id,)
    ).fetchone()
    if offre is None:
        # Même code que celui du worker quand l'offre disparaît en cours de
        # route : un seul vocabulaire pour une seule situation, vue de deux
        # endroits. Cf. `worker._traiter_un`.
        return _erreur("OFFER_NOT_FOUND",
                       f"aucune offre ne porte la clé « {offer_id} ».", 404)

    texte = jobs.lire_texte(conn, offer_id)
    if not texte:
        return _erreur(
            "TEXT_MISSING",
            f"le texte de l'offre « {offre['title']} » n'est pas en base : il "
            f"n'y a rien à envoyer au modèle. Récupérez-le d'abord "
            f"(python backfill_textes.py --apply).",
            422,
        )

    master = Path(config.CV_MASTER_PATH)
    if not master.is_file():
        # `MASTER_INVALID` et non `INTERNAL_ERROR`, contrairement à ce que
        # fait cv_forge pour un master illisible. Le cas n'est pas le même :
        # ici on n'a rien tenté, on CONSTATE que `config.CV_MASTER_PATH` ne
        # désigne aucun fichier. C'est un défaut de configuration, dont
        # l'utilisateur peut faire quelque chose. « Erreur interne »
        # l'enverrait lire une pile pour une ligne de `config.py`.
        return _erreur("MASTER_INVALID",
                       f"master introuvable : {master}. Vérifiez "
                       f"`CV_MASTER_PATH` dans config.py.", 503)

    from cv_forge import ForgeConfig

    empreinte = jobs.calculer_hash(texte, master_path=master, config=ForgeConfig())
    job, reutilise = jobs.enfiler(conn, offer_id, empreinte)
    return jsonify({**_vue_job(conn, job), "reutilise": reutilise}), 202


@app.get("/api/jobs/<int:job_id>")
@_json_api
def api_cv_job(job_id: int):
    """État d'un job. Ne lève jamais, même si l'offre a disparu."""
    job = jobs.lire(_conn(), job_id)
    if job is None:
        return _erreur("JOB_NOT_FOUND", f"aucun job n°{job_id}.", 404)
    return jsonify(_vue_job(_conn(), job))


@app.get("/api/offers/<offer_id>/cv/latest")
@_json_api
def api_cv_dernier(offer_id: str):
    """Dernière génération connue pour une offre, quel que soit son statut.

    C'est ce que la liste d'offres du CP4 interrogera au chargement, pour
    savoir s'il faut afficher « Générer », « en cours » ou « Télécharger ».

    404 quand l'offre n'a jamais rien produit — y compris quand l'offre
    elle-même n'existe pas : les deux se répondent pareil, et surtout
    aucune des deux ne lève. On ne consulte pas `offres` du tout.
    """
    job = jobs.dernier_pour_offre(_conn(), offer_id)
    if job is None:
        return _erreur("JOB_NOT_FOUND",
                       f"aucune génération connue pour l'offre « {offer_id} ».", 404)
    return jsonify(_vue_job(_conn(), job))


@app.get("/api/cv/states")
@_json_api
def api_cv_etats():
    """État de génération de TOUTES les offres, en une seule réponse.

    Le CP3 n'exposait que la lecture unitaire (``/cv/latest``). Avec 294
    offres à l'écran, la liste aurait fait 294 requêtes au chargement puis
    294 par tour de polling. Cette route est l'ajout que le CP4 réclamait.

    Sous ``/api/cv/`` et non ``/api/offers/cv/`` : ce second chemin a le
    même nombre de segments que ``/api/offers/<offer_id>/cv`` et n'aurait
    tenu que par la valeur littérale du dernier segment. Une route dont la
    correction dépend de ce qu'un identifiant d'offre ne vaudra jamais
    « cv » est une route qui attend son incident.

    Ne sont renvoyées que les offres dont l'état N'EST PAS ``idle`` :
    l'absence d'entrée VEUT DIRE ``idle``. Sur 294 offres dont la plupart
    n'ont jamais été générées, envoyer les 294 gonflerait la réponse pour
    n'apprendre que du vide — et ``/api/etat`` a déjà montré qu'une charge
    JSON trop grosse fait couper Werkzeug sous Windows.
    """
    conn = _conn()
    derniers = jobs.derniers_par_offre(conn)
    positions = jobs.positions_pending(conn)
    avec_texte = jobs.cles_avec_texte(conn)

    etats: dict[str, dict] = {}
    for offer_id, job in derniers.items():
        etats[offer_id] = {
            "job_id": job["id"],
            "status": job["status"],
            "position": positions.get(job["id"]),
            "pdf_url": (url_for("api_cv_telecharger", job_id=job["id"])
                        if job["status"] == "done" and job["pdf_path"] else None),
            "error_code": job["error_code"],
            "error_message": job["error"],
            "attempts": job["attempts"],
        }

    # `text_missing` n'est pas un statut de job — aucun job n'existe dans
    # ce cas, le POST le refuse. C'est un état de l'OFFRE, calculé ici.
    # Un job existant l'emporte : il décrit quelque chose qui a vraiment
    # eu lieu, alors que l'absence de texte est une disponibilité.
    for ligne in conn.execute("SELECT cle FROM offres"):
        cle = ligne["cle"]
        if cle not in etats and cle not in avec_texte:
            etats[cle] = {"job_id": None, "status": "text_missing",
                          "position": None, "pdf_url": None,
                          "error_code": "TEXT_MISSING", "error_message": None,
                          "attempts": 0}

    return jsonify({"etats": etats})


@app.get("/api/jobs/<int:job_id>/download")
@_json_api
def api_cv_telecharger(job_id: int):
    """Sert le PDF d'un job terminé, en pièce jointe."""
    job = jobs.lire(_conn(), job_id)
    if job is None:
        return _erreur("JOB_NOT_FOUND", f"aucun job n°{job_id}.", 404)

    if job["status"] == "failed":
        # On RÉPERCUTE le code du job plutôt que d'en inventer un : celui
        # qui demande le PDF d'une génération ratée veut savoir pourquoi
        # elle a raté, pas s'entendre dire qu'il n'y a pas de fichier.
        return _erreur(job["error_code"] or "INTERNAL_ERROR",
                       job["error"] or "génération en échec, sans message.", 409)
    if job["status"] != "done":
        return _erreur("PDF_UNAVAILABLE",
                       f"génération {job['status']} : le PDF n'existe pas encore.",
                       409)
    if not job["pdf_path"]:
        return _erreur("PDF_UNAVAILABLE",
                       "job terminé mais aucun chemin de PDF enregistré.", 404)

    chemin, refus = _resoudre_pdf(job["pdf_path"])
    if refus == "hors_racine":
        logger.warning("Job %s : chemin de PDF hors de la racine de sortie (%s). "
                       "Refus de servir.", job_id, job["pdf_path"])
        return _erreur("PDF_UNAVAILABLE",
                       "le chemin enregistré sort de la racine des CV produits : "
                       "refus de servir ce fichier.", 403)
    if refus == "absent":
        # État RÉEL et pas une impossibilité : le dossier de sortie a pu
        # être vidé, ou le PDF déplacé, longtemps après la génération.
        return _erreur("PDF_UNAVAILABLE",
                       f"le job est terminé mais le fichier a disparu du disque "
                       f"({job['pdf_path']}). Relancez une génération.", 404)

    return send_file(chemin, as_attachment=True, download_name=chemin.name,
                     mimetype="application/pdf")


# ---------------------------------------------------------------------------
# Page (HTML + CSS + JS en un seul fichier autonome)
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stage Finder — tri IA local</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 1.5rem; background: #f6f7f9; color: #1a1a1a; }
  h1 { margin: 0 0 .2rem; font-size: 1.4rem; }
  .meta { color: #666; font-size: .85rem; margin-bottom: 1rem; }
  .ref { background: #fff; border-left: 4px solid #1a9850; padding: .6rem .9rem;
         border-radius: 6px; margin-bottom: 1rem; font-size: .9rem; }
  .barre-outils { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center;
                  margin-bottom: 1rem; }
  .barre-outils input[type=number] { width: 4.5rem; padding: .4rem; border: 1px solid #ccc;
         border-radius: 6px; font-size: .9rem; }
  button { padding: .45rem .8rem; border: 0; border-radius: 6px; font-size: .88rem;
           font-weight: 600; cursor: pointer; background: #1d4ed8; color: #fff; }
  button.sec { background: #e5e7eb; color: #222; }
  button.pri { background: linear-gradient(90deg,#7c3aed,#db2777); color: #fff;
               padding: .55rem 1rem; font-size: .92rem; box-shadow: 0 1px 4px rgba(124,58,237,.35); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .filtre input { padding: .45rem .7rem; border: 1px solid #ccc; border-radius: 6px;
                  min-width: 240px; font-size: .9rem; }
  .chk { font-size: .84rem; color: #444; display: flex; align-items: center; gap: .3rem;
         cursor: pointer; }
  .prog { flex: 1 1 220px; min-width: 200px; }
  .prog .bar { background: #e5e7eb; border-radius: 999px; height: 10px; overflow: hidden; }
  .prog .bar span { display: block; height: 100%; background: linear-gradient(90deg,#7c3aed,#db2777);
                    width: 0; transition: width .3s; }
  .prog .txt { font-size: .78rem; color: #555; margin-top: .2rem; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  th, td { padding: .5rem .6rem; text-align: left; border-bottom: 1px solid #eee;
           font-size: .88rem; vertical-align: top; }
  th { background: #1f2937; color: #fff; position: sticky; top: 0; font-weight: 600;
       cursor: pointer; user-select: none; white-space: nowrap; }
  th.actif { background: #0f766e; }
  .rk { text-align: center; font-weight: 700; width: 2.6rem; }
  .cos { color: #888; }
  .ia  { color: #6d28d9; }
  .delta-up { color: #16a34a; font-weight: 700; }
  .delta-down { color: #dc2626; font-weight: 700; }
  .delta-zero { color: #9ca3af; }
  .delta-new { background: #16a34a; color: #fff; padding: .05rem .35rem; border-radius: 999px;
               font-size: .68rem; font-weight: 700; }
  a.titre { color: #1d4ed8; text-decoration: none; font-weight: 600; }
  a.titre:hover { text-decoration: underline; }
  .badges { margin-top: .25rem; display: flex; gap: .25rem; flex-wrap: wrap; }
  .badge { background: #e0edff; color: #1d4ed8; padding: .05rem .45rem; border-radius: 999px;
           font-size: .68rem; font-weight: 600; }
  .badge-combo { background: linear-gradient(90deg,#7c3aed,#db2777); color: #fff;
                 padding: .05rem .5rem; border-radius: 999px; font-size: .68rem; font-weight: 700; }
  .llm-score { background: #ede9fe; color: #6d28d9; padding: .05rem .4rem; border-radius: 4px;
               font-size: .72rem; font-weight: 700; }
  .llm-ko { background: #fee2e2; color: #b91c1c; padding: .05rem .4rem; border-radius: 4px;
            font-size: .68rem; font-weight: 600; }
  .drapeau { display: inline-block; background: #fff7ed; color: #9a3412; padding: .05rem .4rem;
             border-radius: 4px; font-size: .68rem; margin: .15rem .15rem 0 0; }
  /* Bouton de dépliage de l'analyse IA (dans la ligne de l'offre). */
  .btn-analyse { background: #ede9fe; color: #6d28d9; font-size: .7rem; font-weight: 700;
                 padding: .1rem .5rem; border-radius: 999px; margin-top: .3rem; }
  /* Panneau d'analyse : ligne PLEINE LARGEUR sous l'offre. Le paragraphe du LLM
     y est lisible en entier — dans la cellule étroite d'origine, il était
     illisible et rogné. */
  tr.analyse > td { background: #faf5ff; border-bottom: 2px solid #e9d5ff;
                    padding: .8rem 1.2rem 1rem 3.4rem; }
  .an-titre { font-size: .74rem; font-weight: 700; color: #6d28d9; text-transform: uppercase;
              letter-spacing: .04em; margin-bottom: .45rem; }
  .an-meta { font-weight: 500; color: #7c6a94; text-transform: none; letter-spacing: 0;
             margin-left: .5rem; }
  .an-texte { margin: 0; font-size: .95rem; line-height: 1.65; color: #262626; max-width: 78ch;
              white-space: pre-wrap; }
  .an-drapeaux { margin-top: .65rem; font-size: .85rem; color: #9a3412; }
  .an-drapeaux ul { margin: .25rem 0 0; padding-left: 1.2rem; line-height: 1.55; }
  .an-vide { color: #888; font-size: .85rem; font-style: italic; }
  .src { background: #eef; color: #334; padding: .05rem .4rem; border-radius: 4px; font-size: .72rem; }
  .vide { text-align: center; color: #999; padding: 2rem; }
  /* --- Colonne « CV » (CP4) -------------------------------------------- */
  td.cv { white-space: nowrap; }
  .cv button { font-size: .72rem; padding: .22rem .55rem; font-weight: 700; }
  .cv-generer   { background: #1d4ed8; color: #fff; }
  .cv-attente   { background: #e5e7eb; color: #4b5563; }
  .cv-encours   { background: #ede9fe; color: #6d28d9; }
  .cv-pret      { background: #16a34a; color: #fff; }
  .cv-echec     { background: #fee2e2; color: #b91c1c; }
  .cv-sans-texte{ background: #f3f4f6; color: #9ca3af; }
  .cv-second    { background: transparent; color: #6b7280; text-decoration: underline;
                  font-weight: 600; padding: .2rem .3rem; }
  .cv-msg { display: block; margin-top: .25rem; font-size: .7rem; color: #b91c1c;
            max-width: 15rem; white-space: normal; line-height: 1.35; }
  /* Point clignotant pendant la génération : un CV prend plusieurs minutes,
     il faut que la page dise qu'elle n'est pas figée. */
  .cv-pouls::before { content: "●"; margin-right: .3rem; animation: pouls 1.2s infinite; }
  @keyframes pouls { 0%,100% { opacity: 1 } 50% { opacity: .25 } }
  /* --- Panneau « état du marché » ------------------------------------- */
  .marche { background: #fff; border-radius: 8px; padding: .8rem 1rem; margin-bottom: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .marche-tete { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
  .marche-sous { color: #777; font-size: .8rem; flex: 1; }
  .marche-verdict { margin-top: .7rem; padding: .6rem .8rem; border-radius: 6px;
                    font-size: .92rem; line-height: 1.5; }
  .t-confortable { background: #ecfdf5; border-left: 4px solid #16a34a; }
  .t-correct     { background: #fffbeb; border-left: 4px solid #d97706; }
  .t-étroit      { background: #fef2f2; border-left: 4px solid #dc2626; }
  .t-inconnu     { background: #f3f4f6; border-left: 4px solid #9ca3af; }
  .marche-grille { display: flex; gap: 1.2rem; flex-wrap: wrap; margin-top: .8rem; }
  .marche-grille > div { flex: 1 1 320px; }
  table.mini { width: 100%; border-collapse: collapse; background: transparent; box-shadow: none; }
  table.mini th, table.mini td { padding: .3rem .45rem; font-size: .82rem; border-bottom: 1px solid #f0f0f0; }
  table.mini th { background: #f3f4f6; color: #374151; position: static; cursor: default;
                  font-weight: 600; }
  table.mini td.n { text-align: right; font-variant-numeric: tabular-nums; }
  .hausse { color: #16a34a; font-weight: 700; }
  .baisse { color: #dc2626; font-weight: 700; }
  .note { color: #777; font-size: .76rem; margin-top: .5rem; line-height: 1.5; }
  .pill { font-size: .72rem; padding: .1rem .5rem; border-radius: 999px; background: #ede9fe; color: #6d28d9; }
</style>
</head>
<body>
  <h1>🎯 Stage Finder <span class="pill" id="modele">—</span></h1>
  <div class="meta" id="meta">Chargement…</div>
  <div class="ref" id="ref"></div>

  <div class="barre-outils">
    <button id="btn-tout" class="pri" onclick="toutLancer()"
            title="Enchaîne automatiquement la recherche sur toutes les sources PUIS la vérification IA. Rien d'autre à cliquer.">
      🚀 Tout lancer (recherche + IA)</button>
    <label>Vérifier les <input type="number" id="n" min="1" value="10"> premières</label>
    <button id="btn-verif" class="sec" onclick="lancerVerif()">🔎 Vérification IA seule</button>
    <button id="btn-refresh" class="sec" onclick="rafraichir()">🔄 Recherche seule</button>
    <label class="chk" title="Interroge seulement les API rapides (Adzuna, Jooble, France Travail, Careerjet, Free-Work) sans scraper LinkedIn/Indeed : ~1 min au lieu de ~5, mais moins d'offres.">
      <input type="checkbox" id="nojobspy"> rapide (sans JobSpy)</label>
    <span class="filtre"><input id="q" placeholder="🔎 filtrer (titre, entreprise…)" oninput="rendre()"></span>
    <label class="chk" title="Déplie l'analyse écrite par le LLM sous chaque offre vérifiée.">
      <input type="checkbox" id="tout-deplier" onchange="basculerTout(this.checked)"> déplier les analyses IA</label>
  </div>

  <div class="marche" id="marche">
    <div class="marche-tete">
      <strong>📊 État du marché</strong>
      <span class="marche-sous" id="marche-sous">stages en Île-de-France — côté offre</span>
      <button class="sec" id="btn-marche" onclick="chargerMarche(true)">Analyser</button>
    </div>
    <div id="marche-corps"></div>
  </div>

  <div class="barre-outils">
    <div class="prog" id="prog" style="display:none">
      <div class="bar"><span id="prog-bar"></span></div>
      <div class="txt" id="prog-txt"></div>
    </div>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th class="rk" data-tri="cos" onclick="trier('cos')" title="Rang avant IA (cosinus)">cos ▾</th>
        <th class="rk" data-tri="ia"  onclick="trier('ia')"  title="Rang après IA">IA</th>
        <th class="rk" title="Mouvement de rang (avant → après IA)">Δ</th>
        <th>Offre</th>
        <th>Entreprise</th>
        <th>Lieu</th>
        <th>Durée</th>
        <th>Début</th>
        <th>Source</th>
        <th title="Génère un CV adapté à cette offre avec ton master.yaml">CV</th>
      </tr>
    </thead>
    <tbody id="corps"><tr><td colspan="10" class="vide">Chargement…</td></tr></tbody>
  </table>

<script src="/static/cv_etats.js"></script>
<script>
let ETAT = null;
let TRI = "cos";            // "cos" ou "ia"
let sonde = null;           // timer de polling
// URLs des offres dont l'analyse IA est dépliée. Indexé par URL (et pas par
// rang) pour survivre à un re-tri ou à un rafraîchissement du classement.
const DEPLIES = new Set();

function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function charger(essais){
  essais = essais == null ? 4 : essais;
  try {
    const r = await fetch("/api/etat", {cache: "no-store"});
    if (!r.ok) throw new Error("http "+r.status);
    ETAT = await r.json();
    rendre();
    gererPolling();
  } catch(e){
    // Werkzeug (serveur de dev) coupe parfois une connexion sous Windows :
    // on retente quelques fois avant d'abandonner, pour ne pas rester bloqué.
    if (essais > 0) setTimeout(() => charger(essais-1), 700);
  }
}

function gererPolling(){
  const actif = ETAT && (ETAT.verif.en_cours || ETAT.collecte.en_cours
                         || (ETAT.auto && ETAT.auto.en_cours));
  majControles(actif);
  majProgression();
  if (actif && !sonde){ sonde = setInterval(charger, 1500); }
  if (!actif && sonde){ clearInterval(sonde); sonde = null; }
}

function majControles(actif){
  document.getElementById("btn-tout").disabled = actif;
  document.getElementById("btn-verif").disabled = actif || !ETAT || !ETAT.n_total;
  document.getElementById("btn-refresh").disabled = actif;
  document.getElementById("n").disabled = actif;
}

function majProgression(){
  const box = document.getElementById("prog");
  const bar = document.getElementById("prog-bar");
  const txt = document.getElementById("prog-txt");
  // Pendant l'enchaînement automatique, on annonce l'étape en cours : sinon on
  // croit la tâche finie quand la collecte se termine, alors que l'IA démarre.
  const auto = ETAT && ETAT.auto && ETAT.auto.en_cours;
  const etape = auto ? `[étape ${ETAT.auto.etape}/2] ` : "";
  if (ETAT && ETAT.collecte.en_cours){
    box.style.display = "block"; bar.style.width = "100%";
    bar.parentElement.style.opacity = ".6";
    txt.textContent = etape + "🔄 Recherche en cours (collecte des sources + ranking)… "
                    + "ça prend quelques minutes"
                    + (auto ? ", la vérification IA suivra automatiquement." : ".");
    return;
  }
  bar.parentElement.style.opacity = "1";
  if (ETAT && ETAT.verif.en_cours){
    const v = ETAT.verif;
    const pct = v.total ? Math.round(100*v.fait/v.total) : 0;
    box.style.display = "block"; bar.style.width = pct + "%";
    if (v.indisponible){
      txt.textContent = "⚠️ Ollama injoignable — démarre `ollama serve`. Classement cosinus conservé.";
    } else {
      txt.textContent = `${etape}🔎 Vérification IA ${v.fait}/${v.total} (appels ${v.appels}, cache ${v.cache}) — modèle ${v.modele}`;
    }
    return;
  }
  box.style.display = "none";
}

function trier(mode){
  if (mode === "ia" && (!ETAT || !ETAT.offres.some(o => o.ia_rang))) return;
  TRI = mode;
  document.querySelectorAll("th[data-tri]").forEach(th => {
    th.classList.toggle("actif", th.dataset.tri === mode);
    th.textContent = th.dataset.tri + (th.dataset.tri === mode ? " ▾" : "");
  });
  rendre();
}

function celluleDelta(o){
  if (o.ia_rang == null) return '<span class="delta-zero">—</span>';
  const d = o.cos_rang - o.ia_rang;   // >0 = monte
  if (d > 0) return `<span class="delta-up">▲ ${d}</span>`;
  if (d < 0) return `<span class="delta-down">▼ ${-d}</span>`;
  return '<span class="delta-zero">=</span>';
}

// --- Verdict IA : résumé compact dans la ligne, texte complet dans un panneau --
// Le paragraphe du LLM ne tient pas dans une cellule de tableau (on n'en voyait
// qu'un bout). On sépare donc : ici les pastilles, plus bas le panneau déplié
// sur toute la largeur.
function verdictResume(o){
  const v = o.verdict;
  if (!v) return "";
  let h = `<span class="llm-score">LLM ${v.score.toFixed(2)}</span>`;
  if (!v.pertinent) h += ' <span class="llm-ko">non pertinent</span>';
  if (v.est_alternance) h += ' <span class="llm-ko">alternance</span>';
  if (v.domaine_match === false) h += ' <span class="llm-ko">hors domaine</span>';
  if (v.niveau && v.niveau !== "stage" && v.niveau !== "inconnu") h += ` <span class="llm-ko">${esc(v.niveau)}</span>`;
  const nd = (v.drapeaux_rouges||[]).length;
  if (nd) h += ` <span class="drapeau">🚩 ${nd} point${nd>1?'s':''} de vigilance</span>`;
  const ouvert = DEPLIES.has(o.url);
  h += ` <button class="btn-analyse" onclick="basculer('${encodeURIComponent(o.url)}')">`
     + `${ouvert ? '▾ masquer' : '▸ lire'} l'analyse IA</button>`;
  return `<div class="badges">${h}</div>`;
}

function panneauAnalyse(o){
  const v = o.verdict;
  if (!v || !DEPLIES.has(o.url)) return "";
  const meta = [
    `score IA ${v.score.toFixed(2)}`,
    v.niveau ? `niveau : ${esc(v.niveau)}` : "",
    v.duree_mois ? `durée annoncée : ${v.duree_mois} mois` : "",
    v.date_debut ? `début : ${esc(v.date_debut)}` : "",
  ].filter(Boolean).join(" · ");
  const texte = v.justification
    ? `<p class="an-texte">${esc(v.justification)}</p>`
    : `<p class="an-vide">Le modèle n'a pas rédigé de justification pour cette offre.</p>`;
  const drapeaux = (v.drapeaux_rouges||[]).length
    ? `<div class="an-drapeaux"><strong>🚩 Points de vigilance</strong><ul>`
      + v.drapeaux_rouges.map(d => `<li>${esc(d)}</li>`).join("") + `</ul></div>`
    : "";
  return `<tr class="analyse"><td colspan="10">
    <div class="an-titre">🤖 Analyse du LLM<span class="an-meta">${meta}</span></div>
    ${texte}${drapeaux}
  </td></tr>`;
}

function basculer(urlEncodee){
  const url = decodeURIComponent(urlEncodee);
  DEPLIES.has(url) ? DEPLIES.delete(url) : DEPLIES.add(url);
  rendre();
}

function basculerTout(ouvrir){
  DEPLIES.clear();
  if (ouvrir && ETAT) ETAT.offres.filter(o => o.verdict).forEach(o => DEPLIES.add(o.url));
  rendre();
}

// ===========================================================================
// Génération de CV (CP4) — colonne « CV »
//
// La DÉCISION (quel bouton pour quel état) vit dans /static/cv_etats.js,
// testé par `node --test`. Ici il ne reste que le rendu et le réseau : une
// machine à états enfermée dans cette chaîne Python ne serait pas testable.
// ===========================================================================
let ETATS_CV = {};            // cle d'offre -> entrée de /api/cv/states
let sondeCV = null;           // handle du setTimeout récursif (UN SEUL cycle)
const cvEnVol = new Set();    // POST en cours : verrou anti double-clic
const DELAI_CV = 1500;

function etatCV(cle){
  try { return CvEtats.etatDepuis(ETATS_CV[cle]); }
  catch (err){ console.error("CV: état illisible pour", cle, err); return "idle"; }
}

function celluleCV(o){
  let vue;
  try { vue = CvEtats.vueBouton(etatCV(o.cle), ETATS_CV[o.cle] || {}); }
  catch (err){
    // `vueBouton` lève sur un état non géré — c'est voulu, ça signale un
    // bug. Mais une ligne fautive ne doit pas vider le tableau entier.
    console.error("CV:", err);
    return '<td class="cv"><span class="cv-msg">état inattendu</span></td>';
  }
  const cle = encodeURIComponent(o.cle);
  const pouls = vue.occupe ? " cv-pouls" : "";
  let h = `<button class="${vue.classe}${pouls}"${vue.actif?"":" disabled"}`
        + ` title="${esc(vue.titre)}"`
        + ` onclick="actionCV('${cle}','${vue.action}')">${esc(vue.libelle)}</button>`;
  if (vue.secondaire)
    h += ` <button class="cv-second" title="Relancer une génération"`
       + ` onclick="actionCV('${cle}','${vue.secondaire.action}')">`
       + `${esc(vue.secondaire.libelle)}</button>`;
  if (vue.message) h += `<span class="cv-msg">${esc(vue.message)}</span>`;
  // `data-cle` : `rendre()` remplace tout le innerHTML, donc une référence
  // DOM gardée d'un rendu à l'autre pointe un nœud détaché. C'est le seul
  // moyen de retrouver la cellule d'une offre après un re-rendu — pour
  // l'inspection comme pour le débogage.
  return `<td class="cv" data-cle="${esc(o.cle)}">${h}</td>`;
}

function actionCV(cleEncodee, action){
  const cle = decodeURIComponent(cleEncodee);
  if (action === "telecharger"){
    const entree = ETATS_CV[cle];
    if (entree && entree.pdf_url) window.location.href = entree.pdf_url;
    return;
  }
  if (action === "generer") genererCV(cle);
}

async function genererCV(cle){
  // Le bouton est déjà désactivé par le rendu optimiste, mais un double
  // clic peut passer AVANT le repaint. Le POST est idempotent de toute
  // façon : ce verrou évite juste deux requêtes pour rien.
  if (cvEnVol.has(cle)) return;
  cvEnVol.add(cle);

  const avant = ETATS_CV[cle];           // mémorisé pour le rollback
  ETATS_CV[cle] = {job_id: null, status: "pending", position: null,
                   pdf_url: null, error_code: null, error_message: null,
                   attempts: 0};
  rendre();                              // optimiste : le bouton se fige tout de suite

  const rollback = () => {
    if (avant === undefined) delete ETATS_CV[cle]; else ETATS_CV[cle] = avant;
    rendre();
  };

  try {
    const r = await fetch(`/api/offers/${encodeURIComponent(cle)}/cv`,
                          {method: "POST", headers: {"Content-Type": "application/json"}});
    const d = await r.json();
    if (!r.ok){ rollback(); alert(CvEtats.messageErreur(d.error_code)); return; }
    ETATS_CV[cle] = {job_id: d.job_id, status: d.status, position: d.position,
                     pdf_url: d.pdf_url, error_code: d.error_code,
                     error_message: d.error_message, attempts: d.attempts};
    rendre();
  } catch (err){
    rollback();
    alert("Le serveur n'a pas répondu. Réessaie.");
  } finally {
    cvEnVol.delete(cle);
  }
}

async function chargerEtatsCV(){
  try {
    const r = await fetch("/api/cv/states", {cache: "no-store"});
    if (!r.ok) return;
    ETATS_CV = (await r.json()).etats || {};
  } catch (err){ /* réseau coupé : on garde le dernier état connu */ }
}

// --- Polling : UN seul cycle, récursif, et seulement s'il y a de quoi -----
// `setTimeout` récursif et jamais `setInterval` : avec un intervalle, un
// tour lent s'empile sur le suivant et la page finit par lancer plusieurs
// requêtes en parallèle sur une base que le worker écrit déjà.
function arreterSondeCV(){
  if (sondeCV){ clearTimeout(sondeCV); sondeCV = null; }
}

function relancerSondeCV(){
  arreterSondeCV();                       // idempotent : jamais deux cycles
  // Sur les offres RÉELLEMENT AFFICHÉES : un filtre qui masque la seule
  // génération en cours doit arrêter le polling.
  const visibles = (window.__offresAffichees || []).map(o => etatCV(o.cle));
  if (!CvEtats.doitSonder(visibles)) return;
  sondeCV = setTimeout(async () => {
    sondeCV = null;
    await chargerEtatsCV();
    rendre();                             // ré-arme via la fin de `rendre`
  }, DELAI_CV);
}

window.addEventListener("beforeunload", arreterSondeCV);

function ligne(o){
  const badges = (o.tags||[]).map(t =>
     `<span class="${t.includes('IA+CYBER')?'badge-combo':'badge'}">${esc(t)}</span>`).join(" ");
  return `<tr>
    <td class="rk cos">${o.cos_rang}</td>
    <td class="rk ia">${o.ia_rang!=null?o.ia_rang:'<span class="delta-zero">—</span>'}</td>
    <td class="rk">${celluleDelta(o)}</td>
    <td>
      <a class="titre" href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.title)}</a>
      ${badges?`<div class="badges">${badges}</div>`:""}
      ${verdictResume(o)}
    </td>
    <td>${esc(o.company)||'—'}</td>
    <td>${esc(o.location)||'—'}</td>
    <td>${o.duree_mois?o.duree_mois+' mois':'—'}</td>
    <td>${esc(o.date_debut)||'—'}</td>
    <td><span class="src">${esc(o.source)}</span></td>
    ${celluleCV(o)}
  </tr>` + panneauAnalyse(o);
}

function rendre(){
  if (!ETAT) return;
  document.getElementById("modele").textContent = ETAT.modele;
  document.getElementById("ref").innerHTML = "<strong>Profil recherché :</strong> " + esc(ETAT.profil);
  const gen = ETAT.genere_le ? new Date(ETAT.genere_le).toLocaleString('fr-FR') : "jamais";
  const nVerif = ETAT.offres.filter(o => o.verdict).length;
  document.getElementById("meta").textContent =
     `${ETAT.n_total} offre(s) · classement généré le ${gen} · ${nVerif} vérifiée(s) par l'IA`
     + ` · sources : ${(ETAT.sources||[]).join(", ")}`;
  const nInput = document.getElementById("n");
  nInput.max = ETAT.n_total || 1;
  if (!nInput.dataset.touched) nInput.value = Math.min(ETAT.top_n_defaut||10, ETAT.n_total||10);

  const q = (document.getElementById("q").value||"").toLowerCase();
  let offres = ETAT.offres.slice();
  if (TRI === "ia") offres.sort((a,b) => (a.ia_rang||1e9) - (b.ia_rang||1e9) || a.cos_rang - b.cos_rang);
  else offres.sort((a,b) => a.cos_rang - b.cos_rang);
  if (q) offres = offres.filter(o =>
     (o.title+" "+o.company+" "+o.location+" "+o.source).toLowerCase().includes(q));

  // Mémorisé AVANT le rendu : c'est cette liste — les offres réellement
  // à l'écran, filtre appliqué — qui décide si le polling doit tourner.
  window.__offresAffichees = offres;

  const corps = document.getElementById("corps");
  corps.innerHTML = offres.length
    ? offres.map(ligne).join("")
    : `<tr><td colspan="10" class="vide">${ETAT.n_total?"Aucune offre ne correspond au filtre.":"Aucun classement — clique « 🚀 Tout lancer »."}</td></tr>`;

  // Seul point de ré-armement du polling CV. `rendre` est appelé après
  // CHAQUE changement d'affichage — filtre, tri, retour de POST, tour de
  // sonde — donc l'arrêt sur état terminal et l'arrêt sur filtre passent
  // tous les deux par ici, sans surveillance séparée.
  relancerSondeCV();
}

async function lancerVerif(){
  const n = parseInt(document.getElementById("n").value, 10);
  const r = await fetch("/api/verifier", {method:"POST", headers:{"Content-Type":"application/json"},
                        body: JSON.stringify({n})});
  if (!r.ok){ const j = await r.json(); alert(j.erreur||"Erreur"); return; }
  charger();
}

async function rafraichir(){
  const rapide = document.getElementById("nojobspy").checked;
  const msg = rapide
    ? "Recherche rapide (APIs seules, sans JobSpy) — environ 1 minute. Continuer ?"
    : "Recherche complète (avec scraping LinkedIn/Indeed) — quelques minutes. Continuer ?";
  if (!confirm(msg)) return;
  const r = await fetch("/api/rafraichir", {method:"POST", headers:{"Content-Type":"application/json"},
                        body: JSON.stringify({no_jobspy: rapide})});
  if (!r.ok){ const j = await r.json(); alert(j.erreur||"Erreur"); return; }
  charger();
}

// --- Tableau de bord marché -------------------------------------------------
// Chargé À LA DEMANDE : une quinzaine d'appels d'API, quelques secondes. Le
// serveur met le résultat en cache, donc rouvrir la page est instantané.
async function chargerMarche(force){
  const btn = document.getElementById("btn-marche");
  const corps = document.getElementById("marche-corps");
  if (force){ btn.disabled = true; btn.textContent = "Mesure en cours…"; }
  try {
    const r = await fetch("/api/marche" + (force ? "?force=1" : ""), {cache:"no-store"});
    const d = await r.json();
    if (!r.ok){ corps.innerHTML = `<div class="note">${esc(d.erreur||"Erreur")}</div>`; return; }
    if (d.vide) return;                    // rien en cache : on laisse le bouton
    rendreMarche(d);
    btn.textContent = "Réactualiser";
  } catch(e){
    if (force) corps.innerHTML = '<div class="note">Indicateurs injoignables.</div>';
  } finally {
    btn.disabled = false;
  }
}

function fmtN(x){ return (x === null || x === undefined) ? "—" : x; }

function fmtRythme(r){
  if (r === null || r === undefined) return '<span class="delta-zero">—</span>';
  if (r >= 1.15) return `<span class="hausse">▲ ×${r}</span>`;
  if (r <= 0.85) return `<span class="baisse">▼ ×${r}</span>`;
  return `<span class="delta-zero">= ×${r}</span>`;
}

// Tension France Travail : positive = les employeurs peinent à recruter, donc
// marché favorable au candidat. On colore dans CE sens, pas dans celui du
// recruteur — sinon le vert voudrait dire l'inverse de ce qui t'intéresse.
function fmtTension(t){
  if (t === null || t === undefined) return '<span class="delta-zero">—</span>';
  if (t >= 0.05) return `<span class="hausse">${t.toFixed(2)}</span>`;
  if (t <= -0.05) return `<span class="baisse">${t.toFixed(2)}</span>`;
  return `<span class="delta-zero">${t.toFixed(2)}</span>`;
}

function rendreMarche(d){
  const l = d.lecture, h = d.historique;
  const lignes = d.volumes.map(v => {
    // Rythme recalculé côté client pour éviter un aller-retour supplémentaire :
    // même formule que market._rythme (7j extrapolés sur la fenêtre longue).
    let r = null;
    if (v.ft_court !== null && v.ft_long) {
      const attendu = v.ft_long * (d.fenetre_courte / d.fenetre_longue);
      if (attendu > 0) r = Math.round(100 * v.ft_court / attendu) / 100;
    }
    const rome = v.code_rome ? `<span class="src">${esc(v.code_rome)}</span> ` : "";
    return `<tr${v.code_rome ? "" : ' style="font-weight:600"'}>
      <td>${rome}${esc(v.theme)}</td>
      <td class="n">${fmtN(v.ft_court)}</td>
      <td class="n">${fmtN(v.ft_long)}</td>
      <td class="n">${fmtRythme(r)}</td>
      <td class="n">${fmtN(v.ft_tous_contrats)}</td>
      <td class="n">${fmtTension(v.tension)}</td>
      <td class="n">${fmtN(v.careerjet_total)}</td></tr>`;
  }).join("");

  const entreprises = (h.top_entreprises||[]).slice(0,6)
      .map(e => `<tr><td>${esc(e.nom)}</td><td class="n">${e.n}</td></tr>`).join("");

  document.getElementById("marche-corps").innerHTML = `
    <div class="marche-verdict t-${esc(l.tension)}">
      <strong>Marché ${esc(l.tension)}</strong> · publication ${esc(l.saison)} —
      ${esc(l.verdict)}
      ${l.lecture_tension ? `<div style="margin-top:.4rem">
        <strong>Tension officielle ${l.tension_officielle}</strong>
        (France Travail, ${esc(l.tension_annee||"")}) : ${esc(l.lecture_tension)}</div>` : ""}
    </div>
    <div class="marche-grille">
      <div>
        <table class="mini">
          <tr><th>métier (code ROME)</th><th>stages ${d.fenetre_courte}j</th>
              <th>stages ${d.fenetre_longue}j</th><th>rythme</th>
              <th>ts contrats ${d.fenetre_longue}j</th>
              <th title="Indicateur officiel France Travail de difficulté de recrutement">tension</th>
              <th>Careerjet</th></tr>
          ${lignes}
        </table>
        <div class="note">Les métiers sont ceux de <code>config.METIERS_SUIVIS</code>, convertis en
        <strong>codes ROME</strong> par l'IA ROMEO de France Travail. Compter par code ROME plutôt
        que par mot-clé change tout : « stage cybersécurité » en texte libre remonte 0 offre sur
        ${d.fenetre_longue} jours, le ROME correspondant en remonte 9.<br>
        « rythme » compare les ${d.fenetre_courte} derniers jours à la moyenne des
        ${d.fenetre_longue} jours. « ts contrats » = le métier recrute-t-il en général, même sans
        stage ouvert — une valeur haute avec 0 stage désigne une cible de
        <strong>candidature spontanée</strong>. La colonne Careerjet est un <em>stock</em> total,
        pas un flux : elle ne se compare à aucune autre, et les colonnes ne s'additionnent pas
        (gisements différents, recouvrement partiel).</div>
      </div>
      <div>
        <table class="mini">
          <tr><th>ta recherche</th><th></th></tr>
          <tr><td>offres connues (toutes campagnes)</td><td class="n">${h.total_offres_connues}</td></tr>
          <tr><td>déjà revues d'un run à l'autre</td><td class="n">${fmtN(h.part_revues)} %</td></tr>
          <tr><td>nouveautés au dernier run</td><td class="n">${fmtN(h.renouvellement)} %</td></tr>
          <tr><td>offres IA × cyber repérées</td><td class="n">${h.combos_ia_cyber}</td></tr>
        </table>
        ${entreprises ? `<table class="mini" style="margin-top:.6rem">
          <tr><th>entreprises les plus présentes</th><th></th></tr>${entreprises}</table>` : ""}
      </div>
    </div>
    <div class="note">La colonne <strong>tension</strong> vient de l'API « Marché du travail »
    de France Travail (indicateur PERSP_2, difficultés de recrutement par métier et par région,
    millésime annuel). Elle est la seule mesure ici qui regarde le côté <em>demande</em> — tout
    le reste compte des annonces. <strong>Positive = les employeurs peinent à recruter, donc
    favorable au candidat</strong> ; les couleurs suivent cette lecture, pas celle du recruteur.
    ${d.depuis_cache ? "Chiffres en cache, " : "Mesuré "}le
    ${new Date(d.calcule_le).toLocaleString('fr-FR')}.</div>`;
}

// Bouton unique : lance la recherche puis, sans rien demander de plus, la
// vérification IA du top N. On peut fermer l'onglet, le serveur continue.
async function toutLancer(){
  const rapide = document.getElementById("nojobspy").checked;
  const n = parseInt(document.getElementById("n").value, 10) || 10;
  const duree = rapide ? "environ 1 minute" : "quelques minutes";
  if (!confirm(`Tout lancer :\n1) recherche sur toutes les sources (${duree})\n`
             + `2) vérification IA des ${n} meilleures offres\n\nLes deux étapes s'enchaînent toutes seules. Continuer ?`)) return;
  const r = await fetch("/api/tout", {method:"POST", headers:{"Content-Type":"application/json"},
                        body: JSON.stringify({no_jobspy: rapide, n})});
  if (!r.ok){ const j = await r.json(); alert(j.erreur||"Erreur"); return; }
  charger();
}

document.getElementById("n").addEventListener("input", e => { e.target.dataset.touched = "1"; });

// L'état CV de TOUTES les offres en UN appel, avant le premier rendu :
// une requête par ligne ferait 294 allers-retours à l'ouverture.
chargerEtatsCV().then(charger);
// Au chargement : on affiche le tableau de bord SI le serveur l'a déjà en
// cache. Sans force=1, l'appel est instantané et ne consomme aucun quota.
chargerMarche(false);
</script>
</body>
</html>"""


def _demarrer_logs() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    for bruyant in ("werkzeug", "httpx", "huggingface_hub", "sentence_transformers",
                    "transformers", "urllib3", "filelock", "JobSpy"):
        logging.getLogger(bruyant).setLevel(logging.WARNING)


# Le reloader de Werkzeug lance DEUX interpréteurs du même script. Le
# worker de génération de CV y tournerait en double, sans que ni le verrou
# de thread ni le drapeau d'instance ne s'en aperçoivent — deux
# générations Ollama simultanées, c'est-à-dire l'écroulement que l'unicité
# du worker existe pour empêcher.
#
# Il est donc explicitement COUPÉ, et non laissé à son défaut : le défaut
# dépend de `debug`, qu'un futur passage à `debug=True` changerait sans
# qu'on pense au worker. `worker.worker_autorise(reloader_actif=...)` lit
# cette constante et resterait correct si elle repassait à True.
UTILISER_RELOADER = False

# Le worker de génération, s'il tourne dans CE processus. Module-level et non
# local à `__main__` pour que les tests puissent constater qu'un second
# démarrage n'en crée pas un deuxième.
WORKER: worker_module.Worker | None = None


def demarrer_worker(*, reloader_actif: bool | None = None):
    """Démarre le worker de génération avec l'app. Rend l'instance, ou None.

    Trois refus possibles, dans cet ordre :

    1. **Ce processus n'est pas le bon.** Sous le reloader de Werkzeug, le
       superviseur ne doit pas porter de worker — il en existerait deux, dans
       deux interpréteurs qui ne se voient pas, donc deux générations Ollama
       simultanées. `worker_autorise` tranche, à partir du drapeau qu'on
       passe réellement à `app.run()` et non d'une devinette.
    2. **Un worker de ce processus tourne déjà** : on rend le même.
    3. `Worker.demarrer()` refuse à son tour si son thread est vivant.

    Appelée UNIQUEMENT depuis `__main__` : importer `app` (ce que font les
    tests) ne doit pas lancer de thread ni ouvrir la vraie base.
    """
    global WORKER
    reloader = UTILISER_RELOADER if reloader_actif is None else reloader_actif
    if not worker_module.worker_autorise(reloader_actif=reloader):
        logger.info("Worker de génération non démarré : ce processus est le "
                    "superviseur du reloader, pas celui qui sert.")
        return None
    if WORKER is not None and WORKER.actif:
        logger.warning("Worker de génération déjà en cours : démarrage ignoré.")
        return WORKER
    WORKER = worker_module.Worker()
    WORKER.demarrer()
    logger.info("Worker de génération de CV démarré (file SQLite, 1 job à la fois).")
    return WORKER


if __name__ == "__main__":
    _demarrer_logs()
    _charger_cache()
    demarrer_worker()
    logger.info("Stage Finder web : http://localhost:5000  (Ctrl+C pour arrêter)")
    app.run(host="127.0.0.1", port=5000, threaded=True,
            use_reloader=UTILISER_RELOADER)
