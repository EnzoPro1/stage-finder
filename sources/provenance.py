"""
sources/provenance.py — Quelle requête a fait remonter quelle offre.

Une source interroge désormais par FAMILLE (ou par terme, qui sert une ou
plusieurs familles). Chaque item brut rapporté porte la trace des familles
dont une requête l'a trouvé, dans la clé ``_familles`` ; ``normalize`` la
recopie sur ``Offre.familles``.

La trace est posée DANS le dict brut, et non renvoyée à côté : la signature
``recuperer_offres() -> list[dict]`` reste celle des six sources, du registre
et des deux appelants. Même raisonnement que `observabilite` — une
fonctionnalité transverse ne doit pas réécrire toutes les interfaces.

## Fusion à l'intérieur d'une source

Une même annonce revient souvent de deux requêtes (« LLM » et « MLOps »
trouvent la même offre de LLMOps). Sans fusion, elle compterait deux fois dans
le brut de la source, et la seconde copie serait supprimée plus loin par la
déduplication en emportant ses familles. ``fusionner`` rassemble les copies
d'un même identifiant de source en une seule, qui porte l'union des familles.
"""

from __future__ import annotations

from typing import Callable, Iterable

import recherche

# Clé réservée dans le dict brut. Préfixe « _ » : aucune API ne l'emploie.
CLE_FAMILLES = "_familles"


def familles_de(item: dict) -> list[str]:
    """Familles portées par un item brut (liste vide si aucune)."""
    return list(item.get(CLE_FAMILLES) or [])


def marquer(items: Iterable[dict], familles: Iterable[str]) -> list[dict]:
    """Ajoute ``familles`` à la trace de chaque item, sans doublon, ordre conservé.

    Modifie les dicts en place et les renvoie en liste : ce sont des objets
    fraîchement désérialisés, que rien d'autre ne référence.
    """
    familles = list(familles)
    lot = list(items)
    for item in lot:
        deja = familles_de(item)
        item[CLE_FAMILLES] = deja + [f for f in familles if f not in deja]
    return lot


def marquer_par_texte(
    items: Iterable[dict], candidates: list[str], texte_de: Callable[[dict], str]
) -> list[dict]:
    """Étiquette chaque item par les familles dont un terme figure dans SON texte.

    Pour une requête OU : le moteur ne dit pas quel terme a répondu. Le texte
    livré (titre, description ou extrait) le dit, mots entiers. Sans terme
    retrouvé, l'item porte ``recherche.ETIQUETTE_NON_RETROUVE`` — jamais rien :
    une offre sans étiquette ressemblerait à une offre d'une source qui
    n'interroge pas par terme.
    """
    lot = list(items)
    connues = recherche.charger().toutes_familles()
    for item in lot:
        trouvees = recherche.familles_dans_texte(texte_de(item) or "", candidates, connues)
        marquer([item], trouvees or [recherche.ETIQUETTE_NON_RETROUVE])
    return lot


def fusionner(items: Iterable[dict], identifiant: Callable[[dict], object]) -> list[dict]:
    """Une copie par identifiant, portant l'union des familles de toutes les copies.

    La PREMIÈRE copie rencontrée est gardée (l'ordre des requêtes est celui du
    fichier de recherche, donc déterministe). Un item sans identifiant est
    conservé tel quel : ne pas savoir le rapprocher n'autorise pas à le jeter.
    """
    gardes: list[dict] = []
    par_id: dict[object, dict] = {}
    for item in items:
        cle = identifiant(item)
        if not cle:
            gardes.append(item)
            continue
        premier = par_id.get(cle)
        if premier is None:
            par_id[cle] = item
            gardes.append(item)
            continue
        marquer([premier], familles_de(item))
    return gardes
