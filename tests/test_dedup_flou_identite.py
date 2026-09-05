"""La dédup floue ne fusionne plus deux offres que le cosinus croit identiques.

## L'incident

`ranker._texte_a_encoder` encode **titre + description**. L'entreprise et le
lieu n'entrent pas dans le vecteur : deux annonces au même gabarit sont donc à
cosinus ~1 même chez des employeurs différents, dans des villes différentes.

Mesuré le 2026-09-05 sur 447 offres de type job étudiant, à 0,90 :
**79 fusions, dont 37 entre communes différentes et 22 entre employeurs
différents.** Six détruisaient l'horaire annoncé dans le titre — « Vendeur en
CDI 10H » absorbé par « CDI 35H » — et une confondait un poste de jour avec le
même poste de nuit, à cosinus 0,999.

Et ce n'était pas un problème de seuil : à 0,99 il restait 17 fusions entre
employeurs distincts et 19 entre communes distinctes. Le seuil ne sépare pas ce
que le vecteur ne contient pas.

## Comment ces tests sont écrits

Les embeddings sont FOURNIS À LA MAIN, tous identiques, donc cosinus = 1,0
partout. Le test isole ainsi le garde-fou d'identité : toute non-fusion
observée vient de lui et de rien d'autre. Aucun modèle n'est chargé.
"""

from __future__ import annotations

import numpy as np
import pytest

import dedup
import observabilite
from normalize import Offre


def _offre(title, company="ACME", location="Paris", description="Service en salle.",
           url=None):
    return Offre(title, company, location, description,
                 url or f"http://x/{title}/{company}", "careerjet", "", "")


def _embeddings_identiques(n):
    """n vecteurs unitaires IDENTIQUES : cosinus = 1,0 entre toutes les paires."""
    v = np.zeros((n, 4), dtype=float)
    v[:, 0] = 1.0
    return v


def _flou(offres):
    gardees, _ = dedup.dedupliquer_flou(offres, _embeddings_identiques(len(offres)))
    return gardees


# ---------------------------------------------------------------------------
# Ce qui doit CESSER de fusionner
# ---------------------------------------------------------------------------
def test_deux_employeurs_differents_ne_fusionnent_pas():
    """« SERVEUR H/F | Chez Justine » et « Serveur H/F | Tripletta », cos 0,990."""
    offres = [_offre("Serveur H/F", "Tripletta"), _offre("SERVEUR H/F", "Chez Justine")]
    assert len(_flou(offres)) == 2


def test_deux_communes_differentes_ne_fusionnent_pas():
    """« Equipier Polyvalent | LIDL Fresnes » et « LIDL Esbly », cos 0,977."""
    offres = [_offre("Equipier Polyvalent", "LIDL", "94 - Fresnes"),
              _offre("Equipier Polyvalent", "LIDL", "77 - Esbly")]
    assert len(_flou(offres)) == 2


@pytest.mark.parametrize("titre_a, titre_b", [
    ("Vendeur(euse) en CDI 10H", "Vendeur(euse) en CDI 35H"),      # Lovisa
    ("CDI 18H - Vendeur", "CDI 35H - Vendeur"),                    # Petit Bateau
    ("Equipier Polyvalent 30H", "Equipier Polyvalent 35H"),        # LIDL
    ("35h - Équipier Polyvalent", "30h/Semaine - Équipier Polyvalent"),  # Hello You
])
def test_des_horaires_differents_ne_fusionnent_pas(titre_a, titre_b):
    """Le champ qui DÉCIDE dans le mode job étudiant.

    Ces quatre paires sont chez le MÊME employeur dans la MÊME commune : le
    garde-fou entreprise+commune ne les voyait pas, et 5 des 6 fusions
    destructrices d'horaire survivaient. Un poste à 10 h et un poste à 35 h ne
    sont pas la même offre.
    """
    offres = [_offre(titre_a, "Lovisa"), _offre(titre_b, "Lovisa")]
    assert len(_flou(offres)) == 2


def test_un_titre_avec_horaire_ne_fusionne_pas_avec_un_titre_muet():
    """Même asymétrie : dans le doute, on garde les deux."""
    offres = [_offre("Vendeur 24H", "Lovisa"), _offre("Vendeur", "Lovisa")]
    assert len(_flou(offres)) == 2


def test_sans_entreprise_on_ne_fusionne_pas():
    """Le cas « Aide-soignant de JOUR » / « de NUIT » : cosinus 0,999, publiés
    sans nom d'entreprise, au même endroit. Seule l'abstention les a sauvés."""
    offres = [_offre("Aide-soignant de jour à temps partiel", "", "95 - TAVERNY"),
              _offre("Aide-soignant à temps partiel de nuit", "", "95 - TAVERNY")]
    assert len(_flou(offres)) == 2


