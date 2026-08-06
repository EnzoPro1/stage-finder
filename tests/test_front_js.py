"""Lance les tests JS du front depuis pytest.

La machine à états du bouton « Générer CV » vit dans
``static/cv_etats.js`` et se teste avec ``node --test``, intégré au
runtime Node : aucun ``node_modules``, aucune chaîne de build, rien
d'ajouté aux dépendances du projet.

Cette enveloppe existe pour qu'une SEULE commande couvre tout le projet.
Une suite qu'il faut penser à lancer à part est une suite qu'on oublie.

Node absent -> skip, jamais échec : il n'est pas une dépendance de
stage_finder, seulement l'outil qui sait exécuter ce fichier.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
FICHIERS_TEST = sorted((RACINE / "tests" / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node absent : la machine à états n'est pas exercée")
@pytest.mark.parametrize("fichier", FICHIERS_TEST,
                         ids=[f.name for f in FICHIERS_TEST])
def test_suite_js(fichier: Path):
    """Chaque fichier de test JS, passé par son CHEMIN.

    Pas ``node --test tests/js/`` : sous Windows, cette forme fait tenter
    à Node de charger le dossier comme un module et échoue en
    ``MODULE_NOT_FOUND``. Le chemin explicite marche partout.
    """
    resultat = subprocess.run(
        ["node", "--test", str(fichier)],
        cwd=RACINE, capture_output=True, text=True, timeout=180,
    )
    assert resultat.returncode == 0, (
        f"\n--- sortie de node --test {fichier.name} ---\n"
        f"{resultat.stdout}\n{resultat.stderr}"
    )


def test_il_y_a_bien_des_tests_js_a_lancer():
    """Sans ça, supprimer les fichiers JS rendrait la suite verte et
    silencieuse — le pire des deux mondes."""
    assert FICHIERS_TEST, "aucun tests/js/*.test.js trouvé"


def test_la_machine_a_etats_est_servie_par_flask():
    """Le fichier doit être sous `static/` : c'est de là que la page le
    charge, et Flask sert ce dossier sans configuration."""
    assert (RACINE / "static" / "cv_etats.js").is_file()
