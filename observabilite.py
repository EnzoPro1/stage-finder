"""
observabilite.py — Ce que chaque source a RÉELLEMENT rendu, et pourquoi.

## L'incident

Jooble a tourné sept semaines dans `SOURCES_ACTIVES` en rendant 86 offres par
run et ZÉRO survivante. Son paramètre `location: "Paris"` était résolu par
l'API en **Paris, Texas** : 86 offres américaines sur 86, éliminées ensuite par
les filtres. Le run n'en a jamais rien dit. Mesuré après coup : 0 offre Jooble
dans `stages.db` sur toute l'historique du projet, 0 dans le corpus étiqueté.

Le défaut n'est pas Jooble, c'est que **« a rendu 86, en a gardé 0 » et « a
rendu 0 » s'écrivent tous les deux `[]`**. Une source en panne, une source mal
configurée et une source qu'un filtre vide entièrement sont indiscernables du
run — donc d'un humain qui le lit.

Ce module rend ces trois cas distincts et bruyants.

## Ce qu'il compte

Par source : le BRUT (ce que l'API a rendu), le NORMALISÉ (ce qui est
convertible au schéma commun), le SURVIVANT (ce qui passe les filtres durs), le
détail des rejets, et les INCIDENTS (403, timeout, JSON illisible, clé absente).

Un incident n'est PAS une exception : les sources continuent d'absorber leurs
erreurs et de rendre `[]` — cette garantie-là ne bouge pas. On ajoute seulement
une trace à côté, pour que l'absorption cesse d'être silencieuse.

## Pourquoi un relevé de portée module, et pas un paramètre

Un compteur passé en paramètre exigerait de changer la signature de
`recuperer_offres()` dans les six sources, dans le registre et chez les deux
appelants — pour une fonctionnalité de DIAGNOSTIC, qui ne doit rien décider.
Le relevé est donc posé par `main.collecter()` pour la durée d'une collecte,
et les sources y déposent leurs incidents sans rien savoir de lui.

Trois conséquences assumées :

- **il est verrouillé** : la collecte des sources API tourne dans un
  `ThreadPoolExecutor`, plusieurs sources écrivent en même temps ;
- **il est facultatif** : sans relevé actif (test unitaire d'une source,
  `python -m sources.jooble`), `signaler()` ne fait rien plutôt que d'échouer ;
- **il ne décide rien** : aucune branche du pipeline ne lit ces compteurs. Les
  retirer changerait ce qui s'affiche, jamais ce qui est collecté.

Utilisation :
    python observabilite.py      # démonstration sur un relevé fabriqué
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Catégories d'incident. Volontairement peu nombreuses et orientées RÉPARATION :
# ce qui compte est de savoir quoi faire, pas de reproduire la taxonomie des
# exceptions de `requests`.
#
#   http        réponse reçue, mais refusée (403, 429, 5xx) -> quota, blocage
#   reseau      aucune réponse (timeout, DNS, connexion) -> réessayer plus tard
#   format      réponse reçue et illisible (non-JSON, structure inattendue)
#   auth        authentification refusée -> identifiants à renouveler
#   cle_absente rien tenté, la clé n'est pas dans le .env -> source désactivée
#   import      le module de la source ne s'importe pas (dépendance manquante)
CATEGORIES = ("http", "reseau", "format", "auth", "cle_absente", "import")

# Seuil à partir duquel « rendu par l'API mais rien de gardé » devient une
# alerte. Un brut de 2 ou 3 qui ne survit pas est banal — un filtre dur fait
# son travail. Un brut de 10+ intégralement éliminé veut dire que la source
# répond à côté de la question qu'on lui pose : c'est le cas Jooble (86 -> 0),
# et c'est celui qu'on veut voir depuis l'autre bout de la pièce.
SEUIL_BRUT_STERILE = 10


@dataclass(frozen=True)
class Incident:
    """Un échec ponctuel rencontré par une source, sans interruption du run."""

    source: str
    categorie: str
    detail: str

    def __str__(self) -> str:
        return f"{self.categorie}: {self.detail}"


@dataclass
class LigneSource:
    """Le parcours d'une source sur un run : brut -> normalisé -> survivant."""

    nom: str
    brut: int = 0
    normalisees: int = 0
    survivantes: int = 0
    fusions: int = 0
    rejets: dict[str, int] = field(default_factory=dict)
    incidents: list[Incident] = field(default_factory=list)

    @property
    def perdues_normalisation(self) -> int:
        """Items bruts inexploitables (titre ou URL manquants, item cassé)."""
        return max(0, self.brut - self.normalisees)

    @property
    def retenues(self) -> int:
        """Ce qui reste VRAIMENT après déduplication.

        ``survivantes`` est compté par `filters.filtrer`, qui tourne AVANT
        `dedup` : sans cette soustraction, la colonne annonçait des offres que
        la déduplication avait déjà supprimées. C'est l'angle mort constaté le
        2026-09-05 — 79 offres fusionnées sans apparaître dans aucun compteur.
        """
        return max(0, self.survivantes - self.fusions)

    @property
    def sterile(self) -> bool:
        """A rendu de quoi travailler, et il n'en reste rien. Le cas Jooble."""
        return self.brut >= SEUIL_BRUT_STERILE and self.survivantes == 0

    @property
    def muette(self) -> bool:
        """N'a rien rendu DU TOUT, et a signalé pourquoi : panne franche."""
        return self.brut == 0 and bool(self.incidents)

    @property
    def degradee(self) -> bool:
        """A rendu quelque chose, mais pas tout : résultat partiel, à lire comme tel."""
        return self.brut > 0 and bool(self.incidents)