def test_sans_lieu_exploitable_on_ne_fusionne_pas():
    offres = [_offre("Serveur", "Tripletta", ""), _offre("Serveur", "Tripletta", "")]
    assert len(_flou(offres)) == 2


# ---------------------------------------------------------------------------
# Ce qui doit CONTINUER de fusionner
# ---------------------------------------------------------------------------
def test_un_vrai_doublon_fusionne_toujours():
    """Le garde-fou ne doit pas désactiver la déduplication floue."""
    offres = [_offre("Serveur H/F", "Tripletta", "Paris", "court"),
              _offre("Serveur H/F", "Tripletta", "Paris",
                     "description nettement plus longue et donc plus riche")]
    gardees = _flou(offres)
    assert len(gardees) == 1
    assert gardees[0].description.startswith("description nettement")


def test_un_accent_sur_l_entreprise_ne_bloque_pas_la_fusion():
    """« Hotel Tourisme Avenue » et « Hôtel Tourisme Avenue », même adresse :
    un vrai doublon, rencontré en mesure."""
    offres = [_offre("Equipier Polyvalent F/H", "Hotel Tourisme Avenue", "Paris"),
              _offre("Equipier Polyvalent F/H", "Hôtel Tourisme Avenue",
                     "15ème Arrondissement, Paris")]
    assert len(_flou(offres)) == 1


def test_les_arrondissements_parisiens_restent_une_seule_commune():
    """Le code INSEE de Paris est unique : 5e et 17e sont la même commune."""
    offres = [_offre("Serveur H/F", "Tripletta", "5ème Arrondissement, Paris"),
              _offre("Serveur H/F", "Tripletta", "17ème Arrondissement, Paris")]
    assert len(_flou(offres)) == 1


def test_le_cosinus_reste_le_test_du_texte():
    """L'identité ne suffit pas : deux offres du même employeur au même endroit
    mais au texte éloigné ne doivent pas fusionner."""
    emb = np.zeros((2, 4))
    emb[0, 0] = 1.0
    emb[1, 1] = 1.0            # orthogonaux -> cosinus 0
    offres = [_offre("Serveur", "Tripletta"), _offre("Plongeur", "Tripletta")]
    gardees, _ = dedup.dedupliquer_flou(offres, emb)
    assert len(gardees) == 2


# ---------------------------------------------------------------------------
# Extraction des horaires du titre
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("titre, attendu", [
    ("Vendeur(euse) en CDI 10H", {10}),
    ("CDI 18H - Vendeur (h/f/x)", {18}),
    ("Equipier polyvalent 10h Midi en semaine H/F", {10}),
    ("SERVEUR TEMPS PARTIEL SOIR 24H / SEMAINE", {24}),
    ("Serveur (H/F) - CDI 35h/semaine", {35}),
    ("Vendeur / Vendeuse expert(e) 21h Herblay (F/H)", {21}),
    ("Serveur H/F", set()),
    ("Equipier Polyvalent F/H", set()),
])
def test_lecture_des_horaires_du_titre(titre, attendu):
    assert dedup._heures_titre(titre) == frozenset(attendu)


def test_h_f_n_est_pas_un_horaire():
    """« H/F » est partout dans les intitulés français : le confondre avec un
    volume horaire bloquerait la fusion de presque tout."""
    assert dedup._heures_titre("Serveur H/F") == frozenset()
    assert dedup._heures_titre("Vendeur (H/F/X)") == frozenset()


def test_une_heure_de_la_journee_n_est_pas_un_volume():
    """« de 8h30 à 14h00 » décrit un créneau, pas un volume hebdomadaire."""
    assert dedup._heures_titre("Equipier - Temps Partiel de midi à 14h00") == frozenset()


# ---------------------------------------------------------------------------
# Attribution par source des offres supprimées
# ---------------------------------------------------------------------------
def test_les_fusions_sont_attribuees_a_leur_source():
    """Sans attribution, 79 disparitions sur 447 n'apparaissaient nulle part.

    `filters.filtrer` compte les « gardées » AVANT `dedup` : une offre
    fusionnée restait comptée comme gardée.
    """
    releve = observabilite.demarrer(["careerjet"])
    offres = [_offre("Serveur H/F", "Tripletta", "Paris", "court"),
              _offre("Serveur H/F", "Tripletta", "Paris", "texte bien plus long")]
    for o in offres:
        releve.compter_survivante(o.source)

    _flou(offres)
    ligne = releve.lignes["careerjet"]
    assert ligne.survivantes == 2
    assert ligne.fusions == 1
    assert ligne.retenues == 1, "la colonne « retenues » déduit les fusions"
    observabilite.arreter()


def test_la_dedup_fonctionne_sans_releve_actif():
    observabilite.arreter()
    assert len(_flou([_offre("Serveur", "Tripletta"), _offre("Serveur", "Tripletta")])) == 1
