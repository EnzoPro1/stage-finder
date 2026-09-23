"""
main.py — Orchestrateur du pipeline complet.

    Sources -> Normalisation -> Filtres durs -> Extraction -> Déduplication
            -> (Dédup floue) -> Ranking -> Persistance -> Sortie

Lancement :
    python main.py                 # pipeline complet, affichage CLI + exports
    python main.py --limit 20      # n'affiche que le top 20 dans la console
    python main.py --no-jobspy     # ignore le scraping JobSpy (plus rapide)
    python main.py --min-score 0.4 # ne garde que les offres au score >= 0.4
    python main.py --new-only      # n'affiche que les offres jamais vues (SQLite)
    python main.py --no-db         # ne lit/écrit pas la base SQLite
    python main.py --no-verify     # désactive la vérification LLM (cosinus seul)
    python main.py --verify-top-n 10        # ne vérifie que le top 10 par LLM
    python main.py --verify-model llama3.1  # change le modèle Ollama de vérification
    python main.py --csv out.csv --html out.html   # chemins d'export personnalisés
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import console  # noqa: F401 - force UTF-8 sur la console Windows (badge ★, emoji)
import cache_embeddings
import config
from config_schema import valider_config
import recherche
from normalize import Offre, normaliser, source_affichee
from filters import filtrer
import dedup
import extract
import llm
import observabilite
import ollama_pool
import progression
import ranker
import rapport
import report
import storage
import verifier
from sources import RedactingFilter, registry
from sources.provenance import familles_de

logger = logging.getLogger("job-finder")


def _configurer_logs() -> None:
    """Logs lisibles pour notre code, silence pour les libs trop bavardes.

    On pose un ``RedactingFilter`` sur le handler racine : tout message (y
    compris ceux des libs tierces) est redacté avant écriture, ce qui empêche
    une clé API / un jeton de fuir dans les logs (durcissement §4.2).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    for bruyant in ("httpx", "huggingface_hub", "sentence_transformers",
                    "transformers", "urllib3", "filelock", "JobSpy"):
        logging.getLogger(bruyant).setLevel(logging.WARNING)


@dataclass(frozen=True)
class Perimetre:
    """Ce qu'une collecte interroge : stages ou jobs étudiants."""

    nom: str                      # libellé du bilan : « stages », « jobs étudiants »
    sources: tuple[str, ...]
    ecartees: dict
    point_entree: str             # fonction appelée dans le module de chaque source
    sites_jobspy: tuple[str, ...]
    familles: tuple[str, ...]     # familles (ou étiquettes) amorcées dans le bilan


def perimetre_stages() -> Perimetre:
    """Lu à l'appel, pas à l'import : un test qui modifie `config` est pris en compte."""
    return Perimetre("stages", tuple(config.SOURCES_ACTIVES), config.SOURCES_ECARTEES_STAGES,
                     "recuperer_offres", tuple(config.JOBSPY_SITES),
                     tuple(recherche.charger().noms_familles()))


def perimetre_jobs() -> Perimetre:
    jobs = recherche.charger().student_jobs
    etiquettes = tuple(jobs.familles()) + (recherche.ETIQUETTE_SANS_MOT_CLE,) if jobs else ()
    return Perimetre("jobs étudiants", tuple(config.SOURCES_ACTIVES_JOBS),
                     config.SOURCES_ECARTEES_JOBS, "recuperer_jobs_etudiants",
                     tuple(config.JOBSPY_SITES_JOBS), etiquettes)


def lignes_du_bilan(source: registry.Source, perimetre: Perimetre | None = None) -> list[str]:
    """Les lignes qu'une source occupe dans le bilan : une par site pour JobSpy.

    Elles sont amorcées AVANT la collecte, pour qu'un site qui ne rend rien
    apparaisse à 0 — donc MUET — au lieu de ne pas apparaître du tout.
    """
    if source.nom == "jobspy":
        sites = perimetre.sites_jobspy if perimetre else config.JOBSPY_SITES
        return [f"jobspy:{site}" for site in sites]
    return [source.nom]


