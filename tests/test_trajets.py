"""Trajets en voiture : résolution des lieux, routage ORS, cache, repli, filtre au seuil.

Aucun appel réseau : la matrice ORS est une doublure qui rend des durées
choisies par le test et compte ses appels.
"""

from __future__ import annotations

import json

import pytest

import config
import observabilite
import recherche
import trajets
from normalize import Offre

_YAML = """
familles:
  ml:
    fr: ["machine learning"]
student_jobs:
  rayon_km: 15
  origines:
    ville_a: {libelle: "Meaux", commune: "Meaux", insee: "77284", code_postal: "77100"}
    campus: {libelle: "ESIEE", commune: "Noisy-le-Grand", insee: "93051", code_postal: "93160",
             coordonnees: [2.583538, 48.838173]}
  termes: ["vendeur"]
  trajet: {seuil_minutes: 45, facteur_trafic: 1.5, facteur_detour: 1.3, vitesse_estimation_kmh: 60}
"""


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    chemin = tmp_path / "recherche.yaml"
    chemin.write_text(_YAML, encoding="utf-8")
    monkeypatch.setattr(recherche, "CHEMIN_RECHERCHE", str(chemin))
    observabilite.arreter()
    yield recherche.charger().student_jobs
    observabilite.arreter()


class _Reponse:
    def __init__(self, status, donnees):
        self.status_code, self._donnees = status, donnees

    def json(self):
        return self._donnees


class _MatriceFactice:
    """Durées en SECONDES par (origine, code commune) ; `None` = non routable."""

    def __init__(self, durees: dict[tuple[str, str], float | None], statut: int = 200,
                 origines=("campus", "ville_a")):
        self.durees, self.statut, self.origines = durees, statut, list(origines)
        self.appels: list[dict] = []

    def post(self, url, json, timeout, headers):
        self.appels.append(json)
        if self.statut != 200:
            return _Reponse(self.statut, {"error": {"code": 6004, "message": "quota"}})
        n = len(json["sources"])
        codes = [self._code(p) for p in json["locations"][n:]]
        # Les origines sont triées par nom dans la requête : campus, ville_a.
        durees = [[self.durees.get((o, c)) for c in codes] for o in self.origines[:n]]
        distances = [[None if d is None else d / 60 for d in ligne] for ligne in durees]
        return _Reponse(200, {"durations": durees, "distances": distances})

    @staticmethod
    def _code(point):
        for commune in trajets.referentiel()["par_code"].values():
            if [commune["lon"], commune["lat"]] == point:
                return commune["code"]
        raise AssertionError(point)


def _calculateur(jobs, tmp_path, session, cle="cle", appels_max=10):
    return trajets.Calculateur(jobs, cle, tmp_path / "trajets.json", session=session,
                               appels_max=appels_max)


def _offre(lieu, titre="Vendeur", source="careerjet"):
    o = Offre(titre, "ACME", lieu, "d", f"http://x/{lieu}", source, "2026-09-17", "")
    o.familles = ["vendeur"]
    return o


# ---------------------------------------------------------------------------
# Résolution d'un lieu
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lieu, code", [
    ("77 - MEAUX", "77284"),
    ("Meaux, A8, FR", "77284"),
    ("Tremblay-en-France, Seine-St-Denis", "93073"),
    ("Dammartin-en-Goële, Meaux", "77153"),
    ("15ème Arrondissement, Paris", "75115"),
    ("75 - Paris 11e Arrondissement", "75111"),
    ("Paris", "75056"),
    ("Château-Thierry, Aisne", "02168"),
    ("Lagny-sur-Marne", "77243"),
    # Formats rencontrés au run réel du 2026-09-17 (96 offres non résolues) :
    ("75 - PARIS 11", "75111"),
    ("75 - PARIS 08", "75108"),
    ("75 - Paris (Dept.)", "75056"),
    ("94 - ST MAUR DES FOSSES", "94068"),
    ("Charles-de-Gaulles Aéroport, Val-d'Oise", "95527"),
    ("Aéroport Paris-Roissy-Charles-de-Gaulle, A8, FR", "95527"),
    ("77 - CLAYE SOUILLY CEDEX", "77118"),
    ("Arnouville-lès-Gonesse, A8, FR", "95019"),
])
def test_resolution_des_formats_de_lieu(lieu, code):
    assert trajets.resoudre(lieu, pres_de=(2.66, 48.89))["code"] == code


def test_un_homonyme_suit_le_departement_cite_puis_la_proximite():
    # « Lagny » seul : la commune de l'Oise — seule à porter exactement ce nom.
    assert trajets.resoudre("Lagny")["departement"] == "60"
    homonymes = [n for n, cs in trajets.referentiel()["par_nom"].items()
                 if len({c["departement"] for c in cs}) > 1]
    nom = next(n for n in homonymes
               if {"77", "02"} <= {c["departement"] for c in trajets.referentiel()["par_nom"][n]})
    assert trajets.resoudre(f"{nom}, Aisne")["departement"] == "02"
    assert trajets.resoudre(f"77 - {nom}")["departement"] == "77"
    proche_77 = trajets.resoudre(nom, pres_de=(2.7, 48.9))
    assert proche_77["departement"] == "77"


