"""Liens d'une offre : identité par source, fusion sans perte, nouveautés.

Le défaut que ces tests verrouillent : l'URL servait d'identité. Or elle change
d'une requête identique à l'autre — Adzuna ajoute un jeton `se=` (6 offres sur
28 au sondage du 2026-09-17), Careerjet réchiffre l'URL entière (30 sur 30).
Une identité par URL ferait d'une même annonce deux liens, et la ferait passer
pour nouvelle à chaque run.
"""

from __future__ import annotations

import numpy as np
import pytest

import dedup
import storage
from normalize import Lien, Offre, cle_native, normaliser, url_normalisee
from sources import provenance

URL_ADZUNA_1 = ("https://www.adzuna.fr/land/ad/5884394723?se=PDZxH6ey8RG51dr89L8inQ"
                "&utm_medium=api&utm_source=4d2c2076&v=7F4926ED")
URL_ADZUNA_2 = ("https://www.adzuna.fr/land/ad/5884394723?se=yDFbIaey8RGeX64IzcI5Jg"
                "&utm_medium=api&utm_source=4d2c2076&v=7F4926ED")


def _brut_adzuna(url, titre="Stage Machine Learning (H/F)", ident="5884394723"):
    brut = {
        "title": titre, "company": {"display_name": "ACME"},
        "location": {"display_name": "Paris"}, "description": "Du ML.",
        "redirect_url": url, "created": "2026-09-16T10:00:00Z",
    }
    if ident is not None:
        brut["id"] = ident
    return brut


def _brut_careerjet(url, **champs):
    brut = {"title": "Stage Data Scientist &amp; IA", "company": "Sia",
            "locations": "Paris", "site": "welcometothejungle.com",
            "description": "x", "url": url, "date": "Tue, 15 Sep 2026 22:23:36 GMT"}
    brut.update(champs)
    return brut


def _offre(titre, source, cle, url, description="d", familles=()):
    o = Offre(titre, "ACME", "Paris", description, url, source, "", "")
    o.familles = list(familles)
    o.liens = [Lien(source, cle, url)]
    return o


# ---------------------------------------------------------------------------
# Clé native, par source
# ---------------------------------------------------------------------------
def test_adzuna_est_identifie_par_son_id_et_pas_par_son_url():
    assert cle_native("adzuna", _brut_adzuna(URL_ADZUNA_1)) == "id:5884394723"
    assert (cle_native("adzuna", _brut_adzuna(URL_ADZUNA_1))
            == cle_native("adzuna", _brut_adzuna(URL_ADZUNA_2)))


@pytest.mark.parametrize("source, brut, attendu", [
    ("jobspy", {"id": "in-54f7cac6d9f4b333", "job_url": "u"}, "id:in-54f7cac6d9f4b333"),
    ("jobspy", {"id": "li-4468391940", "job_url": "u"}, "id:li-4468391940"),
    ("free_work", {"id": 657052, "slug": "s"}, "id:657052"),
    ("france_travail", {"id": "213TFNQ"}, "id:213TFNQ"),
    ("jooble", {"id": -123456, "link": "u"}, "id:-123456"),
])
def test_les_sources_a_identifiant_natif(source, brut, attendu):
    assert cle_native(source, brut) == attendu


def test_careerjet_est_identifie_par_son_contenu_malgre_une_url_rechiffree():
    a = cle_native("careerjet", _brut_careerjet("https://jobviewtrack.com/v2/AlGoBsbM"))
    b = cle_native("careerjet", _brut_careerjet("https://jobviewtrack.com/v2/1c7APbZG"))
    assert a == b and a.startswith("contenu:")


def test_careerjet_contenu_insensible_a_la_casse_aux_accents_et_au_html():
    a = cle_native("careerjet", _brut_careerjet("u1"))
    b = cle_native("careerjet", _brut_careerjet(
        "u2", title="STAGE data scientist & ia", locations="  paris "))
    assert a == b


