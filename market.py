"""
market.py — Indicateurs « le marché est-il favorable ? ».

Le reste de l'outil répond à « quelles offres ». Ce module répond à une autre
question : **est-ce le bon moment, et ma niche est-elle porteuse ?** Deux
familles d'indicateurs, volontairement séparées parce qu'elles n'ont pas la
même fiabilité :

1. **Volumes de marché** (temps réel, sources externes) — combien de stages sont
   publiés en Île-de-France, en ce moment, par thème. Mesurés SANS rapatrier les
   offres : France Travail donne le total dans l'en-tête ``Content-Range``,
   Careerjet dans son champ ``hits``. Un appel par indicateur, donc quelques
   secondes pour tout le tableau de bord.

   Les deux sources sont conservées SÉPARÉMENT et jamais additionnées : elles se
   recouvrent partiellement (une même annonce peut être dans les deux) et ne
   couvrent pas le même gisement — France Travail voit le service public de
   l'emploi, Careerjet agrège le privé. Les additionner produirait un chiffre
   faux ; les lire côte à côte est informatif.

2. **Dynamique de TA recherche** (historique local SQLite) — ce que tes propres
   runs racontent : le gisement se renouvelle-t-il, ou tournes-tu en rond sur
   les mêmes annonces ?

Aucune de ces mesures n'est un taux de tension officiel (offres / demandeurs) :
ça, c'est l'API « Statistiques marché du travail » de France Travail, qui
demande une souscription à part. Ce qui est mesuré ici est le CÔTÉ OFFRE, et
c'est annoncé comme tel dans l'interface.

Testable isolément :  python -m market
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import config

logger = logging.getLogger(__name__)


# Fenêtres d'observation, en jours. La comparaison 7 j / 31 j donne le RYTHME de
# publication : c'est ce qui distingue « marché étroit » de « marché à l'arrêt ».
FENETRE_COURTE = 7
FENETRE_LONGUE = 31

# Ligne de référence : TOUS les stages d'Île-de-France, tous métiers confondus.
# Sert de dénominateur (ta niche pèse-t-elle lourd ?) et de baromètre de
# saisonnalité indépendant de ton domaine.
THEME_REFERENCE = "tous stages (IdF)"

# Code région pour l'indicateur de tension. Résolu UNE fois au premier usage
# depuis le référentiel France Travail (et non supposé égal au code INSEE).
_REGION = "11"
_region_resolue = False


def _resoudre_region() -> None:
    """Fixe ``_REGION`` depuis le référentiel France Travail (une seule fois)."""
    global _REGION, _region_resolue
    if _region_resolue:
        return
    from sources import marche_travail

    _region_resolue = True
    code = marche_travail.code_region()
    if code:
        _REGION = code


def metiers_cibles() -> list[dict]:
    """Métiers suivis, résolus en codes ROME par ROMEO (ou repli config).

    Un même intitulé peut donner plusieurs codes ROME (ex. « ingénieur
    cybersécurité » -> architecte sécurité ET expert cybersécurité) : on les
    garde tous, chacun devient une ligne du tableau de bord.
    """
    from sources import romeo

    predits = romeo.predire_metiers(config.METIERS_SUIVIS, config.ROME_PAR_METIER)
    if not predits:
        logger.info("ROMEO indisponible : codes ROME de repli (config.ROME_REPLI).")
        predits = config.ROME_REPLI

    metiers, vus = [], set()
    for intitule in config.METIERS_SUIVIS:
        for m in (predits.get(intitule) or [])[: config.ROME_PAR_METIER]:
            code = m.get("code")
            if not code or code in vus:  # un code peut sortir sur deux intitulés
                continue
            vus.add(code)
            metiers.append({"code": code, "libelle": m.get("libelle") or code,
                            "intitule": intitule})
    return metiers


def _mesurer_metier(metier: dict) -> dict:
    """Volumes d'un métier : stages 7 j / 31 j, tous contrats, et TENSION."""
    from sources import careerjet, france_travail, marche_travail

    code = metier["code"]
    # Indicateur officiel de difficulté de recrutement (France Travail PERSP_2).
    # C'est la seule mesure ici qui regarde le côté DEMANDE : tout le reste
    # compte des annonces. On la garde entière (valeur + année + sous-facteurs).
    tension = marche_travail.tension(code, _REGION)
    return {
        "tension": tension.get("principal") if tension else None,
        "tension_annee": tension.get("annee") if tension else None,
        "tension_details": tension.get("details", [])[:3] if tension else [],
        "theme": metier["libelle"],
        "code_rome": code,
        "requete": metier["intitule"],
        "ft_court": france_travail.compter_offres(
            code_rome=code, publiee_depuis=FENETRE_COURTE, stages_seulement=True),
        "ft_long": france_travail.compter_offres(
            code_rome=code, publiee_depuis=FENETRE_LONGUE, stages_seulement=True),
        # Volume TOUS CONTRATS du métier : dit si le métier recrute en général,
        # même quand il n'ouvre pas de stage. Un métier porteur sans stage
        # publié est une cible de candidature spontanée.
        "ft_tous_contrats": france_travail.compter_offres(
            code_rome=code, publiee_depuis=FENETRE_LONGUE),
        # Careerjet ignore le ROME : on l'interroge sur l'intitulé, et sa valeur
        # est un STOCK total, pas un flux. On ne la compare jamais aux colonnes
        # France Travail, on l'affiche à part.
        "careerjet_total": careerjet.compter_offres(f"stage {metier['intitule']}"),
    }


