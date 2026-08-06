"""Lecture groupée des états de génération — `GET /api/cv/states` (CP4).

Le CP3 n'exposait que la lecture unitaire. Avec 294 offres à l'écran, la
liste aurait fait 294 requêtes au chargement puis 294 par tour de polling.

Ce qui est vérifié ici : que la lecture groupée dit EXACTEMENT la même
chose que la lecture unitaire. Deux chemins de lecture qui divergeraient
afficheraient un tour de passage faux, ou un bouton « Télécharger » sur
un job qui n'a rien produit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app as webapp
import config
import jobs
import storage


@pytest.fixture
def base(tmp_path, monkeypatch):
    chemin = str(tmp_path / "cv.db")
    master = tmp_path / "master.yaml"
    master.write_text("identite:\n  nom: Test\n", encoding="utf-8")
    sortie = tmp_path / "out"
    sortie.mkdir()
    monkeypatch.setattr(config, "CHEMIN_BASE", chemin)
    monkeypatch.setattr(config, "CV_MASTER_PATH", master)
    monkeypatch.setattr(config, "CV_OUT_ROOT", sortie)
    conn = storage.ouvrir(chemin)
    jobs.ensure_schema(conn)
    yield SimpleNamespace(conn=conn, chemin=chemin, sortie=sortie)
    conn.close()


@pytest.fixture
def client(base):
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _offre(conn, cle, titre="Stage IA", texte="Le texte de l'annonce."):
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, nb_vues) "
        "VALUES (?,?,?,?,?,1)", (cle, titre, "ACME", "Paris", "http://x"))
    conn.commit()
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)


def _etats(client) -> dict:
    reponse = client.get("/api/cv/states")
    assert reponse.status_code == 200
    return reponse.get_json()["etats"]


# =====================================================================
# Le contrat : absent == idle
# =====================================================================
def test_une_offre_sans_job_et_avec_texte_est_absente(client, base):
    """L'absence d'entrée VEUT DIRE `idle`. C'est ce qui garde la réponse
    petite : la plupart des 294 offres n'ont jamais été générées."""
    _offre(base.conn, "cle-a")
    assert _etats(client) == {}


def test_une_offre_sans_texte_est_annoncee_text_missing(client, base):
    """220 des 294 offres réelles sont dans ce cas : le front doit le
    savoir sans avoir à tenter un POST pour se le faire refuser."""
    _offre(base.conn, "cle-a", texte=None)

    entree = _etats(client)["cle-a"]
    assert entree["status"] == "text_missing"
    assert entree["error_code"] == "TEXT_MISSING"
    assert entree["job_id"] is None


