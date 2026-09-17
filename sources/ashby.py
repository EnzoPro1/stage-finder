"""
sources/ashby.py — Tableaux d'annonces Ashby des entreprises de recherche.yaml.

Endpoint public, sans clé (vérifié le 2026-09-17) :
    GET https://api.ashbyhq.com/posting-api/job-board/<identifiant>

Réponse `{"jobs": [...]}`. Une annonce `isListed: false` n'est pas publiée :
elle est ignorée. Lieux dans `location`, `secondaryLocations[].location` et
`address.postalAddress` ; date `publishedAt` ; lien `jobUrl`.
Mécanique commune : sources/ats.py.

Testable isolément :  python -m sources.ashby
"""

from __future__ import annotations

import logging

from sources import ats

NOM_SOURCE = "ashby"
_URL = "https://api.ashbyhq.com/posting-api/job-board/{}"


def _lieux(annonce: dict) -> list[str]:
    adresse = ((annonce.get("address") or {}).get("postalAddress") or {})
    postale = ", ".join(filter(None, [adresse.get("addressLocality"), adresse.get("addressCountry")]))
    secondaires = [s.get("location") or "" for s in annonce.get("secondaryLocations") or []]
    return [annonce.get("location") or "", postale] + secondaires


def _texte(annonce: dict) -> str:
    return f"{annonce.get('title') or ''} {annonce.get('descriptionPlain') or ''}"


def recuperer_offres() -> list[dict]:
    return ats.recuperer(
        NOM_SOURCE, _URL.format, {"includeCompensation": "false"},
        annonces_de=lambda donnees: donnees.get("jobs") or [],
        lieux_de=_lieux, texte_de=_texte,
        garder=lambda annonce: annonce.get("isListed", True) is not False,
    )


if __name__ == "__main__":
    import console  # noqa: F401

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} annonce(s) Ashby ===")
    for o in offres[:5]:
        print(f"- {o.get('title')}  |  {o.get('_entreprise')}  |  {o.get('_lieu')}")
