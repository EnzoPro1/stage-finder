"""
reglages_env.py — Défauts de config.py surchargés par variables d'environnement.

Un seul mécanisme pour tous les réglages surchargeables (LLM dans `llm.py`,
chemins cv_forge dans `reglages_cv.py`) :

- les DÉFAUTS sont lus dans `config.py` au moment de l'appel ;
- chaque champ a SA variable (`SF_…`), lue dans l'environnement, lui-même
  complété par le `.env` — c'est lui, le « fichier de config » ;
- une variable vide est ignorée : une ligne `SF_X=` recopiée de
  `.env.example` sans être remplie ne doit pas écraser le défaut ;
- le tout est validé par un modèle Pydantic, et une valeur refusée lève une
  erreur qui nomme la VARIABLE fautive (ou la constante de config.py si le
  défaut lui-même est mauvais) : c'est elle que l'utilisateur doit corriger.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ValidationError

Modele = TypeVar("Modele", bound=BaseModel)


class ReglagesInvalides(ValueError):
    """Une variable d'environnement (ou une constante de config.py) est refusée."""


def _lire_dotenv() -> None:
    """Complète ``os.environ`` par le ``.env`` (sans écraser le shell).

    Isolé pour que la suite de tests puisse le neutraliser : le ``.env`` du
    poste ne doit pas décider du résultat d'un test.
    """
    from dotenv import load_dotenv

    load_dotenv()


def environnement(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """``env`` tel quel, ou ``os.environ`` après lecture du ``.env``."""
    if env is not None:
        return env
    _lire_dotenv()
    return os.environ


def surcharges(variables: Mapping[str, str], env: Mapping[str, str]) -> dict[str, str]:
    """``champ -> valeur brute`` pour chaque variable posée et non vide."""
    trouvees = {}
    for champ, variable in variables.items():
        brut = env.get(variable)
        if brut is not None and brut.strip():
            trouvees[champ] = brut.strip()
    return trouvees


def construire(
    modele: type[Modele],
    defauts: dict,
    variables: Mapping[str, str],
    env: Mapping[str, str] | None = None,
    *,
    libelle: str,
    erreur: type[ReglagesInvalides] = ReglagesInvalides,
) -> Modele:
    """Instancie ``modele`` à partir des défauts et des surcharges d'environnement."""
    env = environnement(env)
    venues_de_env = surcharges(variables, env)
    valeurs = {**defauts, **venues_de_env}
    try:
        return modele(**valeurs)
    except ValidationError as err:
        problemes = []
        for e in err.errors():
            champ = str(e["loc"][0]) if e["loc"] else "?"
            source = (variables.get(champ, champ) if champ in venues_de_env
                      else f"config.py ({champ})")
            problemes.append(f"{source} = {valeurs.get(champ)!r} : {e['msg']}")
        raise erreur(f"{libelle} invalides — " + " ; ".join(problemes)) from None
