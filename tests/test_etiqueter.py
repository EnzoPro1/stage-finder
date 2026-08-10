"""Tests du corpus étiqueté : schéma, vivier, export/import, affichage à l'aveugle."""

from __future__ import annotations

import json
import sqlite3

import pytest

import etiqueter
import jobs
import storage


# ---------------------------------------------------------------------------
# Base de test
# ---------------------------------------------------------------------------
def _offre(conn, cle, title, company="ACME", location="Paris", source="careerjet",
           score=0.5, texte="Vos missions : développer des modèles.", vue="2026-08-08"):
    conn.execute(
        """INSERT INTO offres (cle, title, company, location, url, source, posted_at,
                               salary, duree_mois, date_debut, tags, dernier_score,
                               premiere_vue, derniere_vue, nb_vues)
           VALUES (?,?,?,?,?,?,'','',NULL,'','',?,?,?,1)""",
        (cle, title, company, location, f"http://x/{cle}", source, score, vue, vue),
    )
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)
    conn.commit()


@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    jobs.ensure_schema(c)
    etiqueter.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def base_peuplee(conn):
    """Deux offres du dernier run, deux plus anciennes, une sans texte."""
    _offre(conn, "aaa", "Ingénieur IA", score=0.9, vue="2026-08-08")
    _offre(conn, "bbb", "Chef de projet digital", score=0.8, vue="2026-08-08")
    _offre(conn, "ccc", "Data Scientist", score=0.7, vue="2026-08-01",
           texte="x" * 3000, source="jobspy:linkedin")
    _offre(conn, "ddd", "Community manager", score=0.3, vue="2026-08-01",
           texte="court")
    _offre(conn, "eee", "Sans texte", score=0.6, vue="2026-08-08", texte=None)
    return conn


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------
def test_ensure_schema_est_idempotent(conn):
    etiqueter.ensure_schema(conn)
    etiqueter.ensure_schema(conn)
    assert etiqueter.lire_etiquettes(conn) == {}


def test_pertinent_hors_0_1_est_refuse(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO etiquettes (cle,pertinent,etiquete_le,title,company,url) "
            "VALUES ('a',2,'','t','c','u')"
        )


# ---------------------------------------------------------------------------
# Vivier
# ---------------------------------------------------------------------------
def test_vivier_ignore_les_offres_sans_texte(base_peuplee):
    cles = {o["cle"] for o in etiqueter.vivier(base_peuplee)}
    assert "eee" not in cles, "une offre sans texte n'est pas jugeable"
    assert {"aaa", "bbb"} <= cles, "tout le dernier run doit être proposé"


def test_vivier_est_deterministe(base_peuplee):
    assert etiqueter.vivier(base_peuplee) == etiqueter.vivier(base_peuplee)


def test_vivier_couvre_les_deux_populations_de_texte(base_peuplee):
    par_cle = {o["cle"]: o for o in etiqueter.vivier(base_peuplee)}
    assert "ccc" in par_cle and "ddd" in par_cle
    assert len(par_cle["ccc"]["texte"]) >= etiqueter.SEUIL_TEXTE_COURT
    assert len(par_cle["ddd"]["texte"]) < etiqueter.SEUIL_TEXTE_COURT


def test_echantillon_etale_prend_les_deux_bouts():
    lignes = [{"i": i} for i in range(20)]
    pris = etiqueter._echantillon_etale(lignes, 5)
    assert len(pris) == 5
    assert pris[0]["i"] == 0 and pris[-1]["i"] == 19
    assert pris == etiqueter._echantillon_etale(lignes, 5)


def test_echantillon_etale_rend_tout_si_la_cible_depasse():
    lignes = [{"i": i} for i in range(3)]
    assert etiqueter._echantillon_etale(lignes, 10) == lignes


# ---------------------------------------------------------------------------
# Ordre de présentation
# ---------------------------------------------------------------------------
def test_ordre_de_presentation_est_stable_et_ne_suit_pas_le_score():
    offres = [{"cle": f"c{i:02}", "score": 1.0 - i / 50} for i in range(30)]
    premier = etiqueter.ordre_presentation(offres)
    assert premier == etiqueter.ordre_presentation(offres), "graine non fixe"
    assert [o["cle"] for o in premier] != [o["cle"] for o in offres], (
        "l'ordre de présentation ne doit pas être l'ordre de score : "
        "il induirait la réponse"
    )
    assert sorted(o["cle"] for o in premier) == sorted(o["cle"] for o in offres)