def _collecter_source(source: registry.Source, perimetre: Perimetre | None = None) -> list[Offre]:
    """Interroge UNE source et renvoie ses offres normalisées.

    Toute erreur est absorbée ici — Y COMPRIS l'import du module de la source,
    qui est paresseux et peut échouer (dépendance manquante) : une source qui
    tombe renvoie [] sans entamer les autres.

    L'absorption reste entière ; ce qui change est qu'elle laisse une TRACE
    (``observabilite``). Une source qui rend [] parce qu'elle est tombée et
    une source qui rend [] parce que le marché est vide s'écrivaient pareil.
    """
    try:
        if perimetre is None or perimetre.point_entree == "recuperer_offres":
            recuperer = source.recuperer()
        else:
            recuperer = source.recuperer(perimetre.point_entree)
        brutes = recuperer()
    except Exception as err:  # noqa: BLE001 - une source ne doit jamais tout casser
        for ligne in lignes_du_bilan(source, perimetre):
            observabilite.signaler(ligne, "import", f"{type(err).__name__}: {err}")
        logger.warning("Source « %s » en échec complet : %s", source.nom, err)
        return []
    offres = normaliser(source.nom, brutes)
    releve = observabilite.actif()
    if releve is not None:
        for ligne, n in Counter(source_affichee(source.nom, b) for b in brutes).items():
            releve.compter_brut(ligne, n)
        for ligne, n in Counter(o.source for o in offres).items():
            releve.compter_normalisees(ligne, n)
        releve.compter_brut_familles([familles_de(b) for b in brutes])
    return offres


def collecter(utiliser_jobspy: bool, perimetre: Perimetre | None = None) -> list[Offre]:
    """
    Interroge les sources du périmètre (stages par défaut : ``config.SOURCES_ACTIVES``)
    et renvoie les offres normalisées.

    Les sources API (Adzuna, Jooble, France Travail, Careerjet, Free-Work) sont
    interrogées EN PARALLÈLE dans un pool de threads : la collecte est quasi
    entièrement I/O-bound (on attend le réseau), donc le parallélisme fait
    gagner du temps sans complexité async. Les sources de SCRAPING (JobSpy)
    restent SÉQUENTIELLES par politesse (délais entre requêtes).

    La liste des sources vient du catalogue (``sources/registry.py``) : ajouter
    un site ne demande plus de toucher à cet orchestrateur.
    """
    perimetre = perimetre or perimetre_stages()
    actives = registry.sources_actives(list(perimetre.sources))
    if not utiliser_jobspy:
        # --no-jobspy / mode « rapide » : on saute tout ce qui scrape.
        actives = [s for s in actives if not s.sequentiel]

    # Relevé posé AVANT le premier appel et amorcé avec les sources attendues :
    # une source qui échoue dès l'import doit apparaître en panne dans le
    # tableau, pas en être absente.
    observabilite.demarrer([ligne for s in actives for ligne in lignes_du_bilan(s, perimetre)],
                           ecartees=perimetre.ecartees, perimetre=perimetre.nom,
                           familles=list(perimetre.familles))

    api = [s for s in actives if not s.sequentiel]
    scraping = [s for s in actives if s.sequentiel]
    logger.info(
        "Sources interrogées : %s.",
        ", ".join(s.nom for s in actives) or "aucune",
    )

    toutes: list[Offre] = []
    total, finies = len(api) + len(scraping), 0
    progression.signaler("Collecte des sources", 0, total)
    if api:
        with ThreadPoolExecutor(max_workers=config.MAX_THREADS_COLLECTE) as executor:
            futures = [executor.submit(_collecter_source, s, perimetre) for s in api]
            for future in as_completed(futures):
                toutes.extend(future.result())
                finies += 1
                progression.signaler("Collecte des sources", finies, total)

    for source in scraping:
        # Le scraping est séquentiel et c'est l'étape la plus longue : on le nomme.
        progression.signaler(f"Collecte des sources ({source.nom}, le plus long)",
                             finies, total)
        toutes.extend(_collecter_source(source, perimetre))
        finies += 1

    logger.info("Collecte totale : %d offre(s) normalisée(s).", len(toutes))
    return toutes


