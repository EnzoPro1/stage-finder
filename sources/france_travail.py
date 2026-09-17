"""
sources/france_travail.py — Source d'offres via l'API officielle France Travail
(ex-Pôle emploi). Officielle, gratuite, elle couvre un gisement FRANÇAIS que
les sources anglo-orientées (Indeed/LinkedIn) ratent en partie.

Doc : https://francetravail.io/produits-partages/catalogue/offres-emploi
Auth : OAuth2 « client_credentials ».
  - Token  : POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire
  - Search : GET  https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search

Sécurité (cf. AMELIORATIONS §4.2) : la clé (client_secret) ne transite JAMAIS en
query string. Elle sert à obtenir un jeton (corps de requête x-www-form-urlencoded),
et toutes les requêtes de recherche s'authentifient par un en-tête
`Authorization: Bearer <token>`.

Mêmes garanties que les autres sources :
- Identifiants absents => warning + [] (pas de crash).
- Toute erreur réseau/format est capturée par terme.
- Renvoie des dicts BRUTS ; la conversion se fait dans normalize.py.

Testable isolément :  python -m sources.france_travail
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import requests
from dotenv import load_dotenv

import config
import observabilite
import recherche
from sources import provenance

load_dotenv()

logger = logging.getLogger(__name__)

NOM_SOURCE = "france_travail"

_URL_TOKEN = (
    "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
)
_URL_SEARCH = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
_SCOPE = "api_offresdemploiv2 o2dsoffre"

# Code INSEE de la région Île-de-France.
#
# On filtre par RÉGION et non par liste de départements : l'API plafonne le
# paramètre ``departement`` à 5 valeurs et rejette la requête (HTTP 400
# « Le nombre de départements maximum autorisé pour la recherche est de 5 »)
# — or l'Île-de-France en compte 8. Le filtre géographique fin reste fait en
# aval par filters.py.
_REGION_IDF = "11"

# Débit maximal imposé par France Travail : 10 appels/seconde. On garde une
# marge (8/s) et on sérialise les appels du module derrière un verrou : la
# collecte est parallélisée entre SOURCES, donc rien ne garantit sans ça qu'on
# ne parte pas en rafale si un jour on paginait en parallèle.
_INTERVALLE_MIN_S = 1 / 8
_verrou_debit = threading.Lock()
_dernier_appel = 0.0

# Jetons mis en cache en mémoire, INDEXÉS PAR SCOPE : chaque API France Travail
# (offres, ROMEO…) exige le sien. Valeur : {"valeur": jeton, "expire_a": epoch}.
_token_cache: dict[str, dict] = {}


def _respecter_debit() -> None:
    """Bloque le temps qu'il faut pour ne jamais dépasser le quota d'appels/s."""
    global _dernier_appel
    with _verrou_debit:
        attente = _INTERVALLE_MIN_S - (time.monotonic() - _dernier_appel)
        if attente > 0:
            time.sleep(attente)
        _dernier_appel = time.monotonic()


def _identifiants() -> tuple[str | None, str | None]:
    """Retourne (client_id, client_secret) lus dans l'environnement (ou None)."""
    return os.getenv("FRANCE_TRAVAIL_ID"), os.getenv("FRANCE_TRAVAIL_KEY")


def obtenir_jeton(scope: str | None = None) -> str | None:
    """Jeton OAuth2 France Travail pour un scope donné, ou ``None``.

    Point d'entrée PARTAGÉ par tous les modules qui parlent à France Travail
    (offres, ROMEO…) : l'authentification, le cache de jeton et la lecture des
    identifiants vivent ici, une seule fois. Le cache est indexé PAR SCOPE — un
    jeton « offres » n'ouvre pas ROMEO.
    """
    client_id, client_secret = _identifiants()
    if not client_id or not client_secret:
        return None
    return _obtenir_token(client_id, client_secret, scope or _SCOPE)


