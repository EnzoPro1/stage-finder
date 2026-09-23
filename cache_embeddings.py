"""
cache_embeddings.py — Cache disque des embeddings calculés par Ollama.

Un run revoit en grande partie les offres du run précédent : sans cache, chaque
run ré-encoderait tout le gisement sur le GPU. Ici, un embedding est calculé
une fois par (modèle, texte), puis relu d'un run à l'autre.

## Clé

``(modele@digest, sha256(texte))``.

- Le texte ENTIER est haché, préfixes compris : changer ``PREFIXE_DOCUMENT``
  ou la composition titre + description change la clé, donc provoque un
  recalcul — jamais un vecteur périmé relu en silence.
- Le DIGEST du modèle (``/api/tags``) identifie ses poids : un ``ollama pull``
  qui les remplace sous le même tag change la clé, et le cache se recalcule
  tout seul. Les anciennes lignes restent en base, orphelines et inoffensives ;
  supprimer le fichier les efface.

Le digest est lu UNE fois par run (``oublier_digests`` ouvre un run) : un
appel à ``/api/tags`` par encodage serait du bruit, et les poids ne changent
pas au milieu d'un classement.

## Fichier séparé

``config.CHEMIN_CACHE_EMBEDDINGS``, pas ``stages.db`` : c'est un cache pur,
qu'on doit pouvoir effacer sans rien perdre, et l'empreinte de l'instantané de
référence n'a pas à dépendre de lui.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import numpy as np

import config
import llm

logger = logging.getLogger(__name__)

# modèle -> digest, pour le run en cours.
_digests: dict[str, str] = {}


def oublier_digests() -> None:
    """Ouvre un nouveau run : les digests seront relus au prochain encodage.

    Nécessaire pour l'app web, dont le processus dure : sans cela, un
    ``ollama pull`` fait pendant qu'elle tourne ne serait vu qu'au redémarrage.
    """
    _digests.clear()


def cle_modele(modele: str, reglages: llm.Reglages | None = None) -> str:
    """``modele@digest`` : la colonne ``modele`` du cache."""
    if modele not in _digests:
        _digests[modele] = llm.digest_modele(modele, reglages)
    return f"{modele}@{_digests[modele]}"

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
    progression: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """Embeddings de ``textes`` (matrice ``n × dim``, float32), cache d'abord.

    Seuls les textes absents du cache partent vers Ollama, dédoublonnés, par
    lots séquentiels (``llm.embed``). Les vecteurs sont rendus BRUTS : la
    normalisation reste l'affaire de l'appelant.

    ``progression(fait, total)`` : appelé après chaque lot, textes déjà en
    cache compris. L'avancement est aussi journalisé tous les 10 % — sans
    lui, des centaines d'offres s'encodent en silence pendant des minutes.
    """
    if not textes:
        return np.zeros((0, 0), dtype=np.float32)
    chemin = chemin or config.CHEMIN_CACHE_EMBEDDINGS
    cles = [empreinte(t) for t in textes]

    cle_mod = cle_modele(modele, reglages)

    conn = _ouvrir(chemin)
    try:
        connus = _lire(conn, cle_mod, sorted(set(cles)))
        manquants: dict[str, str] = {}
        for cle, texte in zip(cles, textes):
            if cle not in connus:
                manquants.setdefault(cle, texte)

        if manquants:
            deja = len(set(cles)) - len(manquants)
            total = len(set(cles))
            palier = {"prochain": 10}

            def apres_lot(calcules: int) -> None:
                fait = deja + calcules
                pourcent = 100 * fait // total
                if pourcent >= palier["prochain"] or fait == total:
                    logger.info("Embeddings « %s » : %d/%d…", modele, fait, total)
                    palier["prochain"] = (pourcent // 10 + 1) * 10
                if progression is not None:
                    progression(fait, total)

            vecteurs = llm.embed(list(manquants.values()), reglages=reglages, modele=modele,
                                 apres_lot=apres_lot)
            nouveaux = {}
            for cle, vecteur in zip(manquants, vecteurs):
                nouveaux[cle] = np.asarray(vecteur, dtype=np.float32)
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings (modele, empreinte, dim, vecteur) "
                    "VALUES (?, ?, ?, ?)",
                    [(cle_mod, cle, int(v.shape[0]), v.tobytes()) for cle, v in nouveaux.items()],
                )
            connus.update(nouveaux)

        depuis_cache = sum(1 for cle in cles if cle not in manquants)
        logger.info("Embeddings « %s » : %d texte(s), %d depuis le cache, %d calculé(s).",
                    modele, len(textes), depuis_cache, len(manquants))
        return np.vstack([connus[cle] for cle in cles])
    finally:
        conn.close()
