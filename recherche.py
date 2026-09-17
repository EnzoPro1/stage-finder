"""
recherche.py — Les familles de requêtes de stage, lues et validées depuis
``recherche.yaml``.

## Pourquoi un fichier YAML, et pas config.py

`config.TERMES_RECHERCHE` était UNE liste globale de cinq phrases, envoyée
telle quelle à toutes les sources. Deux défauts en sortaient :

- les moteurs n'ont pas la même grammaire. Adzuna et France Travail sont
  conjonctifs : « stage intelligence artificielle » exige les trois mots et
  rapporte 0. Careerjet sait faire un OU. Une liste unique ne peut pas être
  bien écrite pour les deux ;
- rien ne disait QUELLE requête avait trouvé une offre. Impossible, donc, de
  lire un rapport par domaine.

Le fichier porte des TERMES COURTS groupés par FAMILLE ; chaque source compose
sa propre requête à partir d'eux, et chaque offre garde la trace des familles
qui l'ont fait remonter.

## Deux vues d'un même contenu

- ``Recherche.familles`` : ce qui est écrit, famille par famille ;
- ``Recherche.termes()`` : ce qui est INTERROGÉ, un terme par ligne avec toutes
  ses familles. « LLM » est écrit dans deux langues et pourrait l'être dans
  deux familles : une source par terme ne doit l'interroger qu'une fois, et
  l'offre trouvée doit porter les deux étiquettes.

Validation stricte (fail-fast), comme `config_schema` : une famille vide, un
nom de famille invalide ou une clé inconnue arrêtent le programme au démarrage
plutôt que de faire tourner une collecte amputée sans le dire.

Utilisation :
    python recherche.py      # valide le fichier et affiche les termes
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Résolu par rapport à CE FICHIER, pas au CWD : même raison que
# `communes.CHEMIN_REFERENTIEL` — lu par du code de bibliothèque.
CHEMIN_RECHERCHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "recherche.yaml")

# Nom de famille : identifiant stable, réutilisé comme étiquette, dans la base
# et dans le rapport. Pas d'espace, pas de majuscule.
_MOTIF_NOM_FAMILLE = re.compile(r"^[a-z][a-z0-9_]*$")


class Famille(BaseModel):
    """Les termes d'un domaine, par langue."""

    model_config = ConfigDict(extra="forbid")

    fr: list[str] = Field(default_factory=list)
    en: list[str] = Field(default_factory=list)

    @field_validator("fr", "en")
    @classmethod
    def _termes_non_vides(cls, termes: list[str]) -> list[str]:
        propres = [t.strip() for t in termes]
        if any(not t for t in propres):
            raise ValueError("terme vide")
        return propres

    @model_validator(mode="after")
    def _au_moins_un_terme(self) -> "Famille":
        if not self.fr and not self.en:
            raise ValueError("famille sans aucun terme")
        return self

    def termes(self) -> list[str]:
        """Termes de la famille, français puis anglais, sans doublon (casse ignorée)."""
        vus: set[str] = set()
        uniques: list[str] = []
        for terme in self.fr + self.en:
            cle = terme.casefold()
            if cle not in vus:
                vus.add(cle)
                uniques.append(terme)
        return uniques


@dataclass(frozen=True)
class TermeInterroge:
    """Un terme à interroger UNE fois, et toutes les familles qu'il sert."""

    terme: str
    familles: tuple[str, ...]