def test_careerjet_une_date_reecrite_ne_cree_pas_un_autre_lien():
    a = cle_native("careerjet", _brut_careerjet("u1"))
    b = cle_native("careerjet", _brut_careerjet("u2", date="Wed, 16 Sep 2026 08:00:00 GMT"))
    assert a == b


@pytest.mark.parametrize("champ", ["title", "company", "locations", "site"])
def test_careerjet_deux_annonces_differentes_ont_deux_cles(champ):
    a = cle_native("careerjet", _brut_careerjet("u"))
    b = cle_native("careerjet", _brut_careerjet("u", **{champ: "autre chose"}))
    assert a != b


def test_sans_identifiant_la_cle_native_est_vide():
    assert cle_native("adzuna", _brut_adzuna(URL_ADZUNA_1, ident=None)) == ""
    assert cle_native("inconnue", {"id": "1"}) == ""


def test_url_normalisee_retire_le_suivi_et_garde_le_reste():
    assert (url_normalisee(URL_ADZUNA_1) == url_normalisee(URL_ADZUNA_2)
            == "https://www.adzuna.fr/land/ad/5884394723")
    assert (url_normalisee("HTTPS://Exemple.FR/offre/?b=2&a=1&utm_campaign=x#haut")
            == "https://exemple.fr/offre?a=1&b=2")


def test_normaliser_pose_un_lien_avec_la_cle_native():
    (offre,) = normaliser("adzuna", [_brut_adzuna(URL_ADZUNA_1)])
    assert offre.liens == [Lien("adzuna", "id:5884394723", URL_ADZUNA_1)]


def test_normaliser_jobspy_porte_le_site_dans_le_lien(charger_fixture):
    offre = normaliser("jobspy", charger_fixture("jobspy"))[0]
    assert offre.liens[0].source == offre.source and offre.source.startswith("jobspy:")


def test_normaliser_sans_identifiant_replie_sur_l_url_normalisee():
    (offre,) = normaliser("adzuna", [_brut_adzuna(URL_ADZUNA_1, ident=None)])
    assert offre.liens[0].cle == "url:https://www.adzuna.fr/land/ad/5884394723"


def test_careerjet_deux_familles_deux_urls_une_seule_copie():
    """Défaut du CP2 : la fusion intra-source se faisait par URL, que Careerjet
    réchiffre à chaque requête — elle ne fusionnait donc jamais rien."""
    lot = (provenance.marquer([_brut_careerjet("https://jobviewtrack.com/v2/AAA")], ["ml"])
           + provenance.marquer([_brut_careerjet("https://jobviewtrack.com/v2/BBB")], ["cyber"]))
    fusion = provenance.fusionner(lot, lambda o: cle_native("careerjet", o))
    assert len(fusion) == 1
    assert provenance.familles_de(fusion[0]) == ["ml", "cyber"]


# ---------------------------------------------------------------------------
# familles_titre
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("titre, familles, attendu", [
    ("Stage Machine-Learning Engineer (H/F)", ["ml", "mlops"], ["ml"]),
    ("Stage LLMOps", ["ai_engineering", "mlops"], ["mlops"]),   # « LLM » ≠ « LLMOps »
    ("Stage CYBERSECURITE", ["cyber"], ["cyber"]),               # accents ignorés
    ("Stage Assistant Marketing", ["ml"], []),
    ("Stage NLP", [], []),                   # jamais au-delà des familles de provenance
    ("Stage NLP", ["famille_disparue"], []),
])
def test_familles_titre(titre, familles, attendu):
    import recherche

    assert recherche.familles_dans_titre(titre, familles) == attendu


def test_normaliser_pose_les_familles_titre():
    brut = provenance.marquer([_brut_adzuna(URL_ADZUNA_1)], ["ml", "cyber"])
    (offre,) = normaliser("adzuna", brut)
    assert offre.familles == ["ml", "cyber"]
    assert offre.familles_titre == ["ml"]


