"""Les valeurs de `config.py` sont-elles UTILISABLES telles quelles ?

# Le trou que ce fichier bouche

Tous les autres tests injectent leur configuration. C'est ce qu'il faut
pour tester une logique, mais ça rend un défaut entier invisible : si une
constante de `config.py` a le mauvais TYPE, aucun test ne la touche, la
suite reste verte, et la panne n'apparaît qu'à la première exécution
réelle.

C'est exactement ce qui s'est produit. `CV_OUT_ROOT` était exporté en
`str`, `worker.py` ne convertissait que la branche injectée, et le premier
vrai `travailler --une-passe` a rendu :

    TypeError: unsupported operand type(s) for /: 'str' and 'str'

Le filet `INTERNAL_ERROR` a tenu — le worker n'est pas mort — mais 266
tests verts n'avaient rien vu.

**Aucun test de ce fichier ne doit rien mocker de `config`.** C'est sa
raison d'être : ce qui est vérifié ici, c'est la configuration RÉELLE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import reglages_cv
import worker

# La configuration RÉELLE, .env compris : c'est le sujet de ce fichier.
pytestmark = pytest.mark.config_reelle


# =====================================================================
# Types des constantes
# =====================================================================
@pytest.mark.parametrize("nom", ["CV_MASTER_PATH", "CV_OUT_ROOT"])
def test_les_chemins_sont_des_Path(nom):
    """Un `str` ici marche partout SAUF là où on écrit `chemin / "x"`,
    c'est-à-dire précisément là où on s'en sert."""
    valeur = getattr(config, nom)
    assert isinstance(valeur, Path), (
        f"config.{nom} est un {type(valeur).__name__}. Les constantes de "
        f"chemin doivent porter leur vrai type : un `str` oblige chaque "
        f"consommateur à se souvenir de le convertir, et il suffit d'un oubli."
    )


@pytest.mark.parametrize("nom", ["CV_MASTER_PATH", "CV_OUT_ROOT"])
def test_les_chemins_supportent_l_operateur_de_composition(nom):
    """Le geste exact qui avait planté."""
    assert isinstance(getattr(config, nom) / "sous-element", Path)


@pytest.mark.parametrize("nom", ["CV_MASTER_PATH", "CV_OUT_ROOT"])
def test_les_chemins_sont_absolus(nom):
    """Le worker tourne dans un thread dont le répertoire courant n'est
    pas garanti — un chemin relatif y désignerait autre chose."""
    assert getattr(config, nom).is_absolute()


def test_les_chemins_effectifs_sont_valides():
    """Défauts de config.py APRÈS surcharge par SF_CV_* : c'est ce que le
    worker utilisera. Une variable mal remplie lève en la nommant."""
    r = reglages_cv.charger_reglages()
    assert r.master_path.is_absolute() and r.out_root.is_absolute()


@pytest.mark.cv_forge
def test_le_master_existe_sur_le_disque():
    """Sans lui, chaque génération échouerait en MASTER_INVALID — proprement,
    mais toutes. Autant le savoir ici. On vérifie le chemin EFFECTIF : celui
    du .env s'il y en a un, sinon le défaut de config.py.

    Sauté sans cv_forge installé (clone public, CI) : la génération de CV
    y est indisponible de toute façon, et il n'y a pas de master à trouver."""
    chemin = reglages_cv.master_path()
    assert chemin.is_file(), reglages_cv.message_master_absent(chemin)


# Marqueurs de nom trahissant une constante de CHEMIN. Les français sont
# là parce que `config.py` nomme en français : la première version du
# balayage ne portait que les anglais et n'aurait donc attrapé aucune
# constante de ce fichier — pas même `CHEMIN_BASE`. Un futur
# `CHEMIN_SORTIE_CV = "..."` serait passé au travers, c'est-à-dire
# exactement le défaut que ce test existe pour empêcher de revenir.
MARQUEURS_CHEMIN = ("_path", "_dir", "_root", "_file",
                    "chemin", "dossier", "fichier")