def _mesurer_reference() -> dict:
    """Ligne « tous stages IdF », mesurée par mot-clé (aucun ROME ne la couvre)."""
    from sources import careerjet, france_travail

    return {
        "theme": THEME_REFERENCE,
        "code_rome": None,
        # La tension est un indicateur PAR MÉTIER : elle n'a pas de sens sur une
        # ligne « tous métiers confondus ».
        "tension": None, "tension_annee": None, "tension_details": [],
        "requete": "stage",
        "ft_court": france_travail.compter_offres("stage", FENETRE_COURTE),
        "ft_long": france_travail.compter_offres("stage", FENETRE_LONGUE),
        "ft_tous_contrats": None,
        "careerjet_total": careerjet.compter_offres("stage"),
    }


def volumes() -> list[dict]:
    """Volumes de stages publiés en IdF, par métier ROME. Mesures en parallèle."""
    _resoudre_region()
    metiers = metiers_cibles()
    with ThreadPoolExecutor(max_workers=config.MAX_THREADS_COLLECTE) as executor:
        mesures = list(executor.map(_mesurer_metier, metiers))
        reference = executor.submit(_mesurer_reference).result()
    return [reference] + mesures


def _part(numerateur: int | None, denominateur: int | None) -> float | None:
    """Pourcentage sûr : None si l'une des deux mesures manque ou si /0."""
    if numerateur is None or not denominateur:
        return None
    return round(100 * numerateur / denominateur, 1)


def _rythme(court: int | None, long: int | None) -> float | None:
    """Rythme de publication récent rapporté à la moyenne de la fenêtre longue.

    1.0 = on publie au rythme habituel du mois écoulé ; > 1 = ça accélère ;
    < 1 = ça ralentit. On extrapole la fenêtre courte à la longue plutôt que de
    comparer des durées différentes (7 jours pèsent moins que 31 par nature).
    """
    if court is None or not long:
        return None
    attendu = long * (FENETRE_COURTE / FENETRE_LONGUE)
    if attendu <= 0:
        return None
    return round(court / attendu, 2)


def historique(conn: sqlite3.Connection, n_runs: int = 12) -> dict:
    """Dynamique de TA recherche, lue dans la base SQLite locale."""
    runs = [
        {"horodatage": h, "nb_offres": n, "nb_nouvelles": nn}
        for h, n, nn in conn.execute(
            "SELECT horodatage, nb_offres, nb_nouvelles FROM runs "
            "ORDER BY id DESC LIMIT ?", (n_runs,)
        )
    ][::-1]  # remis en ordre chronologique pour l'affichage

    total_offres = conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0]
    # Une offre « revue » (nb_vues > 1) est une annonce qui reste ouverte : un
    # gisement fait surtout de revues signifie que tu as déjà tout vu.
    revues = conn.execute("SELECT COUNT(*) FROM offres WHERE nb_vues > 1").fetchone()[0]
    combos = conn.execute(
        "SELECT COUNT(*) FROM offres WHERE tags LIKE '%IA+CYBER%'"
    ).fetchone()[0]
    entreprises = [
        {"nom": nom or "—", "n": n}
        for nom, n in conn.execute(
            "SELECT company, COUNT(*) c FROM offres WHERE company <> '' "
            "GROUP BY company ORDER BY c DESC, company LIMIT 8"
        )
    ]
    dernier = runs[-1] if runs else None
    return {
        "runs": runs,
        "total_offres_connues": total_offres,
        "offres_revues": revues,
        "part_revues": _part(revues, total_offres),
        "combos_ia_cyber": combos,
        "top_entreprises": entreprises,
        "renouvellement": _part(
            dernier["nb_nouvelles"], dernier["nb_offres"]
        ) if dernier else None,
    }


