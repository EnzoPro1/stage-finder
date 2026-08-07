"""cv_cli.py — Pilotage de la file de génération de CV, sans interface web.

Le CP2 se vérifie entièrement d'ici : enfiler, lister, consommer, sans
qu'une seule ligne de Flask soit impliquée. L'UI du CP4 empruntera
exactement le même chemin de code.

    python cv_cli.py etat                       # file + couverture en texte
    python cv_cli.py enfiler <cle|préfixe>      # demande un CV
    python cv_cli.py batch --limite 12          # les 12 meilleures, puis consomme
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

# Nombre d'offres qu'un batch considère par défaut. Aligné sur
# `cv_forge.batch.MAX_FRESH_OFFERS` (12), qui vient d'une mesure : une
# offre à extraire coûte ~800 s au pire, et 12 × 800 s ≈ 2 h 40, soit une
# fenêtre nocturne. La valeur est recopiée plutôt qu'importée : le batch
# de cv_forge travaille sur un dossier de fichiers, celui-ci sur une base
# — deux plafonds qui se trouvent égaux aujourd'hui, pas un seul.
LIMITE_BATCH_DEFAUT = 12


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

    from cv_forge import ForgeConfig

    try:
        job, reutilise = jobs.demander(
            conn, offre["cle"],
            master_path=Path(config.CV_MASTER_PATH), config=ForgeConfig(),
        )
    except jobs.DemandeRefusee as refus:
        print(f"Erreur [{refus.code}] : {refus.message}", file=sys.stderr)
        conn.close()
        return 1

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


def cmd_orphelins(args) -> int:
    """Jobs dont l'offre a disparu (fusion de doublons, purge de la base)."""
    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)
    liste = jobs.orphelins(conn)

    if not liste:
        print("Aucun job orphelin.")
        conn.close()
        return 0

    print(f"{len(liste)} job(s) orphelin(s) — leur offre n'existe plus :")
    for job in liste:
        pdf = job["pdf_path"] or "aucun PDF"
        print(f"  #{job['id']:<4} {job['status']:<8} {job['offer_id'][:12]}…  {pdf}")

    if not args.purger:
        print("\nRien n'a été supprimé. Ajoutez --purger pour les oublier.")
        print("  Note : la purge ne supprime AUCUN fichier PDF.")
        conn.close()
        return 0

    n = jobs.purger_orphelins(conn, seulement_echoues=args.seulement_echoues)
    print(f"\n{n} job(s) purgé(s). Les PDF éventuels sont restés sur le disque.")
    conn.close()
    return 0


def cmd_travailler(args) -> int:
    if not worker_module.worker_autorise():
        print("Ce processus ne doit pas porter le worker.", file=sys.stderr)
        return 1

    w = worker_module.Worker(db_path=args.db, intervalle=args.intervalle)

    if args.une_passe:
        return _vider(w)

    print("Worker démarré. Ctrl+C pour arrêter.")
    w.demarrer()
    try:
        while w.actif:
            w._thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("\nArrêt demandé…")
        w.arreter()
    if w.refuse_faute_de_verrou:
        print(_AUTRE_WORKER, file=sys.stderr)
        return 1
    return 0


_AUTRE_WORKER = (
    "Un autre processus consomme déjà la file de cette base (l'app web, "
    "sans doute).\n  Rien n'a été traité ici : deux workers se disputeraient "
    "Ollama."
)


def _vider(w) -> int:
    """Vide la file dans ce processus. Rend le code de sortie."""
    traites = w.vider()
    if traites is None:
        print(_AUTRE_WORKER, file=sys.stderr)
        return 1
    print(f"{traites} job(s) traité(s). File vide.")
    if w.decharges:
        print("Modèle déchargé (file vidée).")
    return 0


