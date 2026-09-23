"""
verifier.py — Vérification d'une offre par un LLM local (Ollama).

Le ranking cosinus (ranker.py) rapproche des textes par proximité topique : il
ne *raisonne* pas. Ce module ajoute une relecture par un LLM 100 % local qui
lit une offre pré-sélectionnée et juge si elle correspond VRAIMENT au stage
recherché, avec un verdict explicable (score, alternance ?, niveau, drapeaux
rouges, justification).

Garanties (cf. docs/SPEC_verification_llm.md) :
- 100 % local / gratuit : appels vers Ollama (http://localhost:11434), aucune API cloud.
- Sortie structurée : on demande à Ollama du JSON contraint par schéma (``format``),
  ``temperature = 0`` pour la reproductibilité. Parsing DÉFENSIF quand même.
- Dégradation gracieuse : toute erreur (réseau, timeout, JSON cassé) => retour
  ``None``, jamais d'exception qui remonte. L'appelant retombe sur le cosinus.
- Enrichir, pas supprimer : le verdict est un signal, il ne *drop* jamais une offre.

Testable isolément :  python -m verifier   (nécessite un Ollama qui tourne)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Annotated, Literal

import requests
from pydantic import BaseModel, BeforeValidator, ConfigDict

import config
import llm

if TYPE_CHECKING:  # évite l'import circulaire (normalize n'importe pas verifier)
    from normalize import Offre

logger = logging.getLogger(__name__)

# Version des RÈGLES de jugement (prompt, schéma, extraction, garde-fous).
#
# Le cache SQLite des verdicts est indexé par (offre, modèle). Sans ce numéro,
# durcir le prompt ne changeait RIEN aux offres déjà vérifiées : elles
# ressortaient telles quelles du cache, avec leur ancien verdict. On incorpore
# donc la version à la clé de cache — l'incrémenter invalide proprement les
# verdicts obsolètes, sans toucher à la base.
#
# À INCRÉMENTER à chaque modification de _PROMPT, du schéma VerdictLLM,
# de _extrait_pertinent ou des fonctions _plafonner_*.
#
# v3 : `niveau` contraint à NIVEAUX dans le schéma envoyé à Ollama.
VERSION_REGLES = 3


def cle_cache(model: str, profil: str | None = None) -> str:
    """Clé de cache : modèle, version des règles ET profil jugé.

    Le profil est injecté dans le prompt : un verdict n'a de sens que pour le
    profil qui l'a produit. Sans son empreinte dans la clé, modifier
    ``config.REQUETE_REFERENCE`` laissait ressortir du cache des verdicts
    rendus pour l'ancien profil. ``profil`` vaut par défaut le profil effectif
    de ``verifier()``, ``config.REQUETE_REFERENCE``.
    """
    profil = config.REQUETE_REFERENCE if profil is None else profil
    empreinte = hashlib.sha256(profil.strip().encode("utf-8")).hexdigest()[:12]
    return f"{model}@v{VERSION_REGLES}#p{empreinte}"


# Niveaux admis. Un petit modèle en invente d'autres (« étudiant »,
# « intermédiaire ») : le schéma envoyé à Ollama les interdit, et
# `normaliser_niveau` ramène dans cet ensemble ce qui passerait quand même
# (modèle qui ignore `format`, verdicts en cache antérieurs à la contrainte).
NIVEAUX = ("stage", "junior", "senior", "inconnu")


def normaliser_niveau(valeur) -> str:
    """Ramène un niveau libre dans ``NIVEAUX`` ; « inconnu » si rien ne colle."""
    texte = unicodedata.normalize("NFKD", str(valeur or ""))
    texte = texte.encode("ascii", "ignore").decode().strip().lower()
    if texte in NIVEAUX:
        return texte
    if "senior" in texte or "confirme" in texte or "expert" in texte:
        return "senior"
    if "junior" in texte or "debutant" in texte or "jeune diplome" in texte:
        return "junior"
    if any(m in texte for m in ("stag", "etudiant", "intern", "student")):
        return "stage"
    return "inconnu"


Niveau = Annotated[Literal[NIVEAUX], BeforeValidator(normaliser_niveau)]


# ---------------------------------------------------------------------------
# Schéma de la sortie structurée (paramètre ``format`` d'Ollama, via llm.py)
# ---------------------------------------------------------------------------
# Ordre des champs VOLONTAIRE : la génération JSON contrainte suit l'ordre du
# schéma, donc on place les champs de « raisonnement » (alternance, niveau,
# domaine, drapeaux, justification) AVANT le ``score``. Le modèle décide ainsi
# les violations d'abord, puis attribue la note en connaissance de cause — un
# petit modèle note bien mieux « à la fin » qu'en tête (où il met 1.0 par défaut).
#
# Deux exigences distinctes, qu'un seul modèle Pydantic doit porter :
# - le schéma ENVOYÉ exige les champs de décision (``_REQUIS``), pour que la
#   génération contrainte les produise toujours ;
# - la VALIDATION tolère l'absence de ``domaine_match`` et des champs
#   descriptifs (défaut ``None``/[]), comme le parsing défensif d'avant.
# D'où ``json_schema_extra``, qui réécrit la liste ``required`` du schéma.
# ``score`` n'est PAS borné ici : ``Verdict.from_dict`` le ramène dans [0, 1]
# plutôt que de jeter un verdict entier pour un 1.2.
_REQUIS = ["pertinent", "est_alternance", "niveau", "domaine_match", "justification", "score"]


class VerdictLLM(BaseModel):
    """Sortie brute attendue du modèle, avant garde-fous."""

    model_config = ConfigDict(
        json_schema_extra=lambda schema: schema.update(required=list(_REQUIS)),
    )

    pertinent: bool
    est_alternance: bool                 # alternance/apprentissage déguisé ?
    niveau: Niveau                       # enum dans le schéma, normalisé à la validation
    domaine_match: bool | None = None    # IA/cyber réellement au cœur du poste ?
    duree_mois: int | None = None
    date_debut: str | None = None        # ISO ou null
    drapeaux_rouges: list[str] = []
    justification: str
    score: float                         # 0.0 à 1.0 — EN DERNIER, après décision


VERDICT_SCHEMA = VerdictLLM.model_json_schema()

# Longueur max de description injectée dans le prompt. Les annonces réelles font
# souvent > 3000 caractères : au-delà, on tronque, pour BORNER le coût
# d'évaluation du prompt — décisif sur CPU où chaque token compte.
_MAX_DESC_CHARS = 600

# Marqueurs de début de la section « missions » d'une annonce.
#
# Prendre les 600 PREMIERS caractères était un piège : dans une annonce réelle,
# ils contiennent la plaquette de l'entreprise, pas le poste. Mesuré sur un run
# complet : 24 annonces sur 76 placent leurs missions AU-DELÀ du 600e caractère
# (position médiane du marqueur : ~1070). Résultat, un « Legal intern » publié
# par un éditeur de cybersécurité était noté 0.90 — le modèle ne lisait que le
# discours corporate saturé de vocabulaire cyber, jamais les missions juridiques.
_MOTIF_MISSIONS = re.compile(
    r"(vos?\s+(principales\s+)?missions|votre\s+mission|missions?\s*:"
    r"|ce que vous ferez|votre r[ôo]le|responsabilit[ée]s|responsibilities"
    r"|what you.ll do|your role|the role|descriptif du poste"
    r"|activit[ée]s principales|au sein de l.[ée]quipe|rattach[ée])",
    re.IGNORECASE,
)


def _extrait_pertinent(description: str) -> str:
    """Fenêtre de description centrée sur les MISSIONS, pas sur la plaquette.

    On cherche le premier marqueur de section « missions » et on lit à partir de
    là. Sans marqueur, on retombe sur le début du texte (comportement d'avant).
    """
    if not description:
        return ""
    marqueur = _MOTIF_MISSIONS.search(description)
    debut = marqueur.start() if marqueur else 0
    return description[debut : debut + _MAX_DESC_CHARS]


# Métiers « support » : le poste porte sur une fonction transverse, pas sur la
# conception d'IA ou de sécurité — même quand l'EMPLOYEUR est une entreprise
# tech. Cherchés en MOT ENTIER dans le TITRE uniquement.
# Les termes AMBIGUS sont écrits en EXPRESSION, jamais en mot isolé — vérifié
# sur un run réel : « talent » seul attrapait « STAGE TALENT DAY » (un nom
# d'événement) sur un poste de Data Engineer, et « communication » seul
# attrapait « Semantic Communication MARL Internship », de la recherche en
# apprentissage par renforcement. Un garde-fou qui déclasse de vraies offres
# techniques coûte plus cher que le faux positif qu'il corrige.
_MOTS_SUPPORT = [
    "legal", "juridique", "juriste", "droit",
    "rh", "hr", "ressources humaines", "recrutement", "recruitment",
    "talent acquisition", "talent management", "paie",
    "marketing", "commercial", "vente", "sales", "achat", "achats",
    "chargé de communication", "chargée de communication", "communication digitale",
    "comptable", "comptabilité", "finance", "financier", "audit", "rse",
    "évènementiel", "evenementiel",
]
_MOTIF_SUPPORT = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _MOTS_SUPPORT) + r")\b", re.IGNORECASE
)

# Signaux techniques du titre : ils ANNULENT la règle « métier support » (voir
# ``est_metier_support``). Réutilise les listes de config.py — une seule source
# de vérité pour « qu'est-ce qu'un signal IA / cyber ».
_MOTIF_IA = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in config.MOTS_CLES_IA) + r")\b", re.IGNORECASE
)
_MOTIF_CYBER = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in config.MOTS_CLES_CYBER) + r")\b", re.IGNORECASE
)

# Plafond de tokens générés (config.VERIFY_MAX_TOKENS). ATTENTION : ce n'est pas
# un simple réglage de verbosité. La sortie étant du JSON contraint et le champ
# ``score`` étant émis EN DERNIER, un budget trop court coupe le JSON avant sa
# fermeture — et c'est le verdict ENTIER qui devient illisible, pas seulement la
# justification. On garde donc de la marge (voir config.py). En filet,
# ``llm.generer_structure`` double le budget si la réponse revient tronquée.

# Gabarit du prompt. Durci contre les FAUX NÉGATIFS : en cas de doute on signale
# (drapeau rouge) plutôt que de rejeter — cohérent avec les filtres permissifs.
# ``/no_think`` désactive le raisonnement de qwen3 pour cette réponse : la sortie
# est contrainte par schéma, le « thinking » n'apporte rien et coûte très cher
# en tokens/latence (sinon chaque appel dépasse le timeout). Complété par
# ``think=False`` dans la charge Ollama pour les versions récentes qui le gèrent.
_PROMPT = """/no_think
Tu vérifies si une offre d'emploi correspond à un profil de stage précis et tu lui
attribues un SCORE GRADUÉ.

PROFIL RECHERCHÉ:
{profil}

OFFRE:
Titre: {title}
Entreprise: {company}
Lieu: {location}
Description: {description}

BARÈME DU SCORE (0.0 à 1.0 — sois DISCRIMINANT, n'attribue PAS 1.0 par défaut) :
- 0.9-1.0 : IA et/ou cybersécurité VRAIMENT au cœur du poste, stage ~6 mois, Île-de-France.
- 0.6-0.8 : bon domaine mais un critère imparfait (durée/date/lieu limite, ou IA/cyber secondaire).
- 0.3-0.5 : lien FAIBLE avec l'IA/cyber, ou plusieurs critères douteux.
- 0.0-0.2 : critère clairement violé — alternance, poste senior, HORS IA/cyber
  (ex. RH, finance, marketing, droit, vente), ou hors Île-de-France.

Règles :
- pertinent = true SAUF si un critère est clairement violé (simple doute -> true + un drapeau rouge).
- ⚠ JUGE LE POSTE, PAS L'EMPLOYEUR. Une entreprise de cybersécurité ou d'IA recrute aussi des
  juristes, des commerciaux et des RH : leurs annonces décrivent longuement l'activité cyber/IA
  de la SOCIÉTÉ alors que le STAGE n'a rien de technique. Le vocabulaire cyber/IA présent dans
  la présentation de l'entreprise ne compte JAMAIS comme domaine_match.
- domaine_match = false si le poste est principalement RH/recrutement, finance/comptabilité,
  marketing, vente, communication, droit, ou tout métier où l'IA/cyber n'est qu'accessoire.
  domaine_match = true UNIQUEMENT si CONCEVOIR/DÉVELOPPER de l'IA ou de la cybersécurité est
  la mission CENTRALE du stage — c'est-à-dire si les MISSIONS décrites (pas la présentation
  de la société) portent sur du code, des modèles, des systèmes ou des tests de sécurité.
- Décide D'ABORD les champs (alternance, niveau, domaine_match, drapeaux) PUIS le score EN DERNIER.
- RÈGLE STRICTE : si est_alternance=true, OU niveau="senior", OU domaine_match=false,
  alors score ≤ 0.2 obligatoirement.

Réponds UNIQUEMENT en JSON conforme au schéma.
Signale par un drapeau rouge toute alternance, poste senior, durée ≠ 6 mois, date
incompatible, ou domaine hors IA/cyber.

JUSTIFICATION — écris {n_phrases} phrases complètes, en français, dans cet ordre :
1. ce que fait CONCRÈTEMENT le stagiaire dans ce poste (missions, technologies) ;
2. en quoi cela colle ou non au profil recherché (IA / cybersécurité) ;
3. les réserves : durée, date de début, lieu, niveau demandé.
Sois factuel et précis, appuie-toi sur le texte de l'offre, pas de formule creuse."""


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
@dataclass
class Verdict:
    """Jugement structuré du LLM sur une offre. Champs optionnels = ``None``/[]."""

    pertinent: bool
    score: float
    est_alternance: bool
    niveau: str
    justification: str
    duree_mois: int | None = None
    date_debut: str | None = None
    domaine_match: bool | None = None
    drapeaux_rouges: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, donnees: dict) -> "Verdict":
        """Reconstruit un Verdict depuis un dict (cache SQLite ou réponse Ollama).

        Défensif : coercition de type + valeurs par défaut, pour ne jamais casser
        sur un JSON approximatif renvoyé par le modèle.
        """
        return cls(
            pertinent=bool(donnees.get("pertinent", False)),
            score=_score_borne(donnees.get("score")),
            est_alternance=bool(donnees.get("est_alternance", False)),
            niveau=normaliser_niveau(donnees.get("niveau")),
            justification=str(donnees.get("justification") or ""),
            duree_mois=_int_ou_none(donnees.get("duree_mois")),
            date_debut=_str_ou_none(donnees.get("date_debut")),
            domaine_match=_bool_ou_none(donnees.get("domaine_match")),
            drapeaux_rouges=[str(x) for x in (donnees.get("drapeaux_rouges") or [])],
        )


