"""Tests de la déduplication (exacte par hash, floue par embeddings)."""

from __future__ import annotations

import numpy as np
import pytest

import dedup
from normalize import Offre


def _offre(title, company="ACME", location="Paris", description="d", url="http://x"):
    return Offre(title, company, location, description, url, "test", "", "")


def test_dedup_exacte_garde_plus_riche():
    courte = _offre("Stage IA (H/F)", description="courte", url="http://a")
    longue = _offre("Stage IA", description="description bien plus longue et riche",
                    url="http://b")
    resultat = dedup.dedupliquer([courte, longue])
    # Même clé (titre normalisé identique) -> une seule offre, la plus riche.
    assert len(resultat) == 1
    assert resultat[0].url == "http://b"


def test_dedup_exacte_arrondissements_paris():
    a = _offre("Stage Data", location="Paris 15e")
    b = _offre("Stage Data", location="Paris 8e")
    assert len(dedup.dedupliquer([a, b])) == 1


# ---------------------------------------------------------------------
# Mentions de genre
#
# Une même annonce republiée dans une autre langue ne change que sa
# mention : « (H/F) » en français, « (M/F) » en anglais, « (M/W/D) » en
# allemand. Si la normalisation n'en retire qu'une partie, la même offre
# occupe deux lignes de `offres` (dont `cle` est la clé primaire) et
# apparaît deux fois à l'écran.
# ---------------------------------------------------------------------
@pytest.mark.parametrize("mention", [
    "(H/F)", "(h/f)", "H/F", "H-F", "H / F", "[H/F]",       # français
    "(F/H)", "F/H",
    "(M/F)", "M/F", "(F/M)", "F/M",                         # anglais
    "(W/M)", "W/M", "(M/W)", "M/W",                         # allemand
    "(H/F/D)", "H/F/N", "(H/F/X)", "(M/W/D)", "(F/M/X)", "(f/m/d)",
    "(x/f/m)", "(X/F/M)",                                   # marqueur en tête
])
def test_toute_mention_de_genre_donne_la_meme_cle(mention):
    nu = _offre("Stage Ingenieur IA")
    avec = _offre(f"Stage Ingenieur IA {mention}")
    assert dedup._cle(avec) == dedup._cle(nu), (
        f"« {mention} » ne se replie pas sur le titre nu : la même offre "
        f"republiée avec cette mention créerait une seconde ligne."
    )


def test_deux_langues_de_la_meme_offre_fusionnent():
    """Le cas réel : LinkedIn publie en « M/F » ce qu'Adzuna publie en « H/F »."""
    fr = _offre("Stage Ingenieur IA (H/F)", description="courte", url="http://fr")
    en = _offre("Stage Ingenieur IA (M/F)", url="http://en",
                description="description bien plus longue et donc plus riche")
    resultat = dedup.dedupliquer([fr, en])
    assert len(resultat) == 1
    assert resultat[0].url == "http://en"          # la plus riche est gardée


@pytest.mark.parametrize("titre, attendu", [
    # Le séparateur est OBLIGATOIRE : sans lui, « hf » au milieu d'un mot
    # était retiré et « Freshfields » entrait dans la clé en « fres ields ».
    ("Freshfields Bruckhaus Deringer", "freshfields bruckhaus deringer"),
    ("Stage HF radio", "stage hf radio"),
    # Les frontières de mot, dans l'autre sens : le « h » de « RH » était
    # avalé avec le « f » suivant, laissant « data r h ».
    ("Valorisation des produits Data RH F/H", "valorisation des produits data rh"),
    ("Stage RH", "stage rh"),
    # Deux lettres séparées par « / » ne sont pas toutes une mention.
    ("Data/Finance", "data finance"),
    ("R&D / Formation", "r d formation"),
    ("Chef de projet", "chef de projet"),
])
def test_ce_qui_ressemble_a_une_mention_sans_en_etre_une_est_intact(titre, attendu):
    assert dedup._normaliser_titre(titre) == attendu


def test_une_mention_seule_ne_vide_pas_le_titre():
    """Garde-fou : un titre réduit à sa mention donnerait une clé vide,
    donc partagée par toutes les offres dans ce cas."""
    assert dedup._normaliser_titre("Stage (H/F)") == "stage"


def test_dedup_floue_fusionne_vecteurs_proches():
    o1 = _offre("Stage IA chez Sanofi", company="Sanofi", url="http://1",
                description="courte")
    o2 = _offre("Stage IA chez Sanofi", company="Sanofi Group", url="http://2",
                description="description plus longue donc plus riche")
    o3 = _offre("Stage Cyber", company="Autre", url="http://3")
    # Embeddings synthétiques normalisés : o1 et o2 quasi identiques, o3 orthogonal.
    emb = np.array([
        [1.0, 0.0, 0.0],
        [0.99, 0.14106736, 0.0],   # cos(o1,o2) ~ 0.99
        [0.0, 0.0, 1.0],
    ])
    emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
    restantes, emb_restants = dedup.dedupliquer_flou([o1, o2, o3], emb, seuil=0.9)
    assert len(restantes) == 2
    assert emb_restants.shape[0] == 2
    # La version la plus riche (o2) est conservée.
    urls = {o.url for o in restantes}
    assert "http://2" in urls and "http://3" in urls
    assert "http://1" not in urls


def test_dedup_floue_seuil_haut_ne_fusionne_pas():
    o1 = _offre("Stage A", url="http://1")
    o2 = _offre("Stage B", url="http://2")
    emb = np.array([[1.0, 0.0], [0.0, 1.0]])  # orthogonaux
    restantes, _ = dedup.dedupliquer_flou([o1, o2], emb, seuil=0.9)
    assert len(restantes) == 2
