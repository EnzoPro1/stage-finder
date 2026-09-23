"""`cv_cli batch` — enfiler en masse, puis consommer, sans second chemin.

Le CP5 demandait que le batch et l'UI empruntent exactement le même code.
Ce qui est vérifié ici, c'est justement ce que le batch NE fait PAS : il
n'appelle ni ``generate_cv``, ni ``cv_forge``, ni un enfilement à lui. Il
pose des lignes avec ``jobs.demander`` et consomme avec ``Worker``.

Rien de réel ne tourne : ni Ollama, ni Typst, ni encodeur.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import cv_cli
import jobs
import storage
import worker as worker_module


@pytest.fixture
def base(tmp_path, monkeypatch):
    chemin = str(tmp_path / "b.db")
    conn = storage.ouvrir(chemin)
    jobs.ensure_schema(conn)
    master = tmp_path / "master.yaml"
    master.write_text("nom: Test\n", encoding="utf-8")
    monkeypatch.setattr(config, "CV_MASTER_PATH", str(master))
    yield SimpleNamespace(chemin=chemin, conn=conn, master=master, tmp=tmp_path)
    conn.close()


def _offre(conn, cle, *, texte="Texte de l'offre.", score=0.5):
    conn.execute(
        "INSERT INTO offres (cle, title, company, location, url, dernier_score, "
        "nb_vues) VALUES (?,?,?,?,?,?,1)",
        (cle, f"Stage {cle}", "ACME", "Paris", "http://x", score),
    )
    conn.commit()
    if texte is not None:
        jobs.enregistrer_texte(conn, cle, texte)


def _args(base, **kw):
    kw.setdefault("db", base.chemin)
    kw.setdefault("limite", 12)
    kw.setdefault("enfiler_seulement", True)
    return SimpleNamespace(**kw)


def _jobs(conn):
    return conn.execute("SELECT COUNT(*) FROM generation_jobs").fetchone()[0]


# =====================================================================
# Enfilement
# =====================================================================
@pytest.mark.cv_forge
def test_le_batch_enfile_les_offres_pourvues_en_texte(base, capsys):
    for i in range(3):
        _offre(base.conn, f"cle-{i}", texte=f"Texte n°{i}, distinct des autres.")

    assert cv_cli.cmd_batch(_args(base)) == 0
    assert _jobs(base.conn) == 3
    assert "enfilées      : 3" in capsys.readouterr().out


@pytest.mark.cv_forge
def test_le_batch_ignore_les_offres_sans_texte(base, capsys):
    """`text_missing` n'entre pas dans la file : un job voué à échouer ne
    ferait qu'occuper le passage et bruiter l'historique.

    Elles sont écartées par la REQUÊTE de sélection, pas par un refus au
    coup par coup — mais le compte est affiché, parce que c'est le
    chiffre à surveiller : il ne descend que par un scrape.
    """
    _offre(base.conn, "avec", texte="Un texte bien réel.")
    _offre(base.conn, "sans", texte=None)

    cv_cli.cmd_batch(_args(base))
    assert _jobs(base.conn) == 1
    assert "sans texte    : 1   [TEXT_MISSING]" in capsys.readouterr().out


@pytest.mark.cv_forge
def test_l_idempotence_vaut_aussi_pour_le_batch(base, capsys):
    """Deux batches d'affilée ne régénèrent rien : même hash, même job.

    C'est l'exigence du CP5, et elle ne coûte pas une ligne de plus —
    elle vient de `jobs.demander`, partagée avec le bouton de l'UI.
    """
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")

    cv_cli.cmd_batch(_args(base))
    capsys.readouterr()
    cv_cli.cmd_batch(_args(base))

    assert _jobs(base.conn) == 1
    assert "déjà connues  : 1" in capsys.readouterr().out


@pytest.mark.cv_forge
def test_deux_offres_au_meme_texte_partagent_un_job(base):
    """L'idempotence porte sur le TEXTE, pas sur la clé d'offre : deux
    annonces identiques republiées ne coûtent pas deux fois 800 s."""
    _offre(base.conn, "cle-a", texte="Exactement le même texte.")
    _offre(base.conn, "cle-b", texte="Exactement le même texte.")

    cv_cli.cmd_batch(_args(base))
    assert _jobs(base.conn) == 1


@pytest.mark.cv_forge
def test_le_batch_prend_les_mieux_classees_d_abord(base):
    _offre(base.conn, "faible", texte="Texte faible.", score=0.10)
    _offre(base.conn, "forte", texte="Texte fort.", score=0.90)
    _offre(base.conn, "moyenne", texte="Texte moyen.", score=0.50)

    cv_cli.cmd_batch(_args(base, limite=2))
    enfilees = {l["offer_id"] for l in
                base.conn.execute("SELECT offer_id FROM generation_jobs")}
    assert enfilees == {"forte", "moyenne"}


@pytest.mark.cv_forge
def test_une_offre_jamais_classee_ne_passe_pas_devant(base):
    """En SQLite, NULL trie AVANT tout le reste en DESC. Sans le
    `IS NULL` explicite, une offre sans score doublerait la meilleure."""
    _offre(base.conn, "sans-score", texte="Texte sans score.", score=None)
    _offre(base.conn, "forte", texte="Texte fort.", score=0.90)

    cv_cli.cmd_batch(_args(base, limite=1))
    enfilees = [l["offer_id"] for l in
                base.conn.execute("SELECT offer_id FROM generation_jobs")]
    assert enfilees == ["forte"]


@pytest.mark.cv_forge
def test_un_master_introuvable_arrete_le_batch_tout_de_suite(base, monkeypatch,
                                                             capsys):
    """Le même refus pour les 12 offres : le répéter douze fois n'apprend
    rien, et laisserait croire à douze problèmes distincts."""
    for i in range(3):
        _offre(base.conn, f"cle-{i}", texte=f"Texte n°{i}, distinct.")
    monkeypatch.setattr(config, "CV_MASTER_PATH", str(base.tmp / "nulle-part.yaml"))

    assert cv_cli.cmd_batch(_args(base)) == 1
    assert _jobs(base.conn) == 0
    assert "MASTER_INVALID" in capsys.readouterr().err


# =====================================================================
# Consommation : qui porte le worker ?
# =====================================================================
class FauxWorker:
    """Double de ``Worker`` : compte les vidanges, n'appelle rien de réel."""

    dernier: "FauxWorker | None" = None

    def __init__(self, *, db_path=None, traites=2):
        self.db_path = db_path
        self._traites = traites
        self.appels = 0
        self.decharges = 1
        FauxWorker.dernier = self

    def vider(self):
        self.appels += 1
        return self._traites


