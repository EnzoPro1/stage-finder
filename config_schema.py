"""
config_schema.py — Validation de la configuration au démarrage (pydantic).

`config.py` est la source unique de vérité, mais sans garde-fou : un poids
négatif, un seuil > 1, une liste vide ou un mode inconnu passeraient en silence
et produiraient un classement subtilement faux. Ce module transforme `config.py`
en un objet validé : au moindre réglage incohérent, le programme s'arrête
immédiatement avec un message clair (fail-fast), plutôt que de tourner faux.

Utilisation :
    from config_schema import valider_config
    valider_config()  # lève pydantic.ValidationError si config.py est incohérent
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

import config


class ProfilReference(BaseModel):
    """Un profil de référence pour le ranking (texte + poids d'agrégation)."""

    texte: str = Field(min_length=1)
    poids: float = Field(gt=0)


class ConfigModel(BaseModel):
    """Schéma validé de config.py. Les bornes reflètent le sens métier."""

    # --- Recherche ---
    REQUETE_REFERENCE: str = Field(min_length=1)
    LIEU: str = Field(min_length=1)
    RESULTATS_PAR_TERME: int = Field(gt=0, le=200)

    # --- Sources ---
    SOURCES_ACTIVES: list[str] = Field(min_length=1)
    SOURCES_ECARTEES_STAGES: dict[str, str]
    ADZUNA_TITRES_EXIGES: list[str] = Field(min_length=1)
    ADZUNA_MOTS_IGNORES: list[str]
    ADZUNA_PAGES: int = Field(gt=0, le=20)
    CAREERJET_PAGES: int = Field(gt=0, le=20)
    CAREERJET_LOCALE: str = Field(min_length=2)
    FREE_WORK_PAGES: int = Field(gt=0, le=20)
    FRANCE_TRAVAIL_RESULTATS: int = Field(gt=0, le=150)  # plafond de l'API
    JOBSPY_SITES: list[str] = Field(min_length=1)

    # --- Jobs étudiants ---
    SOURCES_ACTIVES_JOBS: list[str] = Field(min_length=1)
    SOURCES_ECARTEES_JOBS: dict[str, str]
    MOTS_CLES_ALTERNANCE: list[str] = Field(min_length=1)
    FRANCE_TRAVAIL_PAGES_JOBS: int = Field(ge=1, le=21)   # l'API s'arrête à l'index 3149
    ADZUNA_PAGES_JOBS: int = Field(ge=1, le=20)
    ADZUNA_RESULTATS_PAR_PAGE_JOBS: int = Field(ge=1, le=50)
    ADZUNA_MOTS_JOBS: list[str] = Field(min_length=1)
    CAREERJET_PAGES_JOBS: int = Field(ge=1, le=20)
    CAREERJET_TAILLE_PAGE_JOBS: int = Field(ge=1, le=99)
    JOBSPY_SITES_JOBS: list[str] = Field(min_length=1)
    JOBSPY_RESULTATS_PAR_SITE: dict[str, int] = Field(min_length=1)

    # --- Tableau de bord marché ---
    METIERS_SUIVIS: list[str] = Field(min_length=1)
    ROME_PAR_METIER: int = Field(gt=0, le=5)
    ROME_REPLI: dict[str, list[dict]]

    # --- Filtres ---
    MOTS_CLES_STAGE: list[str] = Field(min_length=1)
    MOTS_CLES_EXCLUS: list[str]
    LIEUX_ACCEPTES: list[str] = Field(min_length=1)
    MARQUEURS_ETRANGERS: list[str]
    MARQUEURS_ETRANGERS_TITRE: list[str]

    # --- Modèle / embeddings ---
    MODELE_EMBEDDING: str = Field(min_length=1)
    MAX_SEQ_LENGTH: int | None = Field(default=None)
    PREFIXE_REQUETE: str = ""
    PREFIXE_DOCUMENT: str = ""

    # --- Composition du score ---
    MODE_COMPOSITION_SCORE: Literal[
        "additif", "additif_normalise", "multiplicatif", "lexicographique"
    ]
    METHODE_NORMALISATION: Literal["minmax", "zscore"]
    PROFILS_REFERENCE: list[ProfilReference]
    AGGREGATION_PROFILS: Literal["max", "moyenne"]

    # --- Boosts (poids >= 0) ---
    MOTS_CLES_IA: list[str] = Field(min_length=1)
    MOTS_CLES_CYBER: list[str] = Field(min_length=1)
    BOOST_IA: float = Field(ge=0, le=1)
    BOOST_CYBER: float = Field(ge=0, le=1)
    BOOST_COMBO: float = Field(ge=0, le=1)
    BOOST_FACTEUR_DESCRIPTION: float = Field(ge=0, le=1)
    COMBO_EXIGE_SIGNAL_TITRE: bool
    COMBO_TOUJOURS_AFFICHE: bool

    # --- Fraîcheur ---
    JOURS_FRAICHEUR: int = Field(gt=0, le=365)
    GARDER_SI_DATE_INCONNUE: bool

    # --- Dédup floue ---
    DEDUP_FLOUE_ACTIVE: bool
    SEUIL_DEDUP_FLOU: float = Field(gt=0, le=1)

    # --- Extraction durée / date ---
    DUREE_CIBLE_MOIS: int = Field(gt=0, le=36)
    DUREE_MIN_ACCEPTABLE: int = Field(gt=0, le=36)
    BONUS_DUREE_CIBLE: float = Field(ge=0, le=1)
    MALUS_DUREE_COURTE: float = Field(ge=0, le=1)
    DATE_DEBUT_CIBLE_ANNEE: int = Field(ge=2020, le=2100)
    DATE_DEBUT_CIBLE_MOIS: int = Field(ge=1, le=12)
    BONUS_DATE_DEBUT: float = Field(ge=0, le=1)

    # --- Vérification LLM (Ollama) ---
    VERIFY_ENABLED: bool
    VERIFY_MODEL: str = Field(min_length=1)
    VERIFY_OLLAMA_URL: str = Field(min_length=1)
    VERIFY_TOP_N: int = Field(gt=0, le=500)
    VERIFY_TIMEOUT_S: int = Field(gt=0, le=600)
    VERIFY_SCORE_WEIGHT: float = Field(ge=0, le=1)
    VERIFY_MIN_SCORE: float = Field(ge=0, le=1)
    VERIFY_MAX_TOKENS: int = Field(ge=120, le=4096)
    VERIFY_JUSTIF_PHRASES: int = Field(ge=1, le=8)

    # --- Persistance / réseau ---
    CHEMIN_BASE: str = Field(min_length=1)
    TIMEOUT_HTTP: int = Field(gt=0, le=300)
    DELAI_ENTRE_REQUETES: float = Field(ge=0, le=60)
    MAX_THREADS_COLLECTE: int = Field(gt=0, le=32)

    @field_validator("PROFILS_REFERENCE", mode="before")
    @classmethod
    def _profils_vers_modeles(cls, valeur):
        # Accepte la liste de dicts telle qu'écrite dans config.py.
        return valeur

    @field_validator("SOURCES_ACTIVES", "SOURCES_ACTIVES_JOBS")
    @classmethod
    def _sources_connues(cls, valeur: list[str]) -> list[str]:
        """Refuse un nom de source absent du catalogue (faute de frappe = fail-fast)."""
        from sources.registry import CATALOGUE

        inconnues = [nom for nom in valeur if nom not in CATALOGUE]
        if inconnues:
            raise ValueError(
                f"Source(s) inconnue(s) dans SOURCES_ACTIVES : {', '.join(inconnues)}. "
                f"Disponibles : {', '.join(sorted(CATALOGUE))}."
            )
        return valeur

    @model_validator(mode="after")
    def _sources_ecartees_coherentes(self) -> "ConfigModel":
        """Une source écartée existe au catalogue, a une raison, et ne tourne pas.

        Écartée ET active, le bilan annoncerait une désactivation qui n'a pas
        eu lieu — une erreur dans le seul tableau qui doit être juste.
        """
        from sources.registry import CATALOGUE

        problemes = []
        perimetres = [
            ("SOURCES_ECARTEES_STAGES", self.SOURCES_ECARTEES_STAGES,
             self.SOURCES_ACTIVES, self.JOBSPY_SITES),
            ("SOURCES_ECARTEES_JOBS", self.SOURCES_ECARTEES_JOBS,
             self.SOURCES_ACTIVES_JOBS, self.JOBSPY_SITES_JOBS),
        ]
        for reglage, ecartees, actives, sites in perimetres:
            for nom, raison in ecartees.items():
                # « jobspy:google » : un site d'une source du catalogue.
                base, _, site = nom.partition(":")
                if base not in CATALOGUE:
                    problemes.append(f"{reglage} : « {nom} » inconnue du catalogue")
                if not site and nom in actives:
                    problemes.append(f"{reglage} : « {nom} » à la fois active et écartée")
                if site and base == "jobspy" and site in sites:
                    problemes.append(f"{reglage} : « {nom} » à la fois interrogé et écarté")
                if not raison.strip():
                    problemes.append(f"{reglage} : « {nom} » écartée sans raison")
        if problemes:
            raise ValueError(" ; ".join(problemes))
        return self

    @model_validator(mode="after")
    def _repli_rome_couvre_les_metiers(self) -> "ConfigModel":
        """Chaque métier suivi doit avoir un repli si ROMEO est indisponible.

        Sans ça, une panne de ROMEO ferait disparaître silencieusement des
        lignes du tableau de bord — on croirait le marché vide sur ce métier.
        """
        manquants = [m for m in self.METIERS_SUIVIS if not self.ROME_REPLI.get(m)]
        if manquants:
            raise ValueError(
                "ROME_REPLI ne couvre pas : " + ", ".join(manquants)
                + ". Ajoute au moins un code ROME par métier suivi."
            )
        return self

    @model_validator(mode="after")
    def _coherence(self) -> "ConfigModel":
        if self.DUREE_MIN_ACCEPTABLE > self.DUREE_CIBLE_MOIS:
            raise ValueError(
                "DUREE_MIN_ACCEPTABLE ne peut pas dépasser DUREE_CIBLE_MOIS."
            )
        return self


def valider_config() -> ConfigModel:
    """Valide `config.py` et retourne l'objet validé. Lève en cas d'incohérence.

    Appelé au démarrage de main.py : un réglage incohérent stoppe le programme
    tout de suite, avec un message pydantic explicite, au lieu d'un classement
    silencieusement cassé.

    ``recherche.yaml`` est validé dans le même geste : c'est lui qui décide ce
    que les sources interrogent, et une famille vide ferait tourner une
    collecte amputée sans que rien ne le signale.
    """
    import recherche

    champs = {
        nom: getattr(config, nom)
        for nom in ConfigModel.model_fields
        if hasattr(config, nom)
    }
    modele = ConfigModel(**champs)
    lue = recherche.lire(recherche.CHEMIN_RECHERCHE)
    if lue.student_jobs:
        termes = {t.casefold() for t in lue.student_jobs.termes}
        etrangers = [m for m in modele.ADZUNA_MOTS_JOBS if m.casefold() not in termes]
        if etrangers:
            raise ValueError(
                "ADZUNA_MOTS_JOBS contient des mots qui ne sont pas des termes de "
                "student_jobs dans recherche.yaml : " + ", ".join(etrangers)
            )
    return modele


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    try:
        valider_config()
    except ValidationError as err:
        print("❌ Configuration invalide :\n", err)
        raise SystemExit(1)
    print("✅ config.py est valide.")