def afficher_cli(classees: list[tuple[Offre, float]], limite: int | None) -> None:
    """Affiche le classement dans la console : rang, score, titre, etc."""
    a_montrer = classees[:limite] if limite else classees
    print(f"\n{'='*100}")
    print(f" {len(classees)} offre(s) classée(s) par pertinence"
          + (f" — top {limite}" if limite and limite < len(classees) else ""))
    print(f"{'='*100}\n")
    for rang, (offre, score) in enumerate(a_montrer, 1):
        marque_neuf = "🆕 " if getattr(offre, "nouvelle", False) else ""
        badges = f"  {' '.join(offre.tags)}" if offre.tags else ""
        details = []
        if offre.duree_mois:
            details.append(f"{offre.duree_mois} mois")
        if offre.date_debut:
            details.append(f"début {offre.date_debut}")
        suffixe = f"  ({', '.join(details)})" if details else ""
        print(f"#{rang:<3} [{score:.3f}]{badges}  {marque_neuf}{offre.title}{suffixe}")
        print(f"       {offre.company or '—'}  |  {offre.location or '—'}  |  {offre.source}")
        _afficher_verdict(getattr(offre, "verdict", None))
        print(f"       {offre.url}\n")


def _afficher_verdict(verdict) -> None:
    """Ligne CLI compacte pour le verdict LLM : score, drapeaux rouges, justif."""
    if verdict is None:
        return
    verdict_bits = [f"LLM {verdict.score:.2f}"]
    if verdict.est_alternance:
        verdict_bits.append("⚠ alternance")
    if verdict.niveau and verdict.niveau not in ("stage", "inconnu"):
        verdict_bits.append(f"niveau={verdict.niveau}")
    if verdict.drapeaux_rouges:
        verdict_bits.append("🚩 " + " ; ".join(verdict.drapeaux_rouges[:3]))
    print("       " + "  ".join(verdict_bits))
    if verdict.justification:
        justif = verdict.justification.strip().replace("\n", " ")
        if len(justif) > 140:
            justif = justif[:137] + "…"
        print(f"       « {justif} »")


def _appliquer_seuil(
    classees: list[tuple[Offre, float]], min_score: float
) -> list[tuple[Offre, float]]:
    """Filtre par score minimal, en préservant les combos si configuré."""
    def garder(offre: Offre, score: float) -> bool:
        if config.COMBO_TOUJOURS_AFFICHE and "★ IA+CYBER" in offre.tags:
            return True
        return score >= min_score

    avant = len(classees)
    gardees = [(o, s) for (o, s) in classees if garder(o, s)]
    logger.info(
        "Après seuil (score >= %.2f, combos préservés) : %d offre(s) sur %d.",
        min_score, len(gardees), avant,
    )
    return gardees


def collecter_et_classer(utiliser_jobspy: bool) -> list[tuple[Offre, float]]:
    """Pipeline cosinus SANS vérification LLM : renvoie le classement de base.

        Sources -> Filtres -> Extraction -> Dédup (exacte + floue) -> Ranking

    Extrait de ``executer`` pour être réutilisé tel quel par l'app web (app.py),
    qui affiche ce classement immédiatement, avant toute vérification IA.
    L'étape en cours est tenue dans ``progression`` (lue par l'app), et
    effacée à la sortie, y compris sur exception.
    """
    try:
        return _collecter_et_classer(utiliser_jobspy)
    finally:
        progression.effacer()


