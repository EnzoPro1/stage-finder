"""
llm.py — Point d'accès unique à Ollama : réglages, sorties structurées, embeddings.

Avant ce module, chaque appelant assemblait sa propre charge HTTP (modèle,
options, schéma) : `verifier.py` pour le jugement, `ollama_pool.py` pour le
déchargement. Changer d'options voulait dire relire chaque site d'appel.
Ici, un seul endroit décide QUOI est envoyé.

## Réglages

`charger_reglages()` part des défauts de `config.py` (section 9) et les
surcharge par variables d'environnement — `VARIABLES_ENV` en donne la liste.
Le `.env` est lu au passage : c'est lui, le « fichier de config ». Une valeur
invalide lève `ReglagesInvalides` en nommant la VARIABLE fautive, pas le champ
interne : c'est la variable que l'utilisateur doit corriger.

## Sorties structurées

`generer_structure(prompt, Schema)` envoie `Schema.model_json_schema()` dans le
paramètre `format` d'Ollama, puis valide la réponse avec Pydantic. Deux
échecs possibles, deux remèdes distincts à la nouvelle tentative :

- **réponse tronquée** (`done_reason == "length"`) : le JSON a été coupé faute
  de tokens. Reposer la même question donnerait la même coupure — on double
  donc le budget `num_predict` (plafonné par `num_ctx`) ;
- **réponse non conforme** : à température 0, reposer la même question rend la
  même réponse. On ajoute donc au prompt l'erreur de validation, pour que la
  nouvelle tentative ne soit PAS une copie de la précédente.

Au-delà de `nouvelles_tentatives`, `SortieStructureeInvalide` est levée avec
le modèle, le schéma, un extrait de la dernière réponse et la cause. C'est à
l'appelant de décider s'il dégrade (le vérificateur rend alors `None`) ou s'il
s'arrête.

## Sérialisation

Chaque appel HTTP d'inférence prend `ollama_pool.JETON` : un seul appel LLM
en vol, tous appelants confondus (vérification ET génération de CV). Le jeton
entoure l'APPEL, jamais la boucle de tentatives : entre deux tentatives,
un autre appelant peut passer.

Diagnostic :  python -m llm     (réglages effectifs + modèles manquants)
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import TypeVar

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import config
import ollama_pool
import reglages_env

logger = logging.getLogger(__name__)

Schema = TypeVar("Schema", bound=BaseModel)

# Champ de `Reglages` -> variable d'environnement qui le surcharge.
VARIABLES_ENV: dict[str, str] = {
    "ollama_url": "SF_OLLAMA_URL",
    "modele": "SF_LLM_MODEL",
    "think": "SF_LLM_THINK",
    "temperature": "SF_LLM_TEMPERATURE",
    "num_ctx": "SF_LLM_NUM_CTX",
    "keep_alive": "SF_LLM_KEEP_ALIVE",
    "top_n": "SF_LLM_TOP_N",
    "timeout_s": "SF_LLM_TIMEOUT_S",
    "num_predict": "SF_LLM_NUM_PREDICT",
    "nouvelles_tentatives": "SF_LLM_NOUVELLES_TENTATIVES",
    "modele_embedding": "SF_EMBED_MODEL",
}

# Syntaxe de durée acceptée par Ollama pour `keep_alive` ("10m", "1h30m", "-1").
_MOTIF_DUREE = re.compile(r"^-?(\d+(\.\d+)?(ns|us|µs|ms|s|m|h))+$")

# Longueur de l'extrait de réponse cité dans les messages d'erreur.
_EXTRAIT = 300

# Consigne ajoutée au prompt après une réponse non conforme.
_RAPPEL = (
    "\n\nATTENTION : ta réponse précédente n'était pas conforme au schéma JSON "
    "demandé ({erreur}). Réponds UNIQUEMENT par un objet JSON conforme au schéma."
)


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------
class ErreurLLM(Exception):
    """Racine des erreurs de ce module."""


class ReglagesInvalides(ErreurLLM, reglages_env.ReglagesInvalides):
    """Une variable d'environnement (ou une constante) a une valeur refusée."""


class OllamaIndisponible(ErreurLLM):
    """Serveur injoignable, timeout, ou réponse HTTP inexploitable."""


class ModeleAbsent(ErreurLLM):
    """Le modèle demandé n'est pas installé dans Ollama."""

    def __init__(self, modeles: list[str]) -> None:
        self.modeles = modeles
        super().__init__(message_modeles_manquants(modeles))


