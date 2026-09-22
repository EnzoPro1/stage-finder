"""Tests du journal de feedback : append-only, instantané complet, rejeu."""

from __future__ import annotations

import json

import pytest

import feedback
import reference
import storage


# ---------------------------------------------------------------------------
# Base de test
# ---------------------------------------------------------------------------
@pytest.fixture
def conn():
    c = storage.ouvrir(":memory:")
    feedback.ensure_schema(c)
    yield c
    c.close()


def _poser(conn, offer_id="aaa", verdict="like", **extra):
    """Événement minimal : seuls les champs sous test sont écrits en clair."""
    champs = {
        "offer_id": offer_id, "verdict": verdict,
        "rank_at_feedback": 3, "score_at_feedback": 0.42,
        "cos_rang": 3, "mode_tri": "cos",
        "title": "Stage IA", "company": "ACME", "url": "http://x/aaa",
    }
    champs.update(extra)
    return feedback.enregistrer(conn, **champs)


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------
def test_ensure_schema_est_idempotent(conn):
    feedback.ensure_schema(conn)
    feedback.ensure_schema(conn)
    assert feedback.evenements(conn) == []


# ---------------------------------------------------------------------------
# Append-only — la garantie porte sur la BASE, pas sur la discipline d'appel
# ---------------------------------------------------------------------------
def test_update_direct_en_sql_est_refuse(conn):
    """Le trigger doit tenir même quand personne ne passe par le module."""
    _poser(conn, verdict="like")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE feedback_events SET verdict = 'dislike'")


def test_delete_direct_en_sql_est_refuse(conn):
    _poser(conn)
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM feedback_events")


def test_un_update_refuse_laisse_l_evenement_intact(conn):
    """On vérifie l'ÉTAT, pas seulement que ça a levé.

    Un trigger qui lève APRÈS avoir laissé passer l'écriture donnerait
    exactement la même exception et un journal corrompu.
    """
    _poser(conn, verdict="like")
    avant = feedback.evenements(conn)
    with pytest.raises(Exception):
        conn.execute("UPDATE feedback_events SET verdict = 'dislike'")
    conn.rollback()
    assert feedback.evenements(conn) == avant


def test_changer_d_avis_ajoute_une_ligne_au_lieu_d_ecraser(conn):
    _poser(conn, verdict="like")
    _poser(conn, verdict="dislike")
    journal = feedback.evenements(conn)
    assert [e["verdict"] for e in journal] == ["like", "dislike"]


# ---------------------------------------------------------------------------
# Écriture / lecture
# ---------------------------------------------------------------------------
def test_aller_retour_conserve_tous_les_champs_et_leurs_types(conn):
    _poser(
        conn, offer_id="bbb", verdict="dislike",
        rank_at_feedback=12, score_at_feedback=0.317,
        cos_rang=40, ia_rang=12, mode_tri="ia",
        tags=["trop junior", "mauvaise stack"], comment="poste de support déguisé",
        filtre_actif=True, exploratoire=True,
        profile_version="20260901-120000", ranking_version="abc123",
        title="Stage RH", company="BetaCorp", url="http://x/bbb",
        horodatage="2026-09-01T12:00:00",
    )
    (e,) = feedback.evenements(conn)
    assert e["offer_id"] == "bbb" and e["verdict"] == "dislike"
    assert e["timestamp"] == "2026-09-01T12:00:00"
    assert e["rank_at_feedback"] == 12 and e["score_at_feedback"] == 0.317
    assert e["cos_rang"] == 40 and e["ia_rang"] == 12 and e["mode_tri"] == "ia"
    assert e["tags"] == ["trop junior", "mauvaise stack"]
    assert e["comment"] == "poste de support déguisé"
    # Booléens Python, pas des 0/1 SQLite : c'est le front et la distillation
    # qui les liront, pas du SQL.
    assert e["filtre_actif"] is True and e["exploratoire"] is True
    assert e["profile_version"] == "20260901-120000"
    assert e["ranking_version"] == "abc123"
    assert (e["title"], e["company"], e["url"]) == ("Stage RH", "BetaCorp", "http://x/bbb")


def test_ia_rang_absent_reste_nul_et_ne_devient_pas_zero(conn):
    """Zéro serait un rang, donc un mensonge : l'IA n'a pas encore vérifié."""
    _poser(conn, ia_rang=None)
    (e,) = feedback.evenements(conn)
    assert e["ia_rang"] is None


def test_evenements_filtre_par_offre(conn):
    _poser(conn, offer_id="aaa")
    _poser(conn, offer_id="bbb")
    _poser(conn, offer_id="aaa")
    assert len(feedback.evenements(conn, "aaa")) == 2
    assert len(feedback.evenements(conn, "bbb")) == 1