def _collecter_et_classer(utiliser_jobspy: bool) -> list[tuple[Offre, float]]:
    # Nouveau run : le digest des modèles d'embeddings Ollama sera relu (un
    # `ollama pull` fait depuis le run précédent invalide leur cache).
    cache_embeddings.oublier_digests()
    offres = collecter(utiliser_jobspy=utiliser_jobspy)
    if not offres:
        logger.warning("Aucune offre collectée. Vérifie tes clés API dans le .env.")
        return []

    progression.signaler("Filtres et déduplication")
    offres = filtrer(offres)
    # Bilan par source : il ne peut être rendu qu'ICI, après les filtres — les
    # survivantes ne sont connues qu'une fois `filtrer` passé — et il doit
    # l'être ici plutôt que dans `executer`, parce que c'est le seul point que
    # traversent les DEUX portes d'entrée (CLI et app web). Même raisonnement
    # que `storage.enregistrer_run` pour le texte des offres.
    observabilite.journaliser()

    extract.annoter_toutes(offres)
    offres = dedup.dedupliquer(offres)
    if not offres:
        logger.warning("Aucune offre après déduplication.")
        return []

    # La dédup floue a SON modèle (seuil calibré sur MiniLM) ; le classement,
    # le sien. Même modèle — celui voulu ou celui de repli : vecteurs calculés
    # UNE fois et partagés.
    connus = {}
    if config.DEDUP_FLOUE_ACTIVE:
        progression.signaler(f"Déduplication floue ({len(offres)} offres)")
        embeddings = ranker.encoder_offres(offres, modele=config.MODELE_EMBEDDING_DEDUP)
        offres, embeddings = dedup.dedupliquer_flou(offres, embeddings)
        connus[config.MODELE_EMBEDDING_DEDUP] = embeddings

    progression.signaler(f"Classement ({len(offres)} offres)")
    resultat = ranker.classer_avec_repli(offres, connus)
    # Le modèle qui a RÉELLEMENT servi part avec le bilan du run (table `runs`).
    releve = observabilite.actif()
    if releve is not None:
        releve.noter_classement(resultat.modele, resultat.repli)
    # Enchaînement VRAM : le modèle d'embeddings (s'il est servi par Ollama)
    # quitte le GPU AVANT que la vérification n'y charge le LLM.
    ranker.liberer_modele(resultat.modele)
    return resultat.classees


def verifier_shortlist(
    classees: list[tuple[Offre, float]],
    conn,
    top_n: int,
    model: str,
    url: str | None = None,
    on_progress=None,
) -> list[tuple[Offre, float]]:
    """Vérifie la shortlist top-N par LLM local, puis recompose et re-trie.

    *retrieve-then-verify* : on ne passe le LLM que sur les ``top_n`` premières
    offres du tri cosinus. Pour chacune : cache hit -> réutilisé ; sinon appel
    Ollama puis mise en cache. Le reste du classement garde son score cosinus.
    Si Ollama est injoignable : classement cosinus conservé tel quel (dégradation).

    ``on_progress(info: dict)`` est notifié à chaque étape (``phase`` =
    "indisponible" | "verif" | "fini"). Cela découple la boucle de son
    affichage : le CLI branche une barre console, l'app web une barre HTTP.
    Source de vérité unique partagée par ``executer`` et ``app.py``.
    """
    reglages = llm.charger_reglages()
    url = (url or reglages.ollama_url).rstrip("/")
    reglages = reglages.model_copy(update={"ollama_url": url, "modele": model})

    if not verifier.ollama_disponible(url):
        if on_progress:
            on_progress({"phase": "indisponible", "url": url})
        return classees

    shortlist = classees[:top_n]
    reste = classees[top_n:]
    total = len(shortlist)
    verifiees: list[tuple[Offre, float]] = []
    nb_appels = nb_cache = nb_echecs = 0
    # Présence du modèle : vérifiée au PREMIER défaut de cache seulement. Un
    # run servi entièrement par le cache n'a pas besoin du modèle ; un modèle
    # absent, lui, ferait échouer chaque offre une à une — on le dit une fois
    # et on garde le cosinus pour tout ce qui n'est pas en cache.
    modele_controle = modele_absent = False

    # Clé de cache VERSIONNÉE et liée au profil : durcir le prompt ou changer
    # le profil doit invalider les anciens verdicts, sinon les offres déjà
    # vues ressortent avec un jugement périmé. Le MÊME profil sert à la clé et
    # au prompt.
    profil = config.REQUETE_REFERENCE
    cle_modele = verifier.cle_cache(model, profil)

    for i, (offre, score) in enumerate(shortlist, 1):
        empreinte = storage.hash_offre(offre)
        cache = storage.get_verdict(conn, empreinte, cle_modele) if conn is not None else None
        if cache is not None:
            verdict = verifier.Verdict.from_dict(cache)
            nb_cache += 1
        else:
            if not modele_controle:
                modele_controle = True
                modele_absent = _signaler_modele_absent(model, reglages, on_progress)
            verdict = None
            if not modele_absent:
                verdict = verifier.verifier(offre, profil,
                                            model=model, url=url, reglages=reglages)
                nb_appels += 1
                if verdict is None:
                    nb_echecs += 1
                elif conn is not None:
                    storage.save_verdict(conn, empreinte, cle_modele, verdict.to_dict())

        offre.verdict = verdict
        verifiees.append((offre, verifier.score_final(score, verdict)))
        if on_progress:
            on_progress({
                "phase": "verif", "fait": i, "total": total,
                "cache": nb_cache, "appels": nb_appels, "echecs": nb_echecs,
            })

    # Fin de lot : le modèle, gardé chaud d'une offre à l'autre par keep_alive,
    # rend la VRAM maintenant plutôt qu'à l'expiration du délai.
    if nb_appels:
        ollama_pool.decharger(model, url=url)

    fusion = verifiees + reste
    fusion.sort(key=lambda couple: couple[1], reverse=True)
    if on_progress:
        on_progress({
            "phase": "fini", "total": total, "cache": nb_cache,
            "appels": nb_appels, "echecs": nb_echecs, "model": model,
        })
    return fusion