def _obtenir_token(client_id: str, client_secret: str, scope: str | None = None) -> str | None:
    """Récupère un jeton OAuth2 (mis en cache par scope, jusqu'à expiration)."""
    scope = scope or _SCOPE
    maintenant = time.time()
    entree = _token_cache.get(scope)
    if entree and maintenant < entree["expire_a"]:
        return entree["valeur"]

    try:
        reponse = requests.post(
            _URL_TOKEN,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=config.TIMEOUT_HTTP,
        )
        reponse.raise_for_status()
        donnees = reponse.json()
    except (requests.exceptions.RequestException, ValueError) as err:
        # On ne loggue pas `err` brut : l'exception requests peut embarquer l'URL,
        # mais surtout on veut éviter toute fuite de corps de requête.
        categorie, detail = observabilite.categorie_requests(err)
        # L'authentification est un cas à part : « auth » dit qu'il faut
        # renouveler des identifiants, là où « http 500 » dit d'attendre.
        observabilite.signaler(NOM_SOURCE, "auth" if categorie == "http" else categorie,
                               f"authentification : {detail}")
        logger.warning("France Travail : échec d'authentification (%s).", type(err).__name__)
        return None

    token = donnees.get("access_token")
    duree = int(donnees.get("expires_in", 1400))
    # Marge de sécurité de 60 s pour ne pas utiliser un jeton juste expiré.
    _token_cache[scope] = {"valeur": token, "expire_a": maintenant + max(0, duree - 60)}
    return token


_MOTIF_MOT = re.compile(r"[^\W_]+", re.UNICODE)


def mots_cles(terme: str) -> str:
    """Un terme de recherche.yaml réduit à ce que ``motsCles`` accepte.

    L'API ne tolère que lettres, chiffres et espaces, et des mots d'au moins
    deux caractères : « optimisation de l'inférence » devient « optimisation
    de inférence ». Le sens conjonctif ne change pas (tous les mots exigés).
    """
    return " ".join(m for m in _MOTIF_MOT.findall(terme) if len(m) >= 2)


def _chercher_un_terme(token: str, terme: str) -> list[dict]:
    """Interroge France Travail pour un seul terme de recherche."""
    params = {
        "motsCles": mots_cles(terme),
        "region": _REGION_IDF,
        # Offres publiées depuis N jours (le paramètre attend un nb de jours, ≤ 31).
        "publieeDepuis": min(config.JOURS_FRAICHEUR, 31),
        "range": f"0-{max(0, config.FRANCE_TRAVAIL_RESULTATS - 1)}",
    }
    _respecter_debit()
    try:
        reponse = requests.get(
            _URL_SEARCH,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.TIMEOUT_HTTP,
        )
    except requests.exceptions.RequestException as err:
        categorie, detail = observabilite.categorie_requests(err)
        observabilite.signaler(NOM_SOURCE, categorie, f"{detail} sur « {terme} »")
        logger.warning("France Travail : échec réseau pour « %s » (%s).", terme, type(err).__name__)
        return []

    # 204 = aucune offre pour ces critères (comportement normal de l'API).
    if reponse.status_code == 204:
        logger.info("France Travail : 0 offre pour « %s ».", terme)
        return []
    if reponse.status_code not in (200, 206):
        # On remonte le MESSAGE de l'API, pas seulement le code : elle explique
        # précisément quel paramètre elle refuse (ex. « Le nombre de départements
        # maximum autorisé est de 5 »). Sans ça, un 400 est indébuggable.
        # Le corps d'une réponse de recherche ne contient jamais de secret ; le
        # RedactingFilter reste posé sur le handler par sécurité.
        observabilite.signaler(NOM_SOURCE, "http",
                               f"HTTP {reponse.status_code} sur « {terme} »")
        logger.warning(
            "France Travail : statut %s pour « %s » — %s",
            reponse.status_code, terme, reponse.text[:200].strip() or "(corps vide)",
        )
        return []

    try:
        donnees = reponse.json()
    except ValueError:
        observabilite.signaler(NOM_SOURCE, "format", f"réponse non-JSON sur « {terme} »")
        logger.warning("France Travail : réponse non-JSON pour « %s ».", terme)
        return []

    resultats = donnees.get("resultats", [])
    if not isinstance(resultats, list):
        observabilite.signaler(NOM_SOURCE, "format", f"structure inattendue sur « {terme} »")
        logger.warning("France Travail : format inattendu pour « %s ».", terme)
        return []

    logger.info("France Travail : %d offre(s) pour « %s ».", len(resultats), terme)
    return resultats


# Code du type de contrat « stage » dans le référentiel France Travail.
# Vérifié à la mesure : sur M1856 (Expert cybersécurité), ``typeContrat=MIS``
# remonte 9 stages sur 31 jours là où ``natureContrat=E2`` en remonte 0.
TYPE_CONTRAT_STAGE = "MIS"


