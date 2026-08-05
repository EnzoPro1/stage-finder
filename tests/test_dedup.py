"""Tests de la déduplication (exacte par hash, floue par embeddings)."""

from __future__ import annotations

import numpy as np

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
