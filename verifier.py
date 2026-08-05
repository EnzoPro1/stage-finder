"""
verifier.py — Vérification d'une offre par un LLM local (Ollama).

Le ranking cosinus (ranker.py) rapproche des textes par proximité topique : il
ne *raisonne* pas. Ce module ajoute une relecture par un LLM 100 % local qui
lit une offre pré-sélectionnée et juge si elle correspond VRAIMENT au stage
recherché, avec un verdict explicable (score, alternance ?, niveau, drapeaux
rouges, justification).

Garanties (cf. SPEC_verification_llm.md) :
- 100 % local / gratuit : appels vers Ollama (http://localhost:11434), aucune API cloud.
- Sortie structurée : on demande à Ollama du JSON contraint par schéma (``format``),
  ``temperature = 0`` pour la reproductibilité. Parsing DÉFENSIF quand même.
- Dégradation gracieuse : toute erreur (réseau, timeout, JSON cassé) => retour
  ``None``, jamais d'exception qui remonte. L'appelant retombe sur le cosinus.
- Enrichir, pas supprimer : le verdict est un signal, il ne *drop* jamais une offre.

Testable isolément :  python -m verifier   (nécessite un Ollama qui tourne)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import requests

import config

if TYPE_CHECKING:  # évite l'import circulaire (normalize n'importe pas verifier)
    from normalize import Offre

logger = logging.getLogger(__name__)

# Version des RÈGLES de jugement (prompt, extraction de description, garde-fous).
#
# Le cache SQLite des verdicts est indexé par (offre, modèle). Sans ce numéro,
# durcir le prompt ne changeait RIEN aux offres déjà vérifiées : elles
# ressortaient telles quelles du cache, avec leur ancien verdict. On incorpore
# donc la version à la clé de cache — l'incrémenter invalide proprement les
# verdicts obsolètes, sans toucher à la base.
#
# À INCRÉMENTER à chaque modification de _PROMPT, _extrait_pertinent ou des
# fonctions _plafonner_*.
VERSION_REGLES = 2


def cle_cache(model: str) -> str:
    """Clé de cache d'un modèle, versionnée par les règles de jugement."""
    return f"{model}@v{VERSION_REGLES}"


# ---------------------------------------------------------------------------
# Schéma JSON attendu d'Ollama (paramètre ``format`` de /api/generate)
# ---------------------------------------------------------------------------
# Ordre des propriétés VOLONTAIRE : la génération JSON contrainte suit l'ordre du
# schéma, donc on place les champs de « raisonnement » (alternance, niveau,
# domaine, drapeaux, justification) AVANT le ``score``. Le modèle décide ainsi
# les violations d'abord, puis attribue la note en connaissance de cause — un
# petit modèle note bien mieux « à la fin » qu'en tête (où il met 1.0 par défaut).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "pertinent":       {"type": "boolean"},
        "est_alternance":  {"type": "boolean"},          # alternance/apprentissage déguisé ?
        "niveau":          {"type": "string"},           # "stage" | "junior" | "senior" | "inconnu"
        "domaine_match":   {"type": "boolean"},          # IA/cyber réellement au cœur du poste ?
        "duree_mois":      {"type": ["integer", "null"]},
        "date_debut":      {"type": ["string", "null"]},  # ISO ou null
        "drapeaux_rouges": {"type": "array", "items": {"type": "string"}},
        "justification":   {"type": "string"},
        "score":           {"type": "number"},           # 0.0 à 1.0 — EN DERNIER, après décision
    },
    "required": ["pertinent", "est_alternance", "niveau", "domaine_match", "justification", "score"],
}

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
# justification. On garde donc de la marge (voir config.py).

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
            niveau=str(donnees.get("niveau") or "inconnu"),
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
    base = (url or config.VERIFY_OLLAMA_URL).rstrip("/")
    try:
        reponse = requests.get(f"{base}/api/tags", timeout=timeout)
        return reponse.status_code == 200
    except requests.exceptions.RequestException:
        return False


def _extraire_json(texte: str) -> dict | None:
    """Extrait le premier objet JSON d'une chaîne (parsing défensif).

    Avec ``format=VERDICT_SCHEMA``, Ollama renvoie normalement du JSON pur ; on
    reste néanmoins robuste à un éventuel préambule (ex. balises ``<think>`` d'un
    modèle « thinking ») en isolant la portion entre la 1re ``{`` et la dernière ``}``.
    """
    if not texte:
        return None
    try:
        return json.loads(texte)
    except ValueError:
        pass
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut != -1 and fin > debut:
        try:
            return json.loads(texte[debut : fin + 1])
        except ValueError:
            return None
    return None


def verifier(
    offre: "Offre",
    profil: str | None = None,
    model: str | None = None,
    url: str | None = None,
    timeout: int | None = None,
) -> Verdict | None:
    """Fait juger une offre par Ollama et renvoie un ``Verdict`` (ou ``None``).

    Accès EXPLICITE aux champs de l'offre (title, company, location, description),
    jamais ``__dict__``. Tout échec (réseau, timeout, JSON invalide, réponse vide)
    est absorbé : on log un avertissement et on renvoie ``None`` — l'appelant
    retombe alors sur le score cosinus (dégradation gracieuse).
    """
    profil = profil if profil is not None else config.REQUETE_REFERENCE
    model = model or config.VERIFY_MODEL
    base = (url or config.VERIFY_OLLAMA_URL).rstrip("/")
    timeout = timeout if timeout is not None else config.VERIFY_TIMEOUT_S

    description = _extrait_pertinent(offre.description or "")
    prompt = _PROMPT.format(
        profil=profil,
        title=offre.title,
        company=offre.company,
        location=offre.location,
        description=description,
        n_phrases=config.VERIFY_JUSTIF_PHRASES,
    )
    charge = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": VERDICT_SCHEMA,
        "think": False,  # désactive le raisonnement (ignoré par les vieux Ollama)
        "options": {"temperature": 0, "num_predict": config.VERIFY_MAX_TOKENS},
    }

    try:
        reponse = requests.post(f"{base}/api/generate", json=charge, timeout=timeout)
        reponse.raise_for_status()
        brut = reponse.json().get("response", "")
    except requests.exceptions.RequestException as err:
        logger.warning("Vérification LLM : appel Ollama en échec (%s).", err)
        return None
    except ValueError:
        logger.warning("Vérification LLM : réponse Ollama non-JSON.")
        return None

    donnees = _extraire_json(brut)
    if not isinstance(donnees, dict):
        # Cas le plus fréquent : le JSON a été coupé net faute de tokens. On le
        # dit explicitement, sinon le réglage à changer est impossible à deviner.
        indice = (
            " (réponse tronquée : augmente VERIFY_MAX_TOKENS)"
            if brut and not brut.rstrip().endswith("}")
            else ""
        )
        logger.warning(
            "Vérification LLM : JSON du modèle illisible pour « %s »%s.", offre.title, indice
        )
        return None

    try:
        verdict = Verdict.from_dict(donnees)
    except Exception as err:  # noqa: BLE001 - un verdict malformé ne casse pas le run
        logger.warning("Vérification LLM : verdict inexploitable (%s).", err)
        return None
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
        print("⚠️  Ollama injoignable — lance `ollama serve` puis `ollama pull qwen3:8b`.")
    else:
        v = verifier(demo)
        print(json.dumps(v.to_dict() if v else None, ensure_ascii=False, indent=2))
