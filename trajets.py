"""
trajets.py — Temps de trajet EN VOITURE depuis chaque origine des jobs étudiants.

## Ce qu'on calcule

Pour une offre : sa commune (résolue depuis le champ « lieu »), puis, pour
chaque origine de recherche.yaml, un temps de trajet :

    brut      durée OpenRouteService `driving-car`, sans trafic
    ajusté    brut × facteur de trafic (1,3 par défaut) — c'est lui qui filtre

L'offre est GARDÉE si son temps ajusté depuis AU MOINS UNE origine est
strictement sous le seuil (45 min par défaut). Jamais de filtre par
département : le département ne dit rien d'un trajet.

## Ce qu'on ne jette jamais

- Une offre dont la commune n'est pas reconnue : trajet INCONNU, gardée.
- Un trajet que le routage n'a pas pu calculer (clé absente, API injoignable,
  quota épuisé, point non routable) : repli sur la distance à vol d'oiseau ×
  facteur de détour, convertie en minutes à une vitesse moyenne réglable, et
  marquée ESTIMATION. L'estimation filtre comme un temps calculé, mais le
  rapport la signale — et un conseil en tête du bilan dit pourquoi le routage
  a manqué.

## Quota OpenRouteService (vérifié le 2026-09-17)

- `/v2/matrix/driving-car` : 3 500 routes par requête au plus (refus 400,
  code 6004, message explicite) ;
- en-têtes `X-Ratelimit-Limit: 50` et `X-Ratelimit-Reset` 24 h après le
  premier appel : 50 requêtes MATRIX PAR JOUR sur cette clé. Une requête
  refusée consomme le quota.

D'où : une seule requête par lot (3 origines × jusqu'à 1 166 communes), un
cache par (origine, commune) dans `trajets.json`, et un plafond d'appels par
run (`config.ORS_APPELS_MAX_PAR_RUN`). Seuls les temps CALCULÉS sont mis en
cache ; une estimation est refaite à chaque run, pour qu'un routage revenu la
remplace. Pas de créneau horaire : une durée par paire.

## Référentiel des communes

`communes_trajets.json`, GÉNÉRÉ et versionné (`python trajets.py --generer`) :
Île-de-France, les six départements voisins de la Seine-et-Marne (02, 10, 45,
51, 60, 89) et les arrondissements de Paris, avec leur centre. Distinct de
`communes_idf.json`, qui décide des clés de déduplication des stages et
qu'on ne touche pas.

Utilisation :
    python trajets.py --generer              # refait communes_trajets.json
    python trajets.py "77 - MEAUX" "Chelles, Seine-et-Marne"   # résout et calcule
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import communes
import config
import observabilite
import recherche

logger = logging.getLogger(__name__)

CHEMIN_REFERENTIEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "communes_trajets.json")

DEPARTEMENTS = ("75", "77", "78", "91", "92", "93", "94", "95",
                "02", "10", "45", "51", "60", "89")

# Noms de département tels que les sources les écrivent (Careerjet abrège :
# « Seine-St-Denis »). Forme normalisée -> code.
_NOMS_DEPARTEMENTS = {
    "paris": "75", "seine et marne": "77", "yvelines": "78", "essonne": "91",
    "hauts de seine": "92", "seine saint denis": "93", "seine st denis": "93",
    "val de marne": "94", "val d oise": "95", "aisne": "02", "aube": "10",
    "loiret": "45", "marne": "51", "oise": "60", "yonne": "89",
}

_MOTIF_ARRONDISSEMENT_PARIS = re.compile(
    r"\b(\d{1,2})\s*(?:er|ere|e|eme)?\s*arrondissement\b|\bparis\s*(\d{1,2})\s*(?:er|ere|e|eme)?\b")

ORS_URL_MATRIX = "https://api.openrouteservice.org/v2/matrix/driving-car"


# ---------------------------------------------------------------------------
# Référentiel et résolution d'un lieu
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def referentiel(chemin: str = CHEMIN_REFERENTIEL) -> dict:
    """{"par_nom": nom normalisé -> [communes], "par_code": code -> commune, "meta": …}."""
    if not os.path.exists(chemin):
        logger.warning("Référentiel des trajets absent (%s) : aucune commune ne sera "
                       "résolue. `python trajets.py --generer`.", chemin)
        return {"par_nom": {}, "par_code": {}, "meta": {}}
    with open(chemin, encoding="utf-8") as f:
        donnees = json.load(f)
    par_nom: dict[str, list[dict]] = {}
    par_code: dict[str, dict] = {}
    for commune in donnees["communes"] + donnees.get("arrondissements", []):
        par_code[commune["code"]] = commune
    for commune in donnees["communes"]:
        nom = communes.normaliser(commune["nom"])
        for forme in {nom, re.sub(r"^(le|la|les|l) ", "", nom)}:
            par_nom.setdefault(forme, []).append(commune)
    return {"par_nom": par_nom, "par_code": par_code, "meta": donnees.get("meta", {})}


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance à vol d'oiseau entre deux (lon, lat), en km."""
    lo1, la1, lo2, la2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _departements_cites(lieu: str) -> set[str]:
    cites = set(communes._departements_cites(lieu))
    complet = f" {communes.normaliser(lieu)} "
    for nom, code in _NOMS_DEPARTEMENTS.items():
        if nom != "paris" and f" {nom} " in complet:
            cites.add(code)
    return cites