def test_un_job_l_emporte_sur_l_absence_de_texte(client, base):
    """Le job décrit quelque chose qui a EU LIEU ; l'absence de texte
    n'est qu'une disponibilité. Cas réel : le texte purgé après coup."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    base.conn.execute("DELETE FROM offres_texte WHERE cle = 'cle-a'")
    base.conn.commit()

    assert _etats(client)["cle-a"]["status"] == "pending"


# =====================================================================
# Cohérence avec la lecture unitaire du CP3
# =====================================================================
def test_la_lecture_groupee_dit_la_meme_chose_que_l_unitaire(client, base):
    """Le test qui compte. Deux chemins de lecture, un seul résultat."""
    for i in range(4):
        _offre(base.conn, f"cle-{i}", texte=f"Texte {i}.")
        client.post(f"/api/offers/cle-{i}/cv")
    # un terminé, un échoué, deux en file
    jobs.reclamer(base.conn)
    jobs.terminer(base.conn, 1, str(base.sortie / "a" / "cv.pdf"))
    jobs.reclamer(base.conn)
    jobs.echouer(base.conn, 2, error="typst absent", error_code="TYPST_FAILED")

    groupes = _etats(client)
    for i in range(4):
        unitaire = client.get(f"/api/offers/cle-{i}/cv/latest").get_json()
        groupe = groupes[f"cle-{i}"]
        for champ in ("job_id", "status", "position", "pdf_url",
                      "error_code", "error_message", "attempts"):
            assert groupe[champ] == unitaire[champ], (
                f"cle-{i} : « {champ} » diverge entre groupé et unitaire")


def test_les_positions_groupees_valent_les_positions_unitaires(client, base):
    """`positions_pending` est une SECONDE implémentation du rang. Si elle
    diverge de `position()`, la page annonce un faux tour de passage."""
    for i in range(5):
        _offre(base.conn, f"cle-{i}", texte=f"Texte {i}.")
        client.post(f"/api/offers/cle-{i}/cv")

    groupees = jobs.positions_pending(base.conn)
    for job_id, rang in groupees.items():
        assert rang == jobs.position(base.conn, job_id)
    assert sorted(groupees.values()) == [1, 2, 3, 4, 5]


def test_seul_le_dernier_job_de_chaque_offre_est_rendu(client, base):
    """Une offre régénérée a plusieurs jobs ; le bouton n'en reflète qu'un."""
    _offre(base.conn, "cle-a")
    premier, _ = jobs.enfiler(base.conn, "cle-a", "h1")
    jobs.reclamer(base.conn)
    jobs.echouer(base.conn, premier["id"], error="raté", error_code="TYPST_FAILED")
    second, _ = jobs.enfiler(base.conn, "cle-a", "h2")

    entree = _etats(client)["cle-a"]
    assert entree["job_id"] == second["id"]
    assert entree["status"] == "pending"


def test_derniers_par_offre_suit_dernier_pour_offre(base):
    """Les deux fonctions de `jobs.py` doivent classer pareil."""
    for i in range(3):
        _offre(base.conn, f"cle-{i}", texte=f"T{i}")
        for k in range(3):
            jobs.enfiler(base.conn, f"cle-{i}", f"h{i}-{k}")
            jobs.reclamer(base.conn)
            jobs.echouer(base.conn, jobs.dernier_pour_offre(base.conn, f"cle-{i}")["id"],
                         error="x", error_code="INTERNAL_ERROR")

    groupes = jobs.derniers_par_offre(base.conn)
    for i in range(3):
        assert groupes[f"cle-{i}"]["id"] == \
            jobs.dernier_pour_offre(base.conn, f"cle-{i}")["id"]


# =====================================================================
# pdf_url et orphelins
# =====================================================================
def test_un_job_done_sans_pdf_n_annonce_pas_de_lien(client, base):
    """Le front en fait un état `failed` : proposer « Télécharger » sur un
    job qui n'a rien produit mènerait à un 404."""
    _offre(base.conn, "cle-a")
    job, _ = jobs.enfiler(base.conn, "cle-a", "h")
    jobs.reclamer(base.conn)
    jobs.terminer(base.conn, job["id"], None)

    entree = _etats(client)["cle-a"]
    assert entree["status"] == "done" and entree["pdf_url"] is None


def test_un_job_orphelin_ne_casse_pas_la_lecture_groupee(client, base):
    """Même garantie qu'au CP3 : l'offre a disparu, la lecture tient."""
    _offre(base.conn, "cle-a")
    jobs.enfiler(base.conn, "cle-a", "h")
    base.conn.execute("DELETE FROM offres WHERE cle = 'cle-a'")
    base.conn.commit()

    etats = _etats(client)
    assert etats["cle-a"]["status"] == "pending"


def test_la_route_ne_rend_jamais_un_500_nu(client, base, monkeypatch):
    monkeypatch.setattr(webapp.jobs, "derniers_par_offre",
                        lambda conn: (_ for _ in ()).throw(RuntimeError("boum")))
    reponse = client.get("/api/cv/states")
    assert reponse.is_json
    assert reponse.get_json()["error_code"] == "INTERNAL_ERROR"


def test_une_base_vide_rend_une_reponse_vide(client, base):
    assert _etats(client) == {}


# =====================================================================
# La page sert bien ce qu'il faut
# =====================================================================
def test_la_page_charge_la_machine_a_etats(client):
    page = client.get("/").get_data(as_text=True)
    assert '<script src="/static/cv_etats.js"></script>' in page
    assert "chargerEtatsCV" in page


def test_la_machine_a_etats_est_servie(client):
    reponse = client.get("/static/cv_etats.js")
    assert reponse.status_code == 200
    assert b"vueBouton" in reponse.data


def test_etat_expose_la_cle_de_chaque_offre(client, monkeypatch):
    """Sans la clé, le front ne peut désigner aucune offre."""
    from normalize import Offre

    offre = Offre("Stage IA", "ACME", "Paris", "desc", "http://x", "src", "", "")
    monkeypatch.setitem(webapp.ETAT, "classees", [(offre, 0.5)])

    rows = client.get("/api/etat").get_json()["offres"]
    assert rows and rows[0]["cle"] == webapp.cle_identite(offre)


def test_le_polling_est_recursif_et_jamais_un_setInterval(client):
    """`setInterval` empilerait les tours sur une base que le worker écrit
    déjà. La garantie est lisible dans la page elle-même."""
    page = client.get("/").get_data(as_text=True)
    debut = page.index("function relancerSondeCV")
    bloc = page[debut:debut + 700]
    assert "setTimeout" in bloc
    assert "setInterval" not in bloc
