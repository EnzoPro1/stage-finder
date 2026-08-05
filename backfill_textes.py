"""Peuple `offres_texte` depuis `.rank_cache.json`. ONE-SHOT et idempotent.

La table ``offres`` ne stocke PAS la description : le texte brut d'une
annonce n'existe aujourd'hui qu'en mémoire (``app.ETAT``) et dans le cache
disque du dernier classement. Or c'est l'entrée de tout le pipeline de
génération de CV, et la première composante de la clé d'idempotence.

Ce script récupère ce qui est récupérable, une fois. Il ne va sur AUCUN
réseau : ce qui n'est pas dans le cache reste sans texte, et l'interface
affichera l'état ``TEXT_MISSING`` — bouton désactivé plus action
« Récupérer » — plutôt qu'une erreur au clic.

Idempotent : les offres déjà pourvues sont laissées telles quelles, sauf
``--forcer``.

    python backfill_textes.py              # simulation
    python backfill_textes.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
import jobs
import storage
from dedup import _cle as cle_identite
from normalize import Offre

CHEMIN_CACHE_DEFAUT = ".rank_cache.json"


def _offre_depuis_row(row: dict) -> Offre:
    """Reconstruit l'offre au strict nécessaire pour recalculer sa clé.

    La clé ne dépend que de title, company et location — mais elle doit
    être calculée par ``dedup._cle``, jamais réimplémentée ici : deux
    calculs concurrents finiraient par diverger, et le symptôme serait un
    texte rattaché à la mauvaise offre.
    """
    return Offre(
        row.get("title", "") or "", row.get("company", "") or "",
        row.get("location", "") or "", "", "", "", "", "",
    )


def charger_cache(chemin: Path) -> list[dict]:
    with open(chemin, "r", encoding="utf-8") as flux:
        return json.load(flux).get("offres", [])


def backfill(conn, rows: list[dict], *, forcer: bool = False,
             ecrire: bool = True) -> dict:
    """Écrit les textes. Rend le rapport, sans jamais lever.

    ``ecrire=False`` calcule le rapport sans toucher la base. C'est la
    SEULE façon correcte de simuler ici : envelopper l'appel dans une
    transaction annulée ne marcherait pas, ``jobs.enregistrer_texte``
    ouvrant la sienne (``with conn:``) qui valide en sortie. Le
    « rollback » n'annulerait rien et la simulation écrirait pour de bon.
    """
    connues = {l["cle"] for l in conn.execute("SELECT cle FROM offres")}
    deja = jobs.cles_avec_texte(conn)

    rapport = {"lues": len(rows), "ecrites": 0, "deja": 0,
               "sans_texte": 0, "hors_base": 0}

    for row in rows:
        texte = (row.get("description") or "").strip()
        if not texte:
            rapport["sans_texte"] += 1
            continue
        cle = cle_identite(_offre_depuis_row(row))
        if cle not in connues:
            # L'offre du cache n'existe plus en base (sortie d'un run, ou
            # clé déplacée par une migration). On ne crée PAS la ligne : ce
            # script peuple une table latérale, il n'invente pas d'offres.
            rapport["hors_base"] += 1
            continue
        if cle in deja and not forcer:
            rapport["deja"] += 1
            continue
        if ecrire:
            jobs.enregistrer_texte(conn, cle, texte)
        deja.add(cle)
        rapport["ecrites"] += 1

    return rapport


def main(argv: list[str] | None = None) -> int:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=config.CHEMIN_BASE)
    parser.add_argument("--cache", default=CHEMIN_CACHE_DEFAUT)
    parser.add_argument("--apply", action="store_true", help="Écrit réellement.")
    parser.add_argument("--forcer", action="store_true",
                        help="Réécrit même les offres déjà pourvues.")
    args = parser.parse_args(argv)

    cache = Path(args.cache)
    if not cache.is_file():
        print(f"Cache introuvable : {cache}", file=sys.stderr)
        return 1

    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)
    rows = charger_cache(cache)
    total_offres, = conn.execute("SELECT COUNT(*) FROM offres").fetchone()

    rapport = backfill(conn, rows, forcer=args.forcer, ecrire=args.apply)

    avec = len(jobs.cles_avec_texte(conn))
    if not args.apply:
        # La couverture annoncée est celle qu'on OBTIENDRAIT, pas celle
        # d'aujourd'hui : sinon la simulation afficherait un état que
        # `--apply` changerait aussitôt.
        avec += rapport["ecrites"]
    sans = total_offres - avec
    conn.close()

    print(f"Cache lu          : {cache}  ({rapport['lues']} entrée(s))")
    print(f"  écrites         : {rapport['ecrites']}")
    print(f"  déjà pourvues   : {rapport['deja']}")
    print(f"  sans description: {rapport['sans_texte']}")
    print(f"  hors base       : {rapport['hors_base']}")
    print()
    print(f"Offres en base    : {total_offres}")
    print(f"  AVEC texte      : {avec}")
    print(f"  SANS texte      : {sans}   -> état TEXT_MISSING dans l'interface")
    if not args.apply:
        print("\nSIMULATION : rien n'a été écrit. Relancez avec --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