def _score_borne(valeur) -> float:
    """Score coercé en float et borné dans [0, 1] (0.0 si illisible)."""
    try:
        return max(0.0, min(1.0, float(valeur)))
    except (TypeError, ValueError):
        return 0.0


def _int_ou_none(valeur) -> int | None:
    try:
        return int(valeur) if valeur is not None else None
    except (TypeError, ValueError):
        return None


def _str_ou_none(valeur) -> str | None:
    if valeur is None:
        return None
    texte = str(valeur).strip()
    return texte or None


def _bool_ou_none(valeur) -> bool | None:
    return bool(valeur) if valeur is not None else None


# ---------------------------------------------------------------------------
# Appel Ollama
# ---------------------------------------------------------------------------
def ollama_disponible(url: str | None = None, timeout: float = 3.0) -> bool:
    """Ping léger du serveur Ollama : True s'il répond, False sinon.

    Sert à l'appelant pour décider UNE fois par run s'il tente la vérification
    LLM ou s'il conserve directement le classement cosinus (dégradation globale).
    """
    base = (url or llm.charger_reglages().ollama_url).rstrip("/")
    try:
        reponse = requests.get(f"{base}/api/tags", timeout=timeout)
        return reponse.status_code == 200
    except requests.exceptions.RequestException:
        return False


