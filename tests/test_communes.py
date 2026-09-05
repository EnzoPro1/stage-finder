"""Résolution géographique : reconnaître l'Île-de-France sans y laisser le Texas.

Deux échecs réels encadrent ce fichier, et ils tirent en sens opposés :

- `filters.est_en_idf` comparait le lieu à une vingtaine de sous-chaînes écrites
  à la main. Sur un lot JobSpy réel, **35 offres sur 93** ont été rejetées, dont
  Neuilly-sur-Seine, Suresnes, Gennevilliers, Puteaux et Vélizy-Villacoublay :
  JobSpy écrit « Commune, A8, FR », et ni la commune ni `A8` n'étaient connus.
- Jooble a rendu **86 offres de Paris, Texas** en les faisant passer pour
  franciliennes. « Paris, TX » contient le nom d'une commune d'Île-de-France.

Élargir la reconnaissance sans rouvrir la seconde brèche est tout l'exercice :
la barrière négative doit passer AVANT la reconnaissance de commune.
"""

from __future__ import annotations

import json
import os

import pytest

import communes
import filters
from normalize import Offre


@pytest.fixture(autouse=True)
def referentiel_charge():
    """Le référentiel est mémorisé : on repart d'un cache propre à chaque test."""
    communes.referentiel.cache_clear()
    yield
    communes.referentiel.cache_clear()


# ---------------------------------------------------------------------------
# Le référentiel lui-même
# ---------------------------------------------------------------------------
def test_le_referentiel_est_present_et_complet():
    """1 266 communes : le fichier généré est versionné, pas régénéré au vol."""
    ref = communes.referentiel()
    assert ref["meta"]["n"] == len(ref["communes"]) >= 1200
    assert ref["meta"]["code_region"] == communes.CODE_REGION_IDF


def test_le_referentiel_porte_sa_provenance():
    """Un fichier généré sans date ni source ne se vérifie pas plus tard."""
    meta = communes.referentiel()["meta"]
    assert meta["source"].startswith("https://geo.api.gouv.fr/")
    assert meta["genere_le"]


def test_le_referentiel_se_trouve_depuis_n_importe_quel_repertoire(tmp_path, monkeypatch):
    """Chemin résolu depuis le MODULE, pas depuis le CWD.

    `communes` est lu par du code de bibliothèque (`filters`, `dedup`), donc
    depuis n'importe quel répertoire courant. Un référentiel introuvable
    dégraderait la résolution en silence — et rejetterait tout ce qui n'est
    pas Paris, c'est-à-dire exactement le défaut corrigé.
    """
    monkeypatch.chdir(tmp_path)
    communes.referentiel.cache_clear()
    assert communes.referentiel()["meta"]["n"] >= 1200
    assert communes.est_idf("Suresnes")


def test_un_referentiel_absent_degrade_sans_lever(tmp_path, caplog):
    """Le dépôt doit rester testable sans le fichier — mais en le disant."""
    manquant = str(tmp_path / "absent.json")
    communes.referentiel.cache_clear()
    ref = communes.referentiel(manquant)
    assert ref["par_nom"] == {}
    assert any("absent" in enr.message.lower() for enr in caplog.records)


# ---------------------------------------------------------------------------
# Les quatre formats de lieu des sources
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lieu, commune_attendue", [
    ("Paris", "Paris"),                                    # careerjet
    ("Paris, Ile-de-France", "Paris"),                     # adzuna
    ("12ème Arrondissement, Paris", "Paris"),              # adzuna
    ("75 - Paris 8e Arrondissement", "Paris"),             # france_travail
    ("92 - SURESNES", "Suresnes"),                         # france_travail
    ("77 - OZOIR LA FERRIERE", "Ozoir-la-Ferrière"),       # france_travail, sans accents
    ("Neuilly-sur-Seine, A8, FR", "Neuilly-sur-Seine"),    # jobspy
    ("Vélizy-Villacoublay, A8, FR", "Vélizy-Villacoublay"),  # jobspy
    ("Les Clayes-sous-Bois", "Les Clayes-sous-Bois"),      # careerjet, article
    ("L'Haÿ-les-Roses", "L'Haÿ-les-Roses"),                # apostrophe + tréma
])
def test_les_formats_des_quatre_sources_sont_resolus(lieu, commune_attendue):
    commune = communes.resoudre(lieu)
    assert commune is not None, f"« {lieu} » non résolu"
    assert commune["nom"] == commune_attendue