class SortieStructureeInvalide(ErreurLLM):
    """Le modèle n'a pas rendu de JSON conforme au schéma, tentatives épuisées."""

    def __init__(self, modele: str, schema: str, tentatives: int,
                 derniere_reponse: str, cause: str, tronquee: bool) -> None:
        self.modele = modele
        self.schema = schema
        self.tentatives = tentatives
        self.derniere_reponse = derniere_reponse
        self.cause = cause
        self.tronquee = tronquee
        indice = (" — réponse tronquée faute de tokens : augmente SF_LLM_NUM_PREDICT"
                  if tronquee else "")
        extrait = derniere_reponse[:_EXTRAIT] + ("…" if len(derniere_reponse) > _EXTRAIT else "")
        super().__init__(
            f"Sortie de « {modele} » non conforme au schéma {schema} après "
            f"{tentatives} tentative(s){indice}. Cause : {cause}. "
            f"Dernière réponse : {extrait!r}"
        )


# ---------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------
class Reglages(BaseModel):
    """Réglages LLM effectifs : défauts de config.py + surcharges d'environnement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ollama_url: str = Field(min_length=1)
    modele: str = Field(min_length=1)
    think: bool
    temperature: float = Field(ge=0, le=2)
    num_ctx: int = Field(ge=512, le=131072)
    keep_alive: str | int
    top_n: int = Field(gt=0, le=500)
    timeout_s: int = Field(gt=0, le=600)
    num_predict: int = Field(ge=120, le=4096)
    nouvelles_tentatives: int = Field(ge=0, le=5)
    modele_embedding: str = Field(min_length=1)

    @field_validator("ollama_url")
    @classmethod
    def _sans_slash_final(cls, valeur: str) -> str:
        return valeur.rstrip("/")

    @field_validator("keep_alive")
    @classmethod
    def _duree_ollama(cls, valeur: str | int) -> str | int:
        # Un nombre SANS unité doit partir en entier : Ollama lit une chaîne
        # comme une durée Go, et « 300 » sans unité y est refusé.
        if isinstance(valeur, str):
            texte = valeur.strip()
            if re.fullmatch(r"-?\d+", texte):
                return int(texte)
            if not _MOTIF_DUREE.fullmatch(texte):
                raise ValueError(
                    f"durée Ollama attendue (ex. « 10m », « 30s », 300, -1), reçu {valeur!r}"
                )
            return texte
        return valeur

    def options(self, num_predict: int | None = None) -> dict:
        """Bloc ``options`` d'une requête de génération."""
        return {
            "temperature": self.temperature,
            "num_ctx": self.num_ctx,
            "num_predict": num_predict or self.num_predict,
        }


# Réglages EFFECTIFS qui décident un score LLM, exposés en attributs du module
# (``llm.EFFECTIF_MODELE``…) pour l'estampille de ``reference.py``, qui lit
# chaque constante par ``getattr(module, nom)``. Sans eux, un
# ``SF_LLM_MODEL=gemma3:4b`` posé dans le .env changerait les verdicts sans que
# la référence le voie : elle n'estampille que les défauts de config.py.
# Ni l'URL, ni keep_alive, ni le timeout : ils ne changent pas une réponse.
ESTAMPILLES = {
    "EFFECTIF_MODELE": "modele",
    "EFFECTIF_THINK": "think",
    "EFFECTIF_TEMPERATURE": "temperature",
    "EFFECTIF_NUM_CTX": "num_ctx",
    "EFFECTIF_NUM_PREDICT": "num_predict",
    "EFFECTIF_NOUVELLES_TENTATIVES": "nouvelles_tentatives",
    "EFFECTIF_TOP_N": "top_n",
}


def __getattr__(nom: str):
    if nom in ESTAMPILLES:
        return getattr(charger_reglages(), ESTAMPILLES[nom])
    raise AttributeError(f"module 'llm' has no attribute {nom!r}")


def reglages_par_defaut() -> dict:
    """Défauts lus dans config.py AU MOMENT de l'appel (monkeypatchables)."""
    return {
        "ollama_url": config.VERIFY_OLLAMA_URL,
        "modele": config.VERIFY_MODEL,
        "think": config.LLM_THINK,
        "temperature": config.LLM_TEMPERATURE,
        "num_ctx": config.LLM_NUM_CTX,
        "keep_alive": config.LLM_KEEP_ALIVE,
        "top_n": config.VERIFY_TOP_N,
        "timeout_s": config.VERIFY_TIMEOUT_S,
        "num_predict": config.VERIFY_MAX_TOKENS,
        "nouvelles_tentatives": config.LLM_NOUVELLES_TENTATIVES,
        "modele_embedding": config.LLM_MODELE_EMBEDDING,
    }


