"""
sources/marche_travail.py — API « Marché du travail » de France Travail (STMT).

C'est l'indicateur qui manquait au tableau de bord. Jusqu'ici, ``market.py``
mesurait le CÔTÉ OFFRE (combien d'annonces sont publiées). Cette API donne
l'indicateur PERSP_2 : les **difficultés de recrutement par métier et par
territoire**, calculé par France Travail à partir des offres, des demandeurs et
des embauches.

Lecture du signe : plus l'indicateur est ÉLEVÉ, plus les employeurs peinent à
recruter sur ce métier — donc plus le marché est FAVORABLE au candidat. C'est
la convention de l'indicateur de tension France Travail / DARES.

Deux pièges d'intégration, découverts à la sonde et documentés ici parce
qu'aucun message d'erreur ne les révèle :

1. **Le scope est DOUBLE** : ``api_stats-offres-demandes-emploiv1`` seul donne
   un jeton valide… et un 403 vide sur tous les endpoints. Il faut y ajouter
   ``offresetdemandesemploi`` (cf. bloc ``security`` de la spec OpenAPI).
2. **Le territoire suit le référentiel maison** : on récupère le code région
   depuis ``/referentiel/territoires/REG`` plutôt que de supposer l'INSEE
   (ça tombe juste — l'Île-de-France est bien « 11 » — mais rien ne le garantit).

Testable isolément :  python -m sources.marche_travail
"""

from __future__ import annotations

import logging

import requests

import config
from sources.france_travail import obtenir_jeton

logger = logging.getLogger(__name__)

NOM_SOURCE = "marche_travail"

_BASE = "https://api.francetravail.io/partenaire/stats-offres-demandes-emploi/v1"

# Scope DOUBLE : voir le point 1 de la docstring. C'est LA subtilité de cette API.
_SCOPE = "api_stats-offres-demandes-emploiv1 offresetdemandesemploi"

# Code de l'indicateur principal parmi les 7 de la nomenclature TYPE_TENSION.
# Les 6 autres (attractivité salariale, conditions de travail, manque de main
# d'œuvre…) expliquent D'OÙ vient la tension : on les garde pour l'affichage.
_CODE_PRINCIPAL = "PERSPECTIVE"


def _entetes() -> dict | None:
    jeton = obtenir_jeton(_SCOPE)
    if not jeton:
        return None
    return {"Authorization": f"Bearer {jeton}",
            "Content-Type": "application/json", "Accept": "application/json"}


def code_region(libelle: str = "ILE-DE-FRANCE") -> str | None:
    """Code région dans le référentiel France Travail, ou ``None``."""
    entetes = _entetes()
    if entetes is None:
        return None
    try:
        reponse = requests.get(f"{_BASE}/referentiel/territoires/REG",
                               headers=entetes, timeout=config.TIMEOUT_HTTP)
        reponse.raise_for_status()
        territoires = reponse.json().get("territoires", [])
    except (requests.exceptions.RequestException, ValueError) as err:
        logger.warning("Marché du travail : référentiel territoires (%s).", type(err).__name__)
        return None

    cible = libelle.upper().replace("Î", "I")
    for t in territoires:
        if t.get("libelleTerritoire", "").upper().replace("Î", "I") == cible:
            return t.get("codeTerritoire")
    return None


def tension(code_rome: str, code_territoire: str = "11") -> dict | None:
    """Indicateur de tension du métier ``code_rome`` sur le territoire donné.

    Retourne ``{"principal": float | None, "annee": str, "details":
    [{"code", "libelle", "valeur"}, …]}``, ou ``None`` si la mesure échoue —
    jamais un zéro, qui se lirait comme « aucune tension ».
    """
    entetes = _entetes()
    if entetes is None:
        return None

    corps = {
        "codeTypeTerritoire": "REG",
        "codeTerritoire": code_territoire,
        "codeTypeActivite": "ROME",
        "codeActivite": code_rome,
        # PERSP_2 n'existe qu'en pas ANNUEL (cf. /referentiel/details-indicateurs).
        "codeTypePeriode": "ANNEE",
        "codeTypeNomenclature": "TYPE_TENSION",
    }
    try:
        reponse = requests.post(f"{_BASE}/indicateur/stat-perspective-employeur",
                                headers=entetes, json=corps,
                                timeout=config.TIMEOUT_HTTP)
        reponse.raise_for_status()
        valeurs = reponse.json().get("listeValeursParPeriode", [])
    except (requests.exceptions.RequestException, ValueError) as err:
        logger.warning(
            "Marché du travail : tension %s indisponible (%s).", code_rome, type(err).__name__
        )
        return None

    if not valeurs:
        return None

    # L'API renvoie toutes les périodes disponibles : on ne garde que la plus
    # récente, sinon on mélangerait des millésimes dans un même verdict.
    derniere = max(v.get("codePeriode", "") for v in valeurs)
    recentes = [v for v in valeurs if v.get("codePeriode") == derniere]

    details, principal = [], None
    for v in recentes:
        valeur = v.get("valeurPrincipaleDecimale")
        if v.get("codeNomenclature") == _CODE_PRINCIPAL:
            principal = valeur
        else:
            details.append({"code": v.get("codeNomenclature"),
                            "libelle": v.get("libNomenclature"),
                            "valeur": valeur})
    details.sort(key=lambda d: (d["valeur"] is None, -(d["valeur"] or 0)))
    return {"principal": principal, "annee": derniere,
            "libelle_metier": recentes[0].get("libActivite"), "details": details}


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    region = code_region() or "11"
    print(f"Région Île-de-France = {region}\n")
    for rome in ("M1889", "M1882", "M1856", "M1405"):
        t = tension(rome, region)
        if t is None:
            print(f"{rome} : indisponible")
            continue
        print(f"{rome}  {t['libelle_metier'][:48]:<48} tension {t['principal']} ({t['annee']})")
        for d in t["details"][:3]:
            print(f"      {d['libelle']:<28} {d['valeur']}")
