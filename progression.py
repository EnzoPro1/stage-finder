"""
progression.py — Étape en cours d'une recherche, pour la console et l'app web.

Une recherche dure plusieurs minutes, et certaines étapes (encodage de
centaines d'offres par bge-m3) ne journalisaient rien avant d'avoir fini : la
console restait muette et la page vide, ce qui ressemblait à un plantage.

Ce module tient UNE information : l'étape en cours, et son avancement quand
il se compte (``fait / total``). Le pipeline l'écrit (``signaler``), l'app la
lit (``lire``) à chaque rafraîchissement de ``/api/etat``. Même principe que
``observabilite.actif()`` : un état de module, protégé par un verrou, parce
que la collecte tourne dans un thread et la lecture dans un autre.

Il n'y a qu'une recherche à la fois (l'app refuse d'en lancer une seconde),
donc un seul état suffit.
"""

from __future__ import annotations

import threading
from datetime import datetime

_verrou = threading.Lock()
_etat: dict = {}


def signaler(libelle: str, fait: int | None = None, total: int | None = None) -> None:
    """Pose l'étape en cours. ``fait``/``total`` : avancement, s'il se compte."""
    with _verrou:
        _etat.clear()
        _etat.update(libelle=libelle, fait=fait, total=total,
                     depuis=datetime.now().isoformat(timespec="seconds"))


def lire() -> dict:
    """Copie de l'étape en cours (``{}`` hors recherche)."""
    with _verrou:
        return dict(_etat)


def effacer() -> None:
    """Fin de recherche : plus d'étape en cours."""
    with _verrou:
        _etat.clear()