# ---------------------------------------------------------------------------
# Déduplication : la version écartée lègue ses liens et ses familles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("riche_en_premier", [True, False])
def test_dedup_exacte_entre_sources_garde_tous_les_liens(riche_en_premier):
    adzuna = _offre("Stage NLP", "adzuna", "id:1", "https://adzuna/1",
                    description="courte", familles=["ml"])
    indeed = _offre("Stage NLP (H/F)", "jobspy:indeed", "id:in-9", "https://indeed/9",
                    description="description nettement plus longue", familles=["ai_engineering"])
    lot = [indeed, adzuna] if riche_en_premier else [adzuna, indeed]
    (fusion,) = dedup.dedupliquer(lot)

    assert fusion is indeed, "la plus riche reste la version affichée"
    assert {(l.source, l.cle, l.url) for l in fusion.liens} == {
        ("adzuna", "id:1", "https://adzuna/1"),
        ("jobspy:indeed", "id:in-9", "https://indeed/9"),
    }
    assert set(fusion.familles) == {"ml", "ai_engineering"}
    assert fusion.familles_titre == ["ml"]


def test_dedup_un_meme_lien_vu_deux_fois_reste_un_lien():
    a = _offre("Stage NLP", "adzuna", "id:1", URL_ADZUNA_1, description="a")
    b = _offre("Stage NLP", "adzuna", "id:1", URL_ADZUNA_2, description="bb")
    (fusion,) = dedup.dedupliquer([a, b])
    assert len(fusion.liens) == 1


def test_dedup_floue_garde_tous_les_liens():
    a = _offre("Stage IA chez Sanofi", "careerjet", "contenu:x", "u1", description="courte")
    b = _offre("Stage IA - Sanofi", "jobspy:linkedin", "id:li-1", "u2",
               description="description plus longue donc plus riche")
    a.company = b.company = "Sanofi"
    emb = np.array([[1.0, 0.0], [0.99, 0.141]])
    emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    (fusion,), _ = dedup.dedupliquer_flou([a, b], emb, seuil=0.9)
    assert {l.source for l in fusion.liens} == {"careerjet", "jobspy:linkedin"}


# ---------------------------------------------------------------------------
# Persistance : un lien par (source, clé), nouveauté par les liens
# ---------------------------------------------------------------------------
def _run(conn, *bruts_adzuna):
    offres = normaliser("adzuna", list(bruts_adzuna))
    storage.enregistrer_run(conn, [(o, 0.5) for o in offres])
    return offres


def _liens_en_base(conn):
    return [dict(l) for l in conn.execute(
        "SELECT source, cle, cle_offre, url FROM offres_liens ORDER BY source, cle")]


def test_meme_offre_adzuna_deux_runs_urls_de_suivi_differentes():
    """Exigence CP3 : une offre, un lien Adzuna, pas nouvelle au run 2."""
    conn = storage.ouvrir(":memory:")
    (run1,) = _run(conn, _brut_adzuna(URL_ADZUNA_1))
    assert run1.nouvelle is True

    (run2,) = _run(conn, _brut_adzuna(URL_ADZUNA_2))
    assert run2.nouvelle is False
    assert conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0] == 1
    liens = _liens_en_base(conn)
    assert len(liens) == 1
    assert liens[0]["source"] == "adzuna" and liens[0]["cle"] == "id:5884394723"
    assert liens[0]["url"] == URL_ADZUNA_2, "l'URL est mise à jour en place"


def test_un_titre_modifie_avec_le_meme_id_n_est_pas_nouveau():
    conn = storage.ouvrir(":memory:")
    _run(conn, _brut_adzuna(URL_ADZUNA_1, titre="Stage ML"))
    (run2,) = _run(conn, _brut_adzuna(URL_ADZUNA_2, titre="Stage ML - Janvier 2027"))
    assert run2.nouvelle is False
    liens = _liens_en_base(conn)
    assert len(liens) == 1
    assert liens[0]["cle_offre"] == storage.cle_identite(run2), "lien repointé"