def _signaler_modele_absent(model: str, reglages, on_progress) -> bool:
    """Vrai si ``model`` manque dans Ollama ; le signale alors via ``on_progress``."""
    try:
        manquants = llm.modeles_manquants([model], reglages)
    except llm.OllamaIndisponible:
        # Le ping vient de répondre : panne passagère. On laisse les appels
        # tenter leur chance, chacun échouera proprement s'il le faut.
        return False
    if manquants and on_progress:
        on_progress({"phase": "modele_absent",
                     "message": llm.message_modeles_manquants(manquants)})
    return bool(manquants)


def _progress_cli(info: dict) -> None:
    """Callback de progression pour la vérif LLM en CLI (barre + logs)."""
    phase = info.get("phase")
    if phase == "indisponible":
        logger.warning(
            "Ollama injoignable (%s) : vérification LLM ignorée, classement "
            "cosinus conservé. (démarre `ollama serve` pour l'activer)", info["url"],
        )
    elif phase == "modele_absent":
        logger.warning("%s — offres hors cache : classement cosinus conservé.",
                       info["message"])
    elif phase == "verif":
        print(f"\r  🔎 Vérification LLM {info['fait']}/{info['total']}"
              f"  (cache {info['cache']}, appels {info['appels']})…", end="", flush=True)
    elif phase == "fini":
        if info["total"]:
            print()  # saut de ligne après la barre de progression
        logger.info(
            "Vérification LLM (%s) : %d offre(s), %d appel(s), %d depuis le cache, %d échec(s).",
            info["model"], info["total"], info["appels"], info["cache"], info["echecs"],
        )