@pytest.mark.cv_forge
def test_le_batch_consomme_lui_meme_quand_le_verrou_est_libre(base, monkeypatch,
                                                              capsys):
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")
    monkeypatch.setattr(cv_cli.worker_module, "Worker", FauxWorker)

    assert cv_cli.cmd_batch(_args(base, enfiler_seulement=False)) == 0
    assert FauxWorker.dernier.appels == 1
    assert "2 job(s) traité(s)" in capsys.readouterr().out


@pytest.mark.cv_forge
def test_le_batch_n_essaie_pas_de_doubler_un_worker_deja_en_place(base, capsys):
    """La décision se prend À L'EXÉCUTION, d'après ce qui tourne.

    « Le batch enfile et attend » laisserait la file pleine au matin si
    personne ne tourne ; « le batch EST le worker » ferait deux workers
    quand l'app web tourne. Il tente donc le verrou, et se règle dessus.
    """
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")
    tenu = worker_module.VerrouWorker(worker_module.chemin_verrou(base.chemin))
    assert tenu.acquerir() is True
    try:
        assert cv_cli.cmd_batch(_args(base, enfiler_seulement=False)) == 0
    finally:
        tenu.relacher()

    sortie = capsys.readouterr().out
    assert "consomme déjà la file" in sortie
    assert "les traitera" in sortie
    # Enfilés quand même : c'est l'autre worker qui les prendra.
    assert _jobs(base.conn) == 1
    assert jobs.en_attente(base.conn) == 1


@pytest.mark.cv_forge
def test_enfiler_seulement_ne_consomme_rien(base, monkeypatch, capsys):
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")
    monkeypatch.setattr(cv_cli.worker_module, "Worker", FauxWorker)
    FauxWorker.dernier = None

    cv_cli.cmd_batch(_args(base, enfiler_seulement=True))
    assert FauxWorker.dernier is None
    assert "rien n'a été consommé" in capsys.readouterr().out


@pytest.mark.cv_forge
def test_une_file_vide_ne_reveille_pas_le_worker(base, monkeypatch):
    """Aucune offre pourvue : inutile de charger quoi que ce soit."""
    _offre(base.conn, "sans", texte=None)
    monkeypatch.setattr(cv_cli.worker_module, "Worker", FauxWorker)
    FauxWorker.dernier = None

    assert cv_cli.cmd_batch(_args(base, enfiler_seulement=False)) == 0
    assert FauxWorker.dernier is None


# =====================================================================
# Le batch n'a pas de chemin de génération à lui
# =====================================================================
@pytest.mark.cv_forge
def test_le_batch_ne_touche_jamais_cv_forge_directement(base, monkeypatch):
    """Le verrou du CP5, écrit comme un sabotage.

    Si `cmd_batch` appelait `generate_cv` — ou reconstruisait une
    `OfferInput`, ou recalculait un `offer_hash` — ce test le verrait.
    Tout cela appartient à `jobs` et à `worker`, et à eux seuls.
    """
    import cv_forge

    def interdit(*_a, **_k):
        raise AssertionError("le batch a appelé generate_cv en direct")

    monkeypatch.setattr(cv_forge, "generate_cv", interdit)
    monkeypatch.setattr(cv_cli.worker_module, "Worker", FauxWorker)
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")

    assert cv_cli.cmd_batch(_args(base, enfiler_seulement=False)) == 0