@pytest.mark.parametrize("lieu", ["", "Marne-la-Vallée", "Télétravail", "Lyon"])
def test_un_lieu_non_resolu_rend_none(lieu):
    assert trajets.resoudre(lieu) is None


# ---------------------------------------------------------------------------
# Filtre au seuil, depuis AU MOINS UNE origine (ajusté = brut × 1,5 ici)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("brut_campus_min, brut_home_min, garde", [
    (29.0, 90.0, True),    # campus : 43,5 ajustées < 45
    (30.0, 90.0, False),   # campus : 45,0 = seuil -> strictement sous : rejetée
    (31.0, 90.0, False),   # campus : 46,5
    (90.0, 29.9, True),    # l'AUTRE origine suffit : 44,85
    (30.0, 30.0, False),   # toutes au seuil exact
])
def test_seuil_multi_origines_aux_bords(jobs, tmp_path, brut_campus_min, brut_home_min, garde):
    matrice = _MatriceFactice({("campus", "77284"): brut_campus_min * 60,
                               ("ville_a", "77284"): brut_home_min * 60})
    calc = _calculateur(jobs, tmp_path, matrice)
    offre = _offre("77 - MEAUX")
    gardees = trajets.filtrer([offre], calc)
    assert (gardees == [offre]) is garde
    assert offre.trajets["campus"]["brut_min"] == brut_campus_min
    assert offre.trajets["campus"]["ajuste_min"] == round(brut_campus_min * 1.5, 1)
    assert offre.trajets["campus"]["estimation"] is False


def test_une_commune_inconnue_est_gardee_sans_trajet(jobs, tmp_path):
    matrice = _MatriceFactice({})
    offre = _offre("Marne-la-Vallée")
    r = observabilite.demarrer(["careerjet"])
    assert trajets.filtrer([offre], _calculateur(jobs, tmp_path, matrice)) == [offre]
    assert offre.trajets == {} and offre.commune_trajet == ""
    assert matrice.appels == [], "rien à router"
    assert "trajet INCONNU" in r.conseils["trajets_inconnus"]


def test_le_rejet_trop_loin_corrige_le_bilan(jobs, tmp_path):
    r = observabilite.demarrer(["careerjet"], familles=["vendeur"])
    offre = _offre("77 - MEAUX")
    r.compter_brut("careerjet", 1)
    r.compter_survivante("careerjet", ["vendeur"])
    matrice = _MatriceFactice({("campus", "77284"): 3600, ("ville_a", "77284"): 3600})
    trajets.filtrer([offre], _calculateur(jobs, tmp_path, matrice))
    ligne = r.lignes["careerjet"]
    assert (ligne.survivantes, ligne.rejets) == (0, {"trop-loin": 1})
    assert r.familles["vendeur"].survivantes == 0


# ---------------------------------------------------------------------------
# Repli : jamais d'offre perdue parce que le routage manque
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cle, statut, raison", [
    (None, 200, "clé ORS_API_KEY absente"),
    ("cle", 403, "ORS HTTP 403"),
    ("cle", 400, "ORS HTTP 400 quota"),
])
def test_sans_routage_le_trajet_est_estime_et_signale(jobs, tmp_path, cle, statut, raison):
    matrice = _MatriceFactice({}, statut=statut)
    r = observabilite.demarrer([])
    offre = _offre("77 - MEAUX")
    gardees = trajets.filtrer([offre], _calculateur(jobs, tmp_path, matrice, cle=cle))

    campus = offre.trajets["campus"]
    meaux = trajets.referentiel()["par_code"]["77284"]
    attendu_km = trajets.distance_km((2.583538, 48.838173), (meaux["lon"], meaux["lat"])) * 1.3
    assert campus["estimation"] is True
    assert campus["distance_km"] == pytest.approx(attendu_km, abs=0.1)
    assert campus["brut_min"] == pytest.approx(attendu_km / 60 * 60, abs=0.1)
    assert campus["ajuste_min"] == pytest.approx(campus["brut_min"] * 1.5, abs=0.1)
    assert raison in r.conseils["trajets"] and "ESTIMÉS" in r.conseils["trajets"]
    assert gardees == [offre] or campus["ajuste_min"] >= 45


def test_une_api_injoignable_ne_leve_pas(jobs, tmp_path):
    class _Coupee:
        def post(self, *a, **k):
            raise ConnectionError("réseau coupé")

    r = observabilite.demarrer([])
    offre = _offre("77 - MEAUX")
    trajets.filtrer([offre], _calculateur(jobs, tmp_path, _Coupee()))
    assert offre.trajets["ville_a"]["estimation"] is True
    assert "injoignable" in r.conseils["trajets"]


