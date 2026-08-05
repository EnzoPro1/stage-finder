"""
console.py — Petit utilitaire d'affichage console robuste sous Windows.

La console Windows par défaut (cp1252) ne sait pas encoder certains caractères
qu'on affiche (le badge « ★ », les emoji des rapports). Sans précaution, un
`print` d'un combo IA+Cyber lève un `UnicodeEncodeError`. On force donc stdout /
stderr en UTF-8 dès l'import, de façon idempotente et sans jamais casser si la
plateforme ne le permet pas.
"""

from __future__ import annotations

import sys


def forcer_utf8() -> None:
    """Force stdout/stderr en UTF-8 quand c'est possible (no-op sinon)."""
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            # Flux redirigé/non reconfigurable : on laisse tel quel.
            pass


forcer_utf8()
