"""Le rapport unifié : en-tête par run, orphelins écartés, liens, nouveautés, trajets.

Les bases sont construites par `storage.enregistrer_run`, comme en vrai : le
rapport ne lit que ce que les runs écrivent.
"""

from __future__ import annotations

import os
import re

import pytest

import rapport
import storage
from normalize import Lien, Offre


def _offre(titre, liens, familles=(), familles_titre=(), lieu="Paris", trajets=None,
           drapeaux=()):
    o = Offre(titre, "ACME", lieu, "desc", liens[0][2], liens[0][0], "2026-09-17", "")
    o.liens = [Lien(s, c, u) for s, c, u in liens]
    o.familles, o.familles_titre = list(familles), list(familles_titre)
    o.trajets = trajets or {}
    o.drapeaux = list(drapeaux)
    return o


def _bilan(sources=(), familles=(), ecartees=None, conseils=None):
    return {
        "perimetre": "stages", "ecartees": ecartees or {}, "conseils": conseils or {},
        "sources": [{"nom": n, "brut": b, "normalisees": b, "survivantes": b, "fusions": 0,
                     "retenues": b, "rejets": {}, "incidents": []} for n, b in sources],
        "familles": [{"nom": f, "brut": b, "survivantes": b} for f, b in familles],
        "alertes": [],
    }


def _run(chemin, offres, bilan=None):
    conn = storage.ouvrir(str(chemin))
    storage.enregistrer_run(conn, [(o, 0.5) for o in offres], bilan=bilan)
    conn.close()


def _lignes_du_tableau(page: str, table_id: str) -> list[str]:
    corps = re.search(rf'<table id="{table_id}">.*?<tbody>(.*?)</tbody>', page, re.S).group(1)
    return re.findall(r"<tr.*?</tr>", corps, re.S)


# ---------------------------------------------------------------------------
# Orphelins
# ---------------------------------------------------------------------------
def test_apres_un_changement_de_titre_seule_la_ligne_rattachee_est_montree(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Stage ML", [("adzuna", "id:1", "https://a/1?se=x")])])
    _run(base, [_offre("Stage ML - Janvier 2027", [("adzuna", "id:1", "https://a/1?se=y")])])

    conn = storage.ouvrir(str(base))
    assert conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0] == 2, \
        "l'orphelin reste en base (génération de CV)"
    conn.close()
    section = rapport.charger_section("Stages", base)
    assert [o["title"] for o in section.offres] == ["Stage ML - Janvier 2027"]


def test_une_ligne_du_run_sans_aucun_lien_n_est_pas_montree(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Avec lien", [("adzuna", "id:1", "u")])])
    conn = storage.ouvrir(str(base))
    (run_id,) = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    conn.execute("""INSERT INTO offres (cle, title, company, location, url, source,
                    dernier_score, premiere_vue, derniere_vue, dernier_run, nb_vues)
                    VALUES ('orpheline', 'Sans lien', 'X', 'Paris', 'u', 'adzuna', 0.9,
                    '2026-09-17', '2026-09-17', ?, 1)""", (run_id,))
    conn.commit()
    conn.close()
    titres = [o["title"] for o in rapport.charger_section("Stages", base).offres]
    assert titres == ["Avec lien"]


# ---------------------------------------------------------------------------
# En-tête : sources muettes, écartées, conseils, comptes
# ---------------------------------------------------------------------------
def test_l_en_tete_signale_une_source_qui_n_a_rien_rendu(tmp_path):
    base = tmp_path / "stages.db"
    bilan = _bilan(sources=[("careerjet", 30), ("jobspy:indeed", 0)], familles=[("ml", 30)],
                   ecartees={"jobspy:google": "page JavaScript"},
                   conseils={"jobspy:linkedin": "« jobspy:linkedin » a été BLOQUÉ (429)"})
    _run(base, [_offre("Stage NLP", [("careerjet", "contenu:a", "u")], ["ml"], ["ml"])], bilan)
    page = rapport.rendre(rapport.charger_section("Stages", base),
                          rapport.Section("Jobs étudiants", None), {})

    assert re.search(r"MUETTE.*jobspy:indeed", page)
    assert "careerjet" not in re.search(r"MUETTE[^<]*(<strong>[^<]*</strong>[^<]*)*", page).group(0)
    assert "jobspy:google</strong> désactivée : page JavaScript" in page
    assert "BLOQUÉ (429)" in page
    assert re.search(r'Par source</span> <span>careerjet <strong>1</strong>', page)


def test_un_run_sans_bilan_le_dit(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Stage NLP", [("careerjet", "contenu:a", "u")])])
    page = rapport.rendre(rapport.charger_section("Stages", base),
                          rapport.Section("Jobs étudiants", None), {})
    assert "Aucun bilan enregistré" in page


def test_une_base_absente_n_est_pas_creee(tmp_path):
    absente = tmp_path / "jobs_etudiants.db"
    section = rapport.charger_section("Jobs étudiants", absente)
    assert section.run is None and section.offres == []
    assert not os.path.exists(absente)
    page = rapport.rendre(rapport.Section("Stages", None), section, {})
    assert page.count("Aucun run enregistré") == 2


# ---------------------------------------------------------------------------
# Offre : liens, première vue, nouveauté, familles, échappement
# ---------------------------------------------------------------------------
def test_tous_les_liens_la_premiere_vue_et_la_nouveaute(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Stage NLP", [("adzuna", "id:1", "https://adzuna/1")])])
    conn = storage.ouvrir(str(base))
    conn.execute("UPDATE offres_liens SET premiere_vue = '2026-09-01'")
    conn.commit()
    conn.close()
    fusion = _offre("Stage NLP", [("adzuna", "id:1", "https://adzuna/1-bis"),
                                  ("jobspy:indeed", "id:in-9", "https://indeed/9")])
    neuve = _offre("Stage Vision", [("careerjet", "contenu:v", "https://cj/v")])
    _run(base, [fusion, neuve])

    section = rapport.charger_section("Stages", base)
    par_titre = {o["title"]: o for o in section.offres}
    assert par_titre["Stage NLP"]["nouvelle"] is False
    assert par_titre["Stage NLP"]["premiere_vue"] == "2026-09-01"
    assert par_titre["Stage Vision"]["nouvelle"] is True

    page = rapport.rendre(section, rapport.Section("Jobs étudiants", None), {})
    lignes = _lignes_du_tableau(page, "table-stages")
    nlp = next(l for l in lignes if "Stage NLP" in l)
    assert 'href="https://adzuna/1-bis"' in nlp and 'href="https://indeed/9"' in nlp
    assert "nouveau" not in nlp
    assert "nouveau" in next(l for l in lignes if "Stage Vision" in l)


def test_les_familles_du_titre_et_de_provenance_sont_filtrables(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Stage NLP", [("careerjet", "contenu:a", "u")],
                       familles=["ml", "mlops"], familles_titre=["ml"])],
         _bilan(sources=[("careerjet", 1)], familles=[("ml", 1), ("mlops", 1), ("cyber", 0)]))
    page = rapport.rendre(rapport.charger_section("Stages", base),
                          rapport.Section("Jobs étudiants", None), {})
    (ligne,) = _lignes_du_tableau(page, "table-stages")
    assert 'data-familles="ml mlops"' in ligne and 'data-familles-titre="ml"' in ligne
    for famille in ("ml", "mlops", "cyber"):
        assert f'data-famille="{famille}"' in page, "un bouton par famille, même vide"
    assert 'class="elargir"' in page


def test_le_contenu_des_offres_est_echappe(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre('Stage <script>alert(1)</script>', [("careerjet", "contenu:a", 'u"><x')])])
    page = rapport.rendre(rapport.charger_section("Stages", base),
                          rapport.Section("Jobs étudiants", None), {})
    assert "<script>alert(1)</script>" not in page
    assert 'u"><x' not in page


