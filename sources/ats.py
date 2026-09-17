"""
sources/ats.py — Pages carrières publiques des entreprises (Greenhouse, Lever, Ashby).

Une entreprise = une ligne `entreprises` de recherche.yaml : son nom, sa
plateforme, son identifiant de tableau. Chaque plateforme expose une API
publique, sans clé, qui rend TOUTES les annonces ouvertes du tableau.

Endpoints vérifiés en réel le 2026-09-17 :

  greenhouse  GET https://boards-api.greenhouse.io/v1/boards/<id>/jobs?content=true
              {"jobs": [...]} ; identifiant inconnu -> 404
  lever       GET https://api.lever.co/v0/postings/<id>?mode=json
              [...] ; identifiant inconnu -> 404 ; « mistral » -> 200 et [] (compte
              sans annonce). L'hôte européen api.eu.lever.co n'a répondu pour
              AUCUNE entreprise essayée : non pris en charge, faute de vérification.
  ashby       GET https://api.ashbyhq.com/posting-api/job-board/<id>
              {"jobs": [...]} ; identifiant inconnu -> 404 ; `isListed` à respecter

Ce qui est commun aux trois, et vit ici :

- un appel par entreprise ; un 404 est un INCIDENT (identifiant faux), un
  tableau vide un CONSEIL en tête du bilan (identifiant à vérifier) — sans ça,
  une faute de frappe dans la liste passerait pour une entreprise qui ne
  recrute pas ;
- le LIEU préféré : une annonce multi-sites (« New York, NY; Paris, France »)
  serait rejetée par le filtre Île-de-France à cause de « NY ». On retient le
  premier lieu français, sinon le premier ;
- les FAMILLES : un tableau d'entreprise n'est pas une requête par terme. Les
  familles sont retrouvées dans le titre et la description
  (`provenance.marquer_par_texte`), comme pour les requêtes OU.

Chaque item brut reçoit `_entreprise` (nom de la liste) et `_lieu` (lieu
préféré) ; `normalize` s'en sert.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import requests

import config
import observabilite
import recherche
from sources import provenance

logger = logging.getLogger(__name__)

_MOTIF_FRANCE = re.compile(r"paris|france|île-de-france|ile-de-france|\bidf\b", re.IGNORECASE)

_ENTETES = {"Accept": "application/json", "User-Agent": "stage-finder (lecture d'API publique)"}


def lieu_prefere(lieux: list[str]) -> str:
    """Le premier lieu français d'une liste, sinon le premier ; "" si aucun."""
    propres = [l.strip() for l in lieux if l and l.strip()]
    return next((l for l in propres if _MOTIF_FRANCE.search(l)), propres[0] if propres else "")


def entreprises(plateforme: str) -> list[recherche.EntrepriseATS]:
    return [e for e in recherche.charger().entreprises if e.plateforme == plateforme]


def recuperer(
    plateforme: str,
    url_de: Callable[[str], str],
    params: dict,
    annonces_de: Callable[[object], list[dict]],
    lieux_de: Callable[[dict], list[str]],
    texte_de: Callable[[dict], str],
    garder: Callable[[dict], bool] = lambda annonce: True,
) -> list[dict]:
    """Toutes les annonces des entreprises d'une plateforme, marquées pour `normalize`."""
    familles = recherche.charger().noms_familles()
    toutes: list[dict] = []
    vides: list[str] = []
    for entreprise in entreprises(plateforme):
        cible = f"{entreprise.nom} ({entreprise.identifiant})"
        try:
            reponse = requests.get(url_de(entreprise.identifiant), params=params,
                                   headers=_ENTETES, timeout=config.TIMEOUT_HTTP)
        except requests.exceptions.RequestException as err:
            categorie, detail = observabilite.categorie_requests(err)
            observabilite.signaler(plateforme, categorie, f"{detail} sur « {cible} »")
            logger.warning("%s : échec pour %s (%s)", plateforme, cible, type(err).__name__)
            continue
        if reponse.status_code == 404:
            observabilite.signaler(plateforme, "http",
                                   f"HTTP 404 sur « {cible} » : identifiant de tableau inconnu")
            logger.warning("%s : tableau inconnu pour %s (404).", plateforme, cible)
            continue
        if reponse.status_code != 200:
            observabilite.signaler(plateforme, "http", f"HTTP {reponse.status_code} sur « {cible} »")
            continue
        try:
            annonces = [a for a in annonces_de(reponse.json()) if garder(a)]
        except (ValueError, AttributeError, TypeError):
            observabilite.signaler(plateforme, "format", f"réponse illisible sur « {cible} »")
            continue
        if not annonces:
            vides.append(cible)
        for annonce in annonces:
            annonce["_entreprise"] = entreprise.nom
            annonce["_lieu"] = lieu_prefere(lieux_de(annonce))
        toutes.extend(provenance.marquer_par_texte(annonces, familles, texte_de))
        logger.info("%s : %d annonce(s) pour %s.", plateforme, len(annonces), cible)
    if vides:
        observabilite.conseiller(
            plateforme,
            f"« {plateforme} » : tableau VIDE pour {', '.join(vides)} — identifiant à vérifier "
            f"(un compte sans annonce répond 200 et une liste vide).",
        )
    logger.info("%s : %d annonce(s) brute(s) au total.", plateforme, len(toutes))
    return toutes