def test_le_journal_suit_l_ordre_d_ecriture_et_non_l_horodatage(conn):
    """Deux retours dans la même seconde sont indiscernables par `timestamp`.

    Si le rejeu triait par horodatage, l'ordre de deux clics rapides
    dépendrait de SQLite ; `id` les ordonne pour de bon.
    """
    _poser(conn, verdict="like", horodatage="2026-09-01T12:00:00")
    _poser(conn, verdict="dislike", horodatage="2026-09-01T12:00:00")
    assert [e["verdict"] for e in feedback.evenements(conn)] == ["like", "dislike"]
    assert feedback.etat_courant(conn)["aaa"]["verdict"] == "dislike"


# ---------------------------------------------------------------------------
# Normalisation à l'écriture
# ---------------------------------------------------------------------------
def test_une_chip_cliquee_deux_fois_ne_compte_qu_une_fois(conn):
    _poser(conn, tags=["localisation", "localisation", "  ", "durée incompatible"])
    (e,) = feedback.evenements(conn)
    assert e["tags"] == ["localisation", "durée incompatible"]


def test_commentaire_vide_est_nul_et_pas_une_chaine_vide(conn):
    """Un champ replié qu'on n'a pas rempli n'est pas un avis écrit."""
    _poser(conn, comment="   ")
    (e,) = feedback.evenements(conn)
    assert e["comment"] is None


def test_sans_etiquette_la_liste_est_vide_pas_nulle(conn):
    _poser(conn)
    (e,) = feedback.evenements(conn)
    assert e["tags"] == []


# ---------------------------------------------------------------------------
# Champs obligatoires (garde-fou du biais d'exposition)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("manquant",
                         ["rank_at_feedback", "score_at_feedback", "cos_rang", "mode_tri"])
def test_rang_et_score_ne_sont_pas_facultatifs(conn, manquant):
    """Sans valeur par défaut dans la signature : l'oubli est une erreur d'appel.

    C'est le garde-fou §5d. Un défaut (0, None) les rendrait optionnels dans
    les faits, et ils seraient vides précisément dans les événements les plus
    anciens — les seuls qui permettraient de mesurer une dérive.
    """
    champs = {
        "offer_id": "aaa", "verdict": "like", "rank_at_feedback": 1,
        "score_at_feedback": 0.5, "cos_rang": 1, "mode_tri": "cos",
        "title": "T", "company": "C", "url": "U",
    }
    champs.pop(manquant)
    with pytest.raises(TypeError):
        feedback.enregistrer(conn, **champs)


def test_verdict_inconnu_est_refuse(conn):
    with pytest.raises(ValueError, match="verdict inconnu"):
        _poser(conn, verdict="peut-être")


def test_mode_tri_inconnu_est_refuse(conn):
    with pytest.raises(ValueError, match="mode_tri inconnu"):
        _poser(conn, mode_tri="alphabétique")


def test_offre_sans_identifiant_est_refusee(conn):
    with pytest.raises(ValueError, match="offer_id"):
        _poser(conn, offer_id="")


def test_versions_par_defaut(conn):
    """Aucun profil promu -> sentinelle explicite ; ranking -> empreinte réelle."""
    _poser(conn)
    (e,) = feedback.evenements(conn)
    assert e["profile_version"] == feedback.PROFIL_AUCUN
    assert e["ranking_version"] == reference.empreinte_config()


# ---------------------------------------------------------------------------
# État courant dérivé
# ---------------------------------------------------------------------------
def test_l_etat_courant_est_le_dernier_evenement(conn):
    _poser(conn, verdict="like")
    _poser(conn, verdict="dislike")
    _poser(conn, verdict="like")
    assert feedback.etat_courant(conn)["aaa"]["verdict"] == "like"


def test_le_dernier_evenement_gagne_EN_BLOC_sans_fusion_partielle(conn):
    """Verdict, chips et commentaire ne se recomposent pas entre événements.

    Un instantané qui hériterait des chips du précédent produirait un état
    que personne ne peut prédire en relisant le journal.
    """
    _poser(conn, verdict="like", tags=["sujet intéressant"], comment="à creuser")
    _poser(conn, verdict="dislike")
    etat = feedback.etat_courant(conn)["aaa"]
    assert etat["verdict"] == "dislike"
    assert etat["tags"] == []
    assert etat["comment"] is None


def test_n_evenements_compte_les_revirements(conn):
    _poser(conn, offer_id="aaa")
    _poser(conn, offer_id="aaa")
    _poser(conn, offer_id="bbb")
    etat = feedback.etat_courant(conn)
    assert etat["aaa"]["n_evenements"] == 2
    assert etat["bbb"]["n_evenements"] == 1


