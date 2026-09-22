"""
comparer_llm.py — Compare des modèles de vérification LLM sur le corpus étiqueté.

`evaluer_ranking.py` mesure le cosinus seul, et exclut le LLM exprès (décision
D3) : en run réel, seule la shortlist porte un verdict, et un chiffre mêlant
les deux mesurerait surtout qui a eu la chance d'y être. Ce script-ci mesure
le LLM LUI-MÊME, en faisant juger TOUTES les offres étiquetées par chaque
modèle, et rend trois classements par modèle :

- **llm seul** : tri par score LLM (départage : rang cosinus, pour que deux
  exécutions rendent le même ordre) ;
- **final** : ``verifier.score_final`` sur toutes les offres — cosinus et LLM
  combinés, comme dans l'app ;
- **final (top N)** : comme en run réel — seules les N premières du cosinus
  reçoivent le verdict, les autres gardent leur cosinus.

## Garanties

- **Instantané en lecture seule** (``reference.ouvrir_pour_mesure``) : aucun
  octet écrit, aucune étiquette touchée.
- **Aucun cache de verdicts** : chaque offre est réellement soumise au modèle.
  Le cache SQLite rendrait un temps nul et, surtout, des verdicts produits par
  une version antérieure des règles.
- **Modèles l'un après l'autre**, déchargés entre deux : jamais deux modèles
  en VRAM à la fois.
- **Temps** : le modèle est déchargé AVANT le premier appel, qui porte donc le
  chargement ; il est rendu à part. La moyenne par offre porte sur les
  suivantes, modèle chaud.

## Ce que ce script ne dit pas

Le classement cosinus est celui du modèle d'embedding COURANT
(``config.MODELE_EMBEDDING``) : changer ce défaut change le « final ». Et 57
offres, dont 20 positives, ne séparent que des écarts nets — une différence
d'une offre dans le top 10 vaut 0,1 de précision.

## Re-pondérer sans relancer le LLM

Le rapport JSON garde, par offre, le score cosinus et le score LLM. ``--depuis``
recalcule le « final » pour d'autres poids du LLM (``verifier.score_final`` :
``(1 - poids) · cosinus + poids · LLM``) à partir de ces seuls chiffres, en une
seconde, sans rappeler aucun modèle.

Utilisation :
    python comparer_llm.py                           # qwen3:4b puis gemma3:4b
    python comparer_llm.py --modeles qwen3:4b --json comparaison.json
    python comparer_llm.py --modeles qwen3:4b --modele-embedding ollama:bge-m3
    python comparer_llm.py --depuis comparaison.json --poids 0.5 0.3 0.2
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable

import config
import etiqueter
import evaluation
import evaluer_ranking
import llm
import ollama_pool
import ranker
import reference
import verifier
from dedup import _cle as cle_identite

logger = logging.getLogger(__name__)

MODELES_DEFAUT = ["qwen3:4b", "gemma3:4b"]


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def _mesurer(ordre: list[str], pertinences: dict[str, float], k: int) -> dict:
    rels = [pertinences[c] for c in ordre]
    return {
        "precision@k": round(evaluation.precision_at_k(rels, k), 4),
        "ndcg@k": round(evaluation.ndcg_at_k(rels, k), 4),
        "rang_median_positives": evaluation.rang_median_positives(rels),
    }


def classements(cosinus: list[dict], verdicts: dict[str, verifier.Verdict | None],
                top_n: int, poids: float | None = None) -> dict[str, list[str]]:
    """Les trois ordres (clés) dérivés du cosinus et des verdicts.

    ``cosinus`` : le classement de ``evaluer_ranking.evaluer_corpus``, trié.
    Un verdict manquant (échec du modèle) compte 0 en « llm seul » — l'offre
    n'est pas gagnée par défaut — et laisse le cosinus en « final ».
    ``poids`` : poids du LLM dans le final (défaut : ``config.VERIFY_SCORE_WEIGHT``).
    """
    rang_cos = {item["cle"]: i for i, item in enumerate(cosinus)}
    score_cos = {item["cle"]: item["score"] for item in cosinus}

    def llm_seul(cle):
        v = verdicts.get(cle)
        return v.score if v is not None else 0.0

    def final(cle, avec_verdict=True):
        v = verdicts.get(cle) if avec_verdict else None
        return verifier.score_final(score_cos[cle], v, poids)

    cles = list(rang_cos)
    shortlist = set(cles[:top_n])
    return {
        "cosinus": cles,
        "llm_seul": sorted(cles, key=lambda c: (-llm_seul(c), rang_cos[c])),
        "final": sorted(cles, key=lambda c: (-final(c), rang_cos[c])),
        "final_top_n": sorted(cles, key=lambda c: (-final(c, c in shortlist), rang_cos[c])),
    }


# ---------------------------------------------------------------------------
# Un modèle
# ---------------------------------------------------------------------------
def juger_corpus(
    offres: dict[str, object],
    ordre: list[str],
    modele: str,
    *,
    juger: Callable = verifier.verifier,
    decharger: Callable = ollama_pool.decharger,
    horloge: Callable[[], float] = time.perf_counter,
) -> dict:
    """Fait juger chaque offre par ``modele``, dans l'ordre cosinus.

    Rend ``{verdicts, secondes, premier_s, moyenne_s, echecs}``. Le modèle est
    déchargé avant (le premier appel porte le chargement) et après (place nette
    pour le suivant).
    """
    reglages = llm.charger_reglages().model_copy(update={"modele": modele})
    decharger(modele)
    verdicts: dict[str, verifier.Verdict | None] = {}
    durees: list[float] = []
    for i, cle in enumerate(ordre, 1):
        debut = horloge()
        verdicts[cle] = juger(offres[cle], reglages=reglages)
        durees.append(horloge() - debut)
        print(f"\r  {modele} : {i}/{len(ordre)}", end="", flush=True)
    print()
    decharger(modele)

    suivantes = durees[1:]
    return {
        "verdicts": verdicts,
        "secondes": {c: round(d, 2) for c, d in zip(ordre, durees)},
        "premier_s": round(durees[0], 1) if durees else None,
        "moyenne_s": round(sum(suivantes) / len(suivantes), 2) if suivantes else None,
        "echecs": sum(1 for v in verdicts.values() if v is None),
    }


# ---------------------------------------------------------------------------
# Comparaison
# ---------------------------------------------------------------------------
def comparer(corpus: dict, modeles: list[str], k: int = 10, top_n: int | None = None,
             **options) -> dict:
    """Classement cosinus du corpus, puis chaque modèle l'un après l'autre."""
    top_n = top_n or llm.charger_reglages().top_n
    cosinus = evaluer_ranking.evaluer_corpus(corpus, k)["classement"]
    # Enchaînement VRAM, comme en run réel : le modèle d'embeddings (s'il est
    # servi par Ollama) quitte le GPU avant que le LLM n'y soit chargé.
    ranker.liberer_modele()
    offres = {cle_identite(o): o for o in corpus["offres"]}
    ordre = [item["cle"] for item in cosinus]
    pertinences = corpus["pertinences"]

    rapport = {
        "k": k, "top_n": top_n, "n": len(ordre),
        "poids_llm": config.VERIFY_SCORE_WEIGHT,
        "n_positives": sum(1 for c in ordre if pertinences[c] > 0),
        "modele_embedding": config.MODELE_EMBEDDING,
        "cosinus": _mesurer(ordre, pertinences, k),
        "modeles": {},
        "offres": {c: {"title": offres[c].title, "company": offres[c].company,
                       "pertinent": int(pertinences[c]), "rang_cosinus": i,
                       "score_cosinus": item["score"]}
                   for i, (c, item) in enumerate(zip(ordre, cosinus), 1)},
    }
    for modele in modeles:
        jugement = juger_corpus(offres, ordre, modele, **options)
        ordres = classements(cosinus, jugement["verdicts"], top_n)
        rapport["modeles"][modele] = {
            "llm_seul": _mesurer(ordres["llm_seul"], pertinences, k),
            "final": _mesurer(ordres["final"], pertinences, k),
            "final_top_n": _mesurer(ordres["final_top_n"], pertinences, k),
            "premier_s": jugement["premier_s"],
            "moyenne_s": jugement["moyenne_s"],
            "echecs": jugement["echecs"],
        }
        for cle, v in jugement["verdicts"].items():
            rapport["offres"][cle][modele] = None if v is None else {
                "score": v.score, "domaine_match": v.domaine_match,
                "est_alternance": v.est_alternance, "niveau": v.niveau,
                "justification": v.justification,
            }
    return rapport


def reponderer(rapport: dict, modele: str, poids: list[float]) -> dict[float, dict]:
    """Métriques du final pour chaque poids du LLM, depuis un rapport existant.

    N'utilise que ``score_cosinus``, le score LLM et l'étiquette de chaque
    offre : aucun modèle n'est rappelé. Rend ``poids -> {final, final_top_n}``.
    """
    k, top_n = rapport["k"], rapport["top_n"]
    offres = sorted(rapport["offres"].items(), key=lambda kv: kv[1]["rang_cosinus"])
    cosinus = [{"cle": c, "score": o["score_cosinus"]} for c, o in offres]
    pertinences = {c: float(o["pertinent"]) for c, o in offres}
    verdicts = {
        c: None if o.get(modele) is None
        else verifier.Verdict(True, o[modele]["score"], False, "stage", "")
        for c, o in offres
    }
    resultats = {}
    for p in poids:
        ordres = classements(cosinus, verdicts, top_n, poids=p)
        resultats[p] = {"final": _mesurer(ordres["final"], pertinences, k),
                        "final_top_n": _mesurer(ordres["final_top_n"], pertinences, k)}
    return resultats


def afficher(r: dict) -> None:
    k, n = r["k"], r["top_n"]
    print(f"\n=== Vérification LLM — {r['n']} offres étiquetées ({r['n_positives']} "
          f"positives), k={k}, embeddings {r['modele_embedding']} ===\n")
    print(f"{'classement':<28} {'P@k':>6} {'nDCG@k':>8} {'rang méd.':>10}"
          f" {'1er appel':>10} {'moy./offre':>11} {'échecs':>7}")
    print("-" * 86)
    c = r["cosinus"]
    print(f"{'cosinus seul':<28} {c['precision@k']:>6} {c['ndcg@k']:>8} "
          f"{str(c['rang_median_positives']):>10}")
    for modele, m in r["modeles"].items():
        for cle, libelle in (("llm_seul", "LLM seul"), ("final", "final"),
                             ("final_top_n", f"final (top {n})")):
            x = m[cle]
            temps = (f" {m['premier_s']:>9}s {m['moyenne_s']:>10}s {m['echecs']:>7}"
                     if cle == "llm_seul" else "")
            print(f"{modele + ' · ' + libelle:<28} {x['precision@k']:>6} {x['ndcg@k']:>8} "
                  f"{str(x['rang_median_positives']):>10}{temps}")


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Compare des modèles de vérification LLM.")
    parser.add_argument("--db", default=None, help="Défaut : l'instantané de référence.")
    parser.add_argument("--etiquettes", default=etiqueter.CHEMIN_JSON)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--modeles", nargs="+", default=MODELES_DEFAUT)
    parser.add_argument("--json", default=None, help="Écrit le rapport complet (verdicts compris).")
    parser.add_argument("--modele-embedding", default=None,
                        help=f"Modèle du classement cosinus (défaut : {config.MODELE_EMBEDDING}). "
                             f"Aucun repli ici : une mesure qui échoue doit échouer.")
    parser.add_argument("--depuis", default=None,
                        help="Rapport JSON existant : re-pondère sans rappeler le LLM.")
    parser.add_argument("--poids", nargs="+", type=float, default=[0.5, 0.3, 0.2],
                        help="Poids du LLM dans le final, avec --depuis.")
    args = parser.parse_args()

    if args.depuis:
        with open(args.depuis, encoding="utf-8") as f:
            rapport = json.load(f)
        c = rapport["cosinus"]
        print(f"\n=== Re-pondération (sans LLM) — embeddings {rapport['modele_embedding']}, "
              f"k={rapport['k']}, top {rapport['top_n']} ===\n")
        print(f"{'classement':<34} {'P@k':>6} {'nDCG@k':>8} {'rang méd.':>10}")
        print("-" * 62)
        print(f"{'cosinus seul':<34} {c['precision@k']:>6} {c['ndcg@k']:>8} "
              f"{str(c['rang_median_positives']):>10}")
        for modele in rapport["modeles"]:
            for p, m in reponderer(rapport, modele, args.poids).items():
                for cle, libelle in (("final", "final"), ("final_top_n", f"top {rapport['top_n']}")):
                    x = m[cle]
                    nom = f"{modele} · {libelle} · {1 - p:.1f}/{p:.1f}"
                    print(f"{nom:<34} {x['precision@k']:>6} {x['ndcg@k']:>8} "
                          f"{str(x['rang_median_positives']):>10}")
        return

    if args.modele_embedding:
        config.MODELE_EMBEDDING = args.modele_embedding

    manquants = llm.modeles_manquants(args.modeles)
    if manquants:
        raise SystemExit(llm.message_modeles_manquants(manquants))

    chemin_db = args.db or reference.base_par_defaut()
    conn = reference.ouvrir_pour_mesure(chemin_db)
    try:
        controle = reference.controler(conn, chemin_db)
        corpus = evaluer_ranking.charger_corpus(conn, args.etiquettes)
    finally:
        conn.close()
    if not controle["donnees"]["conforme"]:
        print("⚠ DONNÉES NON CONFORMES à l'instantané de référence :")
        for alerte in controle["donnees"]["alertes"]:
            print(f"  · {alerte}")
    if corpus["manquantes"] or corpus["cles_incoherentes"]:
        raise SystemExit(f"Corpus incomplet : {len(corpus['manquantes'])} étiquette(s) sans "
                         f"texte, {len(corpus['cles_incoherentes'])} clé(s) incohérente(s).")

    rapport = comparer(corpus, args.modeles, args.k, args.top_n)
    rapport["base"] = chemin_db
    afficher(rapport)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False, indent=2)
        print(f"\nRapport complet : {args.json}")


if __name__ == "__main__":
    main()