# ---------------------------------------------------------------------------
# Affichage à l'aveugle — l'exigence méthodologique centrale
# ---------------------------------------------------------------------------
def test_affichage_ne_divulgue_ni_source_ni_score_ni_longueur(capsys):
    offre = {
        "cle": "aaa", "title": "Ingénieur.e IA - Stage",
        "company": "Colombus Consulting", "location": "Paris",
        "source": "jobspy:linkedin", "score": 0.3641,
        "texte": "Vos missions : concevoir des modèles de langage. " + "z" * 900,
    }
    etiqueter._afficher(offre, 3, 40, {"oui": 1, "non": 1, "passees": 0}, integral=False)
    sortie = capsys.readouterr().out

    assert "Ingénieur.e IA - Stage" in sortie
    assert "Colombus Consulting" in sortie
    for fuite in ("jobspy", "linkedin", "0.3641", "0.364", "careerjet",
                  str(len(offre["texte"])), "caractère"):
        assert fuite not in sortie, f"« {fuite} » ne doit pas être montré à l'étiqueteur"


def test_touche_t_deroule_le_texte_integral(capsys):
    marqueur = "PHRASE-TOUT-A-LA-FIN"
    offre = {"cle": "a", "title": "T", "company": "C", "location": "L",
             "source": "s", "score": 1.0,
             "texte": "Vos missions : " + "z" * 2000 + marqueur}
    etiqueter._afficher(offre, 1, 1, {"oui": 0, "non": 0, "passees": 0}, integral=False)
    assert marqueur not in capsys.readouterr().out
    etiqueter._afficher(offre, 1, 1, {"oui": 0, "non": 0, "passees": 0}, integral=True)
    assert marqueur in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def test_session_enregistre_o_et_n_et_saute_sur_interrogation(base_peuplee, tmp_path):
    chemin = str(tmp_path / "etiquettes.json")
    reponses = iter(["o", "n", "?", "q"])
    bilan = etiqueter.session(base_peuplee, chemin, lire=lambda _: next(reponses))

    assert bilan["oui"] == 1 and bilan["non"] == 1 and bilan["passees"] == 1
    assert bilan["corpus"] == 2, "une offre passée n'entre pas dans le corpus"
    assert len(json.loads(open(chemin, encoding="utf-8").read())) == 2


def test_session_exporte_meme_apres_interruption(base_peuplee, tmp_path):
    chemin = str(tmp_path / "etiquettes.json")

    def lire(_):
        raise KeyboardInterrupt

    etiqueter.enregistrer_etiquette(
        base_peuplee, {"cle": "aaa", "title": "t", "company": "c", "url": "u"}, 1)
    etiqueter.session(base_peuplee, chemin, lire=lire)
    assert len(json.loads(open(chemin, encoding="utf-8").read())) == 1


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------
def test_aller_retour_json_est_a_l_identique(base_peuplee, tmp_path):
    """Étiqueter -> exporter -> détruire la table -> importer -> égalité stricte."""
    chemin = str(tmp_path / "etiquettes.json")
    for cle, pertinent in (("aaa", 1), ("bbb", 0), ("ccc", 1)):
        etiqueter.enregistrer_etiquette(
            base_peuplee,
            {"cle": cle, "title": f"T{cle}", "company": "ACME", "url": f"http://x/{cle}"},
            pertinent,
        )
    avant = etiqueter.lire_etiquettes(base_peuplee)
    etiqueter.exporter_json(avant, chemin)

    base_peuplee.execute("DROP TABLE etiquettes")
    base_peuplee.commit()

    rapport = etiqueter.importer_json(base_peuplee, chemin, appliquer=True)
    assert len(rapport["ajouts"]) == 3
    assert etiqueter.lire_etiquettes(base_peuplee) == avant


def test_import_est_idempotent(base_peuplee, tmp_path):
    chemin = str(tmp_path / "etiquettes.json")
    etiqueter.enregistrer_etiquette(
        base_peuplee, {"cle": "aaa", "title": "T", "company": "C", "url": "U"}, 1)
    etiqueter.exporter_json(etiqueter.lire_etiquettes(base_peuplee), chemin)

    etiqueter.importer_json(base_peuplee, chemin, appliquer=True)
    second = etiqueter.importer_json(base_peuplee, chemin, appliquer=True)
    assert second["ajouts"] == [] and second["modifications"] == []
    assert second["suppressions"] == [] and second["inchangees"] == 1


