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

import hashlib
import html as html_module
import logging
import re
import unicodedata
from dataclasses import dataclass, asdict, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import recherche
from sources.provenance import familles_de

if TYPE_CHECKING:  # verdict typé sans import runtime (évite le cycle verifier<->normalize)
    from verifier import Verdict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Lien:
    """Une annonce chez UNE source : où elle est, et comment la reconnaître.

    ``cle`` identifie l'annonce chez sa source d'un run à l'autre, et c'est
    elle — jamais l'URL — qui décide qu'on a déjà vu ce lien. Trois formes,
    de la plus sûre à la moins sûre (cf. ``cle_native``) :

        id:<identifiant de la source>     adzuna, jobspy, free_work, france_travail, jooble
        contenu:<empreinte>               careerjet, qui n'expose aucun identifiant
        url:<URL normalisée>              repli, pour un item sans identifiant

    ``url`` n'est que la DERNIÈRE adresse connue : elle peut changer à chaque
    requête (Adzuna ajoute un jeton `se=`, Careerjet réchiffre l'URL entière).
    """

    source: str  # nom porté par l'offre : « adzuna », « jobspy:indeed »…
    cle: str
    url: str


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

    # Familles de requêtes (recherche.yaml) dont une requête a fait remonter
    # l'offre. PROVENANCE, pas classification : vide pour une source qui
    # n'interroge pas par terme (Free-Work filtre sur le type de contrat).
    familles: list[str] = field(default_factory=list)

    # Parmi `familles`, celles dont un terme figure DANS LE TITRE normalisé.
    # Une offre remontée par « LLM » dans sa description seulement porte la
    # famille, pas la famille-titre. Aucune requête : comparaison de chaînes.
    familles_titre: list[str] = field(default_factory=list)

    # Signalements qui n'excluent PAS l'offre (jobs étudiants : « alternance »).
    drapeaux: list[str] = field(default_factory=list)

    # Toutes les annonces fusionnées dans cette offre, une par (source, clé).
    # `url` et `source` ci-dessus restent ceux de la version la plus riche.
    liens: list[Lien] = field(default_factory=list)

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
# Échappements Unicode AMPUTÉS DE LEUR ANTISLASH, tels qu'Adzuna les livre :
# « Hu002FF » pour « H/F », « césure u002F fin d'étude », « pru00e9-embauche ».
# Mesuré le 2026-09-05 sur 100 offres Adzuna : u00e9 (é) ×10, u002F (/) ×5,
# u00e8 (è) ×5, u00e0 (à) ×2, u00a0 (espace insécable) ×1 — dans les titres ET
# les descriptions.
#
# Ce n'est pas cosmétique. `dedup._normaliser_titre` retire les mentions de
# genre pour que « (H/F) » et « (M/F) » produisent UNE clé ; « Hu002FF » n'est
# pas reconnu comme une mention de genre, donc la même annonce republiée dans
# les deux formes occupe deux lignes. Le correctif de déduplication du commit
# précédent était neutralisé sur ces titres-là.
#
# LA PLAGE EST VOLONTAIREMENT LIMITÉE À `u00XX`, et pas au `uXXXX` général :
# sans l'antislash, le motif large mordrait sur des mots français ordinaires.
# « aubade » contient « ubade », dont les quatre caractères sont des chiffres
# hexadécimaux valides — il deviendrait « a뫞 ». Exiger les deux zéros
# rend la collision impossible en pratique (aucun mot ne contient « u00 ») et
# couvre 100 % de ce qui a été observé : Adzuna n'émet que du Latin-1.
_MOTIF_ECHAPPEMENT = re.compile(r"u00([0-9a-fA-F]{2})")


def _desechapper(texte: str) -> str:
    """Restaure les caractères des échappements amputés (« u002F » -> « / »)."""
    if "u00" not in texte:
        return texte
    return _MOTIF_ECHAPPEMENT.sub(lambda m: chr(int(m.group(1), 16)), texte)


def _texte(valeur) -> str:
    """Convertit proprement une valeur potentiellement None en chaîne nettoyée."""
    if valeur is None:
        return ""
    return _desechapper(str(valeur)).strip()


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