class Releve:
    """Compteurs d'un run de collecte. Sûr à écrire depuis plusieurs threads."""

    def __init__(
        self,
        sources: list[str] | None = None,
        ecartees: dict[str, str] | None = None,
        perimetre: str = "",
    ) -> None:
        self._verrou = threading.Lock()
        self.lignes: dict[str, LigneSource] = {}
        # Sources RETIRÉES volontairement de ce périmètre, avec leur raison.
        # Elles n'ont pas de ligne — elles ne tournent pas — mais le bilan les
        # nomme en tête : sans ça, une source retirée et une source oubliée
        # s'écrivent pareil, par leur absence.
        self.ecartees: dict[str, str] = dict(ecartees or {})
        self.perimetre = perimetre
        # Les sources attendues sont créées D'AVANCE : une source qui échoue à
        # l'import ne poserait jamais sa ligne, et disparaîtrait du tableau au
        # lieu d'y apparaître en panne — exactement le silence qu'on corrige.
        for nom in sources or []:
            self.lignes[nom] = LigneSource(nom)

    def _ligne(self, source: str) -> LigneSource:
        """Ligne de la source, créée à la volée. À appeler VERROU TENU."""
        if source not in self.lignes:
            self.lignes[source] = LigneSource(source)
        return self.lignes[source]

    # -- Écriture ----------------------------------------------------------
    def signaler(self, source: str, categorie: str, detail: str) -> None:
        """Consigne un incident. Ne lève jamais : un diagnostic ne casse rien."""
        if categorie not in CATEGORIES:
            categorie = "format"
        with self._verrou:
            self._ligne(source).incidents.append(
                Incident(source, categorie, str(detail)[:200])
            )

    def compter_brut(self, source: str, n: int) -> None:
        with self._verrou:
            self._ligne(source).brut += int(n)

    def compter_normalisees(self, source: str, n: int) -> None:
        with self._verrou:
            self._ligne(source).normalisees += int(n)

    def compter_survivante(self, source: str) -> None:
        with self._verrou:
            self._ligne(racine(source)).survivantes += 1

    def compter_fusion(self, source: str) -> None:
        """Une offre supprimee par la deduplication (exacte ou floue)."""
        with self._verrou:
            self._ligne(racine(source)).fusions += 1

    def compter_rejet(self, source: str, motif: str) -> None:
        with self._verrou:
            rejets = self._ligne(racine(source)).rejets
            rejets[motif] = rejets.get(motif, 0) + 1

    # -- Lecture -----------------------------------------------------------
    def triees(self) -> list[LigneSource]:
        """Lignes par brut décroissant, puis par nom (ordre déterministe)."""
        return sorted(self.lignes.values(), key=lambda l: (-l.brut, l.nom))

    def alertes(self) -> list[str]:
        """Messages à hurler, du plus grave au plus anodin. Vide si tout va bien.

        Trois cas, qui ne se réparent pas pareil et ne sont donc pas fondus en
        un seul message : la source est en panne, la source répond à côté, la
        source a répondu partiellement.
        """
        messages: list[str] = []
        for ligne in self.triees():
            causes = ", ".join(sorted({i.categorie for i in ligne.incidents}))
            if ligne.muette:
                messages.append(
                    f"Source « {ligne.nom} » MUETTE : 0 offre rendue, "
                    f"{len(ligne.incidents)} incident(s) [{causes}]. "
                    f"Ce n'est pas un marché vide, c'est une panne."
                )
            elif ligne.sterile:
                messages.append(
                    f"Source « {ligne.nom} » STÉRILE : {ligne.brut} offre(s) rendue(s), "
                    f"0 gardée ({_detail_rejets(ligne)}). La source répond, mais à "
                    f"côté de la question posée — vérifie ses paramètres de "
                    f"recherche (lieu, termes, type de contrat)."
                )
            elif ligne.degradee:
                messages.append(
                    f"Source « {ligne.nom} » DÉGRADÉE : {ligne.brut} offre(s) rendue(s) "
                    f"malgré {len(ligne.incidents)} incident(s) [{causes}] — "
                    f"résultat partiel."
                )
        return messages

    def messages_ecartees(self) -> list[str]:
        """Une ligne par source retirée du périmètre, raison comprise, dans l'ordre de la config."""
        pour = f" pour les {self.perimetre}" if self.perimetre else ""
        return [f"⊘ Source « {nom} » désactivée{pour} : {raison}"
                for nom, raison in self.ecartees.items()]

    def resume(self) -> dict:
        """Vue sérialisable du relevé (pour un rapport JSON ou un test)."""
        return {
            "perimetre": self.perimetre,
            "ecartees": dict(self.ecartees),
            "sources": [
                {
                    "nom": l.nom, "brut": l.brut, "normalisees": l.normalisees,
                    "survivantes": l.survivantes, "fusions": l.fusions,
                    "retenues": l.retenues, "rejets": dict(sorted(l.rejets.items())),
                    "incidents": [{"categorie": i.categorie, "detail": i.detail}
                                  for i in l.incidents],
                }
                for l in self.triees()
            ],
            "alertes": self.alertes(),
        }