# `CHEMIN_BASE` est légitimement une `str` : elle part directement dans
# `sqlite3.connect()`, qui en prend une, et elle vaut `":memory:"` dans
# les tests — ce qui n'est pas un chemin du tout et qu'un `Path` abîmerait.
# L'exemption est NOMMÉE plutôt que déduite : une exception qu'on ne voit
# pas dans le code est une exception qui s'étend toute seule.
CHEMINS_EXEMPTES = {"CHEMIN_BASE"}


def test_aucune_constante_de_chemin_n_est_restee_en_str():
    """Balayage : attrape la PROCHAINE constante ajoutée en `str`.

    Les deux corrigées ne sont pas forcément les dernières ; ce test vaut
    pour celles qui n'existent pas encore.
    """
    fautives = []
    for nom in dir(config):
        if nom.startswith("_") or nom in CHEMINS_EXEMPTES:
            continue
        valeur = getattr(config, nom)
        if not isinstance(valeur, str):
            continue
        if any(marqueur in nom.lower() for marqueur in MARQUEURS_CHEMIN):
            fautives.append(f"{nom} = {valeur!r}")
    assert not fautives, (
        "constante(s) de chemin exportée(s) en str : " + ", ".join(fautives))


def test_le_balayage_attraperait_une_constante_francaise():
    """Le balayage se vérifie lui-même : sans ce test, élargir les
    marqueurs serait un changement dont rien ne prouve l'effet."""
    faux_config = type("FauxConfig", (), {
        "CHEMIN_SORTIE_CV": "C:/quelque/part",
        "DOSSIER_RAPPORTS": "rapports",
        "FICHIER_JOURNAL": "app.log",
        "VERIFY_TOP_N": 10,
    })
    attrapes = [
        nom for nom in dir(faux_config)
        if not nom.startswith("_")
        and nom not in CHEMINS_EXEMPTES
        and isinstance(getattr(faux_config, nom), str)
        and any(m in nom.lower() for m in MARQUEURS_CHEMIN)
    ]
    assert sorted(attrapes) == ["CHEMIN_SORTIE_CV", "DOSSIER_RAPPORTS",
                                "FICHIER_JOURNAL"]


def test_les_reglages_numeriques_sont_numeriques():
    """Même défaut, autre type : un `"30"` au lieu de `30` passerait les
    comparaisons de config et casserait au premier calcul."""
    attendus = {
        "VERIFY_TOP_N": int, "VERIFY_TIMEOUT_S": (int, float),
        "VERIFY_MAX_TOKENS": int, "VERIFY_SCORE_WEIGHT": (int, float),
        "VERIFY_MIN_SCORE": (int, float), "RESULTATS_PAR_TERME": int,
    }
    faux = [
        f"{nom} est {type(getattr(config, nom)).__name__}"
        for nom, types in attendus.items()
        if hasattr(config, nom) and not isinstance(getattr(config, nom), types)
    ]
    assert not faux, "réglage(s) mal typé(s) : " + ", ".join(faux)


# =====================================================================
# Le worker construit avec la config réelle
# =====================================================================
def test_un_worker_sans_argument_a_des_chemins_utilisables():
    """Le constructeur par DÉFAUT — celui qu'utilise `cv_cli travailler`,
    et le seul que les tests injectés n'exerçaient jamais."""
    w = worker.Worker()
    assert isinstance(w.master_path, Path)
    assert isinstance(w.out_root, Path)
    assert isinstance(w.out_root / "cle-quelconque", Path)


def test_un_worker_accepte_aussi_des_str_injectes(tmp_path):
    """L'inverse doit rester vrai : un appelant qui passe une chaîne ne
    doit pas rouvrir le même trou."""
    w = worker.Worker(master_path=str(tmp_path / "m.yaml"),
                      out_root=str(tmp_path / "out"))
    assert isinstance(w.master_path, Path)
    assert isinstance(w.out_root, Path)


def test_le_dossier_de_sortie_se_compose_comme_dans_le_pont():
    """Reproduit littéralement `worker._appel_generate_cv` sans rien mocker
    de la config. C'est la ligne qui avait levé."""
    w = worker.Worker()
    cle = "146487d7a2ce27b9a51fbf675f8e9d87de06d7b3"
    sortie = w.out_root / cle[:16]
    assert isinstance(sortie, Path)
    assert sortie.name == cle[:16]
    assert sortie.parent == w.out_root