# =====================================================================
# Batch : enfiler en masse, puis consommer
# =====================================================================
def cmd_batch(args) -> int:
    """Demande un CV pour les meilleures offres, puis vide la file.

    ## Le batch n'est PAS un second chemin de génération

    Il n'appelle ni ``generate_cv`` ni ``cv_forge`` : il pose des lignes
    dans ``generation_jobs`` avec ``jobs.demander`` — le même point
    d'entrée que le bouton de l'UI — puis consomme la file avec le même
    ``Worker``. Construction de l'``OfferInput``, nettoyage du titre,
    calcul de l'``offer_hash``, jeton Ollama et déchargement : rien de
    tout cela n'est réécrit ici, tout est déjà dans ``jobs`` et
    ``worker``.

    L'idempotence vaut donc pour le batch sans une ligne de plus : une
    offre déjà générée sous le même ``offer_hash`` retrouve son job
    ``done`` et n'est pas régénérée.

    ## Le worker unique reste unique

    Deux réponses étaient possibles, et c'est la RÉUNION des deux qui est
    juste, parce qu'aucune ne couvre les deux situations réelles :

    - « le batch enfile et attend » suppose que quelqu'un consomme. La
      nuit, Flask ne tourne pas : la file resterait pleine au matin ;
    - « le batch EST le worker » suppose l'inverse. Lancé pendant que
      l'app web tourne, il ferait deux workers sur un Ollama qui n'en
      supporte qu'un.

    Le batch tente donc de PRENDRE le verrou de worker. S'il l'obtient,
    il consomme lui-même ; sinon, il sait que quelqu'un d'autre consomme,
    et il se contente d'attendre la file. Le choix se fait à l'exécution,
    d'après ce qui tourne — et non d'après ce qu'on croyait au moment de
    lancer la commande.
    """
    conn = storage.ouvrir(args.db)
    jobs.ensure_schema(conn)

    from cv_forge import ForgeConfig

    master = Path(config.CV_MASTER_PATH)
    forge = ForgeConfig()

    # Les meilleures d'abord : `dernier_score` est le score du dernier run.
    # NULLS LAST explicite — en SQLite, NULL trie AVANT tout le reste en
    # DESC, donc les offres jamais classées passeraient devant.
    candidates = conn.execute(
        """SELECT o.cle FROM offres o
           JOIN offres_texte t ON t.cle = o.cle
           ORDER BY (o.dernier_score IS NULL), o.dernier_score DESC, o.cle
           LIMIT ?""",
        (args.limite,),
    ).fetchall()

    enfiles = reutilises = 0
    refus: dict[str, int] = {}
    for ligne in candidates:
        try:
            _job, reutilise = jobs.demander(
                conn, ligne["cle"], master_path=master, config=forge
            )
        except jobs.DemandeRefusee as r:
            refus[r.code] = refus.get(r.code, 0) + 1
            if r.code == "MASTER_INVALID":
                # Le même refus pour toutes les offres : inutile d'itérer.
                print(f"Erreur [{r.code}] : {r.message}", file=sys.stderr)
                conn.close()
                return 1
            continue
        reutilises += reutilise
        enfiles += not reutilise

    total, = conn.execute("SELECT COUNT(*) FROM offres").fetchone()
    # Comptée par la JOINTURE et non par `COUNT(*) FROM offres_texte` : un
    # texte peut survivre à son offre (fusion de doublons), et le compte
    # afficherait alors plus d'offres pourvues qu'il n'y a d'offres.
    sans_texte, = conn.execute(
        "SELECT COUNT(*) FROM offres o LEFT JOIN offres_texte t ON t.cle = o.cle "
        "WHERE t.cle IS NULL"
    ).fetchone()
    en_attente = jobs.en_attente(conn)
    conn.close()

    print(f"Offres          : {total}  (avec texte : {total - sans_texte})")
    # Le chiffre à surveiller : ces offres-là ne sont PAS candidates, et
    # le resteront tant qu'un scrape ne les aura pas revues en ligne.
    print(f"  sans texte    : {sans_texte}   [TEXT_MISSING] -> non candidates")
    print(f"  candidates    : {len(candidates)}   (limite {args.limite})")
    print(f"  enfilées      : {enfiles}")
    print(f"  déjà connues  : {reutilises}   (idempotence : même hash -> même job)")
    for code, n in sorted(refus.items()):
        print(f"  refusées      : {n}   [{code}]")
    print(f"File            : {en_attente} job(s) à traiter")

    if args.enfiler_seulement:
        print("\n--enfiler-seulement : rien n'a été consommé.")
        return 0
    if not en_attente:
        return 0

    w = worker_module.Worker(db_path=args.db)
    traites = w.vider()
    if traites is not None:
        print(f"\n{traites} job(s) traité(s). File vide.")
        if w.decharges:
            print("Modèle déchargé (file vidée).")
        return 0

    print(f"\n{_AUTRE_WORKER}")
    print("Les jobs sont enfilés : l'autre worker les traitera.")
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

    p_orp = sous.add_parser("orphelins", help="Jobs dont l'offre a disparu")
    p_orp.add_argument("--purger", action="store_true",
                       help="Supprime les lignes (jamais les fichiers PDF)")
    p_orp.add_argument("--seulement-echoues", action="store_true",
                       help="Ne purge que les jobs qui n'ont rien produit")

    p_tra = sous.add_parser("travailler", help="Consomme la file")
    p_tra.add_argument("--une-passe", action="store_true",
                       help="S'arrête dès que la file est vide")
    p_tra.add_argument("--intervalle", type=float,
                       default=worker_module.INTERVALLE_SONDAGE_S)

    p_bat = sous.add_parser(
        "batch", help="Demande un CV pour les meilleures offres, puis consomme")
    p_bat.add_argument("--limite", type=int, default=LIMITE_BATCH_DEFAUT,
                       help=f"Nombre d'offres considérées, les mieux classées "
                            f"d'abord (défaut : {LIMITE_BATCH_DEFAUT})")
    p_bat.add_argument("--enfiler-seulement", action="store_true",
                       help="Enfile sans consommer : laisse la file au worker "
                            "de l'app web, ou à un « travailler » ultérieur")

    args = parser.parse_args(argv)
    return {"etat": cmd_etat, "enfiler": cmd_enfiler, "orphelins": cmd_orphelins,
            "travailler": cmd_travailler, "batch": cmd_batch}[args.commande](args)


if __name__ == "__main__":
    raise SystemExit(main())