def _detail_rejets(ligne: LigneSource) -> str:
    """« hors-stage 84, hors-IDF 2 » — le détail lisible d'une élimination."""
    if ligne.perdues_normalisation:
        # Une perte à la normalisation n'est pas un rejet de filtre : la
        # confondre avec un « hors-IDF » enverrait chercher la panne au
        # mauvais endroit.
        base = [f"non normalisables {ligne.perdues_normalisation}"]
    else:
        base = []
    base += [f"{motif} {n}" for motif, n in sorted(ligne.rejets.items())]
    return ", ".join(base) or "sans détail"


def racine(source: str) -> str:
    """« jobspy:indeed » -> « jobspy ». Aligne les compteurs sur le catalogue.

    Le nom porté par une offre est celui du SITE (`normalize` préfixe JobSpy
    par son site d'origine), là où le brut est compté par SOURCE du catalogue.
    Sans ce repliage, `jobspy` afficherait un brut sans survivantes et trois
    lignes fantômes de survivantes sans brut — soit une fausse alerte
    « stérile » sur la source la plus productive du projet.
    """
    return source.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Relevé actif du run en cours
# ---------------------------------------------------------------------------
_actif: Releve | None = None
_verrou_actif = threading.Lock()


def demarrer(
    sources: list[str] | None = None,
    ecartees: dict[str, str] | None = None,
    perimetre: str = "",
) -> Releve:
    """Installe un relevé neuf pour la collecte qui commence, et le retourne.

    ``ecartees`` : sources retirées volontairement du périmètre, et pourquoi —
    nommées en tête du bilan (cf. ``Releve.messages_ecartees``).
    """
    global _actif
    with _verrou_actif:
        _actif = Releve(sources, ecartees=ecartees, perimetre=perimetre)
        return _actif


def actif() -> Releve | None:
    """Relevé en cours, ou ``None`` hors collecte."""
    return _actif


def arreter() -> None:
    """Retire le relevé actif. Les tests s'en servent pour ne pas fuir d'un cas à l'autre."""
    global _actif
    with _verrou_actif:
        _actif = None


def signaler(source: str, categorie: str, detail: str) -> None:
    """Consigne un incident sur le relevé actif, s'il y en a un.

    Point d'entrée des SOURCES. Hors collecte (test unitaire d'une source,
    `python -m sources.adzuna`), ne fait rien : une source doit rester
    exécutable seule, sans rien installer au préalable.
    """
    releve = _actif
    if releve is not None:
        releve.signaler(source, categorie, detail)


def signaler_fusion(source: str) -> None:
    """Une offre supprimee par la deduplication, sur le releve actif s'il existe.

    Point d'entree de `dedup`. Sans lui, la colonne « gardees » — comptee par
    `filters.filtrer`, donc AVANT la deduplication — annoncait des offres deja
    supprimees : 79 disparitions sur 447 n'apparaissaient dans aucun compteur.
    """
    releve = _actif
    if releve is not None:
        releve.compter_fusion(source)


