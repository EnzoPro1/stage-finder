"""
normalize.py — Ramène chaque offre brute (quel que soit son fournisseur)
à un schéma commun unique.

Schéma commun (dataclass ``Offre``) :
    title, company, location, description, url, source, posted_at, salary

Chaque source ayant un JSON différent, on écrit un normaliseur par source.
Le point d'entrée est ``normaliser(nom_source, items)`` qui dispatche vers
le bon convertisseur et ignore silencieusement les items non convertibles
(un item cassé ne doit pas faire tomber tout le lot).
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, asdict, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # verdict typé sans import runtime (évite le cycle verifier<->normalize)
    from verifier import Verdict

logger = logging.getLogger(__name__)


@dataclass
class Offre:
    """Schéma commun à toutes les sources."""

    title: str
    company: str
    location: str
    description: str
    url: str
    source: str
    posted_at: str  # date de publication, format libre selon la source (ou "")
    salary: str  # chaîne lisible (ou "")

    # Badges de correspondance mots-clés, remplis par le ranker (ex. ["IA", "Cyber"]).
    # Placé en dernier avec une valeur par défaut : les normaliseurs positionnels
    # existants continuent de fonctionner sans le préciser.
    tags: list[str] = field(default_factory=list)

    # Signaux extraits par extract.py (colonnes filtrables). None/"" si illisibles.
    duree_mois: int | None = None      # durée du stage en mois
    date_debut: str = ""               # date de début au format "AAAA-MM"

    # Verdict de la vérification LLM (verifier.py), attaché pour la shortlist
    # vérifiée. None si la couche est désactivée ou l'appel Ollama a échoué.
    verdict: "Verdict | None" = None

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _texte(valeur) -> str:
    """Convertit proprement une valeur potentiellement None en chaîne nettoyée."""
    if valeur is None:
        return ""
    return str(valeur).strip()


# Balises HTML + entités : plusieurs sources livrent leur description en HTML
# (<p>, <br />, <b>…). Ces balises polluent l'embedding ET le prompt du LLM
# (tokens gaspillés, phrases coupées) : on les retire à la normalisation, une
# fois pour toutes, plutôt que dans chaque consommateur.
_MOTIF_BALISE = re.compile(r"<[^>]+>")
_MOTIF_ESPACES = re.compile(r"[ \t ]+")


def _sans_html(valeur) -> str:
    """Convertit en texte brut lisible : balises retirées, entités décodées."""
    texte = _texte(valeur)
    if not texte:
        return ""
    # <br> et </p> marquent une rupture : on les remplace par un espace pour ne
    # pas recoller deux phrases ("...sécurité<br/>Vous serez..." -> deux mots).
    texte = re.sub(r"(?i)<\s*(br|/p|/div|/li)\s*/?\s*>", " ", texte)
    texte = _MOTIF_BALISE.sub("", texte)
    texte = html_module.unescape(texte)
    return _MOTIF_ESPACES.sub(" ", texte).strip()


def _salaire_depuis_bornes(mini, maxi, devise: str = "€") -> str:
    """Construit une chaîne salaire lisible à partir de bornes numériques."""
    mini = mini if isinstance(mini, (int, float)) and mini else None
    maxi = maxi if isinstance(maxi, (int, float)) and maxi else None
    if mini and maxi:
        return f"{int(mini)} - {int(maxi)} {devise}"
    if mini:
        return f"à partir de {int(mini)} {devise}"
    if maxi:
        return f"jusqu'à {int(maxi)} {devise}"
    return ""


# ---------------------------------------------------------------------------
# Normaliseurs par source
# ---------------------------------------------------------------------------
def normaliser_adzuna(raw: dict) -> Offre:
    """Convertit une offre brute Adzuna."""
    return Offre(
        title=_texte(raw.get("title")),
        company=_texte((raw.get("company") or {}).get("display_name")),
        location=_texte((raw.get("location") or {}).get("display_name")),
        description=_texte(raw.get("description")),
        url=_texte(raw.get("redirect_url")),
        source="adzuna",
        posted_at=_texte(raw.get("created")),
        salary=_salaire_depuis_bornes(raw.get("salary_min"), raw.get("salary_max")),
    )


def normaliser_jooble(raw: dict) -> Offre:
    """Convertit une offre brute Jooble."""
    return Offre(
        title=_texte(raw.get("title")),
        company=_texte(raw.get("company")),
        location=_texte(raw.get("location")),
        # Jooble met le texte de l'annonce dans "snippet" (HTML léger possible).
        description=_sans_html(raw.get("snippet")),
        url=_texte(raw.get("link")),
        source="jooble",
        posted_at=_texte(raw.get("updated")),
        salary=_texte(raw.get("salary")),
    )


def normaliser_jobspy(raw: dict) -> Offre:
    """Convertit une ligne JobSpy (dict issu d'un DataFrame)."""
    salaire = _salaire_depuis_bornes(
        raw.get("min_amount"), raw.get("max_amount"), _texte(raw.get("currency")) or "€"
    )
    return Offre(
        title=_texte(raw.get("title")),
        company=_texte(raw.get("company")),
        location=_texte(raw.get("location")),
        description=_texte(raw.get("description")),
        url=_texte(raw.get("job_url")),
        # "site" = indeed / linkedin / google : on préfixe pour la traçabilité.
        source=f"jobspy:{_texte(raw.get('site')) or 'inconnu'}",
        posted_at=_texte(raw.get("date_posted")),
        salary=salaire,
    )


def normaliser_france_travail(raw: dict) -> Offre:
    """Convertit une offre brute de l'API France Travail."""
    return Offre(
        title=_texte(raw.get("intitule")),
        company=_texte((raw.get("entreprise") or {}).get("nom")),
        location=_texte((raw.get("lieuTravail") or {}).get("libelle")),
        description=_texte(raw.get("description")),
        # L'URL publique de l'offre est dans origineOffre.urlOrigine.
        url=_texte((raw.get("origineOffre") or {}).get("urlOrigine")),
        source="france_travail",
        posted_at=_texte(raw.get("dateCreation")),
        salary=_texte((raw.get("salaire") or {}).get("libelle")),
    )


def normaliser_careerjet(raw: dict) -> Offre:
    """Convertit une offre brute Careerjet.

    Deux spécificités : le lieu est dans ``locations`` (pluriel, chaîne libre)
    et la description est un EXTRAIT avec des ``<b>`` autour des mots trouvés —
    d'où le nettoyage HTML.
    """
    return Offre(
        title=_sans_html(raw.get("title")),
        company=_texte(raw.get("company")),
        location=_texte(raw.get("locations")),
        description=_sans_html(raw.get("description")),
        url=_texte(raw.get("url")),
        source="careerjet",
        # Format RFC 1123 ("Thu, 30 Jul 2026 22:21:35 GMT"), parsé par filters.py.
        posted_at=_texte(raw.get("date")),
        salary=_texte(raw.get("salary")),
    )


# Gabarit d'URL publique d'une annonce Free-Work : l'API ne renvoie pas d'URL,
# seulement les « slugs » du métier et de l'annonce.
_FREE_WORK_URL = "https://www.free-work.com/fr/tech-it/{metier}/job-mission/{annonce}"


def normaliser_free_work(raw: dict) -> Offre:
    """Convertit une annonce brute Free-Work.

    L'API ne fournit ni URL ni lieu à plat : on reconstruit l'URL publique à
    partir des slugs et on prend le libellé de localisation le plus précis.
    """
    lieu = raw.get("location") or {}
    metier = (raw.get("job") or {}).get("nameForContributionSlug") or "informatique"
    annonce = _texte(raw.get("slug"))
    url = _texte(raw.get("applicationUrl")) or (
        _FREE_WORK_URL.format(metier=metier, annonce=annonce) if annonce else ""
    )
    return Offre(
        title=_texte(raw.get("title")),
        company=_texte((raw.get("company") or {}).get("name")),
        # "label" = "Paris, France" / "Massy, Île-de-France" ; sinon la région.
        location=_texte(lieu.get("label") or lieu.get("adminLevel1")),
        description=_sans_html(raw.get("description")),
        url=url,
        source="free_work",
        posted_at=_texte(raw.get("publishedAt") or raw.get("createdAt")),
        salary=_salaire_depuis_bornes(
            raw.get("minAnnualSalary"), raw.get("maxAnnualSalary")
        ),
    )


# Table de dispatch : nom logique de source -> fonction de normalisation.
_NORMALISEURS = {
    "adzuna": normaliser_adzuna,
    "jooble": normaliser_jooble,
    "jobspy": normaliser_jobspy,
    "france_travail": normaliser_france_travail,
    "careerjet": normaliser_careerjet,
    "free_work": normaliser_free_work,
}


def normaliser(nom_source: str, items: list[dict]) -> list[Offre]:
    """
    Normalise une liste d'items bruts provenant de ``nom_source``.

    Un item qui provoque une erreur de conversion est ignoré (avec un log
    debug) plutôt que de faire échouer tout le lot.
    """
    convertisseur = _NORMALISEURS.get(nom_source)
    if convertisseur is None:
        logger.warning("Aucun normaliseur pour la source « %s ».", nom_source)
        return []

    offres: list[Offre] = []
    for item in items:
        try:
            offre = convertisseur(item)
        except Exception as err:  # noqa: BLE001 - item cassé : on le saute
            logger.debug("Item ignoré (%s) : %s", nom_source, err)
            continue
        # On garde uniquement les offres a minima exploitables (titre + url).
        if offre.title and offre.url:
            offres.append(offre)

    logger.info("Normalisation %s : %d offre(s) exploitable(s).", nom_source, len(offres))
    return offres