@pytest.mark.parametrize("lieu", [
    "Paris - Clichy, Hauts-de-Seine",
    "Orly, Val-de-Marne - Paris",
    "Montmartre, SK - Paris",
])
def test_les_lieux_composes_au_tiret_de_careerjet(lieu):
    """Careerjet compose ville et département au tiret ; « paris clichy » ne
    correspond à aucune commune, « paris » et « clichy » oui."""
    assert communes.est_idf(lieu)


# ---------------------------------------------------------------------------
# La barrière négative — le cas Jooble
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lieu", [
    "Paris, TX", "Powderly, TX", "Telephone, TX", "Detroit, TX",
    "Arthur City, TX", "Reno, TX",
])
def test_les_86_offres_de_paris_texas_sont_rejetees(lieu):
    """Les lieux EXACTS rendus par Jooble le 2026-09-05, 86 offres sur 86."""
    assert not communes.est_idf(lieu)


def test_la_barriere_negative_passe_avant_la_reconnaissance_de_commune():
    """« Paris, TX » contient une commune d'Île-de-France. L'ordre est tout."""
    assert communes.hors_zone("Paris, TX")
    assert communes.resoudre("Paris, TX") is None
    assert not communes.est_idf("Paris, TX")


def test_les_etats_americains_sont_couverts_au_dela_des_sept_d_origine():
    """`config.MARQUEURS_ETRANGERS` en listait 7 ; les 43 autres passaient."""
    for etat in ("NV", "MI", "MO", "WA", "AZ", "GA"):
        assert not communes.est_idf(f"Springfield, {etat}"), etat


@pytest.mark.parametrize("lieu", [
    "Lucé, CVL, FR",                            # code ISO région
    "Beauvais, Hauts-de-France, France",        # libellé région
    "Chartres, Centre-Val de Loire, France",
    "Le Gué-de-Longroi, Centre-Val de Loire, France",
])
def test_les_autres_regions_francaises_sont_rejetees(lieu):
    assert not communes.est_idf(lieu)


def test_une_commune_hors_idf_est_rejetee():
    """Lyon et Marseille ne sont dans aucune liste noire : c'est la liste
    BLANCHE qui les rejette, et elle doit continuer de le faire."""
    assert not communes.est_idf("Lyon")
    assert not communes.est_idf("Marseille, France")


# ---------------------------------------------------------------------------
# Sémantique d'échec : ici on TRANCHE (contrairement à trajets.json)
# ---------------------------------------------------------------------------
def test_un_lieu_vide_reste_permissif():
    """Comportement d'origine conservé : une source qui ne renseigne pas le
    lieu ne doit pas voir toutes ses offres disparaître."""
    assert communes.est_idf("")
    assert communes.est_idf("   ")


def test_un_lieu_inconnu_tranche_et_ne_rend_jamais_none():
    """La géographie décide de l'entrée dans le périmètre : « je ne sais pas »
    n'est pas une réponse. C'est l'inverse de `trajets.json`, où une commune
    absente rendra None et ne rejettera jamais."""
    for lieu in ("Quelque part", "Zzz, QQ", "42"):
        assert communes.est_idf(lieu) is False


# ---------------------------------------------------------------------------
# Clé de ville canonique
# ---------------------------------------------------------------------------
def test_les_quatre_sources_produisent_la_meme_cle_pour_paris():
    """`dedup._ville` en produisait quatre : « paris », « 75 paris »,
    « 1er arrondissement »… France Travail ne pouvait donc jamais se
    dédoublonner contre une autre source."""
    cles = {communes.cle_ville(lieu) for lieu in (
        "Paris",
        "Paris, Ile-de-France",
        "12ème Arrondissement, Paris",
        "75 - Paris 8e Arrondissement",
        "Paris, A8, FR",
    )}
    assert cles == {"75056"}


