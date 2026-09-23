"""Tests des familles de requêtes (recherche.yaml / recherche.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import recherche
from config_schema import valider_config


def _ecrire(tmp_path, contenu: str) -> str:
    chemin = tmp_path / "recherche.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    return str(chemin)


# ---------------------------------------------------------------------------
# Le fichier du dépôt
# ---------------------------------------------------------------------------
def test_fichier_du_depot_est_valide():
    r = recherche.lire(recherche.CHEMIN_RECHERCHE)
    assert r.noms_familles() == ["ai_engineering", "ml", "mlops", "inference", "cyber"]


@pytest.mark.parametrize("famille, termes", [
    ("ai_engineering", ["AI engineer", "ingénieur IA", "LLM", "GenAI", "agents IA",
                        "AI agents"]),
    ("ml", ["machine learning", "ingénieur machine learning", "deep learning",
            "data scientist", "NLP", "computer vision", "applied scientist",
            "research engineer"]),
    ("mlops", ["MLOps", "LLMOps", "ML platform", "ML infrastructure"]),
    ("inference", ["inference optimization", "model serving", "vLLM",
                   "quantization", "GPU"]),
    ("cyber", ["cybersécurité", "AI security"]),
])
def test_les_termes_demandes_sont_presents(famille, termes):
    presents = recherche.lire(recherche.CHEMIN_RECHERCHE).familles[famille].termes()
    for terme in termes:
        assert terme in presents


def test_agents_n_est_interroge_qu_en_phrase():
    """« agents » seul attrape « agent de sécurité » ; seules les phrases restent."""
    termes = recherche.lire(recherche.CHEMIN_RECHERCHE).termes()
    assert "agents" not in {t.terme.casefold() for t in termes}


def test_chaque_famille_a_du_francais_et_de_l_anglais():
    for nom, famille in recherche.lire(recherche.CHEMIN_RECHERCHE).familles.items():
        assert famille.fr and famille.en, nom


# ---------------------------------------------------------------------------
# Termes interrogés : une fois chacun, avec toutes leurs familles
# ---------------------------------------------------------------------------
def test_un_terme_ecrit_dans_deux_langues_n_est_interroge_qu_une_fois(tmp_path):
    r = recherche.lire(_ecrire(tmp_path, """
familles:
  mlops:
    fr: ["MLOps"]
    en: ["mlops", "ML platform"]
"""))
    assert [t.terme for t in r.termes()] == ["MLOps", "ML platform"]


def test_un_terme_partage_porte_toutes_ses_familles(tmp_path):
    r = recherche.lire(_ecrire(tmp_path, """
familles:
  ai_engineering:
    en: ["LLM", "AI engineer"]
  mlops:
    en: ["LLMOps", "llm"]
"""))
    par_terme = {t.terme: t.familles for t in r.termes()}
    assert par_terme == {
        "LLM": ("ai_engineering", "mlops"),
        "AI engineer": ("ai_engineering",),
        "LLMOps": ("mlops",),
    }


def test_l_ordre_des_termes_est_deterministe():
    a = recherche.lire(recherche.CHEMIN_RECHERCHE).termes()
    b = recherche.lire(recherche.CHEMIN_RECHERCHE).termes()
    assert a == b


# ---------------------------------------------------------------------------
# Validation (fail-fast)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("contenu", [
    "familles: {}",                                   # aucune famille
    "familles:\n  ml: {}",                            # famille sans terme
    "familles:\n  ml:\n    fr: ['  ']",               # terme vide
    "familles:\n  Machine Learning:\n    fr: [x]",    # nom invalide
    "familles:\n  ml:\n    de: [x]",                  # langue inconnue
    "familles:\n  ml:\n    fr: [x]\ninconnu: 1",      # clé racine inconnue
])
def test_contenu_invalide_rejete(tmp_path, contenu):
    with pytest.raises(ValidationError):
        recherche.lire(_ecrire(tmp_path, contenu))


def test_racine_non_dictionnaire_rejetee(tmp_path):
    with pytest.raises(ValueError):
        recherche.lire(_ecrire(tmp_path, "- une liste"))


def test_yaml_illisible_rejete(tmp_path):
    with pytest.raises(ValueError):
        recherche.lire(_ecrire(tmp_path, "familles: [non fermé"))


def test_fichier_absent(tmp_path):
    with pytest.raises(FileNotFoundError):
        recherche.lire(str(tmp_path / "absent.yaml"))


def test_valider_config_refuse_une_recherche_invalide(tmp_path, monkeypatch):
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", _ecrire(tmp_path, "familles: {}"))
    with pytest.raises(ValidationError):
        valider_config()


def test_charger_suit_le_chemin_configure(tmp_path, monkeypatch):
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", _ecrire(tmp_path, """
familles:
  seule:
    en: ["x"]
"""))
    assert recherche.charger().noms_familles() == ["seule"]


# ---------------------------------------------------------------------------
# Surcharges locales (recherche.local.yaml)
# ---------------------------------------------------------------------------
_AVEC_JOBS = """
familles:
  seule:
    en: ["x"]
student_jobs:
  rayon_km: 30
  origines:
    ville_a: {libelle: "Versailles", commune: "Versailles", insee: "78646", code_postal: "78000"}
  termes: ["vendeur"]
  trajet: {seuil_minutes: 45, facteur_trafic: 1.3, facteur_detour: 1.3, vitesse_estimation_kmh: 50}
"""


def test_la_surcharge_locale_remplace_les_origines_et_garde_le_reste(tmp_path, monkeypatch):
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", _ecrire(tmp_path, _AVEC_JOBS))
    locale = tmp_path / "recherche.local.yaml"
    locale.write_text("""
student_jobs:
  origines:
    maison: {libelle: "Meaux", commune: "Meaux", insee: "77284", code_postal: "77100"}
""", encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE_LOCALE", str(locale))

    r = recherche.charger()
    assert list(r.student_jobs.origines) == ["maison"]
    assert r.student_jobs.termes == ["vendeur"] and r.student_jobs.rayon_km == 30
    assert r.noms_familles() == ["seule"]


def test_sans_surcharge_locale_le_depot_fait_foi(tmp_path, monkeypatch):
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", _ecrire(tmp_path, _AVEC_JOBS))
    assert list(recherche.charger().student_jobs.origines) == ["ville_a"]


def test_une_surcharge_locale_invalide_est_rejetee(tmp_path, monkeypatch):
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", _ecrire(tmp_path, _AVEC_JOBS))
    locale = tmp_path / "recherche.local.yaml"
    locale.write_text("student_jobs:\n  rayon_km: -3\n", encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE_LOCALE", str(locale))
    with pytest.raises(ValidationError):
        valider_config()