# Lieux qui ne sont pas des communes mais désignent un point précis : l'aéroport
# Charles-de-Gaulle est sur la commune de Roissy-en-France. Cherchés dans le
# lieu normalisé entier (« Aéroport Paris-Roissy-Charles-de-Gaulle, A8, FR »,
# « Charles-de-Gaulles Aéroport, Val-d'Oise » — la faute est d'origine).
# « Marne-la-Vallée » n'y est PAS : le secteur couvre des communes de Noisy à
# Chessy, et lui donner un centre inventerait un trajet.
_LIEUX_DITS = {
    "charles de gaulle": "95527",
    "charles de gaulles": "95527",
    "roissy cdg": "95527",
    # Anciens noms encore écrits par les employeurs : « Arnouville-lès-Gonesse »
    # est « Arnouville » depuis 2011.
    "arnouville les gonesse": "95019",
}


def _variantes(segment: str) -> list[str]:
    """Formes à essayer pour un segment, de la plus fidèle à la plus réduite.

    France Travail abrège (« ST MAUR DES FOSSES »), numérote l'arrondissement
    sans suffixe (« PARIS 11 ») et ajoute « (Dept.) » après Paris.
    """
    formes = [segment]
    developpe = re.sub(r"\bste\b", "sainte", re.sub(r"\bst\b", "saint", segment))
    sans_bruit = re.sub(r"\s+cedex(\s+\d{1,2})?$", "", developpe)
    sans_bruit = re.sub(r"\s+\d{1,2}$|\s+dept$", "", sans_bruit).strip()
    for forme in (developpe, sans_bruit):
        if forme and forme not in formes:
            formes.append(forme)
    return formes


def resoudre(lieu: str, pres_de: tuple[float, float] | None = None) -> dict | None:
    """Commune du référentiel désignée par ``lieu``, ou None.

    Homonymes (« Saint-Mard » existe en 77 et en 02) : d'abord le département
    cité dans le lieu, sinon la commune la plus proche de ``pres_de`` (le
    centre des origines — les sources ont été interrogées autour d'elles).
    Paris est précisé à l'arrondissement quand le lieu le donne.
    """
    if not (lieu or "").strip():
        return None
    ref = referentiel()
    complet = f" {communes.normaliser(lieu)} "
    for lieu_dit, code in _LIEUX_DITS.items():
        if f" {lieu_dit} " in complet and code in ref["par_code"]:
            return ref["par_code"][code]
    cites = _departements_cites(lieu)
    segments = [v for s in communes._segments(lieu) for v in _variantes(s)]
    for segment in segments:
        candidats = ref["par_nom"].get(segment)
        if not candidats:
            continue
        if cites and any(c["departement"] in cites for c in candidats):
            candidats = [c for c in candidats if c["departement"] in cites]
        if len(candidats) > 1 and pres_de is not None:
            candidats = sorted(candidats, key=lambda c: distance_km(pres_de, (c["lon"], c["lat"])))
        commune = candidats[0]
        if commune["code"] == "75056":
            m = _MOTIF_ARRONDISSEMENT_PARIS.search(communes.normaliser(lieu))
            numero = int(next(g for g in m.groups() if g)) if m else 0
            commune = ref["par_code"].get(f"751{numero:02d}", commune) if 1 <= numero <= 20 else commune
        return commune
    return None