# ---------------------------------------------------------------------------
# Jobs étudiants : trajets par origine, tri
# ---------------------------------------------------------------------------
def _trajet(brut, ajuste, estimation=False):
    return {"brut_min": brut, "ajuste_min": ajuste, "distance_km": 10.0, "estimation": estimation}


def test_les_trajets_par_origine_et_le_tri_par_meilleur_trajet(tmp_path):
    base = tmp_path / "jobs.db"
    _run(base, [
        _offre("Loin", [("careerjet", "contenu:1", "u1")],
               trajets={"ville_a": _trajet(30, 39), "campus": _trajet(33, 42.9)}),
        _offre("Inconnu", [("careerjet", "contenu:2", "u2")], lieu="Marne-la-Vallée"),
        _offre("Proche", [("careerjet", "contenu:3", "u3")], drapeaux=["alternance"],
               trajets={"ville_a": _trajet(12, 15.6), "campus": _trajet(20, 26, estimation=True)}),
    ])
    page = rapport.rendre(rapport.Section("Stages", None), rapport.charger_section("Jobs", base),
                          {"ville_a": "Meaux", "campus": "ESIEE"})
    lignes = _lignes_du_tableau(page, "table-jobs")
    assert [re.search(r'class="titre">(?:<span class="neuf">nouveau</span> )?([^<]+)', l).group(1)
            for l in lignes] == ["Proche", "Loin", "Inconnu"], "meilleur trajet d'abord, inconnu en dernier"

    proche = lignes[0]
    assert "12 → <strong>16</strong> min" in proche
    assert "20 → <strong>26</strong> min <span class=\"estim\">estim.</span>" in proche
    assert 'data-ville_a="15.6"' in proche and 'data-meilleur="15.6"' in proche
    assert "⚑ alternance" in proche
    assert lignes[2].count('class="trajet inconnu">—') == 2
    assert 'data-tri="ville_a"' in page and 'data-tri="campus"' in page and 'data-tri="meilleur"' in page


def test_generer_ecrit_une_page_depuis_les_deux_bases(tmp_path):
    stages, jobs = tmp_path / "stages.db", tmp_path / "jobs.db"
    _run(stages, [_offre("Stage NLP", [("careerjet", "contenu:a", "u")])])
    _run(jobs, [_offre("Vendeur", [("france_travail", "id:1", "v")],
                       trajets={"ville_a": _trajet(10, 13)})])
    sortie = rapport.generer(stages, jobs, tmp_path / "flux.html")
    page = sortie.read_text(encoding="utf-8")
    assert "Stage NLP" in page and "Vendeur" in page
    assert page.index('id="stages"') < page.index('id="jobs"')


def test_un_run_anterieur_aux_colonnes_de_run_retombe_sur_la_date(tmp_path):
    base = tmp_path / "stages.db"
    _run(base, [_offre("Stage NLP", [("careerjet", "contenu:a", "u")])])
    conn = storage.ouvrir(str(base))
    conn.execute("UPDATE offres SET dernier_run = NULL, premier_run = NULL")
    conn.commit()
    conn.close()
    section = rapport.charger_section("Stages", base)
    assert [o["title"] for o in section.offres] == ["Stage NLP"]
    assert section.offres[0]["nouvelle"] is False
