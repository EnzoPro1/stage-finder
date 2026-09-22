"""
Tests de comparer_llm.py. Aucun modèle n'est appelé : le juge, le
déchargement et l'horloge sont injectés, le classement cosinus est figé.
"""

from __future__ import annotations

import pytest

import comparer_llm
import evaluer_ranking
import verifier
from dedup import _cle as cle_identite
from normalize import Offre


def _verdict(score):
    return verifier.Verdict(True, score, False, "stage", "ok")


@pytest.fixture
def corpus(monkeypatch):
    """4 offres ; cosinus A > B > C > D ; seules B et D sont pertinentes."""
    offres = [Offre(t, "ACME", "Paris", "desc", f"http://{t}", "test", "", "")
              for t in ("A", "B", "C", "D")]
    cles = [cle_identite(o) for o in offres]
    cosinus = [{"cle": c, "score": s} for c, s in zip(cles, (0.9, 0.8, 0.7, 0.6))]
    monkeypatch.setattr(evaluer_ranking, "evaluer_corpus",
                        lambda corpus, k: {"classement": cosinus})
    return {
        "offres": offres,
        "pertinences": dict(zip(cles, (0.0, 1.0, 0.0, 1.0))),
        "cles": cles,
    }


class Horloge:
    """Chaque appel au juge « dure » la valeur suivante de ``durees``."""

    def __init__(self, durees):
        self.durees, self.t, self.n = list(durees), 0.0, 0

    def __call__(self):
        # Appelée deux fois par offre : début, puis fin.
        self.n += 1
        if self.n % 2 == 0:
            self.t += self.durees.pop(0)
        return self.t


def _options(scores_par_titre, decharges, durees=(20.0, 1.0, 2.0, 3.0)):
    def juger(offre, reglages=None):
        s = scores_par_titre[offre.title]
        return None if s is None else _verdict(s)
    return {"juger": juger, "decharger": decharges.append, "horloge": Horloge(durees)}


def test_le_llm_qui_trouve_les_positives_gagne(corpus):
    decharges = []
    r = comparer_llm.comparer(
        corpus, ["modele-x"], k=2, top_n=4,
        **_options({"A": 0.1, "B": 0.9, "C": 0.2, "D": 0.8}, decharges))
    m = r["modeles"]["modele-x"]
    assert r["cosinus"]["precision@k"] == 0.5          # A, B
    assert m["llm_seul"]["precision@k"] == 1.0         # B, D
    assert m["llm_seul"]["ndcg@k"] == 1.0


def test_temps_premier_appel_a_part(corpus):
    r = comparer_llm.comparer(
        corpus, ["modele-x"], k=2, top_n=4,
        **_options({"A": 0.1, "B": 0.9, "C": 0.2, "D": 0.8}, []))
    m = r["modeles"]["modele-x"]
    assert m["premier_s"] == 20.0                      # chargement compris
    assert m["moyenne_s"] == 2.0                       # (1 + 2 + 3) / 3


def test_modeles_l_un_apres_l_autre_decharges_avant_et_apres(corpus):
    decharges = []
    opts = _options({"A": 0.1, "B": 0.9, "C": 0.2, "D": 0.8}, decharges,
                    durees=[1.0] * 8)
    comparer_llm.comparer(corpus, ["m1", "m2"], k=2, top_n=4, **opts)
    assert decharges == ["m1", "m1", "m2", "m2"]


def test_echec_compte_et_ne_gagne_rien(corpus):
    r = comparer_llm.comparer(
        corpus, ["modele-x"], k=2, top_n=4,
        **_options({"A": None, "B": 0.9, "C": 0.2, "D": 0.8}, []))
    m = r["modeles"]["modele-x"]
    assert m["echecs"] == 1
    assert r["offres"][corpus["cles"][0]]["modele-x"] is None


def test_final_top_n_ne_juge_que_la_shortlist(corpus):
    cosinus = evaluer_ranking.evaluer_corpus(None, 2)["classement"]
    cles = corpus["cles"]
    verdicts = {c: _verdict(s) for c, s in zip(cles, (0.0, 0.0, 0.0, 1.0))}
    ordres = comparer_llm.classements(cosinus, verdicts, top_n=2)
    a, b, c, d = cles
    # Top 2 = A, B : jugés à 0, ils tombent à 0,45 / 0,40. C et D, hors
    # shortlist, gardent leur cosinus (0,70 / 0,60) — le 1.0 de D est ignoré.
    assert ordres["final_top_n"] == [c, d, a, b]
    # Toutes jugées : D monte à 0,80, C tombe à 0,35.
    assert ordres["final"] == [d, a, b, c]


def test_departage_stable_par_le_cosinus(corpus):
    cosinus = evaluer_ranking.evaluer_corpus(None, 2)["classement"]
    verdicts = {c: _verdict(0.5) for c in corpus["cles"]}
    assert comparer_llm.classements(cosinus, verdicts, 4)["llm_seul"] == corpus["cles"]


def test_rapport_porte_verdicts_et_etiquettes(corpus):
    r = comparer_llm.comparer(
        corpus, ["modele-x"], k=2, top_n=4,
        **_options({"A": 0.1, "B": 0.9, "C": 0.2, "D": 0.8}, []))
    b = r["offres"][corpus["cles"][1]]
    assert b["pertinent"] == 1 and b["rang_cosinus"] == 2
    assert b["modele-x"]["score"] == 0.9


def test_n_utilise_jamais_la_base_en_ecriture(monkeypatch):
    """La porte d'entrée ouvre l'instantané en lecture seule, jamais via ouvrir()."""
    import storage

    def interdit(*a, **k):
        raise AssertionError("storage.ouvrir écrit dans la base : interdit ici")
    monkeypatch.setattr(storage, "ouvrir", interdit)
    ouvertures = []

    class Arret(Exception):
        pass

    def lecture_seule(chemin):
        ouvertures.append(chemin)
        raise Arret
    monkeypatch.setattr(storage, "ouvrir_lecture_seule", lecture_seule)
    monkeypatch.setattr(comparer_llm.llm, "modeles_manquants", lambda m: [])
    monkeypatch.setattr("sys.argv", ["comparer_llm.py", "--db", "instantane.db"])
    with pytest.raises(Arret):
        comparer_llm.main()
    assert ouvertures == ["instantane.db"]