# ---------------------------------------------------------------------------
# Trajets
# ---------------------------------------------------------------------------
@dataclass
class Trajet:
    """Un trajet origine -> commune d'une offre."""

    brut_min: float          # durée sans trafic (routage) ou estimée
    ajuste_min: float        # brut × facteur de trafic : la valeur qui filtre
    distance_km: float       # distance routière (routage) ou estimée
    estimation: bool         # True = vol d'oiseau × détour, pas un routage


def coordonnees_origine(origine: recherche.Origine) -> tuple[float, float] | None:
    """Coordonnées explicites de l'origine, sinon le centre de sa commune."""
    if origine.coordonnees:
        return tuple(origine.coordonnees)
    commune = referentiel()["par_code"].get(origine.insee)
    return (commune["lon"], commune["lat"]) if commune else None


class Calculateur:
    """Calcule et met en cache les trajets (origine, commune) d'un run."""

    def __init__(self, jobs: recherche.JobsEtudiants, cle_api: str | None,
                 chemin_cache: Path, session=None, appels_max: int | None = None):
        import requests

        self.jobs = jobs
        self.cle_api = cle_api
        self.chemin_cache = Path(chemin_cache)
        self.session = session or requests
        self.appels_max = config.ORS_APPELS_MAX_PAR_RUN if appels_max is None else appels_max
        self.appels = 0
        self.origines = {nom: coordonnees_origine(o) for nom, o in jobs.origines.items()}
        self._cache = self._lire_cache()

    @classmethod
    def depuis_config(cls) -> "Calculateur":
        from dotenv import load_dotenv

        load_dotenv()
        return cls(recherche.charger().student_jobs, os.getenv("ORS_API_KEY"),
                   config.CHEMIN_CACHE_TRAJETS)

    # -- cache ---------------------------------------------------------------
    def _lire_cache(self) -> dict:
        try:
            with open(self.chemin_cache, encoding="utf-8") as f:
                return json.load(f).get("entrees", {})
        except (OSError, ValueError, AttributeError):
            return {}

    def _ecrire_cache(self) -> None:
        try:
            with open(self.chemin_cache, "w", encoding="utf-8") as f:
                json.dump({"moteur": "openrouteservice driving-car", "entrees": self._cache},
                          f, ensure_ascii=False, indent=0, sort_keys=True)
        except OSError as err:
            logger.warning("Cache des trajets non écrit (%s).", err)

    def _en_cache(self, origine: str, commune: dict) -> dict | None:
        """Entrée valide : même origine (coordonnées) et même point d'arrivée."""
        entree = self._cache.get(f"{origine}|{commune['code']}")
        if not entree:
            return None
        if entree.get("origine") != list(self.origines[origine]) or \
           entree.get("arrivee") != [commune["lon"], commune["lat"]]:
            return None
        return entree

    # -- calcul --------------------------------------------------------------
    @property
    def centre(self) -> tuple[float, float] | None:
        points = [p for p in self.origines.values() if p]
        if not points:
            return None
        return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))

    def _estimer(self, origine: str, commune: dict) -> Trajet:
        reglage = self.jobs.trajet
        km = distance_km(self.origines[origine], (commune["lon"], commune["lat"])) * reglage.facteur_detour
        brut = km / reglage.vitesse_estimation_kmh * 60
        return Trajet(round(brut, 1), round(brut * reglage.facteur_trafic, 1), round(km, 1), True)

    def _router(self, manquantes: list[tuple[str, dict]]) -> str | None:
        """Routage des paires manquantes par la matrice ORS. Rend la raison d'un échec, ou None."""
        if not self.cle_api:
            return "clé ORS_API_KEY absente du .env"
        par_origine: dict[str, list[dict]] = {}
        for origine, commune in manquantes:
            par_origine.setdefault(origine, []).append(commune)
        origines = sorted(par_origine)
        destinations = list({c["code"]: c for o in origines for c in par_origine[o]}.values())
        par_lot = max(1, config.ORS_ROUTES_MAX_PAR_APPEL // len(origines))
        for debut in range(0, len(destinations), par_lot):
            if self.appels >= self.appels_max:
                return f"plafond de {self.appels_max} appel(s) ORS par run atteint"
            lot = destinations[debut:debut + par_lot]
            corps = {
                "locations": [list(self.origines[o]) for o in origines]
                             + [[c["lon"], c["lat"]] for c in lot],
                "sources": list(range(len(origines))),
                "destinations": list(range(len(origines), len(origines) + len(lot))),
                "metrics": ["duration", "distance"], "units": "km",
            }
            self.appels += 1
            try:
                reponse = self.session.post(
                    ORS_URL_MATRIX, json=corps, timeout=config.TIMEOUT_HTTP,
                    headers={"Authorization": self.cle_api, "Content-Type": "application/json"})
            except Exception as err:  # noqa: BLE001 - le routage ne doit jamais rien casser
                return f"ORS injoignable ({type(err).__name__})"
            if reponse.status_code != 200:
                try:
                    detail = reponse.json().get("error", {}).get("message", "")
                except (ValueError, AttributeError):
                    detail = ""
                return f"ORS HTTP {reponse.status_code} {detail}".strip()
            donnees = reponse.json()
            aujourd_hui = date.today().isoformat()
            for i, origine in enumerate(origines):
                for j, commune in enumerate(lot):
                    duree = donnees["durations"][i][j]
                    distance = donnees["distances"][i][j]
                    if duree is None or distance is None:
                        continue   # non routable : restera une estimation
                    self._cache[f"{origine}|{commune['code']}"] = {
                        "duree_s": duree, "distance_km": distance, "calcule_le": aujourd_hui,
                        "origine": list(self.origines[origine]),
                        "arrivee": [commune["lon"], commune["lat"]],
                    }
        return None

    def trajets(self, communes_offres: list[dict]) -> dict[str, dict[str, Trajet]]:
        """code commune -> {origine -> Trajet} pour toutes les communes données."""
        uniques = list({c["code"]: c for c in communes_offres}.values())
        origines = [o for o, p in self.origines.items() if p]
        manquantes = [(o, c) for c in uniques for o in origines if self._en_cache(o, c) is None]
        echec = None
        if manquantes:
            echec = self._router(manquantes)
            self._ecrire_cache()
        reglage = self.jobs.trajet
        resultat: dict[str, dict[str, Trajet]] = {}
        estimees = 0
        for commune in uniques:
            par_origine = {}
            for origine in origines:
                entree = self._en_cache(origine, commune)
                if entree is None:
                    par_origine[origine] = self._estimer(origine, commune)
                    estimees += 1
                    continue
                brut = entree["duree_s"] / 60
                par_origine[origine] = Trajet(round(brut, 1), round(brut * reglage.facteur_trafic, 1),
                                              round(entree["distance_km"], 1), False)
            resultat[commune["code"]] = par_origine
        if estimees:
            observabilite.conseiller(
                "trajets",
                f"Trajets ESTIMÉS pour {estimees} paire(s) origine-commune (vol d'oiseau × "
                f"{reglage.facteur_detour}, {reglage.vitesse_estimation_kmh} km/h) : "
                f"{echec or 'points non routables'}.",
            )
        return resultat


# ---------------------------------------------------------------------------
# Annotation et filtre des offres
# ---------------------------------------------------------------------------
def annoter(offres: list, calculateur: Calculateur) -> None:
    """Pose `commune_trajet` et `trajets` sur chaque offre. Ne retire rien."""
    centre = calculateur.centre
    resolues = []
    for offre in offres:
        commune = resoudre(offre.location, pres_de=centre)
        offre.commune_trajet = commune["code"] if commune else ""
        offre.trajets = {}
        if commune:
            resolues.append((offre, commune))
    table = calculateur.trajets([c for _, c in resolues]) if resolues else {}
    for offre, commune in resolues:
        offre.trajets = {o: asdict(t) for o, t in table.get(commune["code"], {}).items()}
    inconnues = len(offres) - len(resolues)
    if inconnues:
        observabilite.conseiller(
            "trajets_inconnus",
            f"{inconnues} offre(s) sans commune reconnue : trajet INCONNU, gardées.",
        )
    logger.info("Trajets : %d offre(s), %d commune(s) résolue(s), %d inconnue(s), "
                "%d appel(s) ORS.", len(offres), len({c['code'] for _, c in resolues}),
                inconnues, calculateur.appels)


def est_assez_proche(offre, seuil_minutes: float) -> bool:
    """Temps ajusté strictement sous le seuil depuis AU MOINS UNE origine.

    Sans trajet (commune inconnue) : gardée — on ne jette pas ce qu'on ne sait
    pas mesurer.
    """
    if not offre.trajets:
        return True
    return any(t["ajuste_min"] < seuil_minutes for t in offre.trajets.values())


def filtrer(offres: list, calculateur: Calculateur) -> list:
    """Annote puis garde les offres assez proches d'une origine. Tient le bilan."""
    annoter(offres, calculateur)
    seuil = calculateur.jobs.trajet.seuil_minutes
    releve = observabilite.actif()
    gardees = []
    for offre in offres:
        if est_assez_proche(offre, seuil):
            gardees.append(offre)
        elif releve is not None:
            releve.retirer_survivante(offre.source, "trop-loin", offre.familles)
    logger.info("Filtre trajet (< %s min ajustées depuis une origine) : %d gardée(s) sur %d.",
                seuil, len(gardees), len(offres))
    return gardees


# ---------------------------------------------------------------------------
# Génération du référentiel
# ---------------------------------------------------------------------------
def generer(chemin: str = CHEMIN_REFERENTIEL) -> int:
    """Refait communes_trajets.json depuis geo.api.gouv.fr. Rend le nombre de communes."""
    import requests

    import reference
    from sources import masquer_secrets  # noqa: F401 - active truststore via le paquet

    def point(c):
        lon, lat = c["centre"]["coordinates"]
        return {"code": c["code"], "nom": c["nom"],
                "departement": c.get("codeDepartement", c["code"][:2]),
                "lon": round(lon, 5), "lat": round(lat, 5)}

    toutes = []
    for dep in DEPARTEMENTS:
        r = requests.get(f"https://geo.api.gouv.fr/departements/{dep}/communes",
                         params={"fields": "nom,code,codeDepartement,centre"}, timeout=60)
        r.raise_for_status()
        toutes += [point(c) for c in r.json() if c.get("centre")]
    r = requests.get("https://geo.api.gouv.fr/communes",
                     params={"codeDepartement": "75", "type": "arrondissement-municipal",
                             "fields": "nom,code,centre"}, timeout=60)
    r.raise_for_status()
    arrondissements = [{**point(c), "departement": "75"} for c in r.json()]

    donnees = {
        "meta": {
            "source": "https://geo.api.gouv.fr (communes par département, arrondissements de Paris)",
            "departements": list(DEPARTEMENTS),
            "genere_le": date.today().isoformat(),
            "generateur": "python trajets.py --generer",
            "n": len(toutes),
            "commentaire": "Fichier GÉNÉRÉ — ne pas éditer à la main. Centres des communes.",
        },
        "communes": sorted(toutes, key=lambda c: c["code"]),
        "arrondissements": sorted(arrondissements, key=lambda c: c["code"]),
    }
    reference.ecrire_atomique(chemin, json.dumps(donnees, ensure_ascii=False, indent=0) + "\n")
    referentiel.cache_clear()
    return len(toutes)


if __name__ == "__main__":
    import argparse

    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(description="Temps de trajet des jobs étudiants.")
    p.add_argument("--generer", action="store_true", help="Refait communes_trajets.json.")
    p.add_argument("lieux", nargs="*", help="Lieux à résoudre et à router.")
    args = p.parse_args()
    if args.generer:
        print(f"✅ {generer()} commune(s) écrite(s) dans {CHEMIN_REFERENTIEL}.")
    if args.lieux:
        calc = Calculateur.depuis_config()
        for lieu in args.lieux:
            commune = resoudre(lieu, pres_de=calc.centre)
            if not commune:
                print(f"  {lieu:40} commune inconnue")
                continue
            for origine, t in calc.trajets([commune])[commune["code"]].items():
                print(f"  {lieu:40} {commune['nom']:25} {origine:8} brut {t.brut_min:5.1f} min  "
                      f"ajusté {t.ajuste_min:5.1f} min  {t.distance_km:5.1f} km"
                      f"{'  (estimation)' if t.estimation else ''}")
