"""Prépare l'environnement avant tout chargement de modèle d'embeddings.

Deux réglages, à poser AVANT que `sentence_transformers` ne touche au
réseau — donc avant le premier `load_embedder`.

## 1. Le magasin de certificats de l'OS (`truststore`)

Sur une machine derrière un antivirus ou un proxy qui intercepte le TLS,
`certifi` ne connaît pas le certificat racine injecté et toute requête
HTTPS échoue en `CERTIFICATE_VERIFY_FAILED`. `truststore` fait pointer
Python sur le magasin de Windows, sans jamais désactiver la vérification.

Le projet le savait déjà : `sources/__init__.py` et `ranker.py` le font
tous les deux, ce dernier avec le commentaire « pour le téléchargement du
modèle ». Le worker de génération, lui, ne passe par NI l'un NI l'autre —
il charge l'encodeur via `cv_forge`. D'où cette troisième activation.

    @sync-a : `ranker.py:32` et `sources/__init__.py:24` gardent leur bloc
    en ligne. Les factoriser ici toucherait deux chemins chauds et
    éprouvés (collecte et classement) pour un gain cosmétique ; ce module
    ne s'occupe donc QUE du chemin de génération de CV, qui n'était
    couvert par aucun des deux.

## 2. La préférence hors ligne

Le modèle d'embeddings est le MÊME pour les deux projets
(`paraphrase-multilingual-MiniLM-L12-v2`) : quand le worker tourne, le
ranker de stage_finder l'a déjà téléchargé. Aller redemander à Hugging
Face s'il a changé, à chaque génération, c'est une latence et une classe
de panne entières pour rien — et c'est contraire à l'invariant « 100 %
local » du projet.

On force donc le mode hors ligne **uniquement si le modèle est déjà en
cache**. Sinon on laisse le réseau ouvert : un premier téléchargement doit
rester possible, et il réussira grâce au point 1.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Fichier présent dans tout dépôt du modèle : sa présence en cache suffit
# à établir que le modèle a déjà été téléchargé une fois.
_FICHIER_TEMOIN = "config.json"


def activer_truststore() -> bool:
    """Fait pointer Python sur le magasin de certificats de l'OS."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - absent ou indisponible : on continue
        # Sans lui, on retombe sur `certifi` : cela marche partout SAUF
        # derrière une interception TLS. Ce n'est pas une raison de refuser
        # de démarrer.
        logger.debug("truststore indisponible : vérification TLS par certifi.")
        return False
    return True


def _racine_du_cache() -> Path:
    """Dossier `hub/` du cache Hugging Face, selon les variables usuelles."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def modele_en_cache(nom_modele: str) -> bool:
    """Le modèle est-il déjà dans le cache Hugging Face local ?

    Sondé **sur le disque**, et surtout PAS via ``huggingface_hub`` : cette
    bibliothèque fige ``HF_HUB_OFFLINE`` dans une constante de module AU
    MOMENT DE SON IMPORT. L'importer pour interroger le cache la
    verrouillerait donc sur la valeur d'AVANT notre réglage, et le mode
    hors ligne posé juste après n'aurait plus aucun effet.

    C'est exactement le piège dans lequel la première version est tombée :
    elle appelait ``try_to_load_from_cache``, donc importait la lib, donc
    son ``os.environ[...] = "1"`` arrivait trop tard. Le run passait — mais
    grâce à ``truststore`` seul, en interrogeant le réseau à chaque fois.

    Disposition stable du cache :
        <hub>/models--<org>--<nom>/snapshots/<révision>/config.json
    """
    depot = nom_modele if "/" in nom_modele else f"sentence-transformers/{nom_modele}"
    dossier = _racine_du_cache() / ("models--" + depot.replace("/", "--"))
    try:
        return any(dossier.glob(f"snapshots/*/{_FICHIER_TEMOIN}"))
    except OSError:  # cache illisible : traité comme absent
        return False


def preparer(nom_modele: str | None = None) -> dict:
    """À appeler une fois, avant tout chargement d'encodeur. Rend un état.

    Idempotente : `HF_HUB_OFFLINE` déjà positionné par l'appelant (ou par
    l'environnement) n'est jamais écrasé — un réglage explicite doit
    l'emporter sur une heuristique.
    """
    if nom_modele is None:
        from cv_forge.embed import DEFAULT_EMBED_MODEL  # type: ignore[attr-defined]

        nom_modele = DEFAULT_EMBED_MODEL

    etat = {"truststore": activer_truststore(), "hors_ligne": False,
            "en_cache": False, "modele": nom_modele}

    if os.environ.get("HF_HUB_OFFLINE") is not None:
        etat["hors_ligne"] = os.environ["HF_HUB_OFFLINE"] == "1"
        etat["impose_par_l_environnement"] = True
        return etat

    etat["en_cache"] = modele_en_cache(nom_modele)
    if etat["en_cache"]:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        etat["hors_ligne"] = True
        etat["constante_corrigee"] = _forcer_constante_hors_ligne()
        logger.info("Embeddings : modèle en cache, chargement hors ligne.")
    else:
        logger.info("Embeddings : modèle absent du cache, téléchargement autorisé.")
    return etat


def _forcer_constante_hors_ligne() -> bool:
    """Aligne la constante de ``huggingface_hub`` si la lib est DÉJÀ importée.

    La variable d'environnement ne suffit que si personne n'a encore
    importé ``huggingface_hub`` : sa valeur y est recopiée dans
    ``constants.HF_HUB_OFFLINE`` à l'import, et plus jamais relue.

    On ne provoque JAMAIS l'import ici — on ne corrige que si quelqu'un
    d'autre l'a déjà fait (``ranker`` par exemple, dans un processus qui
    aurait classé avant de générer). Rend True si une correction a eu lieu.
    """
    module = sys.modules.get("huggingface_hub.constants")
    if module is None or getattr(module, "HF_HUB_OFFLINE", True):
        return False
    module.HF_HUB_OFFLINE = True
    return True