def _lecture(volumes_mesures: list[dict], hist: dict) -> dict:
    """Traduit les chiffres en une lecture qualitative, seuils explicites.

    Assumé : ces seuils sont des REPÈRES de bon sens pour une recherche de stage
    en IdF, pas une vérité statistique. Ils sont écrits ici, en clair, pour être
    discutables et modifiables — plutôt que cachés dans une phrase toute faite.
    """
    # La niche = tous les métiers ROME suivis. La ligne de référence (« tous
    # stages ») est exclue : elle sert de baromètre, pas de mesure de ta niche.
    niche = [v for v in volumes_mesures if v.get("code_rome")]
    reference = next((v for v in volumes_mesures if not v.get("code_rome")), {})
    mesures_niche = [v["ft_court"] for v in niche if v["ft_court"] is not None]
    flux_niche = sum(mesures_niche)
    stock_niche = sum(v["careerjet_total"] or 0 for v in niche)

    if not mesures_niche:
        # AUCUNE mesure disponible (clés absentes, API en panne). On ne conclut
        # PAS : annoncer « marché étroit » sur un flux de 0 non mesuré serait un
        # contresens — l'absence de chiffre n'est pas un chiffre nul.
        tension, verdict = "inconnu", (
            "Volumes non mesurables pour l'instant (France Travail injoignable "
            "ou identifiants absents) : aucune conclusion sur le marché."
        )
    elif flux_niche >= 15:
        tension, verdict = "confortable", "Le marché publie largement sur ta niche."
    elif flux_niche >= 5:
        tension, verdict = "correct", "Flux régulier mais étroit : candidate vite sur chaque offre."
    else:
        tension, verdict = "étroit", (
            "Très peu de nouvelles offres cette semaine sur ta niche : "
            "élargis les termes de recherche ou vise la campagne de septembre."
        )

    rythme_global = _rythme(reference.get("ft_court"), reference.get("ft_long"))
    if rythme_global is None:
        saison = "inconnue"
    elif rythme_global >= 1.15:
        saison = "en accélération"
    elif rythme_global <= 0.85:
        saison = "en ralentissement"
    else:
        saison = "stable"

    # Tension officielle : moyenne des métiers mesurés. Elle regarde le côté
    # DEMANDE (les employeurs peinent-ils à recruter ?), là où tout le reste
    # compte des annonces — c'est donc un signal indépendant, pas une redite.
    tensions = [v["tension"] for v in niche if v.get("tension") is not None]
    tension_moyenne = round(sum(tensions) / len(tensions), 3) if tensions else None
    if tension_moyenne is None:
        lecture_tension = None
    elif tension_moyenne >= 0.05:
        lecture_tension = ("recrutement difficile pour les employeurs — "
                           "favorable au candidat")
    elif tension_moyenne <= -0.05:
        lecture_tension = ("recrutement facile pour les employeurs — "
                           "concurrence forte entre candidats")
    else:
        lecture_tension = "marché équilibré entre employeurs et candidats"

    # Part de ta niche dans le marché du stage francilien : remet le flux en
    # perspective (5 offres, c'est peu dans l'absolu mais beaucoup si le marché
    # entier n'en publie que 30).
    return {
        "tension": tension,
        "verdict": verdict,
        "tension_officielle": tension_moyenne,
        "lecture_tension": lecture_tension,
        "tension_annee": next((v["tension_annee"] for v in niche
                               if v.get("tension_annee")), None),
        "saison": saison,
        "rythme_global": rythme_global,
        "flux_niche_7j": flux_niche,
        "stock_niche": stock_niche,
        "part_niche": _part(flux_niche, reference.get("ft_court")) if mesures_niche else None,
        "renouvellement": hist.get("renouvellement"),
    }


def tableau_de_bord(conn: sqlite3.Connection | None = None) -> dict:
    """Tableau de bord complet : volumes + historique + lecture qualitative."""
    import storage

    fermer = conn is None
    conn = conn or storage.ouvrir(config.CHEMIN_BASE)
    try:
        mesures = volumes()
        hist = historique(conn)
    finally:
        if fermer:
            conn.close()

    return {
        "volumes": mesures,
        "historique": hist,
        "lecture": _lecture(mesures, hist),
        "fenetre_courte": FENETRE_COURTE,
        "fenetre_longue": FENETRE_LONGUE,
    }


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    tb = tableau_de_bord()

    fmt = lambda x: "—" if x is None else str(x)  # noqa: E731
    print(f"\n{'ROME':<6} {'métier':<42} {'7j':>4} {'31j':>5} {'rythme':>7} "
          f"{'ts contrats':>12} {'tension':>8} {'Careerjet':>10}")
    print("-" * 101)
    for v in tb["volumes"]:
        r = _rythme(v["ft_court"], v["ft_long"])
        print(f"{fmt(v['code_rome']):<6} {v['theme'][:42]:<42} {fmt(v['ft_court']):>4} "
              f"{fmt(v['ft_long']):>5} {fmt(r):>7} {fmt(v['ft_tous_contrats']):>12} "
              f"{fmt(v.get('tension')):>8} {fmt(v['careerjet_total']):>10}")

    lec = tb["lecture"]
    print(f"\nMarché : {lec['tension']} · publication {lec['saison']}"
          f" (rythme {lec['rythme_global']})")
    print(f"  {lec['verdict']}")
    if lec.get("lecture_tension"):
        print(f"  Tension officielle {lec['tension_officielle']} "
              f"({lec['tension_annee']}) : {lec['lecture_tension']}")
    h = tb["historique"]
    print(f"\nTa recherche : {h['total_offres_connues']} offre(s) connue(s), "
          f"{h['combos_ia_cyber']} combo(s) IA+cyber, "
          f"{h['part_revues']}% déjà revues, renouvellement du dernier run : "
          f"{h['renouvellement']}%")
    if h["top_entreprises"]:
        print("  entreprises les plus présentes : "
              + ", ".join(f"{e['nom']} ({e['n']})" for e in h["top_entreprises"][:5]))
