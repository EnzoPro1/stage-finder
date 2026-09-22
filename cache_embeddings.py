"""
cache_embeddings.py — Cache disque des embeddings calculés par Ollama.

Un run revoit en grande partie les offres du run précédent : sans cache, chaque
run ré-encoderait tout le gisement sur le GPU. Ici, un embedding est calculé
une fois par (modèle, texte), puis relu d'un run à l'autre.

## Clé

``(modele, sha256(texte))``. Le texte ENTIER est haché, préfixes compris :
changer ``PREFIXE_DOCUMENT`` ou la composition titre + description change la
clé, donc provoque un recalcul — jamais un vecteur périmé relu en silence.

## Ce que la clé ne couvre PAS

Les poids du modèle : seul son NOM est dans la clé. Un ``ollama pull bge-m3``
qui ramènerait d'autres poids sous le même tag serait invisible ici — même
risque, accepté pour la même raison, que pour l'estampille de ``reference.py``.
Supprimer le fichier suffit à tout recalculer.

## Fichier séparé

``config.CHEMIN_CACHE_EMBEDDINGS``, pas ``stages.db`` : c'est un cache pur,
qu'on doit pouvoir effacer sans rien perdre, et l'empreinte de l'instantané de
référence n'a pas à dépendre de lui.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import numpy as np

import config
import llm

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    modele    TEXT NOT NULL,
    empreinte TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vecteur   BLOB NOT NULL,
    PRIMARY KEY (modele, empreinte)
)
"""


def empreinte(texte: str) -> str:
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def _ouvrir(chemin: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(chemin)
    conn.execute(_SCHEMA)
    return conn


def _lire(conn: sqlite3.Connection, modele: str, cles: list[str]) -> dict[str, np.ndarray]:
    trouves: dict[str, np.ndarray] = {}
    # Par paquets : SQLite borne le nombre de paramètres d'une requête.
    for debut in range(0, len(cles), 500):
        paquet = cles[debut : debut + 500]
        marques = ",".join("?" * len(paquet))
        for cle, dim, blob in conn.execute(
            f"SELECT empreinte, dim, vecteur FROM embeddings "
            f"WHERE modele = ? AND empreinte IN ({marques})",
            [modele, *paquet],
        ):
            vecteur = np.frombuffer(blob, dtype=np.float32)
            if vecteur.shape == (dim,):
                trouves[cle] = vecteur
    return trouves


def encoder(
    textes: list[str],
    modele: str,
    *,
    chemin: str | Path | None = None,
    reglages: llm.Reglages | None = None,
) -> np.ndarray:
    """Embeddings de ``textes`` (matrice ``n × dim``, float32), cache d'abord.

    Seuls les textes absents du cache partent vers Ollama, dédoublonnés, par
    lots séquentiels (``llm.embed``). Les vecteurs sont rendus BRUTS : la
    normalisation reste l'affaire de l'appelant.
    """
    if not textes:
        return np.zeros((0, 0), dtype=np.float32)
    chemin = chemin or config.CHEMIN_CACHE_EMBEDDINGS
    cles = [empreinte(t) for t in textes]

    conn = _ouvrir(chemin)
    try:
        connus = _lire(conn, modele, sorted(set(cles)))
        manquants: dict[str, str] = {}
        for cle, texte in zip(cles, textes):
            if cle not in connus:
                manquants.setdefault(cle, texte)

        if manquants:
            vecteurs = llm.embed(list(manquants.values()), reglages=reglages, modele=modele)
            nouveaux = {}
            for cle, vecteur in zip(manquants, vecteurs):
                nouveaux[cle] = np.asarray(vecteur, dtype=np.float32)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings (modele, empreinte, dim, vecteur) "
                    "VALUES (?, ?, ?, ?)",
                    [(modele, cle, int(v.shape[0]), v.tobytes()) for cle, v in nouveaux.items()],
                )
            connus.update(nouveaux)

        depuis_cache = sum(1 for cle in cles if cle not in manquants)
        logger.info("Embeddings « %s » : %d texte(s), %d depuis le cache, %d calculé(s).",
                    modele, len(textes), depuis_cache, len(manquants))
        return np.vstack([connus[cle] for cle in cles])
    finally:
        conn.close()
