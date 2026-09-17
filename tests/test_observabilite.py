"""Le relevé par source : « a rendu 86, en a gardé 0 » doit être BRUYANT.

Jooble a tourné sept semaines en rendant 86 offres américaines par run et zéro
survivante, sans que rien ne le dise : `[]` s'écrit pareil qu'une source en
panne et qu'un marché vide. Ce fichier vérifie que les trois cas se distinguent
maintenant, et surtout que le cas STÉRILE — celui qui est passé inaperçu —
déclenche une alerte.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest
import requests

import config
import observabilite
from normalize import Offre


@pytest.fixture(autouse=True)
def releve_propre():
    """Aucun relevé ne fuit d'un test à l'autre (le relevé actif est global)."""
    observabilite.arreter()
    yield
    observabilite.arreter()


def _lot(releve, nom, brut, gardees, rejets=None, incidents=()):
    """Simule le passage d'une source : brut -> normalisé -> filtres."""
    releve.compter_brut(nom, brut)
    releve.compter_normalisees(nom, brut)
    for _ in range(gardees):
        releve.compter_survivante(nom)
    for motif, n in (rejets or {}).items():
        for _ in range(n):
            releve.compter_rejet(nom, motif)
    for categorie, detail in incidents:
        releve.signaler(nom, categorie, detail)


# ---------------------------------------------------------------------------
# L'alerte « stérile » — la raison d'être du module
# ---------------------------------------------------------------------------
def test_source_qui_rend_beaucoup_et_ne_garde_rien_declenche_une_alerte():
    """LE cas Jooble : 86 offres rendues, 0 gardée, et personne ne l'a su."""
    r = observabilite.Releve(["jooble"])
    _lot(r, "jooble", brut=86, gardees=0, rejets={"hors-stage": 84, "hors-IDF": 2})

    assert r.lignes["jooble"].sterile
    alertes = r.alertes()
    assert len(alertes) == 1
    assert "STÉRILE" in alertes[0]
    assert "86" in alertes[0]
    # L'alerte doit dire OÙ ça meurt, sinon elle n'aide pas à réparer.
    assert "hors-stage 84" in alertes[0]


def test_source_productive_ne_declenche_aucune_alerte():
    r = observabilite.Releve(["careerjet"])
    _lot(r, "careerjet", brut=313, gardees=132, rejets={"hors-IDF": 30})
    assert not r.lignes["careerjet"].sterile
    assert r.alertes() == []


def test_petit_brut_entierement_rejete_ne_declenche_pas_d_alerte():
    """3 offres éliminées par un filtre dur est un fonctionnement normal.

    Le seuil existe pour que l'alerte reste rare — donc lisible. Une alerte qui
    se déclenche à chaque run n'est plus lue, et on retombe sur le silence
    qu'elle est censée rompre.
    """
    r = observabilite.Releve(["free_work"])
    _lot(r, "free_work", brut=3, gardees=0, rejets={"hors-IDF": 3})
    assert not r.lignes["free_work"].sterile
    assert r.alertes() == []


def test_le_seuil_de_sterilite_est_la_frontiere_annoncee():
    seuil = observabilite.SEUIL_BRUT_STERILE
    juste_en_dessous = observabilite.Releve()
    _lot(juste_en_dessous, "x", brut=seuil - 1, gardees=0, rejets={"hors-IDF": seuil - 1})
    juste_au_dessus = observabilite.Releve()
    _lot(juste_au_dessus, "x", brut=seuil, gardees=0, rejets={"hors-IDF": seuil})

    assert not juste_en_dessous.lignes["x"].sterile
    assert juste_au_dessus.lignes["x"].sterile


# ---------------------------------------------------------------------------
# Panne franche vs marché vide vs résultat partiel
# ---------------------------------------------------------------------------
def test_panne_et_zero_silencieux_sont_muets_mais_ne_s_ecrivent_pas_pareil():
    """0 offre est MUET, avec ou sans incident — c'est la forme exacte de la
    panne de Google Jobs, 200 à chaque requête et 0 offre en 36 runs. Le
    message, lui, distingue la panne franche du zéro silencieux."""
    panne = observabilite.Releve(["jooble"])
    _lot(panne, "jooble", brut=0, gardees=0, incidents=[("http", "HTTP 403")])

    silence = observabilite.Releve(["jooble"])
    _lot(silence, "jooble", brut=0, gardees=0)

    assert panne.lignes["jooble"].muette and silence.lignes["jooble"].muette
    assert "MUETTE" in panne.alertes()[0] and "c'est une panne" in panne.alertes()[0]
    assert "MUETTE" in silence.alertes()[0] and "sans incident" in silence.alertes()[0]


