"""Tests du harnais d'évaluation : chargement, déterminisme, métriques."""

from __future__ import annotations

import json

import numpy as np
import pytest

import etiqueter
import evaluer_ranking
import jobs
import ranker
import storage
from dedup import _cle as cle_identite
from normalize import Offre


def _inserer(conn, title, company, location="Paris", source="careerjet",
             score=0.5, texte="Vos missions : développer.", vue="2026-08-08"):
    """Insère une offre en calculant sa clé comme le fait le pipeline."""
    offre = Offre(title, company, location, texte, f"http://x/{title}", source, "", "")
    cle = cle_identite(offre)
    conn.execute(
        """INSERT INTO offres (cle,title,company,location,url,source,posted_at,salary,
                               duree_mois,date_debut,tags,dernier_score,
                               premiere_vue,derniere_vue,nb_vues)
           VALUES (?,?,?,?,?,?,'','',NULL,'','',?,?,?,1)""",
        (cle, title, company, location, offre.url, source, score, vue, vue),
    )
    jobs.enregistrer_texte(conn, cle, texte)
    conn.commit()
    return cle


@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    jobs.ensure_schema(c)
    etiqueter.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def sims_figees(monkeypatch):
    """Remplace le modèle par des similarités décidées par le test.

    Le harnais doit être testable sans charger torch : on court-circuite les
    deux seules fonctions de `ranker` qui touchent au modèle.
    """
    etat = {"sims": None}

    def _encoder(offres, modele=None):
        return np.zeros((len(offres), 2))

    def _similarites(emb, modele=None):
        return np.asarray(etat["sims"][: len(emb)], dtype=float)

    monkeypatch.setattr(ranker, "encoder_offres", _encoder)
    monkeypatch.setattr(ranker, "similarites_reference", _similarites)
    return etat


def _corpus(conn, chemin, etiquettes: dict[str, int]):
    """Écrit un etiquettes.json et rend le corpus chargé."""
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(
            [{"cle": c, "pertinent": p, "title": "", "company": "",
              "url": "", "etiquete_le": ""} for c, p in sorted(etiquettes.items())],
            f,
        )
    return evaluer_ranking.charger_corpus(conn, chemin)


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------
def test_charger_corpus_joint_les_etiquettes_aux_offres(conn, tmp_path):
    a = _inserer(conn, "Ingénieur IA", "Colombus")
    b = _inserer(conn, "Chef de projet", "Ornikar")
    corpus = _corpus(conn, str(tmp_path / "e.json"), {a: 1, b: 0})

    assert len(corpus["offres"]) == 2
    assert corpus["pertinences"] == {a: 1.0, b: 0.0}
    assert corpus["manquantes"] == [] and corpus["cles_incoherentes"] == []


def test_charger_corpus_trie_par_cle(conn, tmp_path):
    """L'ordre d'entrée fixe le départage des ex æquo : il ne peut pas flotter."""
    cles = [_inserer(conn, f"Offre {i}", "ACME") for i in range(5)]
    corpus = _corpus(conn, str(tmp_path / "e.json"), {c: 1 for c in cles})
    obtenues = [cle_identite(o) for o in corpus["offres"]]
    assert obtenues == sorted(obtenues)


def test_charger_corpus_signale_une_etiquette_sans_offre(conn, tmp_path):
    a = _inserer(conn, "Ingénieur IA", "Colombus")
    chemin = str(tmp_path / "e.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump([
            {"cle": a, "pertinent": 1, "title": "Ingénieur IA", "company": "",
             "url": "", "etiquete_le": ""},
            {"cle": "disparue", "pertinent": 0, "title": "Offre effacée",
             "company": "", "url": "", "etiquete_le": ""},
        ], f)
    corpus = evaluer_ranking.charger_corpus(conn, chemin)
    assert len(corpus["offres"]) == 1
    assert [e["title"] for e in corpus["manquantes"]] == ["Offre effacée"]


def test_charger_corpus_exclut_une_offre_sans_texte(conn, tmp_path):
    a = _inserer(conn, "Ingénieur IA", "Colombus")
    conn.execute("DELETE FROM offres_texte WHERE cle = ?", (a,))
    conn.commit()
    corpus = _corpus(conn, str(tmp_path / "e.json"), {a: 1})
    assert corpus["offres"] == [] and len(corpus["manquantes"]) == 1


# ---------------------------------------------------------------------------
# Déterminisme
# ---------------------------------------------------------------------------
def test_ex_aequo_departages_par_la_cle_et_pas_par_le_hasard(conn, tmp_path, sims_figees):
    cles = sorted(_inserer(conn, f"Offre {i}", "ACME", texte="idem") for i in range(4))
    corpus = _corpus(conn, str(tmp_path / "e.json"), {c: 1 for c in cles})
    sims_figees["sims"] = [0.5, 0.5, 0.5, 0.5]  # tous ex æquo

    premier = evaluer_ranking.evaluer_corpus(corpus, 10)
    second = evaluer_ranking.evaluer_corpus(corpus, 10)
    ordre = [i["cle"] for i in premier["classement"]]
    assert ordre == [i["cle"] for i in second["classement"]]
    assert ordre == cles, "à score égal, l'ordre doit suivre la clé"


