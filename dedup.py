"""
dedup.py — Déduplication des offres.

Une même annonce peut apparaître sur plusieurs sources (ex. la même offre
récupérée via Adzuna ET via JobSpy). On la fait apparaître UNE seule fois.

Clé de dédoublonnage = hash de (titre normalisé + entreprise + ville).
En cas de collision (= doublon), on garde la version la PLUS RICHE : celle qui
apporte le plus d'information (description la plus longue, présence d'un salaire
et d'une date). On préserve ainsi un maximum de contexte pour le ranking.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

import numpy as np

import config
from normalize import Offre

logger = logging.getLogger(__name__)


def _sans_accents(texte: str) -> str:
    """Retire les accents (é -> e) pour un rapprochement robuste."""
    nfkd = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Mentions de genre accolées aux intitulés. Une même annonce publiée en
# « (H/F) » et en « (M/F) » doit produire UNE clé, pas deux.
#
# Les paires sont énumérées explicitement plutôt que dérivées d'une classe
# de caractères : « [hfmw]/[hfmw] » accepterait « h/h » ou « f/w », qui ne
# sont pas des mentions de genre, et l'appliquer à un intitulé anglais
# découperait des sigles au hasard.
#
#   fr  H/F, F/H          en  M/F, F/M        de  M/W, W/M (souvent M/W/D)
#
# Le terme non binaire optionnel (D pour « divers », N, X) est accepté
# des DEUX CÔTÉS de la paire : les annonces allemandes écrivent « m/w/d »,
# mais « (x/f/m) » existe aussi et se rencontre en base. Ne l'accepter
# qu'en fin laisserait un « x » orphelin dans la clé.
#
# Le SÉPARATEUR EST OBLIGATOIRE, et c'est une correction : la version
# précédente le rendait optionnel (« h\s*[/-]?\s*f »), ce qui faisait
# correspondre un simple « hf » au milieu d'un mot. « Freshfields »
# devenait « fres ields », et le nom du cabinet entrait dans la clé
# amputé de deux lettres.
#
# Les frontières de mot sont là pour la même raison, dans l'autre sens :
# sans elles, « Data RH F/H » voyait le « h » de « RH » avalé avec le
# « f » qui suit, laissant « data r h » — le sigle détruit ET la mention
# mal retirée.
_MENTION_GENRE = re.compile(
    r"[(\[]?\s*"
    r"\b(?:[dnx]\s*[/-]\s*)?"                # non binaire en tête : (x/f/m)
    r"(?:h\s*[/-]\s*f|f\s*[/-]\s*h"          # français
    r"|m\s*[/-]\s*f|f\s*[/-]\s*m"            # anglais
    r"|m\s*[/-]\s*w|w\s*[/-]\s*m)"           # allemand
    r"(?:\s*[/-]\s*[dnx])?\b"                # non binaire en queue : (m/w/d)
    r"\s*[)\]]?"
)


def _normaliser_titre(titre: str) -> str:
    """Normalise un titre : minuscules, sans accents, sans mentions parasites."""
    t = _sans_accents(titre.lower())
    # Retire les mentions de genre courantes qui font varier les titres identiques.
    t = _MENTION_GENRE.sub(" ", t)
    # Ne garde que lettres/chiffres, réduit les espaces multiples.
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _ville(location: str) -> str:
    """Extrait une ville comparable : 1er segment de la localisation, sans accents."""
    premier = location.split(",")[0]
    ville = _sans_accents(premier.lower())
    ville = re.sub(r"[^a-z0-9]+", " ", ville)
    # Regroupe tous les arrondissements parisiens sous "paris".
    ville = re.sub(r"\bparis\b.*", "paris", ville)
    return ville.strip()


def _cle(offre: Offre) -> str:
    """Construit la clé de dédoublonnage (hash SHA-1 lisible-agnostique)."""
    base = "|".join(
        [
            _normaliser_titre(offre.title),
            _sans_accents(offre.company.lower()).strip(),
            _ville(offre.location),
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _richesse(offre: Offre) -> int:
    """Score de richesse d'une offre : plus c'est haut, plus l'offre est complète."""
    score = len(offre.description)
    if offre.salary:
        score += 100  # un salaire renseigné est précieux
    if offre.posted_at:
        score += 30
    return score


def dedupliquer(offres: list[Offre]) -> list[Offre]:
    """Retourne la liste dédoublonnée, en gardant la version la plus riche."""
    meilleures: dict[str, Offre] = {}
    for offre in offres:
        cle = _cle(offre)
        existante = meilleures.get(cle)
        if existante is None or _richesse(offre) > _richesse(existante):
            meilleures[cle] = offre

    resultat = list(meilleures.values())
    logger.info(
        "Déduplication : %d offre(s) unique(s) sur %d (%d doublon(s) retiré(s)).",
        len(resultat),
        len(offres),
        len(offres) - len(resultat),
    )
    return resultat


# ---------------------------------------------------------------------------
# Déduplication FLOUE par embeddings
# ---------------------------------------------------------------------------
# Le hash exact ne rapproche pas « Sanofi » et « Sanofi Group », ou deux
# reformulations d'une même annonce. Comme les embeddings sont DÉJÀ calculés
# pour le ranking, on s'en sert : deux offres dont les vecteurs sont très
# proches (cosinus > seuil) sont considérées comme un seul et même poste.
# On garde la version la plus riche, exactement comme la dédup exacte.
#
# Ordre efficace : le hash exact (rapide) filtre déjà le gros du volume ; cette
# passe cosinus ne s'applique qu'au résidu.
def dedupliquer_flou(
    offres: list[Offre], embeddings: np.ndarray, seuil: float | None = None
) -> tuple[list[Offre], np.ndarray]:
    """Fusionne les quasi-doublons par similarité cosinus des embeddings.

    ``embeddings`` doit être aligné sur ``offres`` (même ordre) et NORMALISÉ
    (produit scalaire = cosinus). Retourne (offres_restantes, embeddings_alignés)
    pour que l'appelant puisse enchaîner le ranking sans ré-encoder.
    """
    seuil = config.SEUIL_DEDUP_FLOU if seuil is None else seuil
    emb = np.asarray(embeddings)
    if len(offres) <= 1:
        return offres, emb

    gardes_idx: list[int] = []  # indices (dans `offres`) des offres conservées
    fusions = 0
    for i, offre in enumerate(offres):
        doublon_de = None
        for pos, j in enumerate(gardes_idx):
            # Vecteurs normalisés -> cosinus = produit scalaire.
            if float(emb[i] @ emb[j]) >= seuil:
                doublon_de = pos
                break
        if doublon_de is None:
            gardes_idx.append(i)
            continue
        # Doublon flou : on garde la version la plus riche des deux.
        fusions += 1
        j = gardes_idx[doublon_de]
        if _richesse(offre) > _richesse(offres[j]):
            gardes_idx[doublon_de] = i

    offres_gardees = [offres[i] for i in gardes_idx]
    emb_gardes = emb[gardes_idx] if gardes_idx else emb[:0]
    logger.info(
        "Déduplication floue (cos ≥ %.2f) : %d offre(s) restante(s), "
        "%d quasi-doublon(s) fusionné(s).",
        seuil, len(offres_gardees), fusions,
    )
    return offres_gardees, emb_gardes