def test_resultat_partiel_est_signale_sans_etre_confondu_avec_une_panne():
    r = observabilite.Releve(["adzuna"])
    _lot(r, "adzuna", brut=141, gardees=98, incidents=[("reseau", "timeout")])
    ligne = r.lignes["adzuna"]
    assert ligne.degradee and not ligne.muette and not ligne.sterile
    assert "DÉGRADÉE" in r.alertes()[0]


def test_les_trois_etats_sont_mutuellement_exclusifs_dans_les_alertes():
    """Une source produit AU PLUS une alerte : trois messages pour un problème
    est une façon de le rendre illisible."""
    r = observabilite.Releve()
    _lot(r, "a", brut=0, gardees=0, incidents=[("http", "HTTP 500")])
    _lot(r, "b", brut=50, gardees=0, rejets={"hors-IDF": 50}, incidents=[("reseau", "timeout")])
    _lot(r, "c", brut=50, gardees=10, incidents=[("format", "non-JSON")])
    assert len(r.alertes()) == 3


# ---------------------------------------------------------------------------
# Classification des incidents : un 403 n'est pas un timeout
# ---------------------------------------------------------------------------
def test_le_code_http_est_conserve_et_pas_fondu_dans_echec_reseau():
    """C'est ce qui a rendu le 403 de Jooble invisible : tout finissait en
    « échec de la requête », sans le code."""
    reponse = requests.Response()
    reponse.status_code = 403
    err = requests.exceptions.HTTPError(response=reponse)
    assert observabilite.categorie_requests(err) == ("http", "HTTP 403")


def test_un_timeout_est_classe_reseau():
    categorie, detail = observabilite.categorie_requests(requests.exceptions.ReadTimeout())
    assert categorie == "reseau" and detail == "timeout"


def test_un_json_illisible_est_classe_format():
    categorie, _ = observabilite.categorie_requests(ValueError("Expecting value"))
    assert categorie == "format"


def test_une_categorie_inconnue_ne_fait_pas_echouer_le_diagnostic():
    """Un module de diagnostic qui lève ferait tomber le run qu'il observe."""
    r = observabilite.Releve()
    r.signaler("x", "catégorie inventée", "détail")
    assert r.lignes["x"].incidents[0].categorie in observabilite.CATEGORIES


# ---------------------------------------------------------------------------
# Repliage « jobspy:indeed » -> « jobspy »
# ---------------------------------------------------------------------------
def test_chaque_site_jobspy_a_sa_ligne():
    """Replié sur une ligne `jobspy`, Google Jobs a rendu 0 offre en 36 runs sans
    que le total d'Indeed et LinkedIn laisse rien voir."""
    r = observabilite.Releve(["jobspy:indeed", "jobspy:linkedin", "jobspy:google"])
    _lot(r, "jobspy:indeed", brut=60, gardees=40)
    _lot(r, "jobspy:linkedin", brut=30, gardees=29)

    assert "jobspy" not in r.lignes, "plus de ligne repliée"
    assert r.lignes["jobspy:indeed"].survivantes == 40
    alertes = r.alertes()
    assert len(alertes) == 1 and "« jobspy:google » MUETTE" in alertes[0]


# ---------------------------------------------------------------------------
# Intégration avec filters.filtrer
# ---------------------------------------------------------------------------
def _offre(titre, source, lieu="Paris", posted=None):
    # Date du jour : `est_recente` refuse aussi bien le trop vieux que le
    # futur (tolérance d'un jour pour les fuseaux), donc une date fixe ferait
    # échouer ce test au bout d'une semaine.
    posted = posted or date.today().isoformat()
    return Offre(titre, "ACME", lieu, "desc", f"http://x/{titre}", source, posted, "")