def verifier(
    offre: "Offre",
    profil: str | None = None,
    model: str | None = None,
    url: str | None = None,
    timeout: int | None = None,
    reglages: "llm.Reglages | None" = None,
) -> Verdict | None:
    """Fait juger une offre par Ollama et renvoie un ``Verdict`` (ou ``None``).

    Accès EXPLICITE aux champs de l'offre (title, company, location, description),
    jamais ``__dict__``. L'appel passe par ``llm.generer_structure`` : réglages
    centralisés (num_ctx, keep_alive, think…), schéma Pydantic, nouvelles
    tentatives, jeton Ollama. Tout échec y est TYPÉ ; ici on l'absorbe — on log
    la cause explicite et on renvoie ``None``, l'appelant retombe alors sur le
    score cosinus (dégradation gracieuse).

    ``model``, ``url`` et ``timeout`` surchargent les réglages effectifs pour
    cet appel seulement (option ``--verify-model`` du CLI, tests).
    """
    r = reglages or llm.charger_reglages()
    surcharges = {"modele": model, "ollama_url": url.rstrip("/") if url else None,
                  "timeout_s": timeout}
    r = r.model_copy(update={k: v for k, v in surcharges.items() if v is not None})
    profil = profil if profil is not None else config.REQUETE_REFERENCE

    description = _extrait_pertinent(offre.description or "")
    prompt = _PROMPT.format(
        profil=profil,
        title=offre.title,
        company=offre.company,
        location=offre.location,
        description=description,
        n_phrases=config.VERIFY_JUSTIF_PHRASES,
    )

    try:
        sortie = llm.generer_structure(prompt, VerdictLLM, reglages=r)
    except llm.SortieStructureeInvalide as err:
        logger.warning("Vérification LLM de « %s » abandonnée : %s", offre.title, err)
        return None
    except llm.ErreurLLM as err:
        logger.warning("Vérification LLM : %s", err)
        return None

    verdict = Verdict.from_dict(sortie.model_dump())
    _plafonner_metier_support(verdict, offre.title)
    _plafonner_violations(verdict)
    return verdict


