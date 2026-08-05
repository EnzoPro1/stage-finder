"""cv_cli.py — Pilotage de la file de génération de CV, sans interface web.

Le CP2 se vérifie entièrement d'ici : enfiler, lister, consommer, sans
qu'une seule ligne de Flask soit impliquée. L'UI du CP4 empruntera
exactement le même chemin de code.

    python cv_cli.py etat                       # file + couverture en texte
    python cv_cli.py enfiler <cle|préfixe>      # demande un CV
    python cv_cli.py travailler --une-passe     # vide la file puis rend la main
    python cv_cli.py travailler                 # worker continu (Ctrl+C)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config
import jobs
import storage
import worker as worker_module


def _resoudre_offre(conn, fragment: str):
    """Retrouve une offre par clé exacte ou par préfixe. Refuse l'ambiguïté."""
    lignes = conn.execute(
        "SELECT cle, title, company FROM offres WHERE cle LIKE ? ORDER BY cle",
        (fragment + "%",),
    ).fetchall()
    if not lignes:
        return None, f"aucune offre dont la clé commence par « {fragment} »"
    if len(lignes) > 1:
        apercu = "\n".join(f"    {l['cle'][:16]}…  {l['title']}" for l in lignes[:5])
        return None, (f"{len(lignes)} offres correspondent à « {fragment} » :\n{apercu}\n"
                      f"  Précisez la clé.")
    return lignes[0], None


def cmd_etat(args) -> int:
    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)

    total, = conn.execute("SELECT COUNT(*) FROM offres").fetchone()
    avec = len(jobs.cles_avec_texte(conn))
    print(f"Offres        : {total}   (avec texte : {avec}, sans : {total - avec})")

    compte = dict(conn.execute(
        "SELECT status, COUNT(*) FROM generation_jobs GROUP BY status"
    ).fetchall())
    if not compte:
        print("Jobs          : aucun")
    else:
        print("Jobs          : " + ", ".join(f"{k}={v}" for k, v in sorted(compte.items())))

    recents = conn.execute(
        """SELECT id, offer_id, status, created_at, error_code, pdf_path
           FROM generation_jobs ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    for job in recents:
        pos = jobs.position(conn, job["id"])
        suffixe = (f"  file n°{pos}" if pos else
                   f"  {job['error_code']}" if job["error_code"] else
                   f"  {job['pdf_path'] or ''}")
        print(f"  #{job['id']:<4} {job['status']:<8} {job['offer_id'][:12]}…{suffixe}")
    conn.close()
    return 0


def cmd_enfiler(args) -> int:
    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)

    offre, erreur = _resoudre_offre(conn, args.cle)
    if erreur:
        print(f"Erreur : {erreur}", file=sys.stderr)
        return 1

    texte = jobs.lire_texte(conn, offre["cle"])
    if not texte:
        print(f"Erreur : l'offre « {offre['title']} » n'a pas de texte en base "
              f"(état TEXT_MISSING).\n  Lancez d'abord :  python backfill_textes.py --apply",
              file=sys.stderr)
        return 1

    from cv_forge import ForgeConfig

    master = Path(config.CV_MASTER_PATH)
    if not master.is_file():
        print(f"Erreur : master introuvable : {master}", file=sys.stderr)
        return 1

    empreinte = jobs.calculer_hash(texte, master_path=master, config=ForgeConfig())
    job, reutilise = jobs.enfiler(conn, offre["cle"], empreinte)

    verbe = "déjà en file" if reutilise else "enfilé"
    print(f"Job #{job['id']} {verbe} — {offre['title']} ({offre['company']})")
    print(f"  statut : {job['status']}")
    if reutilise:
        print("  (idempotence : même texte, même master, même config -> même job)")
    pos = jobs.position(conn, job["id"])
    if pos:
        print(f"  position : n°{pos}")
    conn.close()
    return 0


def cmd_travailler(args) -> int:
    if not worker_module.worker_autorise():
        print("Ce processus ne doit pas porter le worker.", file=sys.stderr)
        return 1

    w = worker_module.Worker(db_path=args.db, intervalle=args.intervalle)
    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)
    repris, abandonnes = jobs.reprendre_orphelins(conn)
    if repris or abandonnes:
        print(f"Reprise : {repris} job(s) remis en file, {abandonnes} abandonné(s).")

    if args.une_passe:
        traites = 0
        while w._traiter_un(conn):
            traites += 1
        w._file_active = traites > 0
        w._au_repos()
        print(f"{traites} job(s) traité(s). File vide.")
        if w.decharges:
            print("Modèle déchargé (file vidée).")
        conn.close()
        return 0

    conn.close()
    print("Worker démarré. Ctrl+C pour arrêter.")
    w.demarrer()
    try:
        while w.actif:
            w._thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nArrêt demandé…")
        w.arreter()
    return 0


def main(argv: list[str] | None = None) -> int:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default=config.CHEMIN_BASE)
    sous = parser.add_subparsers(dest="commande", required=True)

    sous.add_parser("etat", help="File et couverture en texte")

    p_enf = sous.add_parser("enfiler", help="Demande un CV pour une offre")
    p_enf.add_argument("cle", help="Clé de l'offre, ou un préfixe non ambigu")

    p_tra = sous.add_parser("travailler", help="Consomme la file")
    p_tra.add_argument("--une-passe", action="store_true",
                       help="S'arrête dès que la file est vide")
    p_tra.add_argument("--intervalle", type=float,
                       default=worker_module.INTERVALLE_SONDAGE_S)

    args = parser.parse_args(argv)
    return {"etat": cmd_etat, "enfiler": cmd_enfiler,
            "travailler": cmd_travailler}[args.commande](args)


if __name__ == "__main__":
    raise SystemExit(main())