def test_filtrer_alimente_le_releve_par_source():
    import filters

    r = observabilite.demarrer(["careerjet", "jooble"])
    filters.filtrer([
        _offre("Stage IA", "careerjet"),
        _offre("Stage Cyber", "careerjet"),
        _offre("Machine Learning Engineer", "jooble", lieu="Paris, TX"),
        _offre("Director Solutions Engineering", "jooble", lieu="Paris, TX"),
    ])
    assert r.lignes["careerjet"].survivantes == 2
    assert r.lignes["jooble"].survivantes == 0
    # Les deux offres Jooble meurent au filtre « pas un stage », pas ailleurs.
    assert r.lignes["jooble"].rejets == {"hors-stage": 2}


def test_filtrer_fonctionne_sans_releve_actif():
    """Une source doit rester testable seule : `filtrer` ne dépend pas du relevé."""
    import filters

    observabilite.arreter()
    gardees = filters.filtrer([_offre("Stage IA", "careerjet")])
    assert len(gardees) == 1


def test_signaler_sans_releve_actif_ne_leve_pas():
    """`python -m sources.jooble` n'installe aucun relevé et doit tourner."""
    observabilite.arreter()
    observabilite.signaler("jooble", "http", "HTTP 403")  # ne doit pas lever


# ---------------------------------------------------------------------------
# Deux collectes dans le MÊME processus
# ---------------------------------------------------------------------------
class _SourceFactice:
    """Stand-in de `registry.Source` : rend un lot fixe, sans réseau.

    `registry.Source` est un dataclass gelé dont `recuperer()` importe un
    module ; on ne peut donc ni lui greffer un lot ni le patcher en place.
    """

    def __init__(self, nom, lot, sequentiel=False):
        self.nom, self._lot, self.sequentiel = nom, lot, sequentiel

    def recuperer(self):
        return lambda: self._lot


def _brut_adzuna(titre, lieu="Paris"):
    """Un item brut au format Adzuna (le normaliseur est choisi par le nom)."""
    return {
        "title": titre, "company": {"display_name": "ACME"},
        "location": {"display_name": lieu}, "description": "desc",
        "redirect_url": f"http://x/{titre}", "created": date.today().isoformat(),
    }


def test_deux_collectes_successives_ne_cumulent_pas_leurs_compteurs(monkeypatch):
    """Le relevé est de portée MODULE : sans remise à zéro, une source stérile
    au premier run passerait pour saine au second.

    Le cas est réel, pas théorique : l'app web (`app._collecte`) et le CLI
    partagent un processus, et le bouton « Tout lancer » se clique deux fois.
    """
    import main

    lot1 = [_brut_adzuna(f"Data Engineer {i}") for i in range(12)]   # 0 survivante
    lot2 = [_brut_adzuna(f"Stage IA {i}") for i in range(3)]         # 3 survivantes

    def collecte(lot):
        monkeypatch.setattr(
            main.registry, "sources_actives",
            lambda noms: [_SourceFactice("adzuna", lot)],
        )
        offres = main.collecter(utiliser_jobspy=False)
        return observabilite.actif(), main.filtrer(offres)

    releve1, gardees1 = collecte(lot1)
    assert releve1.lignes["adzuna"].brut == 12
    assert releve1.lignes["adzuna"].survivantes == 0
    assert releve1.lignes["adzuna"].sterile, "12 rendues, 0 gardée : stérile"

    releve2, gardees2 = collecte(lot2)
    assert releve2 is not releve1, "chaque collecte pose un relevé NEUF"
    # Le point du test : 3, et surtout pas 15.
    assert releve2.lignes["adzuna"].brut == 3
    assert releve2.lignes["adzuna"].survivantes == 3
    assert not releve2.lignes["adzuna"].sterile, "l'état stérile ne se traîne pas"
    # Les items factices ne portent aucune famille : seules les alertes de
    # SOURCE comptent ici.
    assert [a for a in releve2.alertes() if a.startswith("Source")] == []

    # Et le relevé du premier run n'a pas été rétroactivement modifié.
    assert releve1.lignes["adzuna"].brut == 12
    assert len(gardees1) == 0 and len(gardees2) == 3