def test_un_point_non_routable_est_estime(jobs, tmp_path):
    matrice = _MatriceFactice({("campus", "77284"): None, ("ville_a", "77284"): 1200})
    offre = _offre("77 - MEAUX")
    trajets.filtrer([offre], _calculateur(jobs, tmp_path, matrice))
    assert offre.trajets["campus"]["estimation"] is True
    assert offre.trajets["ville_a"]["estimation"] is False


# ---------------------------------------------------------------------------
# Cache et quota
# ---------------------------------------------------------------------------
def test_le_cache_evite_un_second_appel(jobs, tmp_path):
    matrice = _MatriceFactice({("campus", "77284"): 1200, ("ville_a", "77284"): 1500})
    trajets.filtrer([_offre("77 - MEAUX")], _calculateur(jobs, tmp_path, matrice))
    assert len(matrice.appels) == 1

    second = _calculateur(jobs, tmp_path, matrice)          # nouveau run, même cache
    offre = _offre("Meaux, A8, FR")
    trajets.filtrer([offre], second)
    assert len(matrice.appels) == 1, "servi depuis trajets.json"
    assert offre.trajets["ville_a"]["brut_min"] == 25.0


def test_les_estimations_ne_sont_pas_mises_en_cache(jobs, tmp_path):
    trajets.filtrer([_offre("77 - MEAUX")], _calculateur(jobs, tmp_path, _MatriceFactice({}), cle=None))
    matrice = _MatriceFactice({("campus", "77284"): 1200, ("ville_a", "77284"): 1500})
    offre = _offre("77 - MEAUX")
    trajets.filtrer([offre], _calculateur(jobs, tmp_path, matrice))
    assert len(matrice.appels) == 1 and offre.trajets["campus"]["estimation"] is False


def test_une_origine_deplacee_invalide_son_cache(jobs, tmp_path):
    matrice = _MatriceFactice({("campus", "77284"): 1200, ("ville_a", "77284"): 1500})
    trajets.filtrer([_offre("77 - MEAUX")], _calculateur(jobs, tmp_path, matrice))
    jobs.origines["campus"].coordonnees = [2.55, 48.83]
    trajets.filtrer([_offre("77 - MEAUX")], _calculateur(jobs, tmp_path, matrice))
    assert len(matrice.appels) == 2
    assert matrice.appels[1]["sources"] == [0], "seule l'origine déplacée est re-routée"


def test_les_lots_respectent_le_plafond_de_routes(jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORS_ROUTES_MAX_PAR_APPEL", 4)   # 2 origines × 2 communes
    lieux = ["77 - MEAUX", "77 - CHELLES", "77 - TORCY", "77 - LAGNY SUR MARNE", "77 - COUPVRAY"]
    matrice = _MatriceFactice({})
    trajets.filtrer([_offre(l) for l in lieux], _calculateur(jobs, tmp_path, matrice))
    assert [len(a["destinations"]) for a in matrice.appels] == [2, 2, 1]
    assert all(len(a["sources"]) * len(a["destinations"]) <= 4 for a in matrice.appels)


def test_le_plafond_d_appels_par_run_bascule_sur_l_estimation(jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORS_ROUTES_MAX_PAR_APPEL", 2)
    r = observabilite.demarrer([])
    matrice = _MatriceFactice({})
    offres = [_offre(l) for l in ["77 - MEAUX", "77 - CHELLES", "77 - TORCY"]]
    trajets.filtrer(offres, _calculateur(jobs, tmp_path, matrice, appels_max=2))
    assert len(matrice.appels) == 2
    assert "plafond de 2 appel(s)" in r.conseils["trajets"]


def test_le_cache_ecrit_est_lisible(jobs, tmp_path):
    matrice = _MatriceFactice({("campus", "77284"): 1200, ("ville_a", "77284"): 1500})
    trajets.filtrer([_offre("77 - MEAUX")], _calculateur(jobs, tmp_path, matrice))
    contenu = json.loads((tmp_path / "trajets.json").read_text(encoding="utf-8"))
    assert contenu["entrees"]["campus|77284"]["duree_s"] == 1200


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_la_configuration_du_depot():
    j = recherche.lire(recherche.CHEMIN_RECHERCHE).student_jobs
    assert (j.trajet.seuil_minutes, j.trajet.facteur_trafic) == (45, 1.3)
    assert j.origines["campus"].coordonnees == [2.583538, 48.838173]
    assert all(trajets.coordonnees_origine(o) for o in j.origines.values())


def test_le_referentiel_couvre_l_idf_et_les_departements_voisins():
    meta = trajets.referentiel()["meta"]
    assert set(meta["departements"]) >= {"75", "77", "93", "94", "95", "02", "60", "51"}
    assert "75111" in trajets.referentiel()["par_code"]