# ---------------------------------------------------------------------------
# Identité d'une annonce chez sa source
# ---------------------------------------------------------------------------
# Sondé le 2026-09-17, deux requêtes identiques par source :
#
#   adzuna          `id` stable ; URL différente pour 6 offres sur 28 (`se=`)
#   careerjet       AUCUN identifiant ; URL différente pour 30 offres sur 30 —
#                   l'URL entière est un jeton chiffré, rien n'y est stable
#   free_work       `id` stable, URL stable
#   france_travail  `id` stable, URL stable
#   jobspy:indeed   `id` (« in-… ») stable, URL stable
#   jobspy:linkedin `id` (« li-… ») stable, URL stable
#   jooble          `id` — non sondé (source inactive)
#
# L'URL ne sert donc JAMAIS d'identité quand un identifiant existe. Careerjet
# n'en a pas, et son URL ne se normalise pas (aucune partie stable) : il est
# identifié par son CONTENU. Le repli par URL normalisée ne sert aujourd'hui à
# aucune source active — il couvre un item qui arriverait sans identifiant.
_CHAMP_IDENTIFIANT = {
    "adzuna": "id",
    "jooble": "id",
    "jobspy": "id",
    "france_travail": "id",
    "free_work": "id",
}

# Careerjet : titre, entreprise, lieu, site d'origine. La date est EXCLUE :
# elle peut être réécrite à une réindexation, ce qui créerait un second lien
# pour la même annonce. Deux annonces identiques sur ces quatre champs sont un
# doublon au sens de la dédup exacte de toute façon.
_CHAMPS_CONTENU = {
    "careerjet": ("title", "company", "locations", "site"),
}

# Paramètres de suivi retirés par le repli d'URL.
_PARAMETRES_SUIVI = {"se", "v", "fbclid", "gclid", "ref", "refid", "src", "trk",
                     "trackingid", "tracking_id"}


def _forme_comparable(texte) -> str:
    """Minuscules, sans accents, sans HTML ni ponctuation, espaces réduits."""
    t = _sans_html(texte).casefold()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", t))


def url_normalisee(url: str) -> str:
    """URL sans fragment ni paramètres de suivi, hôte en minuscules, paramètres triés."""
    morceaux = urlsplit((url or "").strip())
    parametres = sorted(
        (k, v) for k, v in parse_qsl(morceaux.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _PARAMETRES_SUIVI
    )
    return urlunsplit((morceaux.scheme.lower(), morceaux.netloc.lower(),
                       morceaux.path.rstrip("/"), urlencode(parametres), ""))


def cle_native(nom_source: str, brut: dict) -> str:
    """Clé stable d'un item BRUT chez sa source, ou "" si la source n'en fournit pas.

    Utilisée avant la normalisation (fusion des copies d'une même annonce
    trouvée par deux requêtes, `sources/provenance.py`) comme après.
    """
    champ = _CHAMP_IDENTIFIANT.get(nom_source)
    if champ:
        valeur = brut.get(champ)
        if valeur is not None and str(valeur).strip():
            return f"id:{str(valeur).strip()}"
    champs = _CHAMPS_CONTENU.get(nom_source)
    if champs:
        empreinte = "|".join(_forme_comparable(brut.get(c)) for c in champs)
        if empreinte.strip("|"):
            return "contenu:" + hashlib.sha1(empreinte.encode("utf-8")).hexdigest()
    return ""


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


def source_affichee(nom_source: str, brut: dict) -> str:
    """Nom que portera l'offre, et donc sa ligne du bilan : « jobspy:indeed » pour JobSpy.

    Calculable sur l'item BRUT, pour que le brut et le normalisé d'un même site
    tombent sur la même ligne.
    """
    if nom_source == "jobspy":
        return f"jobspy:{_texte(brut.get('site')) or 'inconnu'}"
    return nom_source


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
        source=source_affichee("jobspy", raw),
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
        # La provenance est portée par le dict brut (sources/provenance.py) et
        # recopiée ici, une fois, plutôt que dans chacun des normaliseurs.
        offre.familles = familles_de(item)
        offre.familles_titre = recherche.familles_dans_titre(offre.title, offre.familles)
        cle = cle_native(nom_source, item) or (
            f"url:{url_normalisee(offre.url)}" if offre.url else "")
        offre.liens = [Lien(offre.source, cle, offre.url)] if cle else []
        # On garde uniquement les offres a minima exploitables (titre + url).
        if offre.title and offre.url:
            offres.append(offre)

    logger.info("Normalisation %s : %d offre(s) exploitable(s).", nom_source, len(offres))
    return offres
