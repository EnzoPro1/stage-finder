"""
communes.py — Référentiel des communes d'Île-de-France, et résolution d'un lieu.

## Le défaut corrigé

`filters.est_en_idf` décidait avec une liste blanche de ~20 sous-chaînes
(`config.LIEUX_ACCEPTES`) : « paris », les numéros de département, et sept
communes écrites à la main. Toute commune absente de cette liste était REJETÉE,
en silence.

Mesuré sur un lot JobSpy réel : **35 offres sur 93 rejetées**, dont l'écrasante
majorité franciliennes — Neuilly-sur-Seine, Suresnes, Gennevilliers, Puteaux,
Thiais, Vélizy-Villacoublay, La Garenne-Colombes. JobSpy écrit ses lieux
« Commune, A8, FR », où `A8` est le code ISO 3166-2 de l'Île-de-France : ni la
commune ni le code région n'étaient dans la liste blanche.

Le référentiel remplace la liste écrite à la main : les 1 266 communes de la
région, plus les formats de lieu des quatre sources.

## Pourquoi un fichier généré et versionné, et pas un appel d'API

Le découpage communal bouge (fusions, communes nouvelles) mais lentement, et une
mesure de ranking doit être REPRODUCTIBLE : un référentiel qui change sous les
pieds entre deux exécutions rendrait deux rapports incomparables sans que rien
ne le signale. Le fichier est donc figé, versionné, horodaté, et régénérable :

    python communes.py --generer     # refait communes_idf.json depuis geo.api.gouv.fr
    python communes.py --tester      # résout quelques lieux réels, sans réseau

Aucune dépendance réseau à l'exécution.

## Deux sémantiques d'échec, à ne pas confondre

Ce module répond à « ce lieu est-il en Île-de-France ». Un lieu inconnu doit
donc trancher — on ne peut pas « ne pas savoir » si une offre entre dans le
périmètre. C'est l'inverse de `trajets.json` (temps de trajet), où une commune
absente rendra `None` et ne rejettera JAMAIS. Les deux artefacts sont séparés
pour cette raison, et le resteront.

Ordre de décision, du plus sûr au moins sûr :

1. lieu vide           -> `True` (permissif, inchangé : le ranking jugera)
2. marqueur HORS zone  -> `False`  (« , tx », « cvl », « hauts-de-france »…)
3. signal IdF positif  -> `True`   (commune connue, `A8`, « île-de-france », département francilien)
4. rien de reconnu     -> `False`  (liste blanche, comme avant)

L'étape 2 passe AVANT l'étape 3, et c'est vital : « Paris, TX » contient
« paris », qui est une commune d'Île-de-France. Sans la barrière négative
d'abord, les 86 offres texanes de Jooble entreraient toutes.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import unicodedata

logger = logging.getLogger(__name__)

# Chemin résolu par rapport à CE FICHIER, et non au répertoire courant.
#
# Les autres données du projet (`etiquettes.json`, `reference.json`) sont
# relatives au CWD, et c'est acceptable : elles sont lues par des exécutables
# qu'on lance depuis la racine. Celle-ci est lue par du code de BIBLIOTHÈQUE,
# depuis `filters` et `dedup`, donc potentiellement depuis n'importe quel CWD
# — un test lancé ailleurs, un script voisin. Un référentiel introuvable
# dégrade la résolution en silence (les communes cessent d'être reconnues et
# tout ce qui n'est pas Paris est rejeté), c'est-à-dire exactement le défaut
# qu'on est en train de corriger.
CHEMIN_REFERENTIEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "communes_idf.json")

# Code région INSEE de l'Île-de-France, et son code ISO 3166-2 (celui que
# JobSpy écrit dans ses lieux : « Courbevoie, A8, FR »).
CODE_REGION_IDF = "11"
CODE_ISO_IDF = "a8"

# Départements franciliens. Sert au format France Travail (« 92 - SURESNES »)
# et de repli quand le nom de commune n'est pas reconnu.
DEPARTEMENTS_IDF = {"75", "77", "78", "91", "92", "93", "94", "95"}

_URL_SOURCE = (
    "https://geo.api.gouv.fr/communes"
    "?codeRegion=11&fields=nom,code,codeDepartement&format=json"
)

# Codes ISO 3166-2 des AUTRES régions françaises, tels que JobSpy les écrit.
# Ce sont des rejets FRANCS : « Lucé, CVL, FR » est en Centre-Val de Loire.
_REGIONS_HORS_IDF = {
    "ara", "bfc", "bre", "cvl", "ges", "hdf", "nor", "naq", "occ", "pdl",
    "pac", "cor", "gua", "guf", "lre", "may", "mq",
}

# Libellés de région en toutes lettres (Adzuna, Careerjet, JobSpy long format).
_LIBELLES_HORS_IDF = {
    "centre-val de loire", "hauts-de-france", "normandie", "bretagne",
    "pays de la loire", "nouvelle-aquitaine", "occitanie", "grand est",
    "bourgogne-franche-comte", "auvergne-rhone-alpes", "corse",
    "provence-alpes-cote d azur", "provence alpes cote d azur",
}

# Codes d'États américains rencontrés en suffixe de ville (« Paris, TX »).
# Jooble résout « Paris » en Paris, Texas : 86 offres sur 86 lors de la mesure
# du 2026-09-05. La liste couvre les 50 États, parce qu'en couvrir 7 (ce que
# faisait `config.MARQUEURS_ETRANGERS`) laisse passer les 43 autres.
_ETATS_US = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

# « 8e Arrondissement », « 12ème arrondissement », « Paris 8e » — tous ramenés
# à leur commune. Paris, Lyon et Marseille sont les trois villes à arrondissements.
_MOTIF_ARRONDISSEMENT = re.compile(
    r"\b\d{1,2}\s*(?:er|ere|eme|e|nd)?\s*arrondissement\b|\b\d{1,2}\s*(?:er|ere|eme|e)\b"
)

# Préfixe de département France Travail : « 92 - SURESNES », « 75 - Paris 8e ».
_MOTIF_PREFIXE_DEPT = re.compile(r"^\s*(\d{2,3})\s*[-–]\s*")

# Code postal accolé (« Issy-les-Moulineaux 92130 »).
_MOTIF_CODE_POSTAL = re.compile(r"\b\d{5}\b")

# Départements franciliens écrits en toutes lettres — Careerjet les compose
# volontiers avec la ville (« Orly, Val-de-Marne - Paris »). Signal POSITIF au
# même titre qu'un code de département : la commune peut être absente du
# découpage (quartier, lieu-dit) alors que le département, lui, est sans
# ambiguïté. « Paris » n'y figure pas : c'est déjà une commune du référentiel.
_DEPARTEMENTS_IDF_NOMS = {
    "hauts de seine", "seine saint denis", "val de marne", "seine et marne",
    "yvelines", "essonne", "val d oise",
}

# Lieux franciliens qui ne sont PAS des communes et n'entreront donc jamais
# dans le référentiel INSEE : quartiers d'affaires, abréviations d'usage,
# emprises aéroportuaires. Les employeurs les écrivent pourtant tels quels
# dans le champ « lieu ».
#
# La liste est courte et sourcée : ce sont les entrées de l'ancienne liste
# blanche `config.LIEUX_ACCEPTES` qui ne correspondent à aucune commune, plus
# ce qui a été rencontré en base. Remplacer une liste écrite à la main par un
# référentiel ne dispense pas de garder les quelques cas que le référentiel,
# par construction, ne peut pas connaître — « La Défense » est à cheval sur
# Puteaux, Courbevoie et Nanterre, et n'est une commune d'aucune des trois.
_LIEUX_DITS_IDF = {
    "la defense", "paris la defense",
    "boulogne",                 # abrégé courant de Boulogne-Billancourt
    "montmartre",               # quartier de Paris
    "la plaine saint denis",    # quartier de Saint-Denis
    "roissy", "roissy cdg", "roissy charles de gaulle",
    "marne la vallee",          # secteur à cheval sur plusieurs communes
}


def _sans_accents(texte: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normaliser(texte: str) -> str:
    """« L'Haÿ-les-Roses » -> « l hay les roses ». Forme de comparaison unique."""
    t = _sans_accents((texte or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@functools.lru_cache(maxsize=1)
def referentiel(chemin: str = CHEMIN_REFERENTIEL) -> dict:
    """Charge le référentiel généré. Mémorisé : lu une seule fois par processus.

    Rend ``{"par_nom": {nom normalisé -> commune}, "communes": [...], "meta": {...}}``.
    Un fichier absent n'est PAS fatal : on retombe sur les seuls signaux
    structurels (code région, département), en le disant une fois. Le dépôt
    doit rester clonable et testable sans avoir à régénérer quoi que ce soit,
    mais un référentiel manquant ne doit pas non plus passer inaperçu.
    """
    if not os.path.exists(chemin):
        logger.warning(
            "Référentiel des communes absent (%s) : la résolution se limite aux "
            "codes région/département. Régénère-le avec `python communes.py --generer`.",
            chemin,
        )
        return {"par_nom": {}, "communes": [], "meta": {}}

    with open(chemin, "r", encoding="utf-8") as f:
        donnees = json.load(f)

    par_nom: dict[str, dict] = {}
    for commune in donnees.get("communes", []):
        par_nom.setdefault(normaliser(commune["nom"]), commune)
        # Variante sans article initial : les sources écrivent « Clayes-sous-Bois »
        # aussi bien que « Les Clayes-sous-Bois ».
        sans_article = re.sub(r"^(le|la|les|l|du|de|des) ", "", normaliser(commune["nom"]))
        par_nom.setdefault(sans_article, commune)
    return {"par_nom": par_nom, "communes": donnees.get("communes", []),
            "meta": donnees.get("meta", {})}


def _segments(lieu: str) -> list[str]:
    """Découpe un lieu en morceaux comparables, un par virgule.

    Chaque morceau est débarrassé de ce qui l'empêche de coïncider avec un nom
    de commune : préfixe de département France Travail, code postal accolé,
    mention d'arrondissement.

    Careerjet compose en outre des lieux au TIRET (« Paris - Clichy,
    Hauts-de-Seine », « Orly, Val-de-Marne - Paris »). Un morceau qui en
    contient un est donc AUSSI éclaté autour, et les deux formes sont gardées :
    « paris clichy » ne correspond à aucune commune, « paris » et « clichy »
    oui. Le préfixe de département étant retiré avant, le tiret de
    « 75 - Paris 8e » n'atteint jamais ce découpage.
    """
    sortie: list[str] = []

    def _ajouter(brut: str) -> None:
        t = normaliser(_MOTIF_PREFIXE_DEPT.sub("", brut.strip()))
        t = _MOTIF_CODE_POSTAL.sub(" ", t)
        t = _MOTIF_ARRONDISSEMENT.sub(" ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t and t not in sortie:
            sortie.append(t)

    for brut in re.split(r"[,/|]", lieu or ""):
        _ajouter(brut)
        if re.search(r"\s[-–]\s", brut):
            for morceau in re.split(r"\s[-–]\s", brut):
                _ajouter(morceau)
    return sortie


def _departements_cites(lieu: str) -> set[str]:
    """Codes de département explicitement présents (format « 92 - SURESNES »)."""
    trouves = set()
    for brut in re.split(r"[,/|]", lieu or ""):
        m = _MOTIF_PREFIXE_DEPT.match(brut.strip())
        if m:
            trouves.add(m.group(1)[:2])
    return trouves


def hors_zone(lieu: str) -> bool:
    """Vrai si le lieu porte un marqueur EXPLICITE de hors Île-de-France.

    Barrière négative, évaluée AVANT toute reconnaissance de commune : « Paris,
    TX » contient le nom d'une commune francilienne et doit pourtant être
    rejeté. C'est le mode de panne réel de Jooble.
    """
    segments = _segments(lieu)
    for s in segments:
        if s in _ETATS_US or s in _REGIONS_HORS_IDF or s in _LIBELLES_HORS_IDF:
            return True
    # Libellés de région écrits en toutes lettres et éventuellement collés à
    # d'autres mots (« Beauvais, Hauts-de-France, France »).
    complet = normaliser(lieu)
    return any(libelle.replace("-", " ") in complet for libelle in _LIBELLES_HORS_IDF)


def resoudre(lieu: str) -> dict | None:
    """Commune d'Île-de-France reconnue dans ``lieu``, ou ``None``.

    Ne se prononce PAS sur l'appartenance à la région (voir ``est_idf``) : rend
    la commune quand elle est identifiée, ce qui sert aussi bien au filtre
    géographique qu'à la clé de déduplication.
    """
    if not lieu:
        return None
    if hors_zone(lieu):
        return None
    index = referentiel()["par_nom"]
    for segment in _segments(lieu):
        commune = index.get(segment)
        if commune is not None:
            return commune
    return None


def est_idf(lieu: str) -> bool:
    """Le lieu est-il en Île-de-France ? Tranche toujours (cf. l'en-tête).

    Un lieu VIDE rend ``True`` : c'est le comportement permissif d'origine
    (`filters.est_en_idf`), délibérément conservé — une source qui ne renseigne
    pas le lieu ne doit pas voir toutes ses offres disparaître.
    """
    if not (lieu or "").strip():
        return True
    if hors_zone(lieu):
        return False

    segments = _segments(lieu)
    # Signal régional explicite : « A8 » (JobSpy), « Île-de-France » (Adzuna).
    if CODE_ISO_IDF in segments:
        return True
    complet = normaliser(lieu)
    if "ile de france" in complet or "idf" in segments:
        return True
    # Département cité en préfixe (France Travail : « 94 - BONNEUIL SUR MARNE »).
    if _departements_cites(lieu) & DEPARTEMENTS_IDF:
        return True
    # Département nommé en toutes lettres (Careerjet).
    if any(s in _DEPARTEMENTS_IDF_NOMS for s in segments):
        return True
    # Quartier / lieu-dit qui n'est pas une commune (« La Défense »).
    if any(s in _LIEUX_DITS_IDF for s in segments):
        return True
    # Commune du référentiel.
    return resoudre(lieu) is not None


def cle_ville(lieu: str) -> str:
    """Ville CANONIQUE pour la déduplication : le code INSEE quand il est connu.

    ## Le défaut corrigé

    `dedup._ville` prenait le premier segment avant la virgule. Les quatre
    sources n'écrivent pas les lieux pareil, donc la même ville produisait
    quatre clés différentes :

        careerjet / jobspy   « paris », « nanterre »
        france_travail       « 75 paris », « 92 suresnes »   <- préfixe département
        adzuna               « 1er arrondissement »          <- arrondissement d'abord

    France Travail ne pouvait donc JAMAIS se dédoublonner contre une autre
    source. Mesuré sur 448 offres candidates : 447 clés avec la ville actuelle,
    411 sur titre+entreprise seuls, soit **au moins 36 doublons inter-sources
    invisibles**.

    Le code INSEE règle le problème à la racine : « Paris » et « 75 - Paris 8e
    Arrondissement » rendent tous deux ``75056``.

    Repli quand la commune est inconnue (hors Île-de-France, ou lieu exotique) :
    la forme normalisée du premier segment, c'est-à-dire l'ancien comportement
    débarrassé du préfixe de département. Deux offres hors région continuent
    donc de se dédoublonner entre elles.
    """
    commune = resoudre(lieu)
    if commune is not None:
        return commune["code"]
    segments = _segments(lieu)
    return segments[0] if segments else ""


# ---------------------------------------------------------------------------
# Génération du référentiel
# ---------------------------------------------------------------------------
def generer(chemin: str = CHEMIN_REFERENTIEL) -> int:
    """Refait le fichier depuis geo.api.gouv.fr. Rend le nombre de communes.

    Écriture ATOMIQUE via ``reference.ecrire_atomique`` : le référentiel est
    une entrée de mesure, l'écraser à moitié le rendrait illisible sans que
    rien ne le dise.
    """
    import requests

    import reference
    from sources import masquer_secrets  # noqa: F401 - active truststore via le paquet

    reponse = requests.get(
        "https://geo.api.gouv.fr/communes",
        params={"codeRegion": CODE_REGION_IDF, "fields": "nom,code,codeDepartement",
                "format": "json"},
        timeout=60,
    )
    reponse.raise_for_status()
    brutes = reponse.json()

    communes = sorted(
        ({"code": c["code"], "nom": c["nom"], "departement": c["codeDepartement"]}
         for c in brutes),
        key=lambda c: c["code"],
    )
    from datetime import date

    donnees = {
        "meta": {
            "source": _URL_SOURCE,
            "genere_le": date.today().isoformat(),
            "generateur": "python communes.py --generer",
            "code_region": CODE_REGION_IDF,
            "n": len(communes),
            "commentaire": (
                "Fichier GÉNÉRÉ — ne pas éditer à la main. Référentiel figé et "
                "versionné pour que les mesures de ranking restent reproductibles."
            ),
        },
        "communes": communes,
    }
    reference.ecrire_atomique(
        chemin, json.dumps(donnees, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    )
    referentiel.cache_clear()
    return len(communes)


# Lieux réels rencontrés dans `stages.db` et dans les sondes du 2026-09-05,
# un par format de source. Sert d'auto-test hors ligne.
_EXEMPLES = [
    ("Paris", True),
    ("Paris, Ile-de-France", True),
    ("12ème Arrondissement, Paris", True),
    ("75 - Paris 8e Arrondissement", True),
    ("92 - SURESNES", True),
    ("77 - OZOIR LA FERRIERE", True),
    ("Neuilly-sur-Seine, A8, FR", True),
    ("Vélizy-Villacoublay, A8, FR", True),
    ("Les Clayes-sous-Bois", True),
    ("L'Haÿ-les-Roses", True),
    # Compositions Careerjet au tiret, rencontrées en base.
    ("Paris - Clichy, Hauts-de-Seine", True),
    ("Orly, Val-de-Marne - Paris", True),
    ("Montmartre, SK - Paris", True),
    ("Paris, TX", False),
    ("Powderly, TX", False),
    ("Lucé, CVL, FR", False),
    ("Beauvais, Hauts-de-France, France", False),
    ("Chartres, Centre-Val de Loire, France", False),
    ("Lyon", False),
    ("", True),
]


if __name__ == "__main__":
    import argparse

    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Référentiel des communes d'Île-de-France.")
    p.add_argument("--generer", action="store_true",
                   help="Refait communes_idf.json depuis geo.api.gouv.fr.")
    p.add_argument("--tester", action="store_true",
                   help="Résout des lieux réels des quatre sources (hors ligne).")
    args = p.parse_args()

    if args.generer:
        n = generer()
        print(f"✅ {n} commune(s) écrite(s) dans {CHEMIN_REFERENTIEL}.")

    if args.tester or not args.generer:
        meta = referentiel()["meta"]
        print(f"Référentiel : {meta.get('n', 0)} communes, généré le "
              f"{meta.get('genere_le', '?')}\n")
        erreurs = 0
        for lieu, attendu in _EXEMPLES:
            obtenu = est_idf(lieu)
            commune = resoudre(lieu)
            marque = "✓" if obtenu == attendu else "✗"
            erreurs += obtenu != attendu
            print(f"  {marque} {lieu or '(vide)':40} IdF={str(obtenu):5} "
                  f"clé={cle_ville(lieu):10} {commune['nom'] if commune else ''}")
        print(f"\n{len(_EXEMPLES) - erreurs}/{len(_EXEMPLES)} corrects.")
        raise SystemExit(1 if erreurs else 0)
