"""
Tests des réglages LLM centralisés (llm.charger_reglages).

Aucun appel réseau : on ne teste ici que la résolution des réglages —
défauts de config.py, surcharges par variables d'environnement, refus
explicite d'une valeur invalide.
"""

from __future__ import annotations

import pytest

import config
import llm


def test_defauts_attendus():
    r = llm.charger_reglages(env={})
    assert r.modele == "qwen3:4b"
    assert r.think is False
    assert r.temperature == 0
    assert r.num_ctx == 8192
    assert r.top_n == 30
    assert r.keep_alive == "10m"


def test_les_defauts_viennent_de_config(monkeypatch):
    monkeypatch.setattr(config, "LLM_NUM_CTX", 4096)
    monkeypatch.setattr(config, "VERIFY_MODEL", "autre:1b")
    r = llm.charger_reglages(env={})
    assert r.num_ctx == 4096
    assert r.modele == "autre:1b"


def test_chaque_champ_a_sa_variable():
    assert set(llm.VARIABLES_ENV) == set(llm.Reglages.model_fields)


@pytest.mark.parametrize("variable, brut, champ, attendu", [
    ("SF_LLM_MODEL", "gemma3:4b", "modele", "gemma3:4b"),
    ("SF_LLM_NUM_CTX", "4096", "num_ctx", 4096),
    ("SF_LLM_THINK", "true", "think", True),
    ("SF_LLM_TEMPERATURE", "0.2", "temperature", 0.2),
    ("SF_LLM_TOP_N", "12", "top_n", 12),
    ("SF_LLM_KEEP_ALIVE", "30m", "keep_alive", "30m"),
    ("SF_OLLAMA_URL", "http://gpu:11434/", "ollama_url", "http://gpu:11434"),
])
def test_surcharge_par_variable(variable, brut, champ, attendu):
    r = llm.charger_reglages(env={variable: brut})
    assert getattr(r, champ) == attendu


@pytest.mark.parametrize("brut, attendu", [("300", 300), ("-1", -1), ("0", 0)])
def test_keep_alive_numerique_part_en_entier(brut, attendu):
    # Ollama refuse une CHAÎNE « 300 » sans unité : elle doit partir en entier.
    assert llm.charger_reglages(env={"SF_LLM_KEEP_ALIVE": brut}).keep_alive == attendu


def test_variable_vide_ignoree():
    # Ligne « SF_LLM_MODEL= » recopiée de .env.example sans être remplie.
    assert llm.charger_reglages(env={"SF_LLM_MODEL": "  "}).modele == "qwen3:4b"


def test_variables_etrangeres_ignorees():
    r = llm.charger_reglages(env={"PATH": "x", "SF_AUTRE": "y"})
    assert r == llm.charger_reglages(env={})


def test_lit_os_environ_par_defaut(monkeypatch):
    monkeypatch.setenv("SF_LLM_TOP_N", "7")
    assert llm.charger_reglages().top_n == 7


@pytest.mark.parametrize("variable, brut", [
    ("SF_LLM_NUM_CTX", "beaucoup"),
    ("SF_LLM_TEMPERATURE", "5"),
    ("SF_LLM_TOP_N", "0"),
    ("SF_LLM_THINK", "peut-être"),
    ("SF_LLM_KEEP_ALIVE", "dix minutes"),
])
def test_valeur_invalide_nomme_la_variable(variable, brut):
    with pytest.raises(llm.ReglagesInvalides) as exc:
        llm.charger_reglages(env={variable: brut})
    assert variable in str(exc.value)


def test_constante_invalide_nomme_config(monkeypatch):
    monkeypatch.setattr(config, "LLM_NUM_CTX", 10)
    with pytest.raises(llm.ReglagesInvalides) as exc:
        llm.charger_reglages(env={})
    assert "config.py (num_ctx)" in str(exc.value)


def test_reglages_figes():
    r = llm.charger_reglages(env={})
    with pytest.raises(Exception):
        r.modele = "autre"


def test_options_de_generation():
    r = llm.charger_reglages(env={})
    assert r.options() == {"temperature": 0.0, "num_ctx": 8192,
                           "num_predict": config.VERIFY_MAX_TOKENS}
    assert r.options(num_predict=1000)["num_predict"] == 1000


def test_plus_de_variable_pour_le_modele_d_embeddings():
    # Une seule source de vérité : config.MODELE_EMBEDDING (estampillé).
    assert "SF_EMBED_MODEL" not in llm.VARIABLES_ENV.values()


@pytest.mark.parametrize("valeur, attendu", [
    ("ollama:bge-m3", "bge-m3"),
    ("paraphrase-multilingual-MiniLM-L12-v2", None),
])
def test_modele_embedding_ollama(monkeypatch, valeur, attendu):
    monkeypatch.setattr(config, "MODELE_EMBEDDING", valeur)
    assert llm.modele_embedding_ollama() == attendu
