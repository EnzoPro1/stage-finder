"""
etiqueter.py — Corpus étiqueté de référence : « m'intéresse » / « ne m'intéresse pas ».

Rien de ce que fait le ranking ne peut être mesuré sans référence humaine. Ce
module en construit une : une session CLI qui présente des offres de `stages.db`
et enregistre un jugement binaire, plus la persistance qui va avec.

## Où vit la vérité

    etiquettes.json   ← SOURCE DE VÉRITÉ, versionnée dans le dépôt
    table `etiquettes`   ← cache dérivé, reconstructible par `--import`

L'inverse serait tentant (la base est déjà là) mais faux : `stages.db` est
gitignorée et sans sauvegarde. Les étiquettes sont la seule donnée du projet
qui ne se régénère pas — un scrape se relance, une heure de lecture d'annonces
non. Elles ne peuvent donc pas vivre uniquement dans le fichier qu'on ne
sauvegarde pas. Le JSON est exporté par écriture ATOMIQUE (fichier temporaire
puis renommage) : une interruption laisse l'ancien fichier intact, jamais un
fichier à moitié écrit.

## Étiquetage à l'aveugle

L'affichage montre le titre, l'entreprise, le lieu et un extrait. Il ne montre
NI la source, NI le score, NI le rang, NI la longueur du texte. C'est le point
de tout l'exercice : un corpus étiqueté en voyant le score du système mesure
l'accord de l'humain avec le système, pas la qualité du système. Pour la même
raison l'ordre de présentation est MÉLANGÉ (graine fixe, affichée au
démarrage) : présenter par score décroissant induirait un « oui » en tête et un
« non » en queue.

## Clé d'étiquetage

`offres.cle` — sha1(titre normalisé | entreprise | ville), cf. `dedup._cle`.
C'est déjà l'identité d'une offre à travers les runs, et la clé primaire que
référencent `offres_texte` et `generation_jobs`. L'URL a été écartée sur pièce :
Careerjet publie des redirections opaques (`jobviewtrack.com/v2/<jeton>`) et
Adzuna colle des `utm_*` — ni l'une ni l'autre ne survit à un re-scrape.

Utilisation :
    python etiqueter.py                 # session d'étiquetage
    python etiqueter.py --etat          # ventilation du corpus, sans rien changer
    python etiqueter.py --export        # réécrit etiquettes.json depuis la table
    python etiqueter.py --import        # SIMULATION du JSON vers la table
    python etiqueter.py --import --apply    # applique
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import statistics
from datetime import datetime

import reference
import storage
import verifier

logger = logging.getLogger(__name__)

CHEMIN_JSON = "etiquettes.json"

# Graine de mélange de l'ordre de présentation. FIXE et affichée : deux
# sessions présentent les mêmes offres dans le même ordre, ce qui rend une
# session interrompue reprenable à l'identique, et le corpus reproductible
# par quelqu'un d'autre.
GRAINE_MELANGE = 20260809

# Frontière entre les deux populations de texte du corpus, en caractères.
#
# Ce n'est pas un réglage arbitraire, c'est une constatation : sur les 111
# offres de `stages.db` qui portent un texte, AUCUNE ne mesure entre 278 et
# 2474 caractères. Careerjet ne livre pas des descriptions mais des extraits
# de résultats de recherche (120 à 284 caractères, avec des « … » au milieu),
# là où JobSpy livre l'annonce entière (2400 à 7800). Le seuil tombe dans un
# trou de la distribution : le déplacer de 300 à 2400 ne changerait aucun
# classement.
SEUIL_TEXTE_COURT = 600

# Objectifs d'échantillonnage du vivier hors dernier run (cf. D4).
VIVIER_COURTS = 10
VIVIER_LONGS = 10


_SCHEMA = """
CREATE TABLE IF NOT EXISTS etiquettes (
    cle         TEXT PRIMARY KEY,
    pertinent   INTEGER NOT NULL CHECK (pertinent IN (0, 1)),
    etiquete_le TEXT NOT NULL,
    -- Recopiés depuis `offres` au moment de l'étiquetage, et non joints à la
    -- demande : `cle` dérive du titre et de l'entreprise, donc un titre
    -- retouché par la source produit une clé neuve et l'étiquette devient
    -- orpheline. Ces trois colonnes sont ce qui permet alors de la rattacher
    -- à la main plutôt que de la perdre.
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    url         TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée la table si besoin. Idempotente, sûre à chaque démarrage."""
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Lecture / écriture de la table
# ---------------------------------------------------------------------------
def lire_etiquettes(conn: sqlite3.Connection) -> dict[str, dict]:
    """``cle -> {pertinent, etiquete_le, title, company, url}``, ordre par clé."""
    ensure_schema(conn)
    return {
        ligne["cle"]: {
            "cle": ligne["cle"],
            "pertinent": int(ligne["pertinent"]),
            "etiquete_le": ligne["etiquete_le"],
            "title": ligne["title"],
            "company": ligne["company"],
            "url": ligne["url"],
        }
        for ligne in conn.execute("SELECT * FROM etiquettes ORDER BY cle")
    }


def enregistrer_etiquette(conn: sqlite3.Connection, offre: dict, pertinent: int) -> None:
    """Écrit (ou remplace) l'étiquette d'une offre. Validée immédiatement.

    Une étiquette par étiquette, et commit à chaque fois : une session de
    quarante annonces interrompue au trente-huitième ne doit pas être à
    recommencer. Le coût est négligeable devant le temps de lecture humain.
    """
    ensure_schema(conn)
    conn.execute(
        """INSERT OR REPLACE INTO etiquettes
           (cle, pertinent, etiquete_le, title, company, url)
           VALUES (?,?,?,?,?,?)""",
        (
            offre["cle"], int(pertinent),
            datetime.now().isoformat(timespec="seconds"),
            offre["title"] or "", offre["company"] or "", offre["url"] or "",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Export / import JSON
# ---------------------------------------------------------------------------
def _serialiser(etiquettes: dict[str, dict]) -> str:
    """Rend le JSON du corpus, forme STABLE (deux exports égaux octet pour octet).

    Tri par clé, ordre des champs fixe, `indent=2`, saut de ligne final : un
    `git diff` sur ce fichier ne doit montrer que les étiquettes qui ont
    réellement bougé, jamais un remaniement de l'ordre du dictionnaire.
    """
    corps = [
        {
            "cle": e["cle"],
            "pertinent": int(e["pertinent"]),
            "title": e["title"],
            "company": e["company"],
            "url": e["url"],
            "etiquete_le": e["etiquete_le"],
        }
        for _, e in sorted(etiquettes.items())
    ]
    return json.dumps(corps, ensure_ascii=False, indent=2) + "\n"


def exporter_json(etiquettes: dict[str, dict], chemin: str = CHEMIN_JSON) -> int:
    """Écrit le corpus dans ``chemin`` de façon ATOMIQUE. Rend le nombre d'étiquettes.

    Écrire en place (`open(chemin, "w")`) tronque AVANT d'écrire : une
    interruption à cet instant détruirait le corpus, c'est-à-dire la seule
    donnée non régénérable du projet. Le mécanisme lui-même vit dans
    ``reference.ecrire_atomique`` — il sert aussi au manifeste de référence, et
    deux implémentations d'une garantie finissent par diverger.
    """
    reference.ecrire_atomique(chemin, _serialiser(etiquettes))
    return len(etiquettes)


def charger_json(chemin: str = CHEMIN_JSON) -> dict[str, dict]:
    """Lit le corpus depuis le JSON. ``{}`` si le fichier n'existe pas."""
    if not os.path.exists(chemin):
        return {}
    with open(chemin, "r", encoding="utf-8") as f:
        donnees = json.load(f)
    if not isinstance(donnees, list):
        raise ValueError(f"{chemin} : une liste JSON d'étiquettes est attendue.")
    corpus: dict[str, dict] = {}
    for item in donnees:
        cle = item["cle"]
        corpus[cle] = {
            "cle": cle,
            "pertinent": int(item["pertinent"]),
            "etiquete_le": item.get("etiquete_le", ""),
            "title": item.get("title", ""),
            "company": item.get("company", ""),
            "url": item.get("url", ""),
        }
    return corpus


def diff_import(conn: sqlite3.Connection, chemin: str = CHEMIN_JSON) -> dict:
    """Ce que ``--import`` ferait : ajouts, modifications, suppressions.

    Calculé AVANT toute écriture, et rendu même en mode simulation — c'est le
    rapport de volume impacté qu'on veut lire avant de décider d'appliquer.
    """
    voulu = charger_json(chemin)
    actuel = lire_etiquettes(conn)
    ajouts = sorted(set(voulu) - set(actuel))
    suppressions = sorted(set(actuel) - set(voulu))
    modifications = sorted(
        c for c in set(voulu) & set(actuel)
        if voulu[c]["pertinent"] != actuel[c]["pertinent"]
    )
    connues = {ligne["cle"] for ligne in conn.execute("SELECT cle FROM offres")}
    return {
        "ajouts": ajouts,
        "modifications": modifications,
        "suppressions": suppressions,
        "inchangees": len(set(voulu) & set(actuel)) - len(modifications),
        "orphelines": sorted(c for c in voulu if c not in connues),
        "voulu": voulu,
    }


def importer_json(
    conn: sqlite3.Connection, chemin: str = CHEMIN_JSON, *, appliquer: bool = False
) -> dict:
    """Reconstruit la table depuis le JSON. SIMULE sauf si ``appliquer``.

    Idempotent : réimporter le même fichier ne change rien (le rapport le
    montre, `ajouts`/`modifications`/`suppressions` tous vides).

    ``with conn:`` est délibérément ABSENT du chemin de simulation, et pas
    seulement inutilisé : ce gestionnaire VALIDE en sortie de bloc, y compris
    quand l'appelant croit avoir tout annulé plus loin. La seule garantie qui
    tienne est de n'exécuter aucun `INSERT`/`DELETE` du tout tant que
    ``appliquer`` est faux — c'est ce que fait le retour anticipé ci-dessous,
    et c'est ce que vérifie ``test_import_simulation_n_ecrit_rien``.
    """
    rapport = diff_import(conn, chemin)
    voulu = rapport.pop("voulu")
    rapport["applique"] = bool(appliquer)
    if not appliquer:
        return rapport

    ensure_schema(conn)
    conn.execute("DELETE FROM etiquettes")
    conn.executemany(
        """INSERT INTO etiquettes (cle, pertinent, etiquete_le, title, company, url)
           VALUES (?,?,?,?,?,?)""",
        [
            (e["cle"], int(e["pertinent"]), e["etiquete_le"],
             e["title"], e["company"], e["url"])
            for _, e in sorted(voulu.items())
        ],
    )
    conn.commit()
    return rapport


# ---------------------------------------------------------------------------
# Vivier : quelles offres proposer à l'étiquetage
# ---------------------------------------------------------------------------
def _echantillon_etale(lignes: list[dict], combien: int) -> list[dict]:
    """``combien`` éléments répartis sur toute la liste, sans hasard.

    ``lignes`` est supposée triée par score décroissant. On prend des indices
    régulièrement espacés plutôt qu'un tirage aléatoire : la plage de scores
    est couverte de bout en bout, et deux appels rendent le même échantillon.
    """
    n = len(lignes)
    if n <= combien:
        return list(lignes)
    if combien <= 1:
        return lignes[:combien]
    indices = sorted({round(i * (n - 1) / (combien - 1)) for i in range(combien)})
    return [lignes[i] for i in indices]


def vivier(conn: sqlite3.Connection) -> list[dict]:
    """Offres proposées à l'étiquetage, dans un ordre déterministe (par clé).

    Deux apports, pour deux raisons différentes :

    1. **Tout le dernier run** — ce sont les offres du symptôme, celles dont on
       veut expliquer le classement, et toutes portent un texte.
    2. **Un échantillon des runs précédents**, étalé sur la plage de scores ET
       réparti entre les deux populations de texte (cf. `SEUIL_TEXTE_COURT`).
       Sans ce second apport, le corpus n'apprendrait que d'un seul run ; sans
       la répartition par population, il pourrait être composé à 80 % de texte
       long alors que le top-10 réel est composé à 90 % d'extraits courts, et
       la métrique ne dirait rien de ce qu'on regarde.

    Une offre sans texte n'entre jamais : elle n'est pas jugeable.
    """
    dernier_run, = conn.execute("SELECT MAX(derniere_vue) FROM offres").fetchone()

    def _lire(clause: str, params: tuple) -> list[dict]:
        return [
            {
                "cle": l["cle"], "title": l["title"], "company": l["company"],
                "location": l["location"], "url": l["url"], "texte": l["texte"],
                "source": l["source"], "score": l["dernier_score"],
            }
            for l in conn.execute(
                f"""SELECT o.cle, o.title, o.company, o.location, o.url, o.source,
                           o.dernier_score, t.texte
                    FROM offres o JOIN offres_texte t ON t.cle = o.cle
                    WHERE {clause}
                    ORDER BY o.dernier_score DESC, o.cle ASC""",
                params,
            )
        ]

    recents = _lire("o.derniere_vue = ?", (dernier_run,))
    autres = _lire("o.derniere_vue <> ?", (dernier_run,))

    courts = [o for o in autres if len(o["texte"]) < SEUIL_TEXTE_COURT]
    longs = [o for o in autres if len(o["texte"]) >= SEUIL_TEXTE_COURT]
    complement = (_echantillon_etale(courts, VIVIER_COURTS)
                  + _echantillon_etale(longs, VIVIER_LONGS))

    par_cle = {o["cle"]: o for o in recents + complement}
    return [par_cle[c] for c in sorted(par_cle)]


def ventilation(offres: list[dict], etiquettes: dict[str, dict] | None = None) -> dict:
    """Comptages par source et par population de texte, pour lecture humaine.

    ``etiquettes`` restreint le comptage aux offres étiquetées et ajoute la
    répartition oui/non. C'est l'exigence de D1 : une métrique calculée sur un
    corpus dont la composition ne ressemble pas au run réel ne se lit pas.
    """
    if etiquettes is not None:
        offres = [o for o in offres if o["cle"] in etiquettes]

    par_source: dict[str, dict] = {}
    par_longueur: dict[str, dict] = {}
    for offre in offres:
        groupe = ("extrait court (< %d car.)" % SEUIL_TEXTE_COURT
                  if len(offre["texte"]) < SEUIL_TEXTE_COURT else "texte long")
        for table, clef in ((par_source, offre["source"]), (par_longueur, groupe)):
            case = table.setdefault(clef, {"n": 0, "oui": 0, "non": 0})
            case["n"] += 1
            if etiquettes is not None:
                case["oui" if etiquettes[offre["cle"]]["pertinent"] else "non"] += 1

    longueurs = sorted(len(o["texte"]) for o in offres)
    return {
        "n": len(offres),
        "par_source": dict(sorted(par_source.items())),
        "par_longueur": dict(sorted(par_longueur.items())),
        "longueur_mediane": int(statistics.median(longueurs)) if longueurs else 0,
    }


# ---------------------------------------------------------------------------
# Session d'étiquetage
# ---------------------------------------------------------------------------
_AIDE = ("  [o] m'intéresse    [n] ne m'intéresse pas    [?] passer\n"
         "  [t] texte intégral  [q] quitter (sauvegarde)")

_LIBELLE = {1: "m'intéresse", 0: "ne m'intéresse pas"}


def _afficher(offre: dict, position: int, total: int, compte: dict, integral: bool,
              precedente: dict | None = None) -> None:
    """Affiche UNE offre à l'aveugle : ni source, ni score, ni rang, ni longueur.

    ``precedente`` — l'étiquette déjà posée sur cette offre, s'il y en a une.
    Elle EST montrée, contrairement au reste : c'est un jugement qu'on a soi-même
    porté, pas une information du système, et l'ignorer ferait écraser un avis
    sans le savoir.
    """
    print("\n" + "─" * 78)
    print(f"Offre {position}/{total}   —   étiquetées : "
          f"{compte['oui']} oui / {compte['non']} non / {compte['passees']} passées")
    if precedente is not None:
        print(f"⚠ DÉJÀ ÉTIQUETÉE le {precedente['etiquete_le'][:10]} : "
              f"« {_LIBELLE[precedente['pertinent']]} »")
    print("─" * 78)
    print(f"\n  {offre['title']}")
    print(f"  {offre['company'] or '—'}   |   {offre['location'] or '—'}\n")

    texte = offre["texte"] or ""
    corps = texte if integral else verifier._extrait_pertinent(texte)
    etiquette = "texte intégral" if integral else "extrait"
    print(f"  ── {etiquette} " + "─" * (72 - len(etiquette)))
    for ligne in corps.splitlines() or [""]:
        print(f"  {ligne}")
    print("  " + "─" * 74 + "\n")
    print(_AIDE)


def ordre_presentation(offres: list[dict]) -> list[dict]:
    """Mélange à graine FIXE : l'ordre ne doit rien devoir au score.

    Présenter par score décroissant induirait mécaniquement une série de
    « oui » en tête et de « non » en queue — l'étiqueteur suivrait la pente du
    système au lieu de la juger. Un mélange au hasard du jour aurait le défaut
    inverse : une session interrompue ne se reprendrait pas dans le même ordre,
    et le corpus ne serait pas reproductible par quelqu'un d'autre.
    """
    melangees = list(offres)
    random.Random(GRAINE_MELANGE).shuffle(melangees)
    return melangees


def session(
    conn: sqlite3.Connection, chemin_json: str = CHEMIN_JSON, *,
    lire=input, revoir: bool = False,
) -> dict:
    """Déroule une session d'étiquetage. Rend un petit bilan.

    Deux garde-fous, tous deux sur le chemin d'ÉCRITURE et non sur la
    présentation, pour qu'ils tiennent quelle que soit la façon dont une offre
    déjà jugée se retrouve à l'écran (``revoir``, réimport, clé recalculée) :

    - **rien n'est écrasé en silence.** Un jugement différent du précédent
      demande confirmation ; un jugement identique passe sans rien demander,
      puisqu'il n'écrase rien ;
    - **rien n'est jamais supprimé.** ``?`` laisse l'étiquette existante en
      place — c'est « je ne sais pas », pas « efface ce que je pensais ».

    ``lire`` est injecté pour que les tests exercent la boucle sans clavier.
    """
    candidates = vivier(conn)
    deja = lire_etiquettes(conn)
    a_voir = candidates if revoir else [o for o in candidates if o["cle"] not in deja]
    a_faire = ordre_presentation(a_voir)

    print(f"\nVivier : {len(candidates)} offre(s) — {len(deja)} déjà étiquetée(s), "
          f"{len(a_faire)} à voir{' (revue comprise)' if revoir else ''}.")
    print(f"Ordre mélangé, graine {GRAINE_MELANGE} (fixe : la reprise suit le même ordre).")

    compte = {"oui": 0, "non": 0, "passees": 0, "conservees": 0}
    integral = False
    i = 0
    try:
        while i < len(a_faire):
            offre = a_faire[i]
            precedente = deja.get(offre["cle"])
            _afficher(offre, i + 1, len(a_faire), compte, integral, precedente)
            touche = (lire("> ") or "").strip().lower()

            if touche == "t":
                integral = not integral
                continue
            integral = False
            if touche == "q":
                break
            if touche == "?":
                # Ne touche à RIEN : une étiquette existante survit à un
                # « je ne sais pas ».
                compte["passees"] += 1
            elif touche in ("o", "n"):
                valeur = 1 if touche == "o" else 0
                if precedente is not None and precedente["pertinent"] != valeur:
                    print(f"  ↳ écraser « {_LIBELLE[precedente['pertinent']]} » "
                          f"par « {_LIBELLE[valeur]} » ? [o/N]")
                    if (lire("  > ") or "").strip().lower() != "o":
                        print("  ↳ conservé.")
                        compte["conservees"] += 1
                        i += 1
                        continue
                enregistrer_etiquette(conn, offre, valeur)
                deja[offre["cle"]] = {**(precedente or {}), "pertinent": valeur,
                                      "etiquete_le": datetime.now().isoformat(
                                          timespec="seconds")}
                compte["oui" if touche == "o" else "non"] += 1
            else:
                print("  ↳ touche inconnue.")
                continue
            i += 1
    except (KeyboardInterrupt, EOFError):
        print("\nInterruption : les étiquettes déjà saisies sont en base.")

    # Export systématique en fin de session, y compris après interruption :
    # la base est gitignorée, le JSON est la copie qui survit.
    total = lire_etiquettes(conn)
    exporter_json(total, chemin_json)
    print(f"\n{len(total)} étiquette(s) au total, exportées dans {chemin_json}.")
    return {"corpus": len(total), **compte}


# ---------------------------------------------------------------------------
# Rapports console
# ---------------------------------------------------------------------------
def _afficher_ventilation(titre: str, v: dict) -> None:
    print(f"\n{titre} : {v['n']} offre(s), longueur médiane {v['longueur_mediane']} car.")
    for intitule, table in (("par source", v["par_source"]),
                            ("par population de texte", v["par_longueur"])):
        print(f"  {intitule} :")
        for nom, case in table.items():
            detail = (f"  ({case['oui']} oui / {case['non']} non)"
                      if case["oui"] or case["non"] else "")
            print(f"    {nom:34} {case['n']:3}{detail}")


def afficher_etat(conn: sqlite3.Connection) -> dict:
    """Ventilation du vivier et du corpus étiqueté. Ne modifie rien."""
    candidates = vivier(conn)
    etiquettes = lire_etiquettes(conn)
    _afficher_ventilation("Vivier proposé", ventilation(candidates))
    _afficher_ventilation("Corpus étiqueté", ventilation(candidates, etiquettes))

    oui = sum(1 for e in etiquettes.values() if e["pertinent"])
    non = len(etiquettes) - oui
    minoritaire = min(oui, non)
    print(f"\nCorpus : {len(etiquettes)} étiquette(s) — {oui} oui, {non} non "
          f"(classe minoritaire : {minoritaire}).")
    if len(etiquettes) < 30:
        print("  ⚠ plancher de 30 étiquettes NON atteint.")
    if etiquettes and minoritaire < 8:
        print("  ⚠ moins de 8 étiquettes dans la classe minoritaire : "
              "la métrique ne discriminera rien.")
    return {"n": len(etiquettes), "oui": oui, "non": non}


def _afficher_rapport_import(rapport: dict, chemin: str) -> None:
    mode = "APPLIQUÉ" if rapport["applique"] else "SIMULATION (rien n'a été écrit)"
    print(f"\nImport de {chemin} — {mode}")
    print(f"  ajouts        : {len(rapport['ajouts'])}")
    print(f"  modifications : {len(rapport['modifications'])}")
    print(f"  suppressions  : {len(rapport['suppressions'])}")
    print(f"  inchangées    : {rapport['inchangees']}")
    if rapport["orphelines"]:
        print(f"  ⚠ {len(rapport['orphelines'])} étiquette(s) sans offre correspondante "
              f"dans `offres` : conservées, rattachables par title/company/url.")
    if not rapport["applique"] and (rapport["ajouts"] or rapport["modifications"]
                                    or rapport["suppressions"]):
        print("  → relance avec --apply pour écrire.")


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Étiquetage du corpus de référence.")
    p.add_argument("--db", default=None,
                   help="Base SQLite (défaut : l'instantané de référence s'il existe).")
    p.add_argument("--json", default=CHEMIN_JSON, dest="chemin_json",
                   help="Fichier d'étiquettes (source de vérité).")
    p.add_argument("--etat", action="store_true", help="Ventilation, sans rien changer.")
    p.add_argument("--revoir", action="store_true",
                   help="Re-présente aussi les offres déjà étiquetées (confirmation exigée).")
    p.add_argument("--export", action="store_true", help="Table -> JSON.")
    p.add_argument("--import", action="store_true", dest="importer",
                   help="JSON -> table (simulation sans --apply).")
    p.add_argument("--apply", action="store_true", help="Autorise l'écriture de --import.")
    args = p.parse_args()

    chemin_db = args.db or reference.base_par_defaut()
    conn = storage.ouvrir(chemin_db)
    ensure_schema(conn)
    try:
        # L'en-tête d'abord, toujours : étiqueter sur la base vive alors qu'une
        # référence est figée produirait un corpus qui ne correspond à aucune
        # mesure. Autant que ce soit visible avant la première annonce.
        reference.afficher_entete(reference.controler(conn, chemin_db), chemin_db)
        if args.etat:
            afficher_etat(conn)
        elif args.export:
            n = exporter_json(lire_etiquettes(conn), args.chemin_json)
            print(f"{n} étiquette(s) exportée(s) dans {args.chemin_json}.")
        elif args.importer:
            _afficher_rapport_import(
                importer_json(conn, args.chemin_json, appliquer=args.apply),
                args.chemin_json,
            )
        else:
            session(conn, args.chemin_json, revoir=args.revoir)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
