"""
reglages_cv.py — Chemins cv_forge effectifs : master et racine de sortie.

`config.CV_MASTER_PATH` et `config.CV_OUT_ROOT` sont des chemins ABSOLUS propres
à un poste : le dépôt cv_forge est un voisin, cloné où l'on veut (`cv_forge`
ici, `cv-forge` là), et `master.yaml` n'est pas versionné (données
personnelles). Écrits en dur dans un fichier versionné, ils étaient faux sur
tout autre poste, et la seule correction possible était de modifier le code.

Ils deviennent donc des DÉFAUTS, surchargeables par variables d'environnement
(`.env` compris), exactement comme les réglages LLM (`reglages_env`) :

    SF_CV_MASTER_PATH   chemin du master.yaml
    SF_CV_OUT_ROOT      racine des CV produits
    SF_CV_LLM_MODEL, SF_CV_LLM_THINK, SF_CV_LLM_NUM_CTX, SF_CV_LLM_KEEP_ALIVE
                        modèle de l'extraction cv_forge (distinct de la vérification)

``forge_config()`` construit LA ``ForgeConfig`` du projet. Worker, app et CLI
passent tous par elle : ``config_version`` entre dans l'``offer_hash`` des
jobs, et deux ``ForgeConfig`` différentes pour la même offre donneraient deux
jobs pour un seul CV.

Les deux doivent être absolus : le worker tourne dans un thread dont le
répertoire courant n'est pas garanti, un chemin relatif y désignerait autre
chose. Refusé à la validation, avec le nom de la variable fautive.

L'EXISTENCE du master n'est PAS une condition de validité : sans master, on
collecte et on classe très bien, seule la génération de CV est impossible.
Elle est constatée au moment de générer, avec `message_master_absent`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
import llm
import reglages_env

VARIABLES_ENV: dict[str, str] = {
    "master_path": "SF_CV_MASTER_PATH",
    "out_root": "SF_CV_OUT_ROOT",
    "llm_modele": "SF_CV_LLM_MODEL",
    "llm_think": "SF_CV_LLM_THINK",
    "llm_num_ctx": "SF_CV_LLM_NUM_CTX",
    "llm_keep_alive": "SF_CV_LLM_KEEP_ALIVE",
}


class ReglagesCV(BaseModel):
    """Chemins cv_forge effectifs : défauts de config.py + surcharges d'environnement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    master_path: Path
    out_root: Path
    llm_modele: str = Field(min_length=1)
    llm_think: bool
    llm_num_ctx: int = Field(ge=512, le=131072)
    llm_keep_alive: str | int

    @field_validator("llm_keep_alive")
    @classmethod
    def _duree_ollama(cls, valeur: str | int) -> str | int:
        # Même syntaxe, même validation que keep_alive côté vérification.
        return llm.Reglages._duree_ollama(valeur)

    @field_validator("master_path", "out_root")
    @classmethod
    def _absolu(cls, chemin: Path) -> Path:
        if not chemin.is_absolute():
            raise ValueError(f"chemin absolu attendu, reçu « {chemin} »")
        return chemin


def charger_reglages(env: Mapping[str, str] | None = None) -> ReglagesCV:
    """Chemins effectifs. ``env=None`` : ``os.environ``, après lecture du ``.env``."""
    return reglages_env.construire(
        ReglagesCV,
        {"master_path": config.CV_MASTER_PATH, "out_root": config.CV_OUT_ROOT,
         "llm_modele": config.CV_LLM_MODELE, "llm_think": config.CV_LLM_THINK,
         "llm_num_ctx": config.CV_LLM_NUM_CTX, "llm_keep_alive": config.CV_LLM_KEEP_ALIVE},
        VARIABLES_ENV, env, libelle="Réglages cv_forge",
    )


def forge_config(reglages: ReglagesCV | None = None):
    """LA ``ForgeConfig`` du projet — seule surface de cv_forge avec ``generate_cv``.

    L'URL d'Ollama est celle des réglages LLM (``SF_OLLAMA_URL``), complétée
    de l'endpoint que cv_forge attend (``/api/chat``) : un seul serveur pour
    la vérification et la génération. Elle n'entre pas dans ``config_version``.
    """
    from cv_forge import ForgeConfig

    r = reglages or charger_reglages()
    return ForgeConfig(
        model=r.llm_modele,
        think=r.llm_think,
        num_ctx=r.llm_num_ctx,
        keep_alive=r.llm_keep_alive,
        ollama_url=f"{llm.charger_reglages().ollama_url}/api/chat",
    )


def master_path() -> Path:
    return charger_reglages().master_path


def out_root() -> Path:
    return charger_reglages().out_root


def message_master_absent(chemin: Path | str) -> str:
    """Où le master est attendu, et quoi changer pour le trouver."""
    return (
        f"master introuvable : attendu à « {chemin} ». Copie ton master.yaml à "
        f"cet endroit, ou pointe {VARIABLES_ENV['master_path']} (dans le .env) "
        f"vers le bon fichier — défaut : config.CV_MASTER_PATH."
    )
