"""
sources/lever.py — Annonces Lever des entreprises de recherche.yaml.

Endpoint public, sans clé (vérifié le 2026-09-17) :
    GET https://api.lever.co/v0/postings/<identifiant>?mode=json

Réponse : une LISTE. Lieux dans `categories.location` et
`categories.allLocations` ; date `createdAt` en millisecondes ; titre `text` ;
lien `hostedUrl`. L'hôte européen (api.eu.lever.co) n'est pas pris en charge :
aucune entreprise essayée n'y répondait, rien n'a pu être vérifié.
Mécanique commune : sources/ats.py.

Testable isolément :  python -m sources.lever
"""

from __future__ import annotations

import logging

from sources import ats

NOM_SOURCE = "lever"
_URL = "https://api.lever.co/v0/postings/{}"


def _lieux(annonce: dict) -> list[str]:
    categories = annonce.get("categories") or {}
    return [categories.get("location") or ""] + list(categories.get("allLocations") or [])


def _texte(annonce: dict) -> str:
    return f"{annonce.get('text') or ''} {annonce.get('descriptionPlain') or ''}"


def recuperer_offres() -> list[dict]:
    return ats.recuperer(
        NOM_SOURCE, _URL.format, {"mode": "json"},
        annonces_de=lambda donnees: donnees if isinstance(donnees, list) else [],
        lieux_de=_lieux, texte_de=_texte,
    )


if __name__ == "__main__":
    import console  # noqa: F401

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} annonce(s) Lever ===")
    for o in offres[:5]:
        print(f"- {o.get('text')}  |  {o.get('_entreprise')}  |  {o.get('_lieu')}")