def test_import_simulation_n_ecrit_rien(base_peuplee, tmp_path):
    """Le piège `with conn:` — une simulation qui valide quand même.

    Le scénario est celui qui a déjà mordu : le rapport annonce « rien
    n'a été écrit » alors qu'un `with conn:` a validé la transaction en
    sortie de bloc. On vérifie donc l'ÉTAT de la table, pas le rapport.
    """
    chemin = str(tmp_path / "etiquettes.json")
    etiqueter.enregistrer_etiquette(
        base_peuplee, {"cle": "aaa", "title": "T", "company": "C", "url": "U"}, 1)
    reference = etiqueter.lire_etiquettes(base_peuplee)

    # Le JSON demande tout autre chose : une suppression, un ajout, un changement.
    json_voulu = [
        {"cle": "aaa", "pertinent": 0, "title": "T", "company": "C",
         "url": "U", "etiquete_le": "2026-01-01T00:00:00"},
        {"cle": "zzz", "pertinent": 1, "title": "Z", "company": "C",
         "url": "U", "etiquete_le": "2026-01-01T00:00:00"},
    ]
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(json_voulu, f)

    rapport = etiqueter.importer_json(base_peuplee, chemin, appliquer=False)
    assert rapport["applique"] is False
    assert rapport["ajouts"] == ["zzz"] and rapport["modifications"] == ["aaa"]

    # L'assertion qui compte : la base est INTACTE, transaction comprise.
    assert etiqueter.lire_etiquettes(base_peuplee) == reference
    base_peuplee.rollback()
    assert etiqueter.lire_etiquettes(base_peuplee) == reference


def test_export_est_atomique_et_stable(base_peuplee, tmp_path):
    chemin = tmp_path / "etiquettes.json"
    etiqueter.enregistrer_etiquette(
        base_peuplee, {"cle": "bbb", "title": "B", "company": "C", "url": "U"}, 0)
    etiqueter.enregistrer_etiquette(
        base_peuplee, {"cle": "aaa", "title": "A", "company": "C", "url": "U"}, 1)
    etiquettes = etiqueter.lire_etiquettes(base_peuplee)

    etiqueter.exporter_json(etiquettes, str(chemin))
    premier = chemin.read_bytes()
    etiqueter.exporter_json(etiquettes, str(chemin))
    assert chemin.read_bytes() == premier, "deux exports doivent être identiques"

    assert list(tmp_path.iterdir()) == [chemin], "aucun fichier temporaire ne doit rester"
    corps = json.loads(premier.decode("utf-8"))
    assert [e["cle"] for e in corps] == ["aaa", "bbb"], "tri par clé attendu"


def test_import_signale_les_etiquettes_orphelines(base_peuplee, tmp_path):
    chemin = str(tmp_path / "etiquettes.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump([{"cle": "inconnue", "pertinent": 1, "title": "T",
                    "company": "C", "url": "U", "etiquete_le": ""}], f)
    rapport = etiqueter.importer_json(base_peuplee, chemin, appliquer=True)
    assert rapport["orphelines"] == ["inconnue"]
    assert "inconnue" in etiqueter.lire_etiquettes(base_peuplee), (
        "une étiquette orpheline est conservée : l'offre peut revenir au prochain scrape"
    )


def test_charger_json_absent_rend_un_corpus_vide(tmp_path):
    assert etiqueter.charger_json(str(tmp_path / "rien.json")) == {}


# ---------------------------------------------------------------------------
# Ventilation
# ---------------------------------------------------------------------------
def test_ventilation_separe_sources_et_populations(base_peuplee):
    offres = etiqueter.vivier(base_peuplee)
    v = etiqueter.ventilation(offres)
    assert v["n"] == 4
    assert set(v["par_source"]) == {"careerjet", "jobspy:linkedin"}
    assert len(v["par_longueur"]) == 2


def test_ventilation_restreinte_aux_etiquetees_compte_oui_et_non(base_peuplee):
    offres = etiqueter.vivier(base_peuplee)
    etiquettes = {"aaa": {"pertinent": 1}, "bbb": {"pertinent": 0}}
    v = etiqueter.ventilation(offres, etiquettes)
    assert v["n"] == 2
    assert v["par_source"]["careerjet"] == {"n": 2, "oui": 1, "non": 1}