def charger_reglages(env: Mapping[str, str] | None = None) -> Reglages:
    """Réglages effectifs. ``env=None`` : ``os.environ``, après lecture du ``.env``.

    Mécanisme commun à tous les réglages surchargeables : ``reglages_env``.
    """
    return reglages_env.construire(
        Reglages, reglages_par_defaut(), VARIABLES_ENV, env,
        libelle="Réglages LLM", erreur=ReglagesInvalides,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _post(r: Reglages, chemin: str, charge: dict) -> dict:
    """POST vers Ollama, sous le jeton. Lève une erreur typée, jamais brute."""
    url = f"{r.ollama_url}{chemin}"
    try:
        with ollama_pool.JETON:
            reponse = requests.post(url, json=charge, timeout=r.timeout_s)
    except requests.exceptions.RequestException as err:
        raise OllamaIndisponible(
            f"Ollama injoignable sur {r.ollama_url} ({err}). Lance « ollama serve »."
        ) from err
    return _lire_reponse(reponse, charge.get("model", "?"))


def _lire_reponse(reponse, modele: str) -> dict:
    try:
        donnees = reponse.json()
    except ValueError:
        donnees = None
    try:
        reponse.raise_for_status()
    except requests.exceptions.HTTPError:
        message = donnees.get("error", "") if isinstance(donnees, dict) else ""
        if reponse.status_code == 404 and "not found" in message:
            raise ModeleAbsent([modele]) from None
        raise OllamaIndisponible(
            f"Ollama a répondu HTTP {reponse.status_code} pour « {modele} » : "
            f"{message or 'sans détail'}"
        ) from None
    if not isinstance(donnees, dict):
        raise OllamaIndisponible(f"Réponse Ollama non-JSON pour « {modele} ».")
    return donnees


# ---------------------------------------------------------------------------
# Sorties structurées
# ---------------------------------------------------------------------------
def valider_sortie(brut: str, schema: type[Schema]) -> Schema:
    """Valide ``brut`` contre ``schema``. Lève ``ValidationError`` sinon.

    Tolère un préambule autour de l'objet (balises ``<think>`` d'un modèle qui
    raisonne malgré la consigne) : si le texte entier n'est pas valide, on
    retente sur la portion entre la première ``{`` et la dernière ``}``.
    """
    try:
        return schema.model_validate_json(brut)
    except ValidationError:
        debut, fin = brut.find("{"), brut.rfind("}")
        if debut == -1 or fin <= debut or brut[debut : fin + 1] == brut.strip():
            raise
        return schema.model_validate_json(brut[debut : fin + 1])


def _resumer(err: ValidationError) -> str:
    """Erreurs de validation en une ligne, sans l'URL de doc de Pydantic."""
    morceaux = []
    for e in err.errors()[:3]:
        lieu = ".".join(str(x) for x in e["loc"]) or "racine"
        morceaux.append(f"{lieu} : {e['msg']}")
    return " ; ".join(morceaux)


def generer_structure(
    prompt: str,
    schema: type[Schema],
    *,
    reglages: Reglages | None = None,
    num_predict: int | None = None,
) -> Schema:
    """Génère une sortie conforme à ``schema`` (``format`` Ollama + Pydantic).

    Lève ``SortieStructureeInvalide`` une fois les tentatives épuisées,
    ``OllamaIndisponible`` / ``ModeleAbsent`` sans nouvelle tentative : une
    panne réseau ou un modèle absent ne se corrige pas en reposant la question.
    """
    r = reglages or charger_reglages()
    format_json = schema.model_json_schema()
    budget = num_predict or r.num_predict
    consigne = prompt
    brut, cause, tronquee = "", "", False
    tentatives = r.nouvelles_tentatives + 1

    for tentative in range(1, tentatives + 1):
        donnees = _post(r, "/api/generate", {
            "model": r.modele,
            "prompt": consigne,
            "stream": False,
            "format": format_json,
            "think": r.think,
            "keep_alive": r.keep_alive,
            "options": r.options(budget),
        })
        brut = str(donnees.get("response") or "")
        tronquee = donnees.get("done_reason") == "length"
        try:
            return valider_sortie(brut, schema)
        except ValidationError as err:
            cause = "réponse vide" if not brut.strip() else _resumer(err)

        logger.info("Sortie %s non conforme (tentative %d/%d) : %s",
                    schema.__name__, tentative, tentatives, cause)
        if tronquee:
            budget = min(budget * 2, r.num_ctx)
        else:
            consigne = prompt + _RAPPEL.format(erreur=cause)

    raise SortieStructureeInvalide(
        modele=r.modele, schema=schema.__name__, tentatives=tentatives,
        derniere_reponse=brut, cause=cause, tronquee=tronquee,
    )


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def embed(
    textes: list[str],
    *,
    reglages: Reglages | None = None,
    modele: str | None = None,
    taille_lot: int = 16,
) -> list[list[float]]:
    """Embeddings de ``textes`` via ``/api/embed``, dans l'ordre, par lots.

    Les lots bornent la mémoire d'un appel ; ils sont envoyés L'UN APRÈS
    L'AUTRE, jamais en parallèle (VRAM limitée).
    """
    if not textes:
        return []
    r = reglages or charger_reglages()
    nom = modele or r.modele_embedding
    vecteurs: list[list[float]] = []
    for debut in range(0, len(textes), taille_lot):
        lot = textes[debut : debut + taille_lot]
        donnees = _post(r, "/api/embed", {
            "model": nom,
            "input": lot,
            "truncate": True,
            "keep_alive": r.keep_alive,
        })
        rendus = donnees.get("embeddings")
        if not isinstance(rendus, list) or len(rendus) != len(lot):
            recu = len(rendus) if isinstance(rendus, list) else "aucun"
            raise OllamaIndisponible(
                f"Embeddings incohérents de « {nom} » : {len(lot)} texte(s) envoyé(s), "
                f"{recu} vecteur(s) reçu(s)."
            )
        vecteurs.extend(rendus)
    return vecteurs


# ---------------------------------------------------------------------------
# Présence des modèles
# ---------------------------------------------------------------------------
def _nom_complet(nom: str) -> str:
    """« bge-m3 » et « bge-m3:latest » désignent le même modèle."""
    return nom if ":" in nom else f"{nom}:latest"


def message_modeles_manquants(modeles: list[str]) -> str:
    commandes = "  ;  ".join(f"ollama pull {m}" for m in modeles)
    return f"Modèle(s) Ollama absent(s) : {', '.join(modeles)}. Lance : {commandes}"


def modeles_installes(reglages: Reglages | None = None) -> set[str]:
    """Noms complets des modèles installés. Lève ``OllamaIndisponible``."""
    r = reglages or charger_reglages()
    try:
        reponse = requests.get(f"{r.ollama_url}/api/tags", timeout=5)
    except requests.exceptions.RequestException as err:
        raise OllamaIndisponible(
            f"Ollama injoignable sur {r.ollama_url} ({err}). Lance « ollama serve »."
        ) from err
    donnees = _lire_reponse(reponse, "/api/tags")
    return {_nom_complet(m.get("name", "")) for m in donnees.get("models", [])}


def modeles_manquants(
    modeles: list[str] | None = None, reglages: Reglages | None = None
) -> list[str]:
    """Modèles requis absents d'Ollama (défaut : le modèle de génération).

    Rend une liste vide si tout est là. Lève ``OllamaIndisponible`` si le
    serveur ne répond pas : « absent » et « injoignable » ne se soignent pas
    pareil, on ne les confond pas.
    """
    r = reglages or charger_reglages()
    requis = modeles if modeles is not None else [r.modele]
    installes = modeles_installes(r)
    return [m for m in requis if _nom_complet(m) not in installes]


def diagnostic_demarrage(
    modeles: list[str] | None = None, reglages: Reglages | None = None
) -> str | None:
    """Avertissement à afficher au démarrage, ou ``None`` si tout est prêt.

    Ne lève jamais : un Ollama absent n'empêche pas de collecter et de classer
    au cosinus. Le but est de le dire TOUT DE SUITE, avant des minutes de
    collecte, et avec la commande qui répare.
    """
    r = reglages or charger_reglages()
    try:
        manquants = modeles_manquants(modeles, r)
    except OllamaIndisponible as err:
        return f"{err} La vérification LLM sera ignorée (classement cosinus seul)."
    if manquants:
        return (f"{message_modeles_manquants(manquants)} — sans cela, la vérification "
                f"LLM sera ignorée (classement cosinus seul).")
    return None


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    reglages = charger_reglages()
    print(json.dumps(reglages.model_dump(), ensure_ascii=False, indent=2))
    try:
        manquants = modeles_manquants([reglages.modele, reglages.modele_embedding], reglages)
    except OllamaIndisponible as err:
        print(f"⚠️  {err}")
        raise SystemExit(1)
    if manquants:
        print(f"⚠️  {message_modeles_manquants(manquants)}")
        raise SystemExit(1)
    print("✅ Modèles présents.")
