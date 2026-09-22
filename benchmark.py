"""
benchmark.py — Compare plusieurs modèles d'embedding sur le jeu labellisé.

`MiniLM-L12-v2` est un bon compromis mais daté (fenêtre courte, ~128 tokens). Ce
script permet de mesurer objectivement l'apport d'un modèle plus récent —
`intfloat/multilingual-e5-large`, `BAAI/bge-m3` (contexte 8k) — au lieu de s'en
remettre à un « gagnant » figé. On réutilise le harnais d'évaluation
(precision@k / nDCG) : la source de vérité au moment du test reste le
leaderboard MTEB (onglets français / retrieval), ce script sert à confirmer sur
TES données.

⚠️ Chaque modèle non déjà en cache est TÉLÉCHARGÉ (plusieurs centaines de Mo à
plusieurs Go pour BGE-m3). Surveille RAM / latence en local.

Le jeu de référence est le CORPUS ÉTIQUETÉ (`etiquettes.json` + `stages.db`),
pas un fichier d'offres inventées : comparer deux modèles sur des annonces
fabriquées ne dit rien de leur comportement sur les vraies.

Utilisation :
    python benchmark.py                       # modèles par défaut
    python benchmark.py --k 10 \
        --modeles paraphrase-multilingual-MiniLM-L12-v2 intfloat/multilingual-e5-large
    python benchmark.py --modeles paraphrase-multilingual-MiniLM-L12-v2 ollama:bge-m3
"""

from __future__ import annotations

import argparse
import logging
import time

import config
import etiqueter
import evaluer_ranking
import reference

logger = logging.getLogger(__name__)

# Modèles candidats par défaut. On garde MiniLM comme référence de base.
MODELES_DEFAUT = [
    "paraphrase-multilingual-MiniLM-L12-v2",
    "intfloat/multilingual-e5-large",
    # "BAAI/bge-m3",  # décommenter si tu acceptes le téléchargement (~2 Go) et la RAM.
    # "ollama:bge-m3",  # bge-m3 servi par Ollama (GPU), vecteurs mis en cache.
]


def _reinitialiser_modele() -> None:
    """Force le rechargement du modèle au prochain encodage."""
    import ranker
    ranker._modele = None


def comparer(modeles: list[str], corpus: dict, k: int) -> list[dict]:
    """Évalue chaque modèle sur le MÊME corpus déjà chargé, et rend les résultats."""
    import ranker

    resultats = []
    modele_initial = config.MODELE_EMBEDDING
    try:
        for nom in modeles:
            config.MODELE_EMBEDDING = nom
            _reinitialiser_modele()
            debut = time.perf_counter()
            try:
                mesures = evaluer_ranking.evaluer_corpus(corpus, k)
            except Exception as err:  # noqa: BLE001 - un modèle KO ne stoppe pas le reste
                logger.warning("Modèle « %s » en échec : %s", nom, err)
                continue
            mesures.pop("classement", None)
            mesures["modele"] = nom
            mesures["secondes"] = round(time.perf_counter() - debut, 1)
            resultats.append(mesures)
            ranker.liberer_modele(nom)  # rend la VRAM avant le modèle suivant
    finally:
        config.MODELE_EMBEDDING = modele_initial
        _reinitialiser_modele()
    return resultats


def main() -> None:
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Benchmark de modèles d'embedding.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--etiquettes", default=etiqueter.CHEMIN_JSON)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--modeles", nargs="+", default=MODELES_DEFAUT)
    args = parser.parse_args()

    chemin_db = args.db or reference.base_par_defaut()
    # Lecture seule : `storage.ouvrir` écrirait dans l'instantané (WAL, schéma).
    conn = reference.ouvrir_pour_mesure(chemin_db)
    try:
        corpus = evaluer_ranking.charger_corpus(conn, args.etiquettes)
    finally:
        conn.close()
    if not corpus["offres"]:
        print("Corpus étiqueté vide : lance d'abord `python etiqueter.py`.")
        return

    resultats = comparer(args.modeles, corpus, args.k)

    print(f"\n=== Benchmark ({len(resultats)} modèle(s), k={args.k}, "
          f"{len(corpus['offres'])} offres étiquetées) ===\n")
    print(f"{'modèle':<44} {'prec@k':>8} {'nDCG@k':>8} {'rang méd.':>10} {'temps(s)':>9}")
    print("-" * 84)
    for r in sorted(resultats, key=lambda x: x["ndcg@k"], reverse=True):
        print(f"{r['modele']:<44} {r['precision@k']:>8} {r['ndcg@k']:>8} "
              f"{str(r['rang_median_positives']):>10} {r['secondes']:>9}")


if __name__ == "__main__":
    main()
