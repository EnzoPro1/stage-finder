"""Étanchéité des deux pools de jugements.

Le corpus étiqueté À L'AVEUGLE mesure le ranking ; le journal de feedback,
posé EN VOYANT le classement, sert à l'améliorer. Si le second alimente le
premier, la métrique cesse de mesurer le système et se met à mesurer l'accord
du système avec lui-même — sans jamais cesser de rendre un chiffre plausible.
C'est ce mode de panne silencieux que ce fichier existe pour rendre bruyant.

Les trois barrières sont testées SÉPARÉMENT : elles ne se réparent pas au même
endroit, et une seule qui tient ne dit rien des deux autres.
"""

from __future__ import annotations

import json

import pytest

import etiqueter
import evaluer_ranking
import feedback
import jobs
import storage
from dedup import _cle as cle_identite
from normalize import Offre


# ---------------------------------------------------------------------------
# Base de test
# ---------------------------------------------------------------------------
def _inserer_offre(conn, title, company="ACME", texte="Vos missions : développer."):
    """Insère une offre complète (ligne + texte), et rend sa clé d'identité."""
    offre = Offre(title, company, "Paris", texte, f"http://x/{title}",
                  "careerjet", "", "")
    cle = cle_identite(offre)
    conn.execute(
        """INSERT INTO offres (cle,title,company,location,url,source,posted_at,salary,
                               duree_mois,date_debut,tags,dernier_score,
                               premiere_vue,derniere_vue,nb_vues)
           VALUES (?,?,?,'Paris',?,'careerjet','','',NULL,'','',0.5,
                   '2026-08-08','2026-08-08',1)""",
        (cle, title, company, offre.url),
    )
    jobs.enregistrer_texte(conn, cle, texte)
    conn.commit()
    return cle


@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    jobs.ensure_schema(c)
    etiqueter.ensure_schema(c)
    feedback.ensure_schema(c)
    yield c
    c.close()


def _ecrire_corpus(chemin, entrees):
    """Écrit un `etiquettes.json` à la main, provenance comprise."""
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(entrees, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LE test : une étiquette de feedback dans le jeu d'évaluation doit ÉCHOUER
# ---------------------------------------------------------------------------
def test_une_etiquette_in_app_dans_le_corpus_fait_echouer_l_evaluation(conn, tmp_path):
    """Le garde-fou §5a, exercé sur le chemin réel de la mesure."""
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-09-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_FEEDBACK,
    }])

    with pytest.raises(etiqueter.ProvenanceEtrangere):
        evaluer_ranking.charger_corpus(conn, chemin)


def test_le_refus_nomme_l_etiquette_en_cause(conn, tmp_path):
    """Un refus qui ne dit pas QUOI retirer envoie fouiller 57 entrées à la main."""
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-09-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_FEEDBACK,
    }])

    with pytest.raises(etiqueter.ProvenanceEtrangere) as capture:
        evaluer_ranking.charger_corpus(conn, chemin)
    assert cle[:12] in str(capture.value)
    assert etiqueter.PROVENANCE_FEEDBACK in str(capture.value)


def test_la_mesure_refuse_au_lieu_de_filtrer_silencieusement(conn, tmp_path):
    """Une étiquette propre à côté d'une polluée ne « sauve » pas la mesure.

    Le mode de panne qu'on refuse : un rapport calculé sur les entrées restées
    valides, qui rendrait un `precision@10` d'allure normale sur un corpus
    amputé d'un nombre inconnu d'étiquettes.
    """
    propre = _inserer_offre(conn, "Stage IA")
    polluee = _inserer_offre(conn, "Stage Cyber")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [
        {"cle": propre, "pertinent": 1, "title": "Stage IA", "company": "ACME",
         "url": "http://x", "etiquete_le": "2026-08-01T12:00:00",
         "provenance": etiqueter.PROVENANCE_CORPUS},
        {"cle": polluee, "pertinent": 0, "title": "Stage Cyber", "company": "ACME",
         "url": "http://y", "etiquete_le": "2026-09-01T12:00:00",
         "provenance": etiqueter.PROVENANCE_FEEDBACK},
    ])

    with pytest.raises(etiqueter.ProvenanceEtrangere):
        evaluer_ranking.charger_corpus(conn, chemin)