def categorie_requests(err: Exception) -> tuple[str, str]:
    """Classe une exception `requests` en (catégorie, détail lisible).

    Le code HTTP est extrait quand il existe : « http: 403 » et
    « reseau: ReadTimeout » n'appellent pas la même réaction, et les fondre
    dans un seul « échec réseau » est précisément ce qui a rendu le 403 de
    Jooble invisible.
    """
    reponse = getattr(err, "response", None)
    if reponse is not None and getattr(reponse, "status_code", None):
        return "http", f"HTTP {reponse.status_code}"
    nom = type(err).__name__
    if "Timeout" in nom:
        return "reseau", "timeout"
    if "JSON" in nom or "Decode" in nom or isinstance(err, ValueError):
        return "format", nom
    return "reseau", nom


# ---------------------------------------------------------------------------
# Restitution
# ---------------------------------------------------------------------------
def journaliser(releve: Releve | None = None) -> None:
    """Écrit le tableau par source dans les logs, et les alertes en WARNING.

    Passe par `logging` et non par `print` : les deux portes d'entrée du
    pipeline (CLI et app web) traversent `main.collecter`, et seule la première
    a une console à elle. Le CLI en fait un tableau imprimé par ailleurs.
    """
    releve = releve if releve is not None else _actif
    if releve is None:
        return
    for message in releve.messages_ecartees():
        logger.info("%s", message)
    for ligne in releve.triees():
        a_detailler = bool(ligne.rejets or ligne.perdues_normalisation)
        logger.info(
            "Source %-15s brut %4d -> normalisées %4d -> gardées %4d "
            "-> retenues %4d (dédup -%d)%s%s",
            ligne.nom, ligne.brut, ligne.normalisees, ligne.survivantes,
            ligne.retenues, ligne.fusions,
            f"  ({_detail_rejets(ligne)})" if a_detailler else "",
            f"  [{len(ligne.incidents)} incident(s)]" if ligne.incidents else "",
        )
    for message in releve.alertes():
        logger.warning("%s", message)


def rendre_tableau(releve: Releve | None = None) -> str:
    """Tableau texte du relevé, pour l'affichage CLI de fin de run."""
    releve = releve if releve is not None else _actif
    if releve is None:
        return ""
    # Les sources écartées en TÊTE : c'est la première chose à savoir pour lire
    # le tableau — ce qui n'y figure pas n'a pas été oublié.
    ecartees = releve.messages_ecartees()
    lignes = ecartees + ([""] if ecartees else []) + [
        f"{'source':16}{'brut':>7}{'normal.':>9}{'gardées':>9}{'dédup':>7}"
        f"{'retenues':>10}   détail",
        "─" * 92,
    ]
    for l in releve.triees():
        detail = _detail_rejets(l) if (l.rejets or l.perdues_normalisation) else ""
        if l.incidents:
            causes = ", ".join(sorted({i.categorie for i in l.incidents}))
            detail = (detail + "  " if detail else "") + f"⚠ {len(l.incidents)} incident(s) [{causes}]"
        lignes.append(f"{l.nom:16}{l.brut:7}{l.normalisees:9}{l.survivantes:9}"
                      f"{-l.fusions if l.fusions else 0:7}{l.retenues:10}   {detail}")
    alertes = releve.alertes()
    if alertes:
        lignes.append("")
        lignes.extend(f"⚠ {m}" for m in alertes)
    return "\n".join(lignes)


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Démonstration sur le cas réel qui a motivé le module : la collecte
    # « job étudiant » mesurée le 2026-09-05 (cf. l'en-tête).
    def _lot(r, nom, brut, gardees, rejets, incidents=()):
        r.compter_brut(nom, brut)
        r.compter_normalisees(nom, brut)
        for _ in range(gardees):
            r.compter_survivante(nom)
        for motif, n in rejets.items():
            for _ in range(n):
                r.compter_rejet(nom, motif)
        for categorie, detail in incidents:
            r.signaler(nom, categorie, detail)

    r = demarrer(["careerjet", "adzuna", "jooble", "free_work"])
    _lot(r, "careerjet", 313, 132, {"hors-IDF": 30, "exclu": 9, "hors-stage": 15,
                                    "doublon": 127})
    _lot(r, "adzuna", 141, 98, {"exclu": 34, "doublon": 9},
         incidents=[("reseau", "timeout")])
    # Le cas Jooble : répond, abondamment, et à côté de la question.
    _lot(r, "jooble", 86, 0, {"hors-stage": 84, "hors-IDF": 2})
    # Une source qui ne répond plus du tout.
    _lot(r, "free_work", 0, 0, {}, incidents=[("http", "HTTP 403")])

    print(rendre_tableau(r))
