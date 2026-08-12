"""
reference.py — Instantané figé de `stages.db`, et détection de sa dérive.

Un chiffre de baseline ne vaut que s'il est comparable au chiffre suivant. Or
`stages.db` bouge : il suffit d'un clic sur « Tout lancer » pour qu'un scrape
remplace des offres, en ajoute, en retire, et le classement mesuré hier ne se
compare plus à celui de demain. C'est arrivé pendant la Phase 0 — un run a
remplacé les 42 offres mesurées par 37 autres.

On fige donc une copie, et toute mesure des Phases 0 à 2 porte sur elle.

## Ce que l'empreinte couvre, et pourquoi pas les octets du fichier

L'empreinte porte sur les **données de mesure** — toutes les colonnes de
`offres`, plus le texte de `offres_texte` — et non sur les octets du fichier.
La raison est concrète : l'étiquetage écrit dans cette même base (table
`etiquettes`). Une empreinte de fichier bougerait donc à chaque étiquette
posée, et crierait à la dérive alors qu'aucune entrée de mesure n'aurait
changé. À l'inverse, un scrape modifie `offres` et sera détecté.

## Deux estampilles, deux verdicts

Une mesure a DEUX entrées : les données, et les constantes de ranking. Elles
sont contrôlées séparément et ne se confondent jamais dans un message, parce
qu'elles ne se soignent pas pareil — « les offres ont changé » se répare en
repointant sur l'instantané, « MAX_SEQ_LENGTH a changé » se répare en
regardant ce qu'on était en train de tester.

Les constantes sont pourtant versionnées, et on pourrait croire qu'un
`git diff` suffit. Il ne suffit pas : il dit que `config.py` a bougé
AUJOURD'HUI, il ne dit pas quelle valeur avait `MAX_SEQ_LENGTH` le jour où le
rapport de baseline a été produit. C'est précisément ce lien-là qu'il faut,
puisque les variantes de la Phase 1 modifient exactement ces constantes.

## Ce que l'instantané ne fige PAS

Les poids du modèle d'embedding : seul son NOM est estampillé. Un modèle
Hugging Face repris sous le même nom serait invisible ici. Le cache HF est
local et immuable en pratique ; le risque est accepté, il est noté.

Utilisation :
    python reference.py --geler          # fige stages.db, écrit reference.json
    python reference.py --controler      # la base ou la config ont-elles dérivé ?
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import sqlite3
from datetime import datetime

import config

logger = logging.getLogger(__name__)

# Manifeste : VERSIONNÉ dans le dépôt. Il ne contient qu'un chemin, une
# empreinte et des compteurs — aucune donnée d'offre. C'est lui qui permet de
# dire « ce rapport a-t-il été calculé sur la bonne base ? » depuis un clone.
CHEMIN_MANIFESTE = "reference.json"

# Les instantanés eux-mêmes sont gitignorés (ils contiennent des offres
# scrapées, comme `stages.db`).
PREFIXE_INSTANTANE = "stages.reference-"


# Constantes qui entrent dans un SCORE, et elles seules. Écrites en toutes
# lettres plutôt que balayées automatiquement : une liste explicite se relit,
# et le test `test_aucune_constante_de_score_n_est_oubliee` refuse qu'on en
# ajoute une dans `config.py` sans l'estampiller ici.
#
# Ne sont PAS estampillés, et c'est délibéré :
#   - MOTS_CLES_STAGE / MOTS_CLES_EXCLUS / LIEUX_ACCEPTES / JOURS_FRAICHEUR :
#     ce sont des filtres de COLLECTE. Sur un corpus figé, ils ne touchent
#     aucun score — ils décident de ce qui entre dans la base, pas de la place
#     qu'y prend une offre. Ils bougeront en Phase 3 et l'instantané changera
#     alors de toute façon.
#   - COMBO_TOUJOURS_AFFICHE, VERIFY_MIN_SCORE : affichage seul.
#   - TERMES_RECHERCHE, SOURCES_ACTIVES : collecte seule.
CONSTANTES_RANKING = (
    "config.MODELE_EMBEDDING",
    "config.MAX_SEQ_LENGTH",
    "config.PREFIXE_REQUETE",
    "config.PREFIXE_DOCUMENT",
    "config.REQUETE_REFERENCE",
    "config.PROFILS_REFERENCE",
    "config.AGGREGATION_PROFILS",
    "config.MODE_COMPOSITION_SCORE",
    "config.METHODE_NORMALISATION",
    "config.MOTS_CLES_IA",
    "config.MOTS_CLES_CYBER",
    "config.BOOST_IA",
    "config.BOOST_CYBER",
    "config.BOOST_COMBO",
    "config.BOOST_FACTEUR_DESCRIPTION",
    "config.COMBO_EXIGE_SIGNAL_TITRE",
    "config.DUREE_CIBLE_MOIS",
    "config.DUREE_MIN_ACCEPTABLE",
    "config.BONUS_DUREE_CIBLE",
    "config.MALUS_DUREE_COURTE",
    "config.DATE_DEBUT_CIBLE_ANNEE",
    "config.DATE_DEBUT_CIBLE_MOIS",
    "config.BONUS_DATE_DEBUT",
    "config.DEDUP_FLOUE_ACTIVE",
    "config.SEUIL_DEDUP_FLOU",
    "config.VERIFY_ENABLED",
    "config.VERIFY_MODEL",
    "config.VERIFY_TOP_N",
    "config.VERIFY_SCORE_WEIGHT",
    "config.VERIFY_MAX_TOKENS",
    "config.VERIFY_JUSTIF_PHRASES",
    # Le prompt et ses garde-fous décident le score LLM au même titre qu'un
    # poids : `verifier.VERSION_REGLES` est incrémenté à chaque fois qu'ils
    # changent, c'est donc lui qui les représente.
    "verifier.VERSION_REGLES",
)


def _canonique(valeur):
    """Valeur sérialisable en JSON, stable d'une exécution à l'autre."""
    if isinstance(valeur, tuple):
        return [_canonique(v) for v in valeur]
    if isinstance(valeur, list):
        return [_canonique(v) for v in valeur]
    if isinstance(valeur, dict):
        return {str(c): _canonique(v) for c, v in valeur.items()}
    if isinstance(valeur, (str, int, float, bool)) or valeur is None:
        return valeur
    return str(valeur)  # Path et consorts


def constantes_ranking() -> dict:
    """Valeurs courantes des constantes estampillées, ``nom -> valeur``."""
    valeurs = {}
    for chemin in CONSTANTES_RANKING:
        module, attribut = chemin.split(".", 1)
        valeurs[chemin] = _canonique(getattr(importlib.import_module(module), attribut))
    return valeurs


def empreinte_config(valeurs: dict | None = None) -> str:
    """sha256 des constantes de ranking."""
    valeurs = constantes_ranking() if valeurs is None else valeurs
    brut = json.dumps(valeurs, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def rendre(valeur) -> str:
    """Rendu court d'une constante, pour un rapport qui se lit côte à côte.

    Les scalaires sont écrits en clair — ce sont eux qu'on compare à l'œil.
    Les listes (38 mots-clés IA, 3 profils de référence) sont résumées par
    leur taille et une empreinte courte : les recopier noierait le rapport,
    et l'empreinte suffit à voir qu'elles ont bougé.
    """
    if isinstance(valeur, (list, dict)):
        court = hashlib.sha256(
            json.dumps(valeur, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:8]
        return f"{len(valeur)} élément(s)  [{court}]"
    texte = str(valeur)
    return texte if len(texte) <= 58 else texte[:55] + "…"


def ecrire_atomique(chemin: str, contenu: str) -> None:
    """Écrit ``contenu`` dans ``chemin`` sans jamais laisser de fichier à moitié écrit.

    Temporaire dans le MÊME dossier (donc même volume, donc renommage
    atomique), `fsync`, puis `os.replace`. Un `open(chemin, "w")` tronque
    AVANT d'écrire : interrompu à cet instant, il détruit l'ancien contenu
    sans avoir produit le nouveau.
    """
    dossier = os.path.dirname(os.path.abspath(chemin))
    temporaire = os.path.join(dossier, f".{os.path.basename(chemin)}.tmp")
    with open(temporaire, "w", encoding="utf-8", newline="\n") as f:
        f.write(contenu)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporaire, chemin)


# ---------------------------------------------------------------------------
# Empreinte des données de mesure
# ---------------------------------------------------------------------------
def _colonnes(conn: sqlite3.Connection, table: str) -> list[str]:
    return [l[1] for l in conn.execute(f"PRAGMA table_info({table})")]


def empreinte_donnees(conn: sqlite3.Connection) -> str:
    """sha256 de tout ce qui entre dans une mesure de ranking.

    Sérialisation canonique : lignes triées par clé, colonnes dans l'ordre du
    schéma, champs séparés par un octet nul (impossible dans les valeurs
    textuelles rendues par SQLite), `None` distinct de la chaîne vide. Deux
    bases au même contenu rendent la même empreinte quel que soit l'ordre
    d'insertion ou la fragmentation du fichier.
    """
    h = hashlib.sha256()
    for table, tri in (("offres", "cle"), ("offres_texte", "cle")):
        colonnes = _colonnes(conn, table)
        if not colonnes:
            continue
        h.update(f"\x01{table}:{','.join(colonnes)}\x01".encode("utf-8"))
        for ligne in conn.execute(
            f"SELECT {', '.join(colonnes)} FROM {table} ORDER BY {tri}"
        ):
            for valeur in ligne:
                h.update(b"\x00NULL" if valeur is None
                         else b"\x00" + str(valeur).encode("utf-8"))
    return h.hexdigest()


def decrire(conn: sqlite3.Connection, chemin: str) -> dict:
    """Carte d'identité de la base : dernier run, volumes, empreinte."""
    ligne = conn.execute(
        "SELECT id, horodatage, nb_offres FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    nb_offres, = conn.execute("SELECT COUNT(*) FROM offres").fetchone()
    nb_textes, = conn.execute("SELECT COUNT(*) FROM offres_texte").fetchone()
    derniere_vue, = conn.execute("SELECT MAX(derniere_vue) FROM offres").fetchone()
    return {
        "chemin": os.path.basename(chemin),
        "run_id": ligne[0] if ligne else None,
        "run_horodatage": ligne[1] if ligne else None,
        "run_nb_offres": ligne[2] if ligne else None,
        "derniere_vue": derniere_vue,
        "nb_offres": nb_offres,
        "nb_textes": nb_textes,
        "empreinte": empreinte_donnees(conn),
    }


# ---------------------------------------------------------------------------
# Geler / relire
# ---------------------------------------------------------------------------
def geler(source: str | None = None, dossier: str = ".",
          chemin_manifeste: str = CHEMIN_MANIFESTE) -> dict:
    """Fige une copie de la base et écrit le manifeste. Rend la description.

    ``VACUUM INTO`` plutôt qu'une copie de fichier, pour deux raisons :
    il produit UN fichier autonome (pas de `-wal` ni de `-shm` à transporter,
    donc pas de risque d'en oublier un), et il lit une vue COHÉRENTE même si
    l'application tourne et écrit en même temps. Une copie à trois fichiers
    prise pendant une écriture ne garantit ni l'un ni l'autre.
    """
    source = source or config.CHEMIN_BASE
    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
    cible = os.path.join(dossier, f"{PREFIXE_INSTANTANE}{horodatage}.db")
    if os.path.exists(cible):
        raise FileExistsError(cible)

    src = sqlite3.connect(source)
    try:
        src.execute("VACUUM INTO ?", (os.path.abspath(cible),))
    finally:
        src.close()

    fige = sqlite3.connect(cible)
    try:
        description = decrire(fige, cible)
    finally:
        fige.close()

    valeurs = constantes_ranking()
    manifeste = {
        "gele_le": datetime.now().isoformat(timespec="seconds"),
        "source": os.path.basename(source),
        **description,
        "constantes": valeurs,
        "empreinte_config": empreinte_config(valeurs),
    }
    ecrire_atomique(chemin_manifeste,
                    json.dumps(manifeste, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n")
    return manifeste


def charger_manifeste(chemin: str = CHEMIN_MANIFESTE) -> dict | None:
    """Manifeste de l'instantané, ou ``None`` si aucune référence n'est figée."""
    if not os.path.exists(chemin):
        return None
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as err:
        logger.warning("Manifeste de référence illisible (%s) : ignoré.", err)
        return None


def base_par_defaut(chemin_manifeste: str = CHEMIN_MANIFESTE) -> str:
    """La base sur laquelle mesurer : l'instantané s'il existe, sinon la vive.

    Le défaut penche du côté sûr. Tant qu'une référence est figée, un outil de
    mesure lancé sans argument ne doit pas aller chercher la base vive — c'est
    exactement l'erreur qu'on veut rendre impossible sans y penser.
    """
    manifeste = charger_manifeste(chemin_manifeste)
    if manifeste and os.path.exists(manifeste["chemin"]):
        return manifeste["chemin"]
    return config.CHEMIN_BASE


# ---------------------------------------------------------------------------
# Contrôle de dérive
# ---------------------------------------------------------------------------
def controler(conn: sqlite3.Connection, chemin_utilise: str,
              chemin_manifeste: str = CHEMIN_MANIFESTE) -> dict:
    """Compare données ET configuration au manifeste. DEUX verdicts distincts.

    « Données conformes, configuration modifiée » et « données modifiées,
    configuration conforme » ne se réparent pas de la même façon : les
    confondre dans un message unique enverrait chercher au mauvais endroit.
    Le verdict agrégé ``conforme`` reste rendu pour qui n'a besoin que de
    savoir s'il peut comparer.

    Chaque verdict vaut ``True``, ``False``, ou ``None`` — ce dernier voulant
    dire « rien à comparer », ce qui n'est PAS « conforme ».
    """
    manifeste = charger_manifeste(chemin_manifeste)
    obtenu = decrire(conn, chemin_utilise)
    valeurs = constantes_ranking()
    obtenu["empreinte_config"] = empreinte_config(valeurs)

    if manifeste is None:
        vide = {"conforme": None, "alertes": []}
        return {"conforme": None, "alertes": [], "attendu": None, "obtenu": obtenu,
                "donnees": dict(vide),
                "config": {**vide, "changements": [], "valeurs": valeurs}}

    # --- Données ---------------------------------------------------------
    alertes_donnees = []
    if os.path.basename(chemin_utilise) != manifeste["chemin"]:
        alertes_donnees.append(
            f"base mesurée « {os.path.basename(chemin_utilise)} » ≠ instantané "
            f"de référence « {manifeste['chemin']} »"
        )
    if obtenu["empreinte"] != manifeste["empreinte"]:
        alertes_donnees.append(
            f"empreinte des données {obtenu['empreinte'][:16]}… ≠ référence "
            f"{manifeste['empreinte'][:16]}… — les offres ou leurs textes ont changé"
        )

    # --- Configuration ---------------------------------------------------
    attendues = manifeste.get("constantes")
    changements: list[dict] = []
    alertes_config: list[str] = []
    if attendues is None:
        # Manifeste antérieur à l'estampille : on ne peut rien affirmer, et
        # surtout pas « conforme ».
        conforme_config = None
        alertes_config.append(
            f"{os.path.basename(chemin_manifeste)} ne porte pas d'estampille de "
            f"configuration : re-fige la référence (python reference.py --geler)"
        )
    else:
        for nom in sorted(set(attendues) | set(valeurs)):
            avant, apres = attendues.get(nom, "<absente>"), valeurs.get(nom, "<absente>")
            if avant != apres:
                changements.append({"nom": nom, "avant": rendre(avant),
                                    "apres": rendre(apres)})
        conforme_config = not changements
        if changements:
            alertes_config.append(
                f"{len(changements)} constante(s) de ranking modifiée(s) depuis le gel"
            )

    alertes = alertes_donnees + alertes_config
    conforme_donnees = not alertes_donnees
    conforme = None if conforme_config is None and conforme_donnees else (
        conforme_donnees and bool(conforme_config)
    )
    return {
        "conforme": conforme,
        "alertes": alertes,
        "attendu": manifeste,
        "obtenu": obtenu,
        "donnees": {"conforme": conforme_donnees, "alertes": alertes_donnees},
        "config": {"conforme": conforme_config, "alertes": alertes_config,
                   "changements": changements, "valeurs": valeurs},
    }


def _bandeau(titre: str, lignes: list[str]) -> None:
    print("\n" + "!" * 78)
    print(titre)
    for ligne in lignes:
        print(f"  · {ligne}")
    print("!" * 78)


def afficher_entete(controle: dict, chemin_utilise: str, constantes: bool = True) -> None:
    """En-tête de tout rapport de mesure : sur quoi il a été calculé.

    Un rapport qui ne dit pas sur quelle base ET quelle configuration il porte
    n'est pas un rapport : deux chiffres côte à côte sans ces lignes ne se
    comparent pas. Les VALEURS sont imprimées, pas seulement les empreintes —
    une empreinte qui diffère signale un problème, la liste dit lequel, et
    deux rapports se lisent alors côte à côte sans rien relancer.
    """
    o, d, c = controle["obtenu"], controle["donnees"], controle["config"]
    print("--- Référence de mesure ---")
    print(f"  base            : {chemin_utilise}")
    print(f"  dernier run     : #{o['run_id']}  {o['run_horodatage']}  "
          f"({o['run_nb_offres']} offres classées, dernière vue {o['derniere_vue']})")
    print(f"  volumes         : {o['nb_offres']} offres, {o['nb_textes']} avec texte")

    etat_d = {None: "AUCUNE RÉFÉRENCE", True: "conforme", False: "DÉRIVE"}[d["conforme"]]
    etat_c = {None: "NON ESTAMPILLÉE", True: "conforme", False: "MODIFIÉE"}[c["conforme"]]
    print(f"  données         : {o['empreinte'][:24]}…  — {etat_d}")
    print(f"  configuration   : {o['empreinte_config'][:24]}…  — {etat_c}")
    if controle["attendu"]:
        print(f"  gelée le        : {controle['attendu']['gele_le']}")

    if constantes:
        print("\n--- Constantes de ranking estampillées ---")
        for nom, valeur in sorted(c["valeurs"].items()):
            print(f"  {nom:38} {rendre(valeur)}")

    # Deux bandeaux SÉPARÉS : les deux pannes ne se réparent pas au même
    # endroit, les confondre enverrait chercher au mauvais.
    if d["conforme"] is None:
        print("\n  Aucun instantané figé : mesure sur base vive, non comparable dans")
        print("  le temps. Fige une référence : python reference.py --geler")
    elif d["conforme"] is False:
        _bandeau("DÉRIVE DES DONNÉES — ce rapport n'est PAS comparable au baseline.",
                 d["alertes"] + [
                     f"attendu : {controle['attendu']['chemin']} "
                     f"run #{controle['attendu']['run_id']} "
                     f"({controle['attendu']['nb_offres']} offres)",
                     f"obtenu  : {o['chemin']} run #{o['run_id']} "
                     f"({o['nb_offres']} offres)",
                 ])

    if c["conforme"] is False:
        _bandeau("CONFIGURATION MODIFIÉE — ce rapport n'est PAS comparable au baseline.",
                 [f"{ch['nom']} : {ch['avant']}  →  {ch['apres']}"
                  for ch in c["changements"]])
    elif c["conforme"] is None and controle["attendu"] is not None:
        _bandeau("CONFIGURATION NON ESTAMPILLÉE — comparabilité indécidable.",
                 c["alertes"])


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Instantané de référence des mesures.")
    p.add_argument("--geler", action="store_true", help="Fige une copie de la base.")
    p.add_argument("--controler", action="store_true", help="La base a-t-elle dérivé ?")
    p.add_argument("--db", default=None, help="Base source (défaut : config.CHEMIN_BASE).")
    p.add_argument("--manifeste", default=CHEMIN_MANIFESTE)
    args = p.parse_args()

    if args.geler:
        m = geler(args.db, chemin_manifeste=args.manifeste)
        print(f"Instantané figé : {m['chemin']}")
        print(f"  dernier run       : #{m['run_id']}  {m['run_horodatage']}")
        print(f"  volumes           : {m['nb_offres']} offres, "
              f"{m['nb_textes']} avec texte")
        print(f"  empreinte données : {m['empreinte']}")
        print(f"  empreinte config  : {m['empreinte_config']}  "
              f"({len(m['constantes'])} constantes)")
        print(f"  manifeste         : {args.manifeste}")
        return

    chemin = args.db or base_par_defaut(args.manifeste)
    conn = sqlite3.connect(chemin)
    try:
        controle = controler(conn, chemin, args.manifeste)
    finally:
        conn.close()
    afficher_entete(controle, chemin)
    # Code de sortie non nul sur dérive : une comparaison invalide doit
    # ÉCHOUER, pas rendre un chiffre plausible qu'on lira sans se méfier.
    if controle["conforme"] is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