def test_une_source_disparue_du_second_run_ne_hante_pas_son_releve(monkeypatch):
    """Les lignes sont amorcées depuis les sources ATTENDUES : une source
    retirée de la config ne doit pas rester affichée avec ses vieux chiffres."""
    import main

    monkeypatch.setattr(
        main.registry, "sources_actives",
        lambda noms: [_SourceFactice("adzuna", [_brut_adzuna("Stage IA")]),
                      _SourceFactice("jooble", [])],
    )
    main.collecter(utiliser_jobspy=False)
    assert set(observabilite.actif().lignes) == {"adzuna", "jooble"}

    monkeypatch.setattr(
        main.registry, "sources_actives",
        lambda noms: [_SourceFactice("adzuna", [_brut_adzuna("Stage IA")])],
    )
    main.collecter(utiliser_jobspy=False)
    assert set(observabilite.actif().lignes) == {"adzuna"}


# ---------------------------------------------------------------------------
# Restitution
# ---------------------------------------------------------------------------
def test_le_tableau_est_deterministe():
    r = observabilite.demarrer(["a", "b"])
    _lot(r, "a", brut=10, gardees=5)
    _lot(r, "b", brut=20, gardees=1)
    assert observabilite.rendre_tableau(r) == observabilite.rendre_tableau(r)
    # Tri par brut décroissant : la source la plus volumineuse en tête.
    lignes = observabilite.rendre_tableau(r).splitlines()
    assert lignes[2].startswith("b") and lignes[3].startswith("a")


def test_les_alertes_partent_en_warning_dans_les_logs(caplog):
    """L'alerte doit atteindre les logs, pas seulement le tableau CLI : l'app
    web ne passe jamais par `rendre_tableau`."""
    r = observabilite.demarrer(["jooble"])
    _lot(r, "jooble", brut=86, gardees=0, rejets={"hors-stage": 86})
    with caplog.at_level(logging.WARNING, logger="observabilite"):
        observabilite.journaliser(r)
    assert any("STÉRILE" in enr.message for enr in caplog.records)


def test_resume_est_serialisable():
    import json

    r = observabilite.Releve(["jooble"])
    _lot(r, "jooble", brut=86, gardees=0, rejets={"hors-stage": 86},
         incidents=[("http", "HTTP 403")])
    assert json.loads(json.dumps(r.resume()))["alertes"]


# ---------------------------------------------------------------------------
# Sources écartées volontairement : nommées en tête du bilan
# ---------------------------------------------------------------------------
def test_une_source_ecartee_est_nommee_en_tete_du_tableau_avec_sa_raison():
    r = observabilite.demarrer(["adzuna"], perimetre="stages",
                               ecartees={"france_travail": "0 stage gardé sur 319"})
    r.compter_brut("adzuna", 5)
    tableau = observabilite.rendre_tableau(r).splitlines()
    assert tableau[0] == ("⊘ Source « france_travail » désactivée pour les stages : "
                          "0 stage gardé sur 319")
    assert tableau[2].startswith("source"), "le tableau suit, après une ligne vide"
    assert "france_travail" not in r.lignes, "écartée : pas de ligne de compteurs"
    assert r.alertes() == [], "écartée n'est ni muette ni en panne"


def test_une_source_ecartee_est_journalisee(caplog):
    import logging

    r = observabilite.demarrer([], ecartees={"jooble": "Paris, Texas"})
    with caplog.at_level(logging.INFO, logger="observabilite"):
        observabilite.journaliser(r)
    assert "« jooble » désactivée : Paris, Texas" in caplog.text


def test_sans_source_ecartee_le_tableau_ne_change_pas():
    r = observabilite.demarrer(["adzuna"])
    assert observabilite.rendre_tableau(r).splitlines()[0].startswith("source")


def test_la_collecte_de_stages_annonce_france_travail_ecartee(monkeypatch):
    import main

    monkeypatch.setattr(main.registry, "sources_actives",
                        lambda noms: [_SourceFactice("adzuna", [])])
    main.collecter(utiliser_jobspy=False)
    releve = observabilite.actif()
    assert releve.perimetre == "stages"
    assert "france_travail" in releve.ecartees
    assert releve.resume()["ecartees"] == config.SOURCES_ECARTEES_STAGES


# ---------------------------------------------------------------------------
# JobSpy : une ligne par site depuis la collecte réelle
# ---------------------------------------------------------------------------
def _brut_jobspy(site, i):
    return {"id": f"{site[:2]}-{i}", "site": site, "title": f"Stage IA {i}",
            "company": "ACME", "location": "Paris, A8, FR", "description": "d",
            "job_url": f"https://{site}/{i}", "date_posted": date.today().isoformat()}