def slug(terme: str) -> str:
    """« Hôte de caisse » -> « hote_de_caisse » : un terme devenu nom d'étiquette."""
    t = unicodedata.normalize("NFKD", terme.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "_".join(re.findall(r"[a-z0-9]+", t))


class Origine(BaseModel):
    """Un point de départ des jobs étudiants, sous les formes que les sources comprennent."""

    model_config = ConfigDict(extra="forbid")

    libelle: str = Field(min_length=1)
    commune: str = Field(min_length=1)
    insee: str = Field(pattern=r"^(\d{5}|2[AB]\d{3})$")
    code_postal: str = Field(pattern=r"^\d{5}$")


# Étiquette de l'unique requête SANS mot-clé (France Travail, temps partiel
# structuré). Ce n'est pas un terme : elle dit quelle requête a trouvé l'offre.
ETIQUETTE_SANS_MOT_CLE = "temps_partiel_sans_mot_cle"

# Étiquette d'une offre rapportée par une requête OU dont aucun terme n'est
# retrouvé dans le texte livré (extrait tronqué, forme fléchie : « vendeuse »
# n'est pas « vendeur »). Elle dit « trouvée, sans savoir par quel terme ».
ETIQUETTE_NON_RETROUVE = "terme_non_retrouve"


class JobsEtudiants(BaseModel):
    """Bloc ``student_jobs`` : origines, rayon, termes."""

    model_config = ConfigDict(extra="forbid")

    rayon_km: int = Field(gt=0, le=100)
    origines: dict[str, Origine] = Field(min_length=1)
    termes: list[str] = Field(min_length=1)

    @field_validator("origines")
    @classmethod
    def _noms_d_origine(cls, origines: dict[str, Origine]) -> dict[str, Origine]:
        invalides = [nom for nom in origines if not _MOTIF_NOM_FAMILLE.match(nom)]
        if invalides:
            raise ValueError("nom(s) d'origine invalide(s) : " + ", ".join(invalides))
        return origines

    @field_validator("termes")
    @classmethod
    def _termes_distincts(cls, termes: list[str]) -> list[str]:
        propres = [t.strip() for t in termes]
        if any(not t for t in propres):
            raise ValueError("terme vide")
        vus: dict[str, str] = {}
        for terme in propres:
            nom = slug(terme)
            if not nom or not _MOTIF_NOM_FAMILLE.match(nom):
                raise ValueError(f"terme sans étiquette possible : « {terme} »")
            if nom in vus:
                raise ValueError(f"« {terme} » et « {vus[nom]} » donnent la même étiquette « {nom} »")
            if nom in (ETIQUETTE_SANS_MOT_CLE, ETIQUETTE_NON_RETROUVE):
                raise ValueError(f"« {terme} » prend l'étiquette réservée {nom}")
            vus[nom] = terme
        return propres

    def familles(self) -> dict[str, Famille]:
        """Chaque terme comme une famille à un terme, nommée par son étiquette.

        Toute la mécanique des familles de stage — provenance, familles-titre,
        bilan par famille — s'applique ainsi telle quelle aux jobs étudiants.
        """
        return {slug(t): Famille(fr=[t]) for t in self.termes}


class Recherche(BaseModel):
    """Contenu validé de ``recherche.yaml``."""

    model_config = ConfigDict(extra="forbid")

    familles: dict[str, Famille] = Field(min_length=1)
    student_jobs: JobsEtudiants | None = None

    @model_validator(mode="after")
    def _etiquettes_disjointes(self) -> "Recherche":
        """Une étiquette de job étudiant ne doit pas porter le nom d'une famille de stage."""
        if self.student_jobs:
            communes = set(self.familles) & set(self.student_jobs.familles())
            if communes:
                raise ValueError("étiquettes à la fois famille de stage et terme de job "
                                 "étudiant : " + ", ".join(sorted(communes)))
        return self

    def toutes_familles(self) -> dict[str, Famille]:
        """Familles de stage et étiquettes de jobs étudiants, pour les retrouver par nom."""
        toutes = dict(self.familles)
        if self.student_jobs:
            toutes.update(self.student_jobs.familles())
        return toutes

    @field_validator("familles")
    @classmethod
    def _noms_valides(cls, familles: dict[str, Famille]) -> dict[str, Famille]:
        invalides = [nom for nom in familles if not _MOTIF_NOM_FAMILLE.match(nom)]
        if invalides:
            raise ValueError(
                "nom(s) de famille invalide(s) : " + ", ".join(invalides)
                + " (attendu : minuscules, chiffres, _)"
            )
        return familles

    def noms_familles(self) -> list[str]:
        """Noms des familles, dans l'ordre du fichier."""
        return list(self.familles)

    def termes(self) -> list[TermeInterroge]:
        """Chaque terme distinct (casse ignorée), avec les familles qui l'écrivent.

        L'ordre suit le fichier (première apparition) : une source par terme
        interroge dans le même ordre d'un run à l'autre.
        """
        ordre: list[str] = []
        premier: dict[str, str] = {}
        familles_de: dict[str, list[str]] = {}
        for nom, famille in self.familles.items():
            for terme in famille.termes():
                cle = terme.casefold()
                if cle not in premier:
                    premier[cle] = terme
                    ordre.append(cle)
                    familles_de[cle] = []
                if nom not in familles_de[cle]:
                    familles_de[cle].append(nom)
        return [TermeInterroge(premier[c], tuple(familles_de[c])) for c in ordre]


# ---------------------------------------------------------------------------
# Composer une requête dans la grammaire de chaque moteur
# ---------------------------------------------------------------------------
# Ce qui est LU ici vient des sondes du 2026-09-17 (cf. l'en-tête de
# recherche.yaml). Chaque source choisit la forme que son moteur comprend :
#
#   expression_ou   Careerjet, Indeed, LinkedIn — OU, guillemets, parenthèses
#   mots_isoles     Adzuna — `what_or` ne connaît que des mots
#   termes()        France Travail — conjonctif, donc un appel par terme


def _entre_guillemets(terme: str) -> str:
    """Un terme de plusieurs mots devient une phrase ; un mot seul reste nu."""
    propre = terme.replace('"', " ").strip()
    return f'"{propre}"' if re.search(r"[\s'’-]", propre) else propre


def expression_ou(termes: list[str]) -> str:
    """« (a OR "b c") » — un OU de phrases, dans la grammaire Careerjet/Indeed/LinkedIn.

    Parenthèses même pour un seul terme : l'expression est toujours juxtaposée
    à une autre (le groupe « type de contrat »), et une juxtaposition vaut ET.
    """
    return "(" + " OR ".join(_entre_guillemets(t) for t in termes) + ")"


def requete_stage(termes: list[str], mots_contrat: list[str]) -> str:
    """« (stage OR internship …) ("machine learning" OR NLP …) ».

    Le groupe contrat reprend `config.MOTS_CLES_STAGE`, c'est-à-dire ce que
    `filters.est_un_stage` exigera ensuite dans le titre : la requête ne
    ramène pas ce que le filtre jetterait par construction.
    """
    return f"{expression_ou(mots_contrat)} {expression_ou(termes)}"


_MOTIF_MOT = re.compile(r"[^\W_]+", re.UNICODE)


def mots_isoles(termes: list[str], ignores: list[str]) -> list[str]:
    """Les MOTS des termes, sans doublon, pour un moteur qui ne fait que des OU de mots.

    « machine learning » devient « machine » et « learning » : c'est tout ce
    qu'Adzuna sait exprimer. ``ignores`` retire les mots trop génériques pour
    signifier la famille (« ingénieur », « data », « ia ») — sans quoi toute
    offre d'ingénieur porterait l'étiquette « ml ». Les mots d'une lettre
    (« l' ») tombent d'office.
    """
    exclus = {m.casefold() for m in ignores}
    vus: list[str] = []
    for terme in termes:
        for mot in _MOTIF_MOT.findall(terme.casefold()):
            if len(mot) > 1 and mot not in exclus and mot not in vus:
                vus.append(mot)
    return vus


def _forme_mots(texte: str) -> str:
    """« Stage Machine-Learning (H/F) » -> « stage machine learning h f », bordé d'espaces.

    Les espaces de bord font de la recherche de sous-chaîne une recherche de
    MOTS ENTIERS : « llm » n'est pas trouvé dans « llmops ».
    """
    t = unicodedata.normalize("NFKD", (texte or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " " + " ".join(re.findall(r"[^\W_]+", t)) + " "


def familles_dans_titre(titre: str, familles: list[str]) -> list[str]:
    """Parmi ``familles``, celles dont au moins un terme figure dans le titre.

    Accents, casse et ponctuation ignorés, mots entiers. Une famille inconnue
    du fichier (renommée depuis) n'est jamais retenue. Sans famille, le
    fichier n'est pas lu. Vaut pour les familles de stage comme pour les
    étiquettes de jobs étudiants.
    """
    if not familles:
        return []
    connues = charger().toutes_familles()
    return familles_dans_texte(titre, [f for f in familles if f in connues], connues)


def familles_dans_texte(texte: str, candidates: list[str],
                        connues: dict[str, Famille] | None = None) -> list[str]:
    """Parmi ``candidates``, les familles dont un terme figure dans ``texte``, dans leur ordre.

    Sert à étiqueter une offre trouvée par une requête OU : le moteur ne dit
    pas lequel des termes a répondu, le texte de l'offre le dit (mots entiers,
    balises HTML comprises — « <b>vendeur</b> » est trouvé).
    """
    connues = connues if connues is not None else charger().toutes_familles()
    t = _forme_mots(texte)
    return [
        nom for nom in candidates
        if nom in connues and any(_forme_mots(terme) in t for terme in connues[nom].termes())
    ]


def lire(chemin: str) -> Recherche:
    """Lit et valide un fichier de recherche. Lève si absent, illisible ou invalide."""
    try:
        with open(chemin, encoding="utf-8") as f:
            brut = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Fichier de recherche introuvable : {chemin}") from None
    except yaml.YAMLError as err:
        raise ValueError(f"YAML illisible dans {chemin} : {err}") from err
    if not isinstance(brut, dict):
        raise ValueError(f"{chemin} doit contenir un dictionnaire à la racine.")
    return Recherche.model_validate(brut)


@functools.lru_cache(maxsize=None)
def _charger_en_cache(chemin: str) -> Recherche:
    return lire(chemin)


def charger() -> Recherche:
    """La recherche du dépôt, lue une fois par processus.

    Le chemin est relu dans ``CHEMIN_RECHERCHE`` à chaque appel, pour qu'un
    test puisse le rediriger ; le cache est indexé par chemin.
    """
    return _charger_en_cache(CHEMIN_RECHERCHE)


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    r = lire(CHEMIN_RECHERCHE)
    termes = r.termes()
    print(f"✅ {CHEMIN_RECHERCHE} est valide : {len(r.familles)} famille(s), "
          f"{len(termes)} terme(s) distinct(s).")
    for t in termes:
        print(f"  {t.terme:40} {', '.join(t.familles)}")