def est_metier_support(titre: str) -> bool:
    """Vrai si le TITRE annonce un métier support sans signal IA/cyber.

    Deux conditions, et la seconde est essentielle : un mot « support » ne
    disqualifie que s'il n'y a AUCUN signal technique dans le titre. « Stage
    Data Scientist - Marketing Analytics » reste donc un poste data ; « Legal
    intern » chez un éditeur cyber, non.
    """
    if not _MOTIF_SUPPORT.search(titre or ""):
        return False
    return not (_MOTIF_IA.search(titre) or _MOTIF_CYBER.search(titre))


def _plafonner_metier_support(v: Verdict, titre: str) -> None:
    """Force ``domaine_match=False`` sur un métier support (modifie ``v``).

    Le prompt le demande déjà, mais un petit modèle se laisse berner par la
    présentation de l'entreprise (« Legal intern » chez un éditeur de
    cybersécurité était noté 0.90). Le titre, lui, ne ment pas : on s'y fie
    mécaniquement, et on trace la raison dans un drapeau rouge pour que la
    décision reste lisible dans l'interface.
    """
    if not est_metier_support(titre):
        return
    if v.domaine_match is not False:
        v.drapeaux_rouges.append(
            "métier support (juridique/RH/marketing…) : le domaine IA/cyber "
            "vient de l'entreprise, pas du poste"
        )
    v.domaine_match = False