def executer(args: argparse.Namespace) -> None:
    """Déroule le pipeline complet de bout en bout."""
    # 0) Garde-fou : la config est-elle cohérente ? (fail-fast si non). Les
    #    réglages LLM effectifs aussi : une variable SF_* invalide s'arrête ici.
    valider_config()
    reglages = llm.charger_reglages()

    if args.jobs_etudiants:
        import jobs_etudiants
        jobs_etudiants.executer(utiliser_jobspy=not args.no_jobspy,
                                chemin_base=None if args.no_db else config.CHEMIN_BASE_JOBS,
                                chemin_rapport=args.html)
        return

    # Ollama prêt ? Dit AVANT des minutes de collecte, avec la commande qui répare.
    verification = config.VERIFY_ENABLED and not args.no_verify
    if verification:
        avertissement = llm.diagnostic_demarrage(
            [args.verify_model or reglages.modele, *ranker.modeles_ollama_requis()],
            reglages)
        if avertissement:
            logger.warning(avertissement)

    # 1-6) Collecte -> ... -> Ranking cosinus (sans IA)
    classees = collecter_et_classer(utiliser_jobspy=not args.no_jobspy)

    # Bilan par source, IMPRIMÉ avant tout retour anticipé : c'est justement
    # quand le run ne rend rien qu'on a besoin de savoir laquelle des sources
    # n'a rien rapporté, et pourquoi.
    tableau = observabilite.rendre_tableau()
    if tableau:
        print(f"\n{'='*78}\n BILAN PAR SOURCE\n{'='*78}\n{tableau}\n")

    if not classees:
        logger.warning("Aucune offre à classer.")
        return

    # 7) Vérification LLM (shortlist top-N) + persistance : une seule connexion
    #    SQLite sert au cache des verdicts ET à l'historique des runs.
    conn = storage.ouvrir(args.db) if not args.no_db else None

    # 7a) Vérification LLM : enrichit chaque offre de la shortlist d'un verdict
    #     explicable et recompose le score (cosinus + LLM). Optionnelle.
    if verification:
        classees = verifier_shortlist(
            classees, conn,
            top_n=args.verify_top_n or reglages.top_n,
            model=args.verify_model or reglages.modele,
            on_progress=_progress_cli,
        )

    # 7b) Persistance SQLite : enregistre le run complet, repère les nouveautés
    nouvelles: set[str] = set()
    if conn is not None:
        releve = observabilite.actif()
        nouvelles = storage.enregistrer_run(conn, classees,
                                            bilan=releve.resume() if releve else None)
        for offre, _score in classees:
            offre.nouvelle = storage.cle_identite(offre) in nouvelles
        conn.close()

    # 8) Filtrages d'affichage : seuil de score, puis « nouveautés seulement »
    if args.min_score is not None:
        classees = _appliquer_seuil(classees, args.min_score)
    if args.new_only:
        classees = storage.filtrer_nouveautes(classees, nouvelles)
        logger.info("Nouveautés uniquement : %d offre(s).", len(classees))

    if not classees:
        logger.warning("Aucune offre à afficher après ranking/seuil.")
        return

    # 9) Sorties. Le rapport HTML est lu dans les BASES (stages + jobs
    #    étudiants) : sans base, il n'a rien de ce run à montrer.
    afficher_cli(classees, args.limit)
    report.exporter_csv(classees, args.csv)
    if args.no_db:
        print(f"Export : {args.csv}  (rapport HTML non réécrit : --no-db)\n")
    else:
        rapport.generer(args.db, config.CHEMIN_BASE_JOBS, args.html)
        print(f"Exports : {args.csv}  |  {args.html}\n")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agrégateur de stages classés par pertinence sémantique.")
    p.add_argument("--limit", type=int, default=None, help="Nombre d'offres affichées dans la console.")
    p.add_argument("--no-jobspy", action="store_true", help="Ne pas scraper via JobSpy (plus rapide).")
    p.add_argument("--jobs-etudiants", action="store_true",
                   help="Collecte des JOBS ÉTUDIANTS autour des origines (base séparée).")
    p.add_argument("--min-score", type=float, default=None, help="Seuil minimal de similarité (0-1).")
    p.add_argument("--new-only", action="store_true", help="N'afficher que les offres jamais vues (SQLite).")
    p.add_argument("--db", default=config.CHEMIN_BASE, help="Chemin de la base SQLite.")
    p.add_argument("--no-db", action="store_true", help="Ne pas lire/écrire la base SQLite.")
    p.add_argument("--csv", default="stages.csv", help="Chemin du fichier CSV de sortie.")
    p.add_argument("--html", default=str(config.CHEMIN_RAPPORT),
                   help="Chemin du rapport HTML unifié (stages + jobs étudiants).")
    p.add_argument("--no-verify", action="store_true",
                   help="Désactive la vérification LLM (pipeline cosinus seul).")
    p.add_argument("--verify-model", default=None,
                   help=f"Modèle Ollama de vérification (défaut : {config.VERIFY_MODEL}, ou SF_LLM_MODEL).")
    p.add_argument("--verify-top-n", type=int, default=None,
                   help=f"Taille de la shortlist vérifiée (défaut : {config.VERIFY_TOP_N}, ou SF_LLM_TOP_N).")
    return p


if __name__ == "__main__":
    _configurer_logs()
    executer(_parser().parse_args())