@pytest.mark.cv_forge
def test_le_batch_passe_par_jobs_demander(base, monkeypatch):
    """Un seul point d'entrée pour l'enfilement, partagé avec l'UI."""
    vus = []
    vrai = jobs.demander
    monkeypatch.setattr(
        jobs, "demander",
        lambda conn, offer_id, **kw: vus.append(offer_id) or vrai(
            conn, offer_id, **kw),
    )
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")
    _offre(base.conn, "cle-b", texte="Un autre texte, bien distinct.")

    cv_cli.cmd_batch(_args(base))
    assert sorted(vus) == ["cle-a", "cle-b"]


# =====================================================================
# `cv_cli sans-texte` — l'inventaire de ce qui bloque
#
# Le texte ne se récupère qu'en revoyant l'offre EN LIGNE : rien ne le
# reconstruit hors ligne. La date de dernier run est donc la seule
# colonne qui décide, et la commande ne fait que lire.
# =====================================================================
def _args_sans_texte(base, **kw):
    kw.setdefault("db", base.chemin)
    kw.setdefault("limite", 25)
    kw.setdefault("tout", False)
    return SimpleNamespace(**kw)


def _vue(conn, cle, jour):
    conn.execute("UPDATE offres SET derniere_vue = ? WHERE cle = ?", (jour, cle))
    conn.commit()


def test_sans_texte_separe_le_recuperable_du_perdu(base, capsys):
    """La coupure est le DERNIER RUN, pas la date du jour.

    Sans scrape depuis une semaine, se référer à aujourd'hui déclarerait
    tout périmé alors que rien n'a été retenté.
    """
    _offre(base.conn, "fraiche", texte=None)
    _vue(base.conn, "fraiche", "2026-08-04")
    _offre(base.conn, "vieille", texte=None)
    _vue(base.conn, "vieille", "2026-07-14")
    _offre(base.conn, "pourvue", texte="Un texte bien réel.")
    _vue(base.conn, "pourvue", "2026-08-04")

    assert cv_cli.cmd_sans_texte(_args_sans_texte(base)) == 0
    sortie = capsys.readouterr().out
    assert "Offres sans texte : 2 sur 3" in sortie
    assert "récupérables  : 1" in sortie
    assert "perdues       : 1" in sortie


def test_sans_texte_ventile_par_date_de_dernier_run(base, capsys):
    for i in range(3):
        _offre(base.conn, f"a{i}", texte=None)
        _vue(base.conn, f"a{i}", "2026-08-04")
    _offre(base.conn, "b", texte=None)
    _vue(base.conn, "b", "2026-07-14")

    cv_cli.cmd_sans_texte(_args_sans_texte(base))
    sortie = capsys.readouterr().out
    assert "2026-08-04      3" in sortie
    assert "2026-07-14      1" in sortie


def test_sans_texte_n_efface_rien(base):
    """Le point le plus important : la commande LIT, et c'est tout.

    Une annonce republiée à l'identique se rattache toute seule à sa
    ligne — la clé est dérivée du contenu. Purger automatiquement
    détruirait cet historique pour une absence temporaire.
    """
    for i in range(4):
        _offre(base.conn, f"a{i}", texte=None)
    _offre(base.conn, "pourvue", texte="Un texte bien réel.")

    cv_cli.cmd_sans_texte(_args_sans_texte(base))

    assert base.conn.execute("SELECT COUNT(*) FROM offres").fetchone()[0] == 5
    assert base.conn.execute("SELECT COUNT(*) FROM offres_texte").fetchone()[0] == 1


def test_sans_texte_tronque_et_le_dit(base, capsys):
    for i in range(10):
        _offre(base.conn, f"a{i}", texte=None)

    cv_cli.cmd_sans_texte(_args_sans_texte(base, limite=3))
    assert "et 7 autre(s)" in capsys.readouterr().out


def test_sans_texte_tout_ne_tronque_pas(base, capsys):
    for i in range(10):
        _offre(base.conn, f"a{i}", texte=None)

    cv_cli.cmd_sans_texte(_args_sans_texte(base, limite=3, tout=True))
    assert "autre(s)" not in capsys.readouterr().out


def test_une_base_entierement_pourvue_le_dit(base, capsys):
    _offre(base.conn, "cle-a", texte="Un texte bien réel.")
    assert cv_cli.cmd_sans_texte(_args_sans_texte(base)) == 0
    assert "Rien à signaler" in capsys.readouterr().out
