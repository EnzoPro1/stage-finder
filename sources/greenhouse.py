"""
sources/greenhouse.py — Tableaux d'annonces Greenhouse des entreprises de recherche.yaml.

Endpoint public, sans clé (vérifié le 2026-09-17) :
    GET https://boards-api.greenhouse.io/v1/boards/<identifiant>/jobs?content=true

`content` est du HTML ÉCHAPPÉ (« &lt;p&gt; ») ; `location.name` peut lister
plusieurs sites séparés par « ; » ; `offices[].location` en donne d'autres.
Mécanique commune : sources/ats.py.

Testable isolément :  python -m sources.greenhouse
"""

from __future__ import annotations

import html
import logging

from sources import ats

NOM_SOURCE = "greenhouse"
_URL = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"


def _lieux(annonce: dict) -> list[str]:
    principal = (annonce.get("location") or {}).get("name") or ""
    bureaux = [b.get("location") or b.get("name") or "" for b in annonce.get("offices") or []]
    return [l for l in principal.split(";")] + bureaux


def _texte(annonce: dict) -> str:
    return f"{annonce.get('title') or ''} {html.unescape(annonce.get('content') or '')}"


def recuperer_offres() -> list[dict]:
    return ats.recuperer(
        NOM_SOURCE, _URL.format, {"content": "true"},
        annonces_de=lambda donnees: donnees.get("jobs") or [],
        lieux_de=_lieux, texte_de=_texte,
    )


if __name__ == "__main__":
    import console  # noqa: F401

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} annonce(s) Greenhouse ===")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('_entreprise')}  |  {o.get('_lieu')}")
