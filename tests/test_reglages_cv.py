"""
Tests des chemins cv_forge surchargeables (reglages_cv.py).

Même mécanisme que les réglages LLM (reglages_env) : défauts de config.py,
surcharge SF_CV_*, validation qui nomme la variable fautive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import reglages_cv
import reglages_env
import worker


def test_defauts_de_config():
    r = reglages_cv.charger_reglages(env={})
    assert r.master_path == config.CV_MASTER_PATH
    assert r.out_root == config.CV_OUT_ROOT


def test_surcharge_par_variables(tmp_path):
    master = tmp_path / "data" / "master.yaml"
    r = reglages_cv.charger_reglages(env={
        "SF_CV_MASTER_PATH": str(master), "SF_CV_OUT_ROOT": str(tmp_path / "out"),
    })
    assert r.master_path == master
    assert r.out_root == tmp_path / "out"
    assert isinstance(r.out_root / "x", Path)  # le geste qui avait planté


def test_variable_vide_ignoree():
    r = reglages_cv.charger_reglages(env={"SF_CV_MASTER_PATH": " "})
    assert r.master_path == config.CV_MASTER_PATH


@pytest.mark.parametrize("variable", ["SF_CV_MASTER_PATH", "SF_CV_OUT_ROOT"])
def test_chemin_relatif_refuse_en_nommant_la_variable(variable):
    with pytest.raises(reglages_env.ReglagesInvalides) as exc:
        reglages_cv.charger_reglages(env={variable: r"data\master.yaml"})
    assert variable in str(exc.value) and "absolu" in str(exc.value)


def test_lit_l_environnement(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_CV_MASTER_PATH", str(tmp_path / "m.yaml"))
    assert reglages_cv.master_path() == tmp_path / "m.yaml"


def test_message_master_absent_dit_ou_et_quoi_changer(tmp_path):
    chemin = tmp_path / "nulle-part.yaml"
    message = reglages_cv.message_master_absent(chemin)
    assert str(chemin) in message
    assert "SF_CV_MASTER_PATH" in message and ".env" in message


def test_worker_suit_les_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("SF_CV_MASTER_PATH", str(tmp_path / "m.yaml"))
    monkeypatch.setenv("SF_CV_OUT_ROOT", str(tmp_path / "out"))
    w = worker.Worker(db_path=str(tmp_path / "x.db"))
    assert w.master_path == tmp_path / "m.yaml"
    assert w.out_root == tmp_path / "out"


# ---------------------------------------------------------------------------
# Modèle de l'extraction cv_forge : distinct de la vérification
# ---------------------------------------------------------------------------
def test_forge_config_par_defaut():
    f = reglages_cv.forge_config()
    assert (f.model, f.think, f.num_ctx, f.keep_alive) == ("qwen3:4b", True, 8192, "10m")
    assert f.ollama_url == "http://localhost:11434/api/chat"


def test_la_verification_ne_change_pas_le_modele_des_cv(monkeypatch):
    avant = reglages_cv.forge_config().config_version
    monkeypatch.setenv("SF_LLM_MODEL", "gemma3:4b")
    monkeypatch.setenv("SF_LLM_THINK", "false")
    monkeypatch.setenv("SF_LLM_NUM_CTX", "4096")
    assert reglages_cv.forge_config().config_version == avant


def test_les_cv_ne_changent_pas_la_verification(monkeypatch):
    import llm

    monkeypatch.setenv("SF_CV_LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("SF_CV_LLM_THINK", "false")
    assert reglages_cv.forge_config().model == "qwen3:8b"
    assert reglages_cv.forge_config().think is False
    r = llm.charger_reglages()
    assert r.modele == "qwen3:4b" and r.think is False


def test_url_d_ollama_partagee(monkeypatch):
    monkeypatch.setenv("SF_OLLAMA_URL", "http://gpu:11434/")
    assert reglages_cv.forge_config().ollama_url == "http://gpu:11434/api/chat"


@pytest.mark.parametrize("brut, attendu", [("300", 300), ("30m", "30m")])
def test_keep_alive_cv(monkeypatch, brut, attendu):
    monkeypatch.setenv("SF_CV_LLM_KEEP_ALIVE", brut)
    assert reglages_cv.forge_config().keep_alive == attendu


def test_keep_alive_cv_invalide_nomme_la_variable():
    with pytest.raises(reglages_env.ReglagesInvalides, match="SF_CV_LLM_KEEP_ALIVE"):
        reglages_cv.charger_reglages(env={"SF_CV_LLM_KEEP_ALIVE": "dix minutes"})


def test_une_seule_forge_config_pour_tous(tmp_path):
    """Worker et demandes (app, CLI) : même config_version, donc même offer_hash."""
    w = worker.Worker(db_path=str(tmp_path / "x.db"))
    assert w.config_forge().config_version == reglages_cv.forge_config().config_version
