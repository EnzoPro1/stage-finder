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
