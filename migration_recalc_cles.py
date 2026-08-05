"""Re-calcule les `cle` de `offres` après le correctif des mentions de genre.

ONE-SHOT. À lancer une seule fois, après le commit qui corrige
``dedup._normaliser_titre`` (mentions M/F, F/H, M/W/D… et séparateur rendu
obligatoire). Idempotent : relancé, il ne trouve plus rien à faire.

POURQUOI MAINTENANT. ``cle`` est la clé primaire de ``offres`` ET le futur
``offer_id`` des jobs de génération de CV. Corriger la normalisation APRÈS la
création de ``generation_jobs`` laisserait des jobs et des PDF rattachés à des
`offer_id` qui ne désignent plus aucune ligne — orphelins, invisibles, et
impossibles à rattacher après coup. Aujourd'hui la table des jobs n'existe pas :
c'est la seule fenêtre où ce recalcul ne casse rien.

CE QUI N'EST PAS TOUCHÉ. La table ``verdicts`` est indexée par ``hash_offre``
(sha256 de title|company|location|description), pas par ``cle`` : les 168
verdicts LLM déjà payés survivent intacts. La table ``runs`` ne référence
aucune offre.

    python migration_recalc_cles.py              # simulation, n'écrit rien
    python migration_recalc_cles.py --apply      # applique, après sauvegarde
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import config
from dedup import _cle
from normalize import Offre


def _offre_depuis_ligne(ligne: sqlite3.Row) -> Offre:
    """Reconstruit le minimum nécessaire au calcul de la clé.

    ``_cle`` ne lit que title, company et location — la description n'y entre
    pas (c'est ``hash_offre`` qui porte le contenu). On ne la charge donc pas.
    """
    return Offre(
        ligne["title"] or "", ligne["company"] or "", ligne["location"] or "",
        "", "", "", "", "",
    )


def _sauvegarder(chemin: str) -> Path:
    """Copie horodatée à côté de la base. Jamais d'écrasement."""
    source = Path(chemin)
    cible = source.with_name(
        f"{source.stem}.avant-recalc-{datetime.now():%Y%m%d-%H%M%S}{source.suffix}"
    )
    shutil.copy2(source, cible)
    return cible


def planifier(conn: sqlite3.Connection) -> tuple[list, dict]:
    """Rend (déplacements, fusions) sans rien écrire.

    - déplacements : (ancienne_cle, nouvelle_cle, titre) pour les lignes dont
      la clé change et qui restent seules sous la nouvelle ;
    - fusions : nouvelle_cle -> [lignes] quand PLUSIEURS lignes existantes
      convergent. C'est le doublon que le correctif existe pour supprimer.
    """
    conn.row_factory = sqlite3.Row
    lignes = conn.execute("SELECT * FROM offres").fetchall()

    par_nouvelle: dict[str, list] = defaultdict(list)
    for ligne in lignes:
        par_nouvelle[_cle(_offre_depuis_ligne(ligne))].append(ligne)

    deplacements, fusions = [], {}
    for nouvelle, groupe in par_nouvelle.items():
        if len(groupe) > 1:
            fusions[nouvelle] = groupe
        elif groupe[0]["cle"] != nouvelle:
            deplacements.append((groupe[0]["cle"], nouvelle, groupe[0]["title"]))
    return deplacements, fusions


def _fusionner(groupe: list) -> dict:
    """Fond plusieurs lignes en une. Choix explicites, pas des accidents.

    - la ligne de BASE est la plus récemment vue : ses champs (titre, url,
      salaire, score) sont les plus à jour ;
    - ``premiere_vue`` prend le MINIMUM du groupe : l'offre a bien été vue pour
      la première fois à cette date, sous une clé ou sous l'autre ;
    - ``nb_vues`` prend le MAXIMUM, jamais la somme : les deux lignes ont été
      vues pendant des runs qui se recouvrent, additionner gonflerait le
      compteur d'un facteur deux.
    """
    base = max(group_sorted := sorted(groupe, key=lambda x: x["derniere_vue"] or ""),
               key=lambda x: x["derniere_vue"] or "")
    return {
        "premiere_vue": min((x["premiere_vue"] or "9999") for x in group_sorted),
        "derniere_vue": base["derniere_vue"],
        "nb_vues": max((x["nb_vues"] or 0) for x in group_sorted),
        "base": base,
    }


def appliquer(conn: sqlite3.Connection, deplacements: list, fusions: dict) -> None:
    """Écrit dans une seule transaction. Tout ou rien.

    Les fusions passent AVANT les déplacements : elles suppriment des lignes,
    ce qui libère d'éventuelles clés cibles.
    """
    with conn:                       # commit/rollback automatique
        for nouvelle, groupe in fusions.items():
            fusionnee = _fusionner(groupe)
            base = fusionnee["base"]
            for ligne in groupe:
                conn.execute("DELETE FROM offres WHERE cle = ?", (ligne["cle"],))
            colonnes = [k for k in base.keys() if k != "cle"]
            valeurs = {k: base[k] for k in colonnes}
            valeurs.update(premiere_vue=fusionnee["premiere_vue"],
                           derniere_vue=fusionnee["derniere_vue"],
                           nb_vues=fusionnee["nb_vues"])
            conn.execute(
                f"INSERT INTO offres (cle, {', '.join(colonnes)}) "
                f"VALUES (?, {', '.join('?' * len(colonnes))})",
                (nouvelle, *[valeurs[k] for k in colonnes]),
            )
        for ancienne, nouvelle, _ in deplacements:
            conn.execute("UPDATE offres SET cle = ? WHERE cle = ?", (nouvelle, ancienne))


def main(argv: list[str] | None = None) -> int:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=config.CHEMIN_BASE)
    parser.add_argument("--apply", action="store_true",
                        help="Écrit réellement. Sans ce drapeau : simulation.")
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"Base introuvable : {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    total = conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0]
    deplacements, fusions = planifier(conn)
    supprimees = sum(len(g) - 1 for g in fusions.values())

    print(f"Base           : {args.db}")
    print(f"Lignes         : {total}")
    print(f"Clés déplacées : {len(deplacements)}")
    print(f"Fusions        : {len(fusions)} groupe(s), soit {supprimees} ligne(s) en moins")
    for nouvelle, groupe in fusions.items():
        print(f"  -> {nouvelle[:12]}…")
        for ligne in groupe:
            print(f"       {ligne['cle'][:12]}…  nb_vues={ligne['nb_vues']}  {ligne['title']}")

    if not deplacements and not fusions:
        print("\nRien à faire — la base est déjà à jour.")
        return 0

    if not args.apply:
        print(f"\nSIMULATION : rien n'a été écrit. Relancez avec --apply.")
        return 0

    sauvegarde = _sauvegarder(args.db)
    print(f"\nSauvegarde     : {sauvegarde}")
    appliquer(conn, deplacements, fusions)

    restants, _ = planifier(conn)
    apres = conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0]
    verdicts = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    conn.close()

    print(f"Lignes après   : {apres}  (attendu {total - supprimees})")
    print(f"Verdicts       : {verdicts}  (intacts : indexés par hash_offre, pas par cle)")
    print(f"Reste à faire  : {len(restants)}  (attendu 0 — idempotence)")
    return 0 if (not restants and apres == total - supprimees) else 1


if __name__ == "__main__":
    raise SystemExit(main())