def test_une_provenance_inconnue_est_refusee_comme_une_fuite(conn, tmp_path):
    """Liste blanche, pas liste noire : `in_app_feedback` n'est pas le seul
    mot dangereux, et un pool futur qui n'aurait pas été prévu ici doit se
    signaler au lieu d'entrer en douce."""
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-09-01T12:00:00",
        "provenance": "import_ami_bienveillant",
    }])

    with pytest.raises(etiqueter.ProvenanceEtrangere):
        evaluer_ranking.charger_corpus(conn, chemin)


def test_un_corpus_en_aveugle_passe_normalement(conn, tmp_path):
    """Le test de fuite ne vaut que si le cas nominal, lui, passe."""
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-08-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_CORPUS,
    }])

    corpus = evaluer_ranking.charger_corpus(conn, chemin)
    assert list(corpus["pertinences"]) == [cle]


def test_un_corpus_sans_champ_provenance_reste_lisible(conn, tmp_path):
    """Rétro-compatibilité : les 57 étiquettes d'origine n'avaient pas le champ.

    Elles viennent toutes de `session()`, donc de l'aveugle. Les refuser
    casserait la baseline au lieu de protéger quoi que ce soit.
    """
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-08-01T12:00:00",
    }])

    corpus = evaluer_ranking.charger_corpus(conn, chemin)
    assert list(corpus["pertinences"]) == [cle]


# ---------------------------------------------------------------------------
# Barrière n°2 — le CHECK en base
# ---------------------------------------------------------------------------
def test_la_table_du_corpus_refuse_une_etiquette_de_feedback(conn):
    """Le cache de mesure ne peut pas être pollué, même en SQL direct."""
    with pytest.raises(Exception, match="CHECK"):
        etiqueter.enregistrer_etiquette(
            conn, {"cle": "aaa", "title": "T", "company": "C", "url": "U"}, 1,
            provenance=etiqueter.PROVENANCE_FEEDBACK,
        )


def test_importer_un_json_pollue_echoue_au_lieu_de_peupler_le_cache(conn, tmp_path):
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": "aaa", "pertinent": 1, "title": "T", "company": "C", "url": "U",
        "etiquete_le": "2026-09-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_FEEDBACK,
    }])

    with pytest.raises(Exception, match="CHECK"):
        etiqueter.importer_json(conn, chemin, appliquer=True)


def test_la_simulation_annonce_la_pollution_avant_de_l_appliquer(conn, tmp_path):
    """Un `CHECK constraint failed` ne dit pas QUELLE étiquette est en cause.

    La simulation doit le dire, sinon le diagnostic se fait à la main dans un
    fichier de plusieurs dizaines d'entrées.
    """
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": "aaa", "pertinent": 1, "title": "T", "company": "C", "url": "U",
        "etiquete_le": "2026-09-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_FEEDBACK,
    }])

    rapport = etiqueter.importer_json(conn, chemin, appliquer=False)
    assert rapport["etrangeres"] == ["aaa"]


def test_une_etiquette_posee_normalement_est_estampillee_aveugle(conn):
    etiqueter.enregistrer_etiquette(
        conn, {"cle": "aaa", "title": "T", "company": "C", "url": "U"}, 1)
    (etiquette,) = etiqueter.lire_etiquettes(conn).values()
    assert etiquette["provenance"] == etiqueter.PROVENANCE_CORPUS


def test_migration_d_une_base_anterieure_retro_remplit_la_provenance(tmp_path):
    """Une base d'avant le champ : les étiquettes déjà posées viennent de
    `session()`, le DEFAULT les qualifie correctement."""
    chemin = str(tmp_path / "ancienne.db")
    conn = storage.ouvrir(chemin)
    conn.executescript(
        """CREATE TABLE etiquettes (
               cle TEXT PRIMARY KEY, pertinent INTEGER NOT NULL,
               etiquete_le TEXT NOT NULL, title TEXT NOT NULL,
               company TEXT NOT NULL, url TEXT NOT NULL);
           INSERT INTO etiquettes VALUES ('aaa',1,'2026-08-01T12:00:00','T','C','U');"""
    )
    conn.commit()

    etiqueter.ensure_schema(conn)
    assert etiqueter.lire_etiquettes(conn)["aaa"]["provenance"] == \
        etiqueter.PROVENANCE_CORPUS
    # Et la barrière est bien posée sur la table migrée, pas seulement sur une
    # table fraîchement créée.
    with pytest.raises(Exception, match="CHECK"):
        etiqueter.enregistrer_etiquette(
            conn, {"cle": "bbb", "title": "T", "company": "C", "url": "U"}, 1,
            provenance=etiqueter.PROVENANCE_FEEDBACK)
    conn.close()


