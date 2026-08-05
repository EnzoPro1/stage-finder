"""
sources/romeo.py — Rapprochement « intitulé libre → code métier ROME » via
l'API ROMEO v2 de France Travail (modèle d'IA hébergé par France Travail).

Pourquoi c'est utile ICI : les volumes de marché mesurés par MOT-CLÉ sont
trompeurs. Mesuré en texte libre, « stage cybersécurité » renvoie 0 offre sur
31 jours en Île-de-France ; la même mesure par code ROME (M1856, Expert en
cybersécurité) en renvoie 9. Le mot-clé ne matche que les intitulés qui
reprennent le mot exact, là où le code ROME rassemble tout un métier.

ROMEO convertit donc ton profil en codes métier, et ``market.py`` compte les
offres par code. Le résultat est comparable d'un métier à l'autre, ce qu'une
recherche plein texte ne garantit jamais.

Deux précautions prises :
- **Résultat mis en cache sur disque** : ton profil ne change presque jamais, et
  l'API est limitée à 3 appels/seconde. On n'interroge donc ROMEO qu'à la
  première demande (ou quand la liste des métiers suivis change).
- **Dégradation gracieuse** : si ROMEO est indisponible, on renvoie ``{}`` et
  l'appelant retombe sur les codes ROME écrits en dur dans la config.

Testable isolément :  python -m sources.romeo
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import requests

import config
from sources.france_travail import obtenir_jeton

logger = logging.getLogger(__name__)

NOM_SOURCE = "romeo"

_URL = "https://api.francetravail.io/partenaire/romeo/v2/predictionMetiers"
_SCOPE = "api_romeov2"

# Nom d'appelant exigé par l'API (champ ``options.nomAppelant``, obligatoire).
_NOM_APPELANT = "stage-finder"

CHEMIN_CACHE = ".rome_cache.json"


def _empreinte(intitules: list[str], n: int) -> str:
    """Empreinte de la demande : change si la liste des métiers suivis change."""
    return hashlib.sha256(json.dumps([sorted(intitules), n]).encode()).hexdigest()[:16]


def _lire_cache(empreinte: str) -> dict | None:
    if not os.path.exists(CHEMIN_CACHE):
        return None
    try:
        with open(CHEMIN_CACHE, "r", encoding="utf-8") as f:
            paquet = json.load(f)
    except (OSError, ValueError):
        return None
    return paquet.get("metiers") if paquet.get("empreinte") == empreinte else None


def _ecrire_cache(empreinte: str, metiers: dict) -> None:
    try:
        with open(CHEMIN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"empreinte": empreinte, "metiers": metiers}, f, ensure_ascii=False)
    except OSError as err:
        logger.warning("ROMEO : cache non écrit (%s).", err)


def predire_metiers(
    intitules: list[str], n_resultats: int = 3, utiliser_cache: bool = True
) -> dict[str, list[dict]]:
    """Associe à chaque intitulé libre ses meilleurs codes ROME.

    Retourne ``{intitulé: [{"code": "M1889", "libelle": "...", "score": 0.83}, …]}``.
    Dictionnaire VIDE si ROMEO est indisponible (l'appelant doit gérer ce cas).
    """
    if not intitules:
        return {}
    empreinte = _empreinte(intitules, n_resultats)
    if utiliser_cache:
        cache = _lire_cache(empreinte)
        if cache is not None:
            logger.info("ROMEO : %d métier(s) relus du cache.", len(cache))
            return cache

    jeton = obtenir_jeton(_SCOPE)
    if not jeton:
        logger.warning("ROMEO ignoré : authentification France Travail indisponible.")
        return {}

    charge = {
        # L'identifiant sert uniquement à rapatrier chaque réponse sur sa demande.
        "appellations": [
            {"intitule": intitule, "identifiant": str(i)}
            for i, intitule in enumerate(intitules, 1)
        ],
        # ``nomAppelant`` est obligatoire (l'API répond 400 sans lui).
        # On demande PLUS d'appellations qu'on ne veut de métiers : comme
        # plusieurs appellations retombent sur le même code ROME, demander
        # exactement N appellations n'en donne souvent qu'un ou deux distincts.
        "options": {"nomAppelant": _NOM_APPELANT,
                    "nbResultats": max(5, n_resultats * 3)},
    }
    try:
        reponse = requests.post(
            _URL,
            json=charge,
            headers={"Authorization": f"Bearer {jeton}",
                     "Content-Type": "application/json", "Accept": "application/json"},
            timeout=config.TIMEOUT_HTTP + 20,  # inférence IA : plus lent qu'une recherche
        )
        reponse.raise_for_status()
        donnees = reponse.json()
    except (requests.exceptions.RequestException, ValueError) as err:
        logger.warning("ROMEO : appel en échec (%s).", type(err).__name__)
        return {}

    par_identifiant = {str(i): intitule for i, intitule in enumerate(intitules, 1)}
    metiers: dict[str, list[dict]] = {}
    for item in donnees if isinstance(donnees, list) else []:
        intitule = par_identifiant.get(str(item.get("identifiant")))
        if intitule is None:
            continue
        # ROMEO raisonne en APPELLATIONS : plusieurs d'entre elles retombent
        # souvent sur le même code ROME (« Data scientist », « Ingénieur data
        # scientist », « Expert en datascience » -> tous M1405). Sans
        # déduplication, demander 2 métiers pour « data scientist » en
        # renverrait un seul, et le second métier réellement distinct serait
        # perdu. On dédoublonne par code en gardant le meilleur score.
        meilleurs: dict[str, dict] = {}
        for m in item.get("metiersRome", []):
            code = m.get("codeRome")
            if not code:
                continue
            score = round(float(m.get("scorePrediction") or 0), 3)
            if code not in meilleurs or score > meilleurs[code]["score"]:
                meilleurs[code] = {"code": code, "libelle": m.get("libelleRome"),
                                   "score": score}
        metiers[intitule] = sorted(
            meilleurs.values(), key=lambda m: m["score"], reverse=True
        )

    if metiers:
        _ecrire_cache(empreinte, metiers)
    logger.info("ROMEO : %d intitulé(s) rapproché(s) du référentiel ROME.", len(metiers))
    return metiers


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    resultat = predire_metiers(config.METIERS_SUIVIS, utiliser_cache=False)
    for intitule, codes in resultat.items():
        print(f"\n« {intitule} »")
        for m in codes:
            print(f"   {m['code']}  {m['libelle']:<45} score {m['score']}")
    if not resultat:
        print("ROMEO indisponible — market.py retombera sur config.ROME_REPLI.")
