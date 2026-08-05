"""Préparation de l'environnement d'embeddings (TLS + hors ligne).

Régression du premier run réel : le worker chargeait l'encodeur sans
`truststore`, la requête vers Hugging Face échouait en
CERTIFICATE_VERIFY_FAILED derrière l'interception TLS de la machine, et
la génération mourait au matching — après dix minutes d'extraction déjà
payées.
"""

from __future__ import annotations

import pytest

import embeddings_env


@pytest.fixture(autouse=True)
def env_propre(monkeypatch):
    """Chaque test part d'un environnement sans réglage hors ligne."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)


def test_truststore_est_active():
    """Le correctif de fond : sans lui, tout HTTPS échoue derrière un
    antivirus ou un proxy qui intercepte le TLS."""
    assert embeddings_env.activer_truststore() is True


def test_un_modele_en_cache_bascule_en_hors_ligne(monkeypatch):
    monkeypatch.setattr(embeddings_env, "modele_en_cache", lambda nom: True)
    etat = embeddings_env.preparer("un-modele")
    assert etat["hors_ligne"] is True
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_un_modele_absent_laisse_le_reseau_ouvert(monkeypatch):
    """Un premier téléchargement doit rester possible : forcer le hors
    ligne transformerait une installation neuve en panne."""
    monkeypatch.setattr(embeddings_env, "modele_en_cache", lambda nom: False)
    etat = embeddings_env.preparer("un-modele")
    assert etat["hors_ligne"] is False
    import os
    assert "HF_HUB_OFFLINE" not in os.environ


def test_un_reglage_explicite_de_l_environnement_l_emporte(monkeypatch):
    """Une heuristique ne doit jamais écraser une décision explicite."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr(embeddings_env, "modele_en_cache", lambda nom: True)
    etat = embeddings_env.preparer("un-modele")
    assert etat["impose_par_l_environnement"] is True
    assert etat["hors_ligne"] is False
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "0"


def test_preparer_est_idempotente(monkeypatch):
    monkeypatch.setattr(embeddings_env, "modele_en_cache", lambda nom: True)
    a = embeddings_env.preparer("un-modele")
    b = embeddings_env.preparer("un-modele")
    assert a["hors_ligne"] == b["hors_ligne"] is True


def test_preparer_n_importe_PAS_huggingface_hub():
    """Régression, et le piège était subtil.

    `huggingface_hub` recopie `HF_HUB_OFFLINE` dans une constante de module
    À SON IMPORT, et ne la relit jamais. La première version sondait le
    cache via `try_to_load_from_cache` — donc importait la lib — donc posait
    la variable TROP TARD : le mode hors ligne n'avait aucun effet.

    Le run passait quand même, grâce à `truststore` seul, en interrogeant
    le réseau à chaque génération. Un correctif qui ne corrigeait rien, et
    que rien ne signalait.
    """
    code = (
        "import sys; import embeddings_env; embeddings_env.preparer(); "
        "print(int('huggingface_hub' in sys.modules))"
    )
    import subprocess
    import sys as _sys

    out = subprocess.run([_sys.executable, "-c", code],
                         capture_output=True, text=True, cwd=".")
    assert out.stdout.strip() == "0", (
        "preparer() importe huggingface_hub : la constante HF_HUB_OFFLINE "
        f"sera figée avant notre réglage. stderr={out.stderr[-300:]}"
    )


def test_le_reglage_prend_effet_a_l_import_ulterieur():
    """Le seul critère qui compte : la constante que la lib lira vraiment."""
    code = (
        "import embeddings_env; embeddings_env.preparer(); "
        "import huggingface_hub.constants as k; print(int(k.HF_HUB_OFFLINE))"
    )
    import subprocess
    import sys as _sys

    out = subprocess.run([_sys.executable, "-c", code],
                         capture_output=True, text=True, cwd=".")
    assert out.stdout.strip() == "1", (
        f"HF_HUB_OFFLINE inopérant. stderr={out.stderr[-300:]}")


def test_la_sonde_de_cache_lit_le_disque(tmp_path, monkeypatch):
    """Disposition stable : <hub>/models--<org>--<nom>/snapshots/<rev>/config.json"""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert embeddings_env.modele_en_cache("un-modele") is False

    snap = tmp_path / "models--sentence-transformers--un-modele" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert embeddings_env.modele_en_cache("un-modele") is True


def test_la_sonde_accepte_un_depot_qualifie(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    snap = tmp_path / "models--une-org--un-modele" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    assert embeddings_env.modele_en_cache("une-org/un-modele") is True


def test_le_modele_reel_est_bien_en_cache():
    """Sans mock. Le ranker de stage_finder l'a téléchargé : le worker ne
    doit donc jamais avoir besoin du réseau pour générer un CV."""
    from cv_forge.embed import DEFAULT_EMBED_MODEL

    assert embeddings_env.modele_en_cache(DEFAULT_EMBED_MODEL), (
        f"« {DEFAULT_EMBED_MODEL} » absent du cache Hugging Face. Le worker "
        f"tentera un téléchargement à la première génération."
    )


def test_un_modele_inconnu_n_est_pas_declare_en_cache():
    assert embeddings_env.modele_en_cache("modele-qui-n-existe-pas-du-tout") is False


def test_preparer_sans_argument_vise_le_modele_de_cv_forge(monkeypatch):
    from cv_forge.embed import DEFAULT_EMBED_MODEL

    monkeypatch.setattr(embeddings_env, "modele_en_cache", lambda nom: True)
    assert embeddings_env.preparer()["modele"] == DEFAULT_EMBED_MODEL