# ---------------------------------------------------------------------------
# Barrière n°1 — deux supports, et aucun pont entre eux
# ---------------------------------------------------------------------------
def test_le_feedback_n_atterrit_jamais_dans_le_corpus(conn, tmp_path):
    """Poser du feedback ne doit toucher ni la table `etiquettes` ni le JSON."""
    cle = _inserer_offre(conn, "Stage IA")
    chemin = str(tmp_path / "etiquettes.json")
    _ecrire_corpus(chemin, [{
        "cle": cle, "pertinent": 1, "title": "Stage IA", "company": "ACME",
        "url": "http://x", "etiquete_le": "2026-08-01T12:00:00",
        "provenance": etiqueter.PROVENANCE_CORPUS,
    }])
    avant = open(chemin, encoding="utf-8").read()

    feedback.enregistrer(
        conn, offer_id=cle, verdict="dislike", rank_at_feedback=1,
        score_at_feedback=0.9, cos_rang=1, mode_tri="cos",
        title="Stage IA", company="ACME", url="http://x",
        tags=["mauvaise stack"],
    )

    assert etiqueter.lire_etiquettes(conn) == {}
    assert open(chemin, encoding="utf-8").read() == avant
    # Et la mesure reste celle du corpus en aveugle, dislike ou pas.
    corpus = evaluer_ranking.charger_corpus(conn, chemin)
    assert corpus["pertinences"][cle] == 1.0


def test_aucun_module_n_ecrit_de_feedback_dans_le_corpus():
    """Barrière n°1, vérifiée sur le CODE et pas sur un scénario.

    Un scénario ne teste que le chemin qu'il emprunte. Ici on vérifie qu'aucun
    module ne peut écrire d'étiquette depuis le feedback : `feedback.py`
    n'importe pas `etiqueter`, et rien n'appelle `enregistrer_etiquette` avec
    une provenance in-app. Le jour où quelqu'un branchera un pont entre les
    deux — « ce serait dommage de perdre ces dislikes » — ce test tombera.
    """
    from pathlib import Path

    racine = Path(__file__).resolve().parent.parent
    source_feedback = (racine / "feedback.py").read_text(encoding="utf-8")
    assert "import etiqueter" not in source_feedback
    assert "enregistrer_etiquette" not in source_feedback

    for module in ("app.py", "worker.py", "main.py", "cv_cli.py"):
        source = (racine / module).read_text(encoding="utf-8")
        assert "enregistrer_etiquette" not in source, (
            f"{module} écrit dans le corpus de mesure : "
            f"seul l'étiquetage à l'aveugle en a le droit."
        )


def test_les_deux_pools_vivent_dans_des_tables_distinctes(conn):
    """Rien ne les joint, et surtout pas une clé étrangère commune."""
    cle = _inserer_offre(conn, "Stage IA")
    etiqueter.enregistrer_etiquette(
        conn, {"cle": cle, "title": "Stage IA", "company": "ACME", "url": "U"}, 1)
    feedback.enregistrer(
        conn, offer_id=cle, verdict="dislike", rank_at_feedback=1,
        score_at_feedback=0.9, cos_rang=1, mode_tri="cos",
        title="Stage IA", company="ACME", url="U")

    # La même offre porte les deux jugements, opposés, sans que l'un n'écrase
    # l'autre : c'est exactement la situation que l'étanchéité doit permettre.
    assert etiqueter.lire_etiquettes(conn)[cle]["pertinent"] == 1
    assert feedback.etat_courant(conn)[cle]["verdict"] == "dislike"
