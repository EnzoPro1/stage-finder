"""
evaluer_ranking.py — Mesure le ranking sur le corpus étiqueté.

C'est le chiffre contre lequel toute modification du classement sera jugée.
Il n'a de valeur que s'il est REPRODUCTIBLE : deux exécutions consécutives
doivent produire une sortie identique octet pour octet, sinon un écart observé
après un changement ne se distingue pas du bruit.

## D'où vient le déterminisme

Trois précautions, aucune n'est superflue :

1. **Ordre d'entrée trié par clé.** `ranker.composer` trie avec `list.sort`,
   qui est STABLE : à score égal, l'ordre d'arrivée est conservé. Nourrir le
   ranking dans l'ordre d'un `SELECT` sans `ORDER BY`, ou dans l'ordre d'un
   dictionnaire, ferait donc dépendre le départage des ex æquo de SQLite ou du
   hasard de construction. Trié par `cle`, le départage est défini.
2. **Corpus lu depuis `etiquettes.json`**, la source de vérité versionnée, et
   non depuis la table — qui est un cache et peut avoir dérivé.
3. **Aucun horodatage dans la sortie.** Un rapport daté ne se compare pas à
   lui-même.

## Périmètre (décision D1)

On classe **le corpus étiqueté seul**, pas le run complet. Les quatre métriques
portent donc sur un classement dont chaque élément porte une étiquette :
`precision@10` y est interprétable. Le rang de l'offre Colombus DANS LE RUN
RÉEL est rendu à part, en annexe, comme un repère — pas comme une métrique.

## Ce que ce harnais ne mesure PAS

Le verdict LLM (décision D3). Le baseline est le cosinus seul
(`ranker.classer`), strictement déterministe. Au dernier run, 10 offres sur 42
seulement portaient un verdict : un chiffre mêlant les deux mesurerait surtout
qui a eu la chance d'être dans la shortlist.

Utilisation :
    python evaluer_ranking.py                    # rapport texte
    python evaluer_ranking.py --json rapport.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import config
import etiqueter
import reference
import storage
from dedup import _cle as cle_identite
from normalize import Offre

logger = logging.getLogger(__name__)

# Plancher d'exploitabilité du corpus (cf. D4). En deçà, les chiffres sont
# affichés mais explicitement disqualifiés comme baseline.
MIN_ETIQUETTES = 30
MIN_CLASSE_MINORITAIRE = 8


# ---------------------------------------------------------------------------
# Chargement du corpus
# ---------------------------------------------------------------------------
def charger_corpus(conn, chemin_json: str = etiqueter.CHEMIN_JSON) -> dict:
    """Joint les étiquettes (JSON) aux offres et à leur texte (SQLite).

    Rend ``{offres, pertinences, meta, manquantes, cles_incoherentes}`` :

    - ``offres`` : liste d'``Offre``, TRIÉE PAR CLÉ (cf. déterminisme) ;
    - ``pertinences`` : ``cle -> 0.0 | 1.0`` ;
    - ``meta`` : ``cle -> {source, longueur, texte}`` pour la ventilation ;
    - ``manquantes`` : étiquettes dont l'offre ou le texte a disparu de la base ;
    - ``cles_incoherentes`` : lignes dont `dedup._cle` recalculée ne retombe pas
      sur la clé stockée. Ce cas ne devrait pas exister ; s'il existe, il
      invalide l'alignement étiquette ↔ offre et doit être vu, pas absorbé.
    """
    etiquettes = etiqueter.charger_json(chemin_json)
    lignes = {
        l["cle"]: l
        for l in conn.execute(
            """SELECT o.cle, o.title, o.company, o.location, o.url, o.source,
                      o.posted_at, o.salary, t.texte
               FROM offres o JOIN offres_texte t ON t.cle = o.cle"""
        )
    }

    offres, pertinences, meta, manquantes, incoherentes = [], {}, {}, [], []
    for cle in sorted(etiquettes):
        ligne = lignes.get(cle)
        if ligne is None:
            manquantes.append(etiquettes[cle])
            continue
        offre = Offre(
            ligne["title"] or "", ligne["company"] or "", ligne["location"] or "",
            ligne["texte"] or "", ligne["url"] or "", ligne["source"] or "",
            ligne["posted_at"] or "", ligne["salary"] or "",
        )
        if cle_identite(offre) != cle:
            incoherentes.append(cle)
            continue
        offres.append(offre)
        pertinences[cle] = float(etiquettes[cle]["pertinent"])
        meta[cle] = {
            "cle": cle, "source": ligne["source"] or "",
            "texte": ligne["texte"] or "", "title": ligne["title"] or "",
            "company": ligne["company"] or "",
        }
    return {
        "offres": offres, "pertinences": pertinences, "meta": meta,
        "manquantes": manquantes, "cles_incoherentes": incoherentes,
    }


# ---------------------------------------------------------------------------
# Mesure
# ---------------------------------------------------------------------------
def evaluer_corpus(corpus: dict, k: int = 10) -> dict:
    """Classe le corpus et rend les quatre métriques + le classement obtenu.

    Import tardif de `ranker` et `extract` : on ne paie le chargement de torch
    que si on mesure vraiment.
    """
    import evaluation
    import extract
    import ranker

    offres = corpus["offres"]
    if not offres:
        return {"n": 0, "k": k, "classement": []}

    extract.annoter_toutes(offres)
    classees = ranker.classer(offres)

    classement = []
    for rang, (offre, score) in enumerate(classees, 1):
        cle = cle_identite(offre)
        classement.append({
            "rang": rang,
            "cle": cle,
            "score": round(float(score), 6),
            "pertinent": int(corpus["pertinences"][cle]),
            "title": offre.title,
            "company": offre.company,
            "source": offre.source,
            "tags": list(offre.tags),
        })

    rels = [float(item["pertinent"]) for item in classement]
    rang_median = evaluation.rang_median_positives(rels)
    return {
        "n": len(classement),
        "k": k,
        "n_positives": sum(1 for r in rels if r > 0),
        "n_negatives": sum(1 for r in rels if r <= 0),
        "precision@k": round(evaluation.precision_at_k(rels, k), 4),
        "ndcg@k": round(evaluation.ndcg_at_k(rels, k), 4),
        "rang_median_positives": rang_median,
        "negatives_dans_top_k": evaluation.negatives_dans_top_k(rels, k),
        "classement": classement,
    }


def rangs_run_reel(conn, motif: str) -> dict:
    """Rangs dans le RUN RÉEL (toutes offres du dernier run, ordre `dernier_score`).

    Repère hors métrique : c'est la liste que l'utilisateur a effectivement
    sous les yeux, corpus étiqueté ou non. Le tri reprend celui de l'interface
    — score décroissant — avec la clé en départage pour rester déterministe.
    """
    dernier_run, = conn.execute("SELECT MAX(derniere_vue) FROM offres").fetchone()
    lignes = list(conn.execute(
        """SELECT cle, title, company, dernier_score FROM offres
           WHERE derniere_vue = ? ORDER BY dernier_score DESC, cle ASC""",
        (dernier_run,),
    ))
    aiguille = motif.lower()
    trouvees = [
        {"rang": rang, "sur": len(lignes), "title": l["title"],
         "company": l["company"], "cle": l["cle"],
         "score": round(float(l["dernier_score"] or 0.0), 4)}
        for rang, l in enumerate(lignes, 1)
        if aiguille in (l["company"] or "").lower() or aiguille in (l["title"] or "").lower()
    ]
    return {"run": dernier_run, "total": len(lignes), "motif": motif,
            "trouvees": trouvees,
            "rangs_par_cle": {l["cle"]: rang for rang, l in enumerate(lignes, 1)}}


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------
def _ventilation_corpus(corpus: dict) -> dict:
    """Ventilation par source et par population de texte (exigence D1)."""
    offres = [corpus["meta"][c] for c in sorted(corpus["meta"])]
    etiquettes = {c: {"pertinent": int(p)} for c, p in corpus["pertinences"].items()}
    return etiqueter.ventilation(offres, etiquettes)


def construire_rapport(conn, chemin_json: str, k: int, motif_annexe: str,
                       chemin_db: str = "") -> dict:
    corpus = charger_corpus(conn, chemin_json)
    mesures = evaluer_corpus(corpus, k)
    reel = rangs_run_reel(conn, motif_annexe)

    positives_reelles = sorted(
        (reel["rangs_par_cle"][c], corpus["meta"][c]["title"])
        for c, p in corpus["pertinences"].items()
        if p > 0 and c in reel["rangs_par_cle"]
    )
    exploitable = (
        mesures["n"] >= MIN_ETIQUETTES
        and min(mesures.get("n_positives", 0), mesures.get("n_negatives", 0))
        >= MIN_CLASSE_MINORITAIRE
    )
    controle = reference.controler(conn, chemin_db) if chemin_db else None
    return {
        "reference": controle,
        "chemin_db": chemin_db,
        "config": {
            "modele": config.MODELE_EMBEDDING,
            "max_seq_length": config.MAX_SEQ_LENGTH,
            "mode_composition": config.MODE_COMPOSITION_SCORE,
            "normalisation": config.METHODE_NORMALISATION,
            "aggregation_profils": config.AGGREGATION_PROFILS,
            "boosts": {"ia": config.BOOST_IA, "cyber": config.BOOST_CYBER,
                       "combo": config.BOOST_COMBO,
                       "facteur_description": config.BOOST_FACTEUR_DESCRIPTION},
        },
        "corpus": {
            "etiquettes_manquantes": [e["title"] for e in corpus["manquantes"]],
            "cles_incoherentes": corpus["cles_incoherentes"],
            "ventilation": _ventilation_corpus(corpus),
        },
        "mesures": {c: v for c, v in mesures.items() if c != "classement"},
        "classement": mesures.get("classement", []),
        "annexe": {
            "run": reel["run"], "total_run": reel["total"],
            "motif": reel["motif"], "trouvees": reel["trouvees"],
            "rangs_reels_positives": positives_reelles,
        },
        "exploitable": exploitable,
    }


def afficher_rapport(r: dict, k: int) -> None:
    """Rapport texte STABLE : aucun horodatage, aucune durée, arrondis fixes."""
    c, m, v = r["config"], r["mesures"], r["corpus"]["ventilation"]

    print("=" * 78)
    print("ÉVALUATION DU RANKING — corpus étiqueté seul, cosinus seul (sans LLM)")
    print("=" * 78)
    if r.get("reference") is not None:
        reference.afficher_entete(r["reference"], r["chemin_db"])
        print()
    print("--- Réglages mesurés ---")
    print(f"  modèle           : {c['modele']}")
    print(f"  max_seq_length   : {c['max_seq_length']}")
    print(f"  composition      : {c['mode_composition']} / {c['normalisation']}"
          f" / profils={c['aggregation_profils']}")
    print(f"  boosts           : IA={c['boosts']['ia']} cyber={c['boosts']['cyber']}"
          f" combo={c['boosts']['combo']} desc×{c['boosts']['facteur_description']}")

    print(f"\n--- Composition du corpus ({v['n']} offre(s), "
          f"longueur médiane {v['longueur_mediane']} car.) ---")
    for intitule, table in (("par source", v["par_source"]),
                            ("par population de texte", v["par_longueur"])):
        print(f"  {intitule} :")
        for nom, case in table.items():
            print(f"    {nom:34} {case['n']:3}  ({case['oui']} oui / {case['non']} non)")

    if r["corpus"]["etiquettes_manquantes"]:
        print(f"\n  ⚠ {len(r['corpus']['etiquettes_manquantes'])} étiquette(s) sans offre "
              f"ou sans texte en base, exclues du calcul :")
        for titre in r["corpus"]["etiquettes_manquantes"]:
            print(f"      {titre[:70]}")
    if r["corpus"]["cles_incoherentes"]:
        print(f"\n  ⚠ {len(r['corpus']['cles_incoherentes'])} clé(s) incohérente(s) — "
              f"alignement étiquette/offre non garanti, exclues.")

    if not m.get("n"):
        print("\nAucune offre étiquetée exploitable : rien à mesurer.")
        return

    if not r["exploitable"]:
        print("\n" + "!" * 78)
        print("CORPUS INSUFFISANT — ces chiffres ne sont PAS retenus comme baseline.")
        print(f"  requis : >= {MIN_ETIQUETTES} étiquettes et >= {MIN_CLASSE_MINORITAIRE} "
              f"dans la classe minoritaire")
        print(f"  obtenu : {m['n']} étiquettes, "
              f"{min(m['n_positives'], m['n_negatives'])} dans la classe minoritaire")
        print("  élargissement possible : augmenter etiqueter.VIVIER_COURTS /")
        print("  VIVIER_LONGS, ou récupérer du texte pour les 215 offres qui n'en")
        print("  ont pas (seules 111 des 326 offres de la base sont étiquetables).")
        print("!" * 78)

    print(f"\n--- Métriques (k={k}) ---")
    print(f"  precision@{k:<12} {m['precision@k']}")
    print(f"  nDCG@{k:<17} {m['ndcg@k']}")
    rang = m["rang_median_positives"]
    print(f"  rang médian positives  {rang if rang is not None else '—'}"
          f"   (sur {m['n']} offres classées)")
    print(f"  négatives dans top-{k:<3}  {m['negatives_dans_top_k']}")
    print(f"  corpus                 {m['n_positives']} positives / "
          f"{m['n_negatives']} négatives")

    print(f"\n--- Top-{k} du corpus étiqueté ---")
    for item in r["classement"][:k]:
        marque = "✓" if item["pertinent"] else "✗"
        tags = " ".join(item["tags"])
        print(f"  {item['rang']:3}. {marque} {item['score']:.4f}  "
              f"{item['title'][:48]:48} | {item['company'][:20]:20} | {tags}")

    positives_hors = [i for i in r["classement"] if i["pertinent"] and i["rang"] > k]
    if positives_hors:
        print(f"\n--- Positives hors du top-{k} ---")
        for item in positives_hors:
            print(f"  {item['rang']:3}.   {item['score']:.4f}  "
                  f"{item['title'][:48]:48} | {item['company'][:20]}")

    a = r["annexe"]
    print(f"\n--- Annexe (hors métrique) : rangs dans le RUN RÉEL du {a['run']}, "
          f"{a['total_run']} offres ---")
    if a["trouvees"]:
        for t in a["trouvees"]:
            print(f"  « {a['motif']} » : rang {t['rang']}/{t['sur']}  "
                  f"score {t['score']}  {t['title'][:44]} | {t['company'][:22]}")
    else:
        print(f"  aucune offre ne correspond au motif « {a['motif']} » dans ce run.")
    if a["rangs_reels_positives"]:
        rangs = ", ".join(str(rg) for rg, _ in a["rangs_reels_positives"])
        print(f"  rangs réels des positives étiquetées présentes dans ce run : {rangs}")


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    # Les logs partent sur stderr : la comparaison de reproductibilité porte
    # sur stdout, qui ne doit contenir que le rapport.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s",
                        stream=sys.stderr)
    p = argparse.ArgumentParser(description="Évaluation du ranking sur le corpus étiqueté.")
    p.add_argument("--db", default=None,
                   help="Base SQLite (défaut : l'instantané de référence s'il existe).")
    p.add_argument("--etiquettes", default=etiqueter.CHEMIN_JSON,
                   help="Corpus étiqueté (source de vérité).")
    p.add_argument("--k", type=int, default=10, help="Rang de coupure des métriques.")
    p.add_argument("--annexe", default="Colombus",
                   help="Motif d'entreprise/titre suivi dans le run réel.")
    p.add_argument("--json", default=None, dest="sortie_json",
                   help="Écrit aussi le rapport complet en JSON.")
    args = p.parse_args()

    chemin_db = args.db or reference.base_par_defaut()
    conn = storage.ouvrir(chemin_db)
    try:
        rapport = construire_rapport(conn, args.etiquettes, args.k, args.annexe, chemin_db)
    finally:
        conn.close()

    afficher_rapport(rapport, args.k)
    if args.sortie_json:
        with open(args.sortie_json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rapport, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print(f"\nRapport JSON : {args.sortie_json}")

    # Une comparaison invalide doit ÉCHOUER, pas rendre un chiffre plausible.
    if rapport.get("reference", {}) and rapport["reference"]["conforme"] is False:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
