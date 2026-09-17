"""
dedup.py — Déduplication des offres.

Une même annonce peut apparaître sur plusieurs sources (ex. la même offre
récupérée via Adzuna ET via JobSpy). On la fait apparaître UNE seule fois.

Clé de dédoublonnage = hash de (titre normalisé + entreprise + ville).
En cas de collision (= doublon), on garde la version la PLUS RICHE : celle qui
apporte le plus d'information (description la plus longue, présence d'un salaire
et d'une date). On préserve ainsi un maximum de contexte pour le ranking.

La version écartée ne disparaît plus tout entière : ses LIENS (une annonce par
source) et ses FAMILLES sont rattachés à la version gardée (``_absorber``).
Avant, fusionner l'offre Adzuna dans l'offre Indeed supprimait l'URL Adzuna.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

import numpy as np

import communes
import config
import observabilite
import recherche
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


def _compter_fusions(avant: list[Offre], gardes_idx: list[int]) -> None:
    """Attribue à leur source les offres supprimées par une passe de dédup.

    La colonne « gardées » du bilan est comptée par `filters.filtrer`, qui
    tourne AVANT la déduplication : sans cette attribution, une offre fusionnée
    restait comptée comme gardée. Mesuré le 2026-09-05 : 79 disparitions sur
    447 n'apparaissaient dans aucun compteur, ni dans les logs autrement que
    par un total agrégé.

    Attribution PAR SOURCE, et pas seulement en total : c'est le total sans
    attribution qui a permis à ces 79 fusions de rester inexaminées.
    """
    restants = set(gardes_idx)
    for i, offre in enumerate(avant):
        if i not in restants:
            observabilite.signaler_fusion(offre.source)


def _absorber(garde: Offre, doublon: Offre) -> None:
    """Rattache à ``garde`` les liens et les familles de ``doublon``, qui va disparaître.

    Un lien est identifié par (source, clé) — jamais par l'URL, qui change
    d'une requête à l'autre chez Adzuna et Careerjet : la même annonce vue deux
    fois reste UN lien. Les familles-titre sont recalculées sur le titre de la
    version gardée, puisque c'est lui que le rapport affiche.
    """
    connus = {(l.source, l.cle) for l in garde.liens}
    for lien in doublon.liens:
        if (lien.source, lien.cle) not in connus:
            garde.liens.append(lien)
            connus.add((lien.source, lien.cle))
    garde.familles = garde.familles + [f for f in doublon.familles if f not in garde.familles]
    garde.familles_titre = recherche.familles_dans_titre(garde.title, garde.familles)


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
        if existante is None:
            meilleures[cle] = offre
        elif _richesse(offre) > _richesse(existante):
            _absorber(offre, existante)
            meilleures[cle] = offre
        else:
            _absorber(existante, offre)

    resultat = list(meilleures.values())
    # Même angle mort que la passe floue : la dédup exacte tourne elle aussi
    # APRÈS `filters.filtrer`, donc après le comptage des « gardées ».
    gardees_idx = [i for i, o in enumerate(offres) if any(o is r for r in resultat)]
    _compter_fusions(offres, gardees_idx)
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
# Volume horaire annoncé DANS LE TITRE : « CDI 10H », « Equipier Polyvalent
# 30H », « 24H / SEMAINE », « Temps Partiel 19h ». Bornes 1-40 pour écarter les
# années et les codes postaux ; la garde négative écarte « 8h30 » (une heure de
# la journée, pas un volume) et les nombres décimaux.
_MOTIF_HEURES_TITRE = re.compile(r"(?<![\d,.])(\d{1,2})\s?[hH](?:\b|/|\s|$)(?!\d{2}\b)")


def _heures_titre(titre: str) -> frozenset[int]:
    """Volumes horaires hebdomadaires lisibles dans le titre. Vide si aucun."""
    return frozenset(
        int(x) for x in _MOTIF_HEURES_TITRE.findall(titre or "") if 1 <= int(x) <= 40
    )


def _identite_fusionnable(offre: Offre) -> tuple[str, str, frozenset[int]] | None:
    """(entreprise, commune, heures du titre) — ou ``None`` si indéterminable.

    Garde-fou de ``dedupliquer_flou``. Le cosinus est calculé sur
    ``ranker._texte_a_encoder``, c'est-à-dire **titre + description** :
    l'entreprise et le lieu n'entrent PAS dans le vecteur. Deux annonces au
    même gabarit sont donc à cosinus ~1 même si elles n'ont ni le même
    employeur ni la même ville — ce qui n'est pas un cas d'école :

        SERVEUR H/F | Chez Justine   ~  Serveur H/F | Tripletta       cos 0.990
        Equipier Polyvalent 30H | LIDL Fresnes ~ 35H | LIDL Esbly     cos 0.977
        Vendeur(euse) en CDI 10H ~ Vendeur(euse) en CDI 35H | Lovisa  cos ~0.98

    Le VOLUME HORAIRE du titre est la troisième composante, et elle est
    indispensable : entreprise et commune ne suffisent pas, parce que les
    fusions les plus coûteuses ont lieu chez le MÊME employeur dans la MÊME
    commune. Mesuré le 2026-09-05, avec le garde-fou entreprise+commune seul,
    5 fusions détruisaient encore l'horaire :

        Vendeur(euse) en CDI 10H   ~  Vendeur(euse) en CDI 35H   | Lovisa, Paris
        CDI 18H - Vendeur          ~  CDI 35H - Vendeur     | Petit Bateau, Paris
        35h - Équipier Polyvalent  ~  30h/Semaine - Équipier | Hello You, Paris

    Un poste à 10 h par semaine et un poste à 35 h ne sont pas la même offre —
    et dans le mode `job_etudiant`, c'est précisément le champ qui décide. Deux
    titres dont les horaires diffèrent ne fusionnent donc jamais ; un titre qui
    annonce un horaire ne fusionne pas non plus avec un titre muet, par la même
    asymétrie que ci-dessous.

    ``None`` quand l'entreprise ou la commune manque : dans ce cas on NE FUSIONNE
    PAS. L'asymétrie est délibérée — une fusion manquée coûte un doublon dans la
    liste, une fusion abusive SUPPRIME une offre réelle sans laisser de trace.
    C'est ce qui a sauvé la paire « Aide-soignant de JOUR » / « Aide-soignant de
    NUIT », publiée sans nom d'entreprise et rapprochée à cosinus 0,999.
    """
    entreprise = _sans_accents((offre.company or "").lower()).strip()
    entreprise = re.sub(r"[^a-z0-9]+", " ", entreprise).strip()
    if not entreprise:
        return None
    commune = communes.cle_ville(offre.location or "")
    if not commune:
        return None
    return entreprise, commune, _heures_titre(offre.title)


def dedupliquer_flou(
    offres: list[Offre], embeddings: np.ndarray, seuil: float | None = None
) -> tuple[list[Offre], np.ndarray]:
    """Fusionne les quasi-doublons par similarité cosinus des embeddings.

    ``embeddings`` doit être aligné sur ``offres`` (même ordre) et NORMALISÉ
    (produit scalaire = cosinus). Retourne (offres_restantes, embeddings_alignés)
    pour que l'appelant puisse enchaîner le ranking sans ré-encoder.

    ## Deux conditions, pas une

    Le cosinus reste le test du TEXTE. Il est désormais assorti d'une condition
    d'IDENTITÉ (``_identite_fusionnable``) : même entreprise ET même commune.
    Mesuré le 2026-09-05 sur 447 offres de type job étudiant, le cosinus seul à
    0,90 fusionnait 79 offres, dont **37 à travers des communes différentes** et
    **22 à travers des employeurs différents**. Six fusions détruisaient
    l'horaire annoncé dans le titre (« CDI 10H » absorbé par « CDI 35H »), et une
    confondait un poste de jour avec le même poste de nuit, à cosinus 0,999.

    Ce n'est pas un problème de seuil : à 0,99 il restait 17 fusions entre
    employeurs distincts et 19 entre communes distinctes. Le seuil ne peut pas
    séparer ce que le vecteur ne contient pas.

    L'offre écartée est DÉTRUITE — elle n'atteint ni le ranking, ni la base, ni
    les exports ; seuls ses liens et ses familles survivent, rattachés à
    l'offre gardée. Son texte est perdu : c'est ce qui justifie de faillir du
    côté de la conservation.
    """
    seuil = config.SEUIL_DEDUP_FLOU if seuil is None else seuil
    emb = np.asarray(embeddings)
    if len(offres) <= 1:
        return offres, emb

    identites = [_identite_fusionnable(o) for o in offres]
    gardes_idx: list[int] = []  # indices (dans `offres`) des offres conservées
    fusions = 0
    refus_identite = 0
    for i, offre in enumerate(offres):
        doublon_de = None
        for pos, j in enumerate(gardes_idx):
            # Vecteurs normalisés -> cosinus = produit scalaire.
            if float(emb[i] @ emb[j]) < seuil:
                continue
            # Texte quasi identique : reste à vérifier que c'est bien la même
            # offre, et pas le même gabarit ailleurs.
            if identites[i] is None or identites[i] != identites[j]:
                refus_identite += 1
                continue
            doublon_de = pos
            break
        if doublon_de is None:
            gardes_idx.append(i)
            continue
        # Doublon flou : on garde la version la plus riche des deux, et elle
        # emporte les liens et familles de l'autre.
        fusions += 1
        j = gardes_idx[doublon_de]
        if _richesse(offre) > _richesse(offres[j]):
            _absorber(offre, offres[j])
            gardes_idx[doublon_de] = i
        else:
            _absorber(offres[j], offre)

    offres_gardees = [offres[i] for i in gardes_idx]
    emb_gardes = emb[gardes_idx] if gardes_idx else emb[:0]
    logger.info(
        "Déduplication floue (cos ≥ %.2f) : %d offre(s) restante(s), "
        "%d quasi-doublon(s) fusionné(s), %d rapprochement(s) refusé(s) "
        "(entreprise ou commune différente).",
        seuil, len(offres_gardees), fusions, refus_identite,
    )
    _compter_fusions(offres, gardes_idx)
    return offres_gardees, emb_gardes
