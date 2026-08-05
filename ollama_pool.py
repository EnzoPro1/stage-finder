"""ollama_pool.py — Sérialise les appels au serveur Ollama local, et décharge.

Deux mécanismes DISTINCTS, à ne pas confondre :

1. **le jeton** (``JETON``) — un seul appel LLM en vol à la fois, tous
   modèles et tous appelants confondus. Acquis et relâché autour de
   CHAQUE appel individuel, jamais autour d'une tâche entière ;
2. **le déchargement** (``decharger``) — libère la RAM occupée par un
   modèle qui ne sert plus.

Le premier ne remplace pas le second, et c'est le point à comprendre.

## Pourquoi le jeton ne suffit pas

Mesuré sur ce poste (Ollama 0.32.5, 16,8 Go de RAM) : après un appel,
Ollama garde le modèle RÉSIDENT pendant ``keep_alive`` (5 min par défaut,
aucun des deux projets ne le renseigne). Enchaîner un appel à ``qwen3:8b``
alors que ``qwen3:1.7b`` est encore résident laisse **les deux** en
mémoire — 5,94 + 1,88 = 7,82 Go, sans éviction — sur une machine qui
avait 2,4 Go de libre.

Le jeton empêche donc deux inférences de se disputer le CPU, ce qui est
réel et utile, mais il ne borne RIEN de la mémoire : la résidence survit
à l'appel, donc au jeton. D'où le déchargement explicite.

## Pourquoi pas ``keep_alive: 0`` sur chaque appel

Parce que ça optimiserait le mauvais côté. Le rechargement de ``qwen3:8b``
coûte 18,7 s (mesuré), et ce modèle est sur le chemin où l'utilisateur
ATTEND, derrière un bouton. Le payer à chaque clic — trois CV d'affilée,
trois rechargements — serait un net recul, alors que le 1,7b travaille en
tâche de fond où l'attente ne se voit pas.

Le déchargement est donc déclenché à la **vidange de la file** (worker) et
en **sortie de boucle** (vérification), jamais entre deux unités de
travail consécutives : le modèle reste chaud tant qu'il sert.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)

# Un SEUL appel LLM en vol, quel que soit le modèle et quel que soit
# l'appelant. Réentrant volontairement NON : un appel imbriqué serait un
# bug de conception, autant qu'il se voie.
JETON = threading.Lock()

# Le déchargement est best-effort et ne doit jamais faire attendre.
_TIMEOUT_DECHARGEMENT_S = 10


def _base_url(url: str | None = None) -> str:
    return (url or config.VERIFY_OLLAMA_URL).rstrip("/")


def decharger(model: str, *, url: str | None = None) -> bool:
    """Demande à Ollama de libérer ``model`` immédiatement. Rend le succès.

    Un ``keep_alive: 0`` sur un prompt vide : Ollama décharge sans rien
    générer. Mesuré, il rend la totalité de la mémoire du modèle.

    **N'échoue jamais bruyamment.** Un déchargement raté n'est pas une
    faute de la tâche qui vient de finir : au pire le modèle reste en
    mémoire cinq minutes, ce qui est exactement l'état d'avant cette
    fonction. Faire échouer un job réussi pour ça serait absurde.
    """
    charge = json.dumps({"model": model, "prompt": "", "keep_alive": 0}).encode()
    requete = urllib.request.Request(
        f"{_base_url(url)}/api/generate",
        data=charge,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(requete, timeout=_TIMEOUT_DECHARGEMENT_S):
            pass
    except (urllib.error.URLError, OSError, ValueError) as err:
        logger.debug("Déchargement de %s sans effet (%s).", model, err)
        return False
    logger.info("Ollama : modèle %s déchargé.", model)
    return True


def modeles_residents(url: str | None = None) -> list[str]:
    """Modèles actuellement en mémoire. Pour le diagnostic et les tests."""
    try:
        with urllib.request.urlopen(
            f"{_base_url(url)}/api/ps", timeout=_TIMEOUT_DECHARGEMENT_S
        ) as reponse:
            donnees = json.loads(reponse.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    return [m.get("name", "") for m in donnees.get("models", [])]