def compter_offres(
    mots_cles: str | None = None,
    publiee_depuis: int = 31,
    code_rome: str | None = None,
    stages_seulement: bool = False,
) -> int | None:
    """Nombre TOTAL d'offres correspondant en Île-de-France, sans les rapatrier.

    Astuce d'API : on demande la plage ``0-0`` (une seule offre) et on lit le
    total dans l'en-tête ``Content-Range: offres 0-0/1234``. Un appel suffit
    donc pour mesurer un volume de marché, quel qu'il soit — c'est ce qui rend
    le tableau de bord de ``market.py`` bon marché.

    On peut filtrer par MOT-CLÉ ou par CODE ROME. Le code ROME est nettement
    plus fiable pour mesurer un marché : il rassemble tout un métier, là où le
    mot-clé ne matche que les intitulés reprenant le terme exact (mesuré :
    « stage cybersécurité » -> 0 offre sur 31 j, ROME M1856 -> 9).

    Retourne ``None`` si la mesure a échoué (identifiants absents, API en
    erreur) : l'appelant affiche alors « — » plutôt qu'un faux zéro.
    """
    token = obtenir_jeton()
    if not token:
        return None

    params: dict = {"region": _REGION_IDF,
                    "publieeDepuis": min(publiee_depuis, 31), "range": "0-0"}
    if mots_cles:
        params["motsCles"] = mots_cles
    if code_rome:
        params["codeROME"] = code_rome
    if stages_seulement:
        params["typeContrat"] = TYPE_CONTRAT_STAGE

    _respecter_debit()
    try:
        reponse = requests.get(
            _URL_SEARCH, params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.TIMEOUT_HTTP,
        )
    except requests.exceptions.RequestException as err:
        logger.warning("France Travail : comptage impossible (%s).", type(err).__name__)
        return None

    # 204 = aucune offre : c'est un vrai zéro, pas un échec.
    if reponse.status_code == 204:
        return 0
    if reponse.status_code not in (200, 206):
        logger.warning(
            "France Travail : comptage « %s » -> statut %s.",
            code_rome or mots_cles, reponse.status_code,
        )
        return None

    # "offres 0-0/1234" -> 1234
    entete = reponse.headers.get("Content-Range", "")
    if "/" in entete:
        try:
            return int(entete.rsplit("/", 1)[1])
        except ValueError:
            pass
    # Repli : compter ce qu'on a reçu (jamais plus de 1, donc 0 ou 1).
    try:
        return len(reponse.json().get("resultats", []))
    except ValueError:
        return None


def recuperer_offres() -> list[dict]:
    """Offres brutes France Travail : un appel par terme distinct de recherche.yaml.

    `motsCles` est conjonctif (sondé le 2026-09-17 : « vendeur,caissier » 8
    contre 1 750 et 218), d'où un appel par terme et non par famille.

    HORS `SOURCES_ACTIVES` pour les stages : 319 offres brutes, 0 stage gardé
    (cf. `config.SOURCES_ECARTEES_STAGES`). Ce chemin reste exécutable par
    ``python -m sources.france_travail`` ; l'adaptateur sert aux jobs étudiants.
    """
    client_id, client_secret = _identifiants()
    if not client_id or not client_secret:
        observabilite.signaler(NOM_SOURCE, "cle_absente",
                               "FRANCE_TRAVAIL_ID / FRANCE_TRAVAIL_KEY absents du .env")
        logger.warning(
            "France Travail ignorée : FRANCE_TRAVAIL_ID / FRANCE_TRAVAIL_KEY "
            "absents du .env."
        )
        return []

    token = obtenir_jeton()
    if not token:
        # `_obtenir_token` a déjà signalé les échecs qu'il voit passer ; ce
        # signal-ci couvre le cas restant — une réponse 200 SANS access_token,
        # qui sortait d'ici en `[]` muet, indiscernable d'un marché vide.
        observabilite.signaler(NOM_SOURCE, "auth", "jeton OAuth2 indisponible")
        return []

    toutes: list[dict] = []
    for t in recherche.charger().termes():
        toutes.extend(provenance.marquer(_chercher_un_terme(token, t.terme), t.familles))

    toutes = provenance.fusionner(toutes, lambda o: o.get("id"))
    logger.info("France Travail : %d offre(s) brute(s) au total.", len(toutes))
    return toutes


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    offres = recuperer_offres()
    print(f"\n=== {len(offres)} offre(s) France Travail récupérée(s) ===\n")
    for o in offres[:5]:
        entreprise = (o.get("entreprise") or {}).get("nom", "?")
        lieu = (o.get("lieuTravail") or {}).get("libelle", "?")
        print(f"- {o.get('intitule')}  |  {entreprise}  |  {lieu}")