def test_la_collecte_amorce_une_ligne_par_site_et_signale_le_site_muet(monkeypatch):
    import main

    monkeypatch.setattr(config, "JOBSPY_SITES", ["indeed", "linkedin"])
    lot = [_brut_jobspy("indeed", i) for i in range(3)]
    monkeypatch.setattr(main.registry, "sources_actives",
                        lambda noms: [_SourceFactice("jobspy", lot, sequentiel=True)])
    offres = main.collecter(utiliser_jobspy=True)
    main.filtrer(offres)
    r = observabilite.actif()

    assert set(r.lignes) == {"jobspy:indeed", "jobspy:linkedin"}
    assert (r.lignes["jobspy:indeed"].brut, r.lignes["jobspy:indeed"].normalisees,
            r.lignes["jobspy:indeed"].survivantes) == (3, 3, 3)
    assert r.lignes["jobspy:linkedin"].muette
    assert any("« jobspy:linkedin » MUETTE" in a for a in r.alertes())


def test_un_echec_d_import_de_jobspy_touche_chaque_site(monkeypatch):
    import main

    class _Casse(_SourceFactice):
        def recuperer(self):
            raise ImportError("pas de jobspy")

    monkeypatch.setattr(config, "JOBSPY_SITES", ["indeed", "linkedin"])
    monkeypatch.setattr(main.registry, "sources_actives",
                        lambda noms: [_Casse("jobspy", [], sequentiel=True)])
    main.collecter(utiliser_jobspy=True)
    r = observabilite.actif()
    assert all(r.lignes[l].incidents[0].categorie == "import"
               for l in ("jobspy:indeed", "jobspy:linkedin"))


# ---------------------------------------------------------------------------
# Par famille
# ---------------------------------------------------------------------------
def test_brut_et_gardees_par_famille(monkeypatch):
    import main
    from sources import provenance

    lot = (provenance.marquer([_brut_adzuna("Stage NLP")], ["ml"])
           + provenance.marquer([_brut_adzuna("Stage LLMOps")], ["ml", "mlops"])
           + provenance.marquer([_brut_adzuna("Data Engineer")], ["mlops"]))
    monkeypatch.setattr(main.registry, "sources_actives",
                        lambda noms: [_SourceFactice("adzuna", lot)])
    main.filtrer(main.collecter(utiliser_jobspy=False))
    r = observabilite.actif()

    assert (r.familles["ml"].brut, r.familles["ml"].survivantes) == (2, 2)
    assert (r.familles["mlops"].brut, r.familles["mlops"].survivantes) == (2, 1)
    assert r.familles["cyber"].brut == 0, "amorcée depuis recherche.yaml"
    assert any("Famille « cyber » MUETTE" in a for a in r.alertes())
    assert not any("« ml » MUETTE" in a for a in r.alertes())


def test_le_tableau_affiche_les_familles_dans_l_ordre_du_fichier():
    r = observabilite.demarrer(["adzuna"], familles=["ai_engineering", "ml"])
    _lot(r, "adzuna", brut=5, gardees=5)
    r.compter_brut_familles([["ml"], ["ml"]])
    lignes = observabilite.rendre_tableau(r).splitlines()
    debut = next(i for i, l in enumerate(lignes) if l.startswith("famille"))
    assert lignes[debut + 2].split() == ["ai_engineering", "0", "0"]
    assert lignes[debut + 3].split() == ["ml", "2", "0"]


# ---------------------------------------------------------------------------
# Google Jobs : écarté, et dit pourquoi
# ---------------------------------------------------------------------------
def test_google_jobs_n_est_plus_interroge_et_figure_dans_les_ecartees():
    assert "google" not in config.JOBSPY_SITES
    assert "JavaScript" in config.SOURCES_ECARTEES_STAGES["jobspy:google"]
    r = observabilite.demarrer([], ecartees=config.SOURCES_ECARTEES_STAGES, perimetre="stages")
    assert any(l.startswith("⊘ Source « jobspy:google » désactivée pour les stages")
               for l in observabilite.rendre_tableau(r).splitlines())