def test_aucun_etat_n_est_stocke_a_cote_du_journal(conn):
    """L'état ne doit exister QUE dérivé. Une table d'état finirait par mentir."""
    _poser(conn)
    tables = {
        l[0] for l in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "feedback_etat" not in tables and "feedback_courant" not in tables
    assert "feedback_events" in tables


# ---------------------------------------------------------------------------
# Rétractation
# ---------------------------------------------------------------------------
def test_none_retire_l_avis_des_opinions_mais_pas_du_journal(conn):
    """`none` = rétractation. Rien n'est détruit : seul l'état dérivé change."""
    _poser(conn, verdict="like")
    _poser(conn, verdict="none")
    etat = feedback.etat_courant(conn)
    assert etat["aaa"]["verdict"] == "none"
    assert feedback.opinions(etat) == {}
    assert len(feedback.evenements(conn)) == 2


def test_une_retractation_peut_etre_suivie_d_un_nouvel_avis(conn):
    _poser(conn, verdict="like")
    _poser(conn, verdict="none")
    _poser(conn, verdict="dislike")
    assert feedback.opinions(feedback.etat_courant(conn))["aaa"]["verdict"] == "dislike"


def test_opinions_ne_garde_que_les_avis_exprimes(conn):
    _poser(conn, offer_id="aaa", verdict="like")
    _poser(conn, offer_id="bbb", verdict="dislike")
    _poser(conn, offer_id="ccc", verdict="like")
    _poser(conn, offer_id="ccc", verdict="none")
    exprimees = feedback.opinions(feedback.etat_courant(conn))
    assert set(exprimees) == {"aaa", "bbb"}


# ---------------------------------------------------------------------------
# Rejeu — la promesse qui protège le séquencement A/B avant D/E
# ---------------------------------------------------------------------------
def test_rejouer_est_pur_et_equivaut_a_l_etat_courant(conn):
    _poser(conn, offer_id="aaa", verdict="like")
    _poser(conn, offer_id="bbb", verdict="dislike")
    _poser(conn, offer_id="aaa", verdict="none")
    assert feedback.rejouer(feedback.evenements(conn)) == feedback.etat_courant(conn)


def test_redistillation_complete_depuis_le_seul_journal(conn):
    """Tout redistiller depuis zéro, sans la base ni aucun état intermédiaire.

    C'est le test qui protège une décision d'architecture : du feedback va
    s'accumuler pendant des semaines avant que la distillation (étape D)
    existe, et la représentation du profil changera ensuite. Il faut donc
    pouvoir tout rejouer depuis l'historique brut. Le journal est SÉRIALISÉ
    puis relu — la connexion n'intervient plus — pour qu'aucune dépendance
    cachée à SQLite ne se glisse dans la dérivation.
    """
    histoire = [
        ("aaa", "like", ["sujet intéressant"], "pile ce que je cherche"),
        ("bbb", "dislike", ["trop senior"], None),
        ("ccc", "like", [], None),
        ("aaa", "dislike", ["mauvaise stack"], "en fait c'est du legacy"),  # revirement
        ("ccc", "none", [], None),                                          # rétractation
        ("ddd", "dislike", ["localisation", "durée incompatible"], None),
    ]
    for rang, (offre, verdict, tags, comment) in enumerate(histoire, 1):
        _poser(conn, offer_id=offre, verdict=verdict, tags=tags, comment=comment,
               rank_at_feedback=rang, score_at_feedback=1.0 / rang, cos_rang=rang)

    # Le journal quitte la base : plus rien d'autre ne sera consulté.
    journal = json.loads(json.dumps(feedback.evenements(conn)))
    conn.close()

    etat = feedback.rejouer(journal)
    exprimees = feedback.opinions(etat)

    assert len(journal) == len(histoire)
    assert set(exprimees) == {"aaa", "bbb", "ddd"}          # ccc est rétractée
    assert exprimees["aaa"]["verdict"] == "dislike"          # le revirement l'emporte
    assert exprimees["aaa"]["tags"] == ["mauvaise stack"]    # sans fusion avec le like
    assert exprimees["aaa"]["comment"] == "en fait c'est du legacy"
    assert exprimees["ddd"]["tags"] == ["localisation", "durée incompatible"]
    # Le contexte d'exposition survit au rejeu : c'est lui qui permettra de
    # diagnostiquer le biais de position bien après coup.
    assert exprimees["bbb"]["rank_at_feedback"] == 2
    assert exprimees["bbb"]["score_at_feedback"] == pytest.approx(0.5)


def test_rejouer_une_sequence_vide_ne_leve_pas(conn):
    assert feedback.rejouer([]) == {}
    assert feedback.opinions({}) == {}


# ---------------------------------------------------------------------------
# Robustesse de lecture
# ---------------------------------------------------------------------------
def test_des_etiquettes_illisibles_ne_bloquent_pas_le_rejeu(conn):
    """Une ligne abîmée à la main ne doit pas rendre les mille autres illisibles."""
    _poser(conn, offer_id="aaa", tags=["trop junior"])
    conn.execute(
        """INSERT INTO feedback_events
           (offer_id, timestamp, verdict, tags, comment, rank_at_feedback,
            score_at_feedback, cos_rang, ia_rang, mode_tri, filtre_actif,
            exploratoire, profile_version, ranking_version, title, company, url)
           VALUES ('bbb','2026-09-01T12:00:00','like','{ pas du json',NULL,1,0.5,1,
                   NULL,'cos',0,0,'aucun','v1','T','C','U')""")
    conn.commit()

    journal = feedback.evenements(conn)
    assert len(journal) == 2
    assert journal[0]["tags"] == ["trop junior"]
    assert journal[1]["tags"] == []