def _plafonner_violations(v: Verdict) -> None:
    """Plafonne EN DUR le score des violations franches (modifie ``v`` en place).

    Un petit modèle repère correctement l'alternance et le hors-domaine (champs
    booléens) mais « oublie » souvent de faire chuter son score en conséquence.
    On garantit donc mécaniquement que ces violations écrasent le score, sans
    dépendre de la clémence du modèle. On NE plafonne PAS sur ``niveau`` seul :
    ce champ est trop peu fiable (un vrai stage est parfois étiqueté "senior").
    """
    if v.est_alternance:
        v.score = min(v.score, 0.2)
    if v.domaine_match is False:
        v.score = min(v.score, 0.3)


# ---------------------------------------------------------------------------
# Combinaison des scores
# ---------------------------------------------------------------------------
def score_final(
    score_cosinus: float, verdict: Verdict | None, poids: float | None = None
) -> float:
    """Combine le score cosinus et le score LLM.

    Avec verdict :  (1 - poids) * cosinus + poids * score_llm.
    Sans verdict (échec/désactivé) :  score_cosinus inchangé — une offre ne
    disparaît JAMAIS faute de verdict.
    """
    if verdict is None:
        return score_cosinus
    poids = config.VERIFY_SCORE_WEIGHT if poids is None else poids
    return (1.0 - poids) * score_cosinus + poids * verdict.score


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows
    from normalize import Offre

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    demo = Offre(
        "Stage IA appliquée à la cybersécurité (6 mois)", "TotalEnergies", "Paris",
        "Détection d'intrusion par machine learning, MLOps sécurisé. "
        "Stage de fin d'études de 6 mois à partir de janvier 2027.",
        "http://x", "demo", "", "",
    )
    if not ollama_disponible():
        print("⚠️  Ollama injoignable — lance `ollama serve` puis `ollama pull qwen3:4b`.")
    else:
        v = verifier(demo)
        print(json.dumps(v.to_dict() if v else None, ensure_ascii=False, indent=2))