def test_un_lien_d_une_nouvelle_source_ne_rend_pas_l_offre_nouvelle():
    conn = storage.ouvrir(":memory:")
    connue = _offre("Stage NLP", "adzuna", "id:1", "https://adzuna/1")
    storage.enregistrer_run(conn, [(connue, 0.5)])

    fusion = _offre("Stage NLP", "adzuna", "id:1", "https://adzuna/1-bis")
    fusion.liens.append(Lien("jobspy:indeed", "id:in-9", "https://indeed/9"))
    nouvelles = storage.enregistrer_run(conn, [(fusion, 0.5)])
    assert nouvelles == set() and fusion.nouvelle is False
    assert len(_liens_en_base(conn)) == 2


def test_une_offre_dont_aucun_lien_n_est_connu_est_nouvelle():
    conn = storage.ouvrir(":memory:")
    storage.enregistrer_run(conn, [(_offre("Stage NLP", "adzuna", "id:1", "u"), 0.5)])
    autre = _offre("Stage Vision", "adzuna", "id:2", "v")
    assert storage.enregistrer_run(conn, [(autre, 0.5)]) == {storage.cle_identite(autre)}
    assert autre.nouvelle is True


def test_une_ligne_d_avant_les_liens_n_est_pas_reannoncee():
    """Base existante : ses lignes n'ont aucun lien enregistré. Sans la
    condition sur la clé d'offre, tout le stock repasserait « nouveau »."""
    conn = storage.ouvrir(":memory:")
    ancienne = Offre("Stage NLP", "ACME", "Paris", "d", "u", "adzuna", "", "")
    storage.enregistrer_run(conn, [(ancienne, 0.5)])       # aucun lien : ligne « legacy »
    assert _liens_en_base(conn) == []

    revue = _offre("Stage NLP", "adzuna", "id:1", "u")
    assert storage.enregistrer_run(conn, [(revue, 0.5)]) == set()
    assert len(_liens_en_base(conn)) == 1


def test_premiere_vue_est_celle_du_plus_ancien_lien():
    conn = storage.ouvrir(":memory:")
    _run(conn, _brut_adzuna(URL_ADZUNA_1, titre="Stage ML"))
    conn.execute("UPDATE offres_liens SET premiere_vue = '2026-09-01'")
    conn.execute("UPDATE offres SET premiere_vue = '2026-09-01'")
    (run2,) = _run(conn, _brut_adzuna(URL_ADZUNA_2, titre="Stage ML renommé"))
    assert run2.premiere_vue == "2026-09-01"
    (pv,) = conn.execute("SELECT premiere_vue FROM offres WHERE cle = ?",
                         (storage.cle_identite(run2),)).fetchone()
    assert pv == "2026-09-01", "la nouvelle ligne hérite de la première vue du lien"


def test_familles_persistees():
    conn = storage.ouvrir(":memory:")
    (offre,) = normaliser("adzuna", provenance.marquer([_brut_adzuna(URL_ADZUNA_1)], ["ml", "cyber"]))
    storage.enregistrer_run(conn, [(offre, 0.5)])
    ligne = conn.execute("SELECT familles, familles_titre FROM offres").fetchone()
    assert tuple(ligne) == ("ml cyber", "ml")


def test_une_base_d_avant_cp3_recoit_les_nouvelles_colonnes(tmp_path):
    import sqlite3

    chemin = tmp_path / "ancienne.db"
    ancienne = sqlite3.connect(chemin)
    ancienne.execute("""CREATE TABLE offres (cle TEXT PRIMARY KEY, title TEXT, company TEXT,
        location TEXT, url TEXT, source TEXT, posted_at TEXT, salary TEXT, duree_mois INTEGER,
        date_debut TEXT, tags TEXT, dernier_score REAL, premiere_vue TEXT, derniere_vue TEXT,
        nb_vues INTEGER)""")
    ancienne.commit()
    ancienne.close()

    conn = storage.ouvrir(str(chemin))
    colonnes = {l[1] for l in conn.execute("PRAGMA table_info(offres)")}
    assert {"familles", "familles_titre"} <= colonnes
    (offre,) = normaliser("adzuna", [_brut_adzuna(URL_ADZUNA_1)])
    storage.enregistrer_run(conn, [(offre, 0.5)])
    assert len(storage.liens_de(conn, storage.cle_identite(offre))) == 1
