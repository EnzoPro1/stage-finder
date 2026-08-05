"""
extract.py — Extraction légère de la durée et de la date de début d'un stage.

Ces informations sont souvent seulement en texte libre : en faire un filtre dur
jetterait de vraies offres (durée non annoncée = offre perdue). On les extrait
donc par regex pour en tirer :
  - des COLONNES filtrables (durée en mois, date de début) exportées dans le CSV ;
  - des SIGNAUX SOUPLES : un petit boost si la durée / la date collent à la cible,
    un petit malus si la durée est nettement trop courte.

Rien n'est jamais rejeté ici : on annote, le ranking décide.
"""

from __future__ import annotations

import re

import config

# ---------------------------------------------------------------------------
# Durée
# ---------------------------------------------------------------------------
# « stage de 6 mois », « stage 4 à 6 mois », « 6-month internship »,
# « durée : 6 mois », « for 6 months ». On capture le plus grand nombre de mois
# mentionné (une fourchette « 4 à 6 mois » vise en pratique 6).
_MOIS_FR = re.compile(r"(\d{1,2})\s*(?:à|-|/|a)?\s*(\d{1,2})?\s*mois", re.IGNORECASE)
_MOIS_EN = re.compile(r"(\d{1,2})\s*(?:to|-|/)?\s*(\d{1,2})?\s*month", re.IGNORECASE)
# « semaines » : converti approximativement en mois (une piste de repli).
_SEMAINES = re.compile(r"(\d{1,2})\s*semaines?", re.IGNORECASE)


def extraire_duree_mois(texte: str) -> int | None:
    """Retourne la durée du stage en mois si elle est lisible, sinon None.

    Sur une fourchette (« 4 à 6 mois »), on retient la borne haute : c'est la
    durée maximale offerte, la plus favorable pour un stage de fin d'études.
    """
    if not texte:
        return None
    candidats: list[int] = []
    for motif in (_MOIS_FR, _MOIS_EN):
        for m in motif.finditer(texte):
            bornes = [int(g) for g in m.groups() if g and 0 < int(g) <= 24]
            if bornes:
                candidats.append(max(bornes))
    if candidats:
        # Plusieurs mentions : on prend la plus fréquente / la plus grande plausible.
        return max(candidats)
    # Repli : durée en semaines convertie en mois.
    sem = _SEMAINES.search(texte)
    if sem:
        semaines = int(sem.group(1))
        if 1 <= semaines <= 104:
            return max(1, round(semaines / 4.33))
    return None


# ---------------------------------------------------------------------------
# Date de début
# ---------------------------------------------------------------------------
_MOIS_NOMS = {
    "janvier": 1, "january": 1, "février": 2, "fevrier": 2, "february": 2,
    "mars": 3, "march": 3, "avril": 4, "april": 4, "mai": 5, "may": 5,
    "juin": 6, "june": 6, "juillet": 7, "july": 7, "août": 8, "aout": 8,
    "august": 8, "septembre": 9, "september": 9, "octobre": 10, "october": 10,
    "novembre": 11, "november": 11, "décembre": 12, "decembre": 12, "december": 12,
}
_NOMS_ALT = "|".join(sorted(_MOIS_NOMS, key=len, reverse=True))

# « à partir de janvier 2027 », « début : mars 2027 », « starting January 2027 »,
# « dès septembre 2026 ». On exige une année à 4 chiffres pour éviter le bruit.
_DATE_MOIS_ANNEE = re.compile(
    rf"(?:à partir de|a partir de|dès|des|début|debut|starting|from|start)?\s*"
    rf"({_NOMS_ALT})\s+(20\d{{2}})",
    re.IGNORECASE,
)
# Format numérique « 01/2027 » ou « 2027-01 ».
_DATE_NUM = re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b|\b(0?[1-9]|1[0-2])[-/](20\d{2})\b")


def extraire_date_debut(texte: str) -> tuple[int, int] | None:
    """Retourne (année, mois) de début si lisible, sinon None."""
    if not texte:
        return None
    m = _DATE_MOIS_ANNEE.search(texte)
    if m:
        mois = _MOIS_NOMS.get(m.group(1).lower())
        annee = int(m.group(2))
        if mois:
            return annee, mois
    m = _DATE_NUM.search(texte)
    if m:
        if m.group(1):  # AAAA-MM
            return int(m.group(1)), int(m.group(2))
        return int(m.group(4)), int(m.group(3))  # MM/AAAA
    return None


def _fmt_date(date: tuple[int, int] | None) -> str:
    """(2027, 1) -> '2027-01' ; None -> ''."""
    if not date:
        return ""
    annee, mois = date
    return f"{annee:04d}-{mois:02d}"


# ---------------------------------------------------------------------------
# Point d'entrée : annote une offre
# ---------------------------------------------------------------------------
def annoter(offre) -> None:
    """Renseigne offre.duree_mois et offre.date_debut à partir de titre+desc.

    Modifie l'offre en place (colonnes filtrables). N'exclut jamais rien.
    """
    texte = f"{offre.title}. {offre.description}"
    offre.duree_mois = extraire_duree_mois(texte)
    offre.date_debut = _fmt_date(extraire_date_debut(texte))


def boost_signaux(offre) -> float:
    """Boost/malus souple selon la durée et la date de début extraites.

    - Durée proche de la cible (6 mois) -> petit bonus.
    - Durée nettement trop courte (< min acceptable) -> petit malus.
    - Date de début qui colle au mois/année visés -> petit bonus.
    Une info absente ne pénalise pas (0), conformément à l'esprit « signal souple ».
    """
    delta = 0.0

    duree = getattr(offre, "duree_mois", None)
    if duree is not None:
        if duree >= config.DUREE_CIBLE_MOIS:
            delta += config.BONUS_DUREE_CIBLE
        elif duree < config.DUREE_MIN_ACCEPTABLE:
            delta -= config.MALUS_DUREE_COURTE

    date_debut = getattr(offre, "date_debut", "") or ""
    cible = f"{config.DATE_DEBUT_CIBLE_ANNEE:04d}-{config.DATE_DEBUT_CIBLE_MOIS:02d}"
    if date_debut:
        # Bonus si l'année de début est la bonne (mois exact = bonus plein).
        if date_debut == cible:
            delta += config.BONUS_DATE_DEBUT
        elif date_debut[:4] == cible[:4]:
            delta += config.BONUS_DATE_DEBUT / 2

    return delta


def annoter_toutes(offres: list) -> None:
    """Annote toute une liste d'offres en place."""
    for offre in offres:
        annoter(offre)


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    exemples = [
        "Stage de 6 mois à partir de janvier 2027, Paris.",
        "6-month internship starting September 2026.",
        "Stage 4 à 6 mois, début mars 2027.",
        "Alternance 12 mois.",
        "Stage de 3 mois cet été.",
        "Contrat sans durée précisée.",
    ]
    for txt in exemples:
        print(f"- {txt!r}")
        print(f"    durée = {extraire_duree_mois(txt)} mois | "
              f"début = {_fmt_date(extraire_date_debut(txt)) or '—'}")