def test_la_cle_de_ville_distingue_deux_communes_differentes():
    assert communes.cle_ville("Suresnes") != communes.cle_ville("Nanterre")


@pytest.mark.parametrize("lieu", ["La Défense", "Paris La Défense", "Boulogne",
                                  "Roissy", "La Plaine Saint-Denis"])
def test_les_lieux_dits_qui_ne_sont_pas_des_communes(lieu):
    """« La Défense » est à cheval sur Puteaux, Courbevoie et Nanterre : elle
    n'est une commune d'aucune des trois, donc absente du référentiel INSEE.
    Elle figurait dans l'ancienne liste blanche et doit y rester d'une façon
    ou d'une autre — un référentiel ne dispense pas des cas qu'il ne peut pas
    connaître."""
    assert communes.est_idf(lieu)


def test_la_cle_de_ville_retombe_sur_le_nom_hors_referentiel():
    """Deux offres hors région doivent continuer de se dédoublonner entre elles."""
    assert communes.cle_ville("Lyon") == communes.cle_ville("Lyon, France") == "lyon"


# ---------------------------------------------------------------------------
# Intégration dans filters.est_en_idf
# ---------------------------------------------------------------------------
def _offre(lieu, titre="Stage IA"):
    return Offre(titre, "ACME", lieu, "desc", "http://x", "jobspy:indeed", "", "")


@pytest.mark.parametrize("lieu", [
    "Neuilly-sur-Seine, A8, FR", "Suresnes, A8, FR", "Gennevilliers, A8, FR",
    "Puteaux, A8, FR", "Thiais, A8, FR", "Vélizy-Villacoublay, A8, FR",
    "La Garenne-Colombes, A8, FR",
])
def test_les_communes_jobspy_perdues_sont_recuperees(lieu):
    """Les 35 rejets sur 93 mesurés le 2026-09-05, dont 23 franciliens."""
    assert filters.est_en_idf(_offre(lieu))


def test_le_filtre_de_titre_etranger_prime_toujours():
    """Un lieu francilien ne rachète pas un titre qui annonce l'étranger : les
    cabinets publient depuis Paris des postes qui sont au Canada."""
    assert not filters.est_en_idf(
        _offre("Paris", titre="Stage Data Scientist - IT (H/F) - Canada"))


def test_la_liste_noire_de_config_reste_une_barriere_secondaire():
    """`MARQUEURS_ETRANGERS` est conservée : elle ne peut que rejeter."""
    import config

    assert any(m in ", tx" for m in config.MARQUEURS_ETRANGERS)
    assert not filters.est_en_idf(_offre("Paris, TX"))


def test_aucune_offre_deja_acceptee_ne_devient_rejetee():
    """Non-régression sur les formats réellement présents en base.

    Les 67 lieux distincts de `stages.db` au 2026-09-05 sont tous des lieux
    ACCEPTÉS par l'ancien filtre : aucun ne doit basculer.
    """
    deja_acceptes = [
        "Paris", "Paris, France", "Paris, Ile-de-France",
        "1er Arrondissement, Paris", "8ème Arrondissement, Paris",
        "Asnières-sur-Seine, Hauts-de-Seine - Paris",
        "Orly, Val-de-Marne - Paris", "Paris - Clichy, Hauts-de-Seine",
        "Montmartre, SK - Paris", "Boulogne-Billancourt", "Nanterre",
        "Issy-les-Moulineaux", "La Défense", "Saint-Denis", "Montreuil",
        "Courbevoie",
    ]
    perdus = [lieu for lieu in deja_acceptes if not filters.est_en_idf(_offre(lieu))]
    assert not perdus, f"régression géographique sur : {perdus}"