def test_deux_evaluations_donnent_le_meme_rapport(conn, tmp_path, sims_figees):
    a = _inserer(conn, "Ingénieur IA", "Colombus", score=0.36)
    b = _inserer(conn, "Chef de projet digital", "Ornikar", score=0.81)
    chemin = str(tmp_path / "e.json")
    _corpus(conn, chemin, {a: 1, b: 0})
    sims_figees["sims"] = [0.9, 0.2] if a < b else [0.2, 0.9]

    r1 = evaluer_ranking.construire_rapport(conn, chemin, 10, "Colombus")
    r2 = evaluer_ranking.construire_rapport(conn, chemin, 10, "Colombus")
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def test_metriques_sur_un_classement_connu(conn, tmp_path, sims_figees):
    positive = _inserer(conn, "Ingénieur IA", "Colombus")
    negatives = [_inserer(conn, f"Chef de projet {i}", "Ornikar") for i in range(3)]
    corpus = _corpus(conn, str(tmp_path / "e.json"),
                     {positive: 1, **{c: 0 for c in negatives}})

    # La positive est mise DERNIÈRE : c'est le symptôme qu'on veut voir chiffré.
    ordre = [cle_identite(o) for o in corpus["offres"]]
    sims_figees["sims"] = [0.1 if c == positive else 0.9 for c in ordre]

    m = evaluer_ranking.evaluer_corpus(corpus, 3)
    assert m["classement"][-1]["cle"] == positive
    assert m["precision@k"] == 0.0
    assert m["ndcg@k"] == 0.0
    assert m["rang_median_positives"] == 4
    assert m["negatives_dans_top_k"] == 3


def test_corpus_vide_ne_leve_pas(conn, tmp_path):
    corpus = _corpus(conn, str(tmp_path / "e.json"), {})
    assert evaluer_ranking.evaluer_corpus(corpus, 10)["n"] == 0


# ---------------------------------------------------------------------------
# Annexe : rangs dans le run réel
# ---------------------------------------------------------------------------
def test_rangs_run_reel_suit_le_score_decroissant(conn):
    _inserer(conn, "Ingénieur IA", "Colombus Consulting", score=0.36)
    _inserer(conn, "Chef de projet", "Ornikar", score=0.81)
    _inserer(conn, "Agentic AI engineer", "CybelAngel", score=1.25)

    reel = evaluer_ranking.rangs_run_reel(conn, "Colombus")
    assert reel["total"] == 3
    assert [t["rang"] for t in reel["trouvees"]] == [3]
    assert reel["trouvees"][0]["company"] == "Colombus Consulting"


def test_rangs_run_reel_ignore_les_runs_precedents(conn):
    _inserer(conn, "Vieille offre", "Colombus", score=1.0, vue="2026-07-01")
    _inserer(conn, "Ingénieur IA", "Colombus", score=0.3, vue="2026-08-08")
    reel = evaluer_ranking.rangs_run_reel(conn, "Colombus")
    assert reel["run"] == "2026-08-08" and reel["total"] == 1


def test_motif_absent_ne_leve_pas(conn):
    _inserer(conn, "Chef de projet", "Ornikar")
    assert evaluer_ranking.rangs_run_reel(conn, "Colombus")["trouvees"] == []


# ---------------------------------------------------------------------------
# Garde-fou du plancher
# ---------------------------------------------------------------------------
def test_corpus_trop_maigre_est_marque_non_exploitable(conn, tmp_path, sims_figees):
    cles = [_inserer(conn, f"Offre {i}", "ACME") for i in range(4)]
    chemin = str(tmp_path / "e.json")
    _corpus(conn, chemin, {c: (1 if i < 2 else 0) for i, c in enumerate(cles)})
    sims_figees["sims"] = [0.9, 0.7, 0.5, 0.3]

    rapport = evaluer_ranking.construire_rapport(conn, chemin, 10, "Colombus")
    assert rapport["exploitable"] is False


def test_rapport_porte_la_ventilation_par_source_et_longueur(conn, tmp_path, sims_figees):
    a = _inserer(conn, "Ingénieur IA", "Colombus", source="jobspy:linkedin",
                 texte="Vos missions : " + "z" * 3000)
    b = _inserer(conn, "Chef de projet", "Ornikar", source="careerjet", texte="court")
    chemin = str(tmp_path / "e.json")
    _corpus(conn, chemin, {a: 1, b: 0})
    sims_figees["sims"] = [0.9, 0.1]

    v = evaluer_ranking.construire_rapport(conn, chemin, 10, "x")["corpus"]["ventilation"]
    assert set(v["par_source"]) == {"jobspy:linkedin", "careerjet"}
    assert len(v["par_longueur"]) == 2
    assert sum(c["oui"] for c in v["par_source"].values()) == 1
