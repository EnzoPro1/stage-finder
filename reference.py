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

## Ce que l'instantané ne fige PAS

Le modèle d'embedding, ses poids, `config.py`. Ce sont des entrées de mesure
elles aussi, mais elles sont versionnées dans le dépôt : un `git diff` les
montre. La base, non — elle est gitignorée.

Utilisation :
    python reference.py --geler          # fige stages.db, écrit reference.json
    python reference.py --controler      # la base pointée a-t-elle dérivé ?
"""

from __future__ import annotations

import argparse
import hashlib
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

    manifeste = {
        "gele_le": datetime.now().isoformat(timespec="seconds"),
        "source": os.path.basename(source),
        **description,
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
    """Compare la base utilisée au manifeste. Rend ``{conforme, alertes, …}``.

    Trois verdicts possibles, et aucun n'est silencieux :

    - pas de manifeste  -> ``conforme`` est ``None`` : rien à comparer, mais on
      le dit, pour qu'un rapport sans référence ne passe pas pour un rapport
      de référence ;
    - base différente de celle du manifeste -> alerte ;
    - même base, empreinte différente -> alerte, c'est LA dérive : quelqu'un a
      scrapé dans l'instantané.
    """
    manifeste = charger_manifeste(chemin_manifeste)
    obtenu = decrire(conn, chemin_utilise)
    if manifeste is None:
        return {"conforme": None, "alertes": [], "attendu": None, "obtenu": obtenu}

    alertes = []
    if os.path.basename(chemin_utilise) != manifeste["chemin"]:
        alertes.append(
            f"base mesurée « {os.path.basename(chemin_utilise)} » ≠ instantané "
            f"de référence « {manifeste['chemin']} »"
        )
    if obtenu["empreinte"] != manifeste["empreinte"]:
        alertes.append(
            f"empreinte des données {obtenu['empreinte'][:16]}… ≠ référence "
            f"{manifeste['empreinte'][:16]}… — les offres ou leurs textes ont changé"
        )
    return {"conforme": not alertes, "alertes": alertes,
            "attendu": manifeste, "obtenu": obtenu}


def afficher_entete(controle: dict, chemin_utilise: str) -> None:
    """En-tête de tout rapport de mesure : sur quoi il a été calculé.

    Un rapport qui ne dit pas sur quelle base il porte n'est pas un rapport :
    deux chiffres côte à côte sans cette ligne ne se comparent pas.
    """
    o = controle["obtenu"]
    print("--- Référence de mesure ---")
    print(f"  base            : {chemin_utilise}")
    print(f"  dernier run     : #{o['run_id']}  {o['run_horodatage']}  "
          f"({o['run_nb_offres']} offres classées, dernière vue {o['derniere_vue']})")
    print(f"  volumes         : {o['nb_offres']} offres, {o['nb_textes']} avec texte")
    print(f"  empreinte       : {o['empreinte'][:32]}…")
    if controle["conforme"] is None:
        print("  instantané      : AUCUN — mesure sur base vive, non comparable "
              "dans le temps")
        print("                    (fige une référence : python reference.py --geler)")
    elif controle["conforme"]:
        print(f"  instantané      : conforme à {CHEMIN_MANIFESTE} "
              f"(figé le {controle['attendu']['gele_le']})")
    else:
        print("\n" + "!" * 78)
        print("DÉRIVE DE LA RÉFÉRENCE — ce rapport n'est PAS comparable au baseline.")
        for alerte in controle["alertes"]:
            print(f"  · {alerte}")
        print(f"  attendu : {controle['attendu']['chemin']} "
              f"run #{controle['attendu']['run_id']} "
              f"({controle['attendu']['nb_offres']} offres)")
        print(f"  obtenu  : {o['chemin']} run #{o['run_id']} "
              f"({o['nb_offres']} offres)")
        print("!" * 78)


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
        print(f"  dernier run   : #{m['run_id']}  {m['run_horodatage']}")
        print(f"  volumes       : {m['nb_offres']} offres, {m['nb_textes']} avec texte")
        print(f"  empreinte     : {m['empreinte']}")
        print(f"  manifeste     : {args.manifeste}")
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
