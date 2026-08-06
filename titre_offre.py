"""titre_offre.py — Rendre un intitulé d'offre présentable en tête de CV.

Le repli du CP3-bis fournit à ``cv_forge`` le titre que stage_finder a en
base. Or ces titres viennent de job boards et traînent leur bagage :

    « Stage Consultant IA & Data - 6 mois H/F »
    « STAGE 6 MOIS ANALYSTE ASSET MANAGER RESIDENTIEL H/F »

Recopié tel quel, ça donne un en-tête de CV qui annonce « H/F » au
recruteur. Le repli est « la fiche de l'appelant » : c'est donc ici, et
pas dans ``cv_forge``, que la fiche doit être présentable.

## Pourquoi PAS ``dedup._normaliser_titre``

Elle existe, elle retire déjà les mentions de genre, et la réutiliser
serait une faute sur deux plans :

1. **Elle est faite pour HACHER.** Elle minuscule, retire les accents et
   réduit à ``[a-z0-9]``. « Stage Ingénieur Cybersécurité F/H » en
   ressort « stage ingenieur cybersecurite » — parfait comme clé,
   illisible en tête de CV.
2. **Elle alimente ``dedup._cle``**, donc l'identité des offres en base.
   Y toucher déplacerait les 294 clés existantes et détacherait tous les
   jobs de génération de leurs offres.

Les deux fonctions ont donc le même sujet et des buts opposés. Elles
restent séparées, et celle-ci ne préserve QUE ce que l'autre détruit :
la casse et les accents.

## Ce qui est retiré, et rien d'autre

- la mention de genre, sous toutes les formes vues dans les données
  réelles : ``H/F``, ``(F/H)``, ``F/H/X``, ``(f/m/d)``, ``(x/f/m)``,
  ``M/F/Mx``, y compris DOUBLÉE (« … civils F/H F/H - L - DIV ») ;
- le suffixe de durée : ``6 mois``, ``(6 mois)``, ``de 6 mois``,
  ``6 months``, ``2 MOIS`` ;
- les séparateurs devenus orphelins par ces retraits.

La mention de genre n'est PAS toujours en fin de titre — « … F/H - Air
France KLM », « … (H/F) - Canada ». Elle est donc retirée EN PLACE, et
surtout pas par troncature : couper au marqueur perdrait l'employeur ou
le pays.

Le reste est laissé intact. Ce module ne réécrit pas les titres, il
enlève du bruit administratif.
"""

from __future__ import annotations

import re

# Composantes d'une mention de genre. ``mx`` fait deux lettres et doit
# être tenté AVANT les alternatives d'une lettre, sinon ``m`` gagne et
# laisse un ``x`` orphelin derrière lui.
_GENRE = r"(?:mx|[hfmwdx])"

# Deux ou trois composantes séparées par ``/``, éventuellement
# parenthésées. Les parenthèses sont capturées AVEC la mention pour ne
# pas laisser « () » derrière — le nettoyage les rattraperait, mais
# autant ne pas en produire.
#
# Les frontières de mot sont indispensables : sans elles, « Analyste
# financier/Assistant » ou « SEO/SEA » deviendraient des mentions de
# genre. Aucun des 294 titres réels ne déclenche ce faux positif, et
# c'est ``\b`` qui l'empêche.
MENTION_GENRE = re.compile(
    rf"[(\[]?\s*\b{_GENRE}\s*/\s*{_GENRE}(?:\s*/\s*{_GENRE})?\b\s*[)\]]?",
    re.IGNORECASE,
)

# Durée. Les parenthèses ne sont VOLONTAIREMENT pas consommées ici : sur
# « Stage Django / React (6 mois - démarrage fin 2026) », avaler la
# parenthèse ouvrante laisserait une fermante orpheline. On retire la
# seule durée et le nettoyage rééquilibre ce qui reste.
#
# Le « de » optionnel évite de laisser « Stage de » pendu après le
# retrait dans « Stage de 6 mois - Analyste stratégique ».
DUREE = re.compile(r"\b(?:de\s+)?\d{1,2}\s*(?:mois|months?)\b", re.IGNORECASE)

_SEPARATEURS = r"-–—|,/"


def _nettoyer_separateurs(titre: str) -> str:
    """Rattrape la ponctuation devenue orpheline après les retraits."""
    # « (  ) » et « [  ] » vidés de leur contenu.
    titre = re.sub(r"[(\[]\s*[)\]]", " ", titre)
    # Séparateur collé à l'intérieur d'une parenthèse : « ( - démarrage ».
    titre = re.sub(rf"([(\[])\s*[{_SEPARATEURS}]+\s*", r"\1", titre)
    titre = re.sub(rf"\s*[{_SEPARATEURS}]+\s*([)\]])", r"\1", titre)
    # « Stage -  - Risk ESG Analyst » -> un seul séparateur.
    titre = re.sub(r"(?:\s*[-–—|]\s*){2,}", " - ", titre)
    titre = re.sub(r"\s+", " ", titre)
    return titre.strip(f" \t{_SEPARATEURS}")


def nettoyer(titre: str | None) -> str:
    """Rend le titre présentable. Jamais vide si l'entrée ne l'était pas.

    Le garde-fou final n'est pas décoratif : un titre entièrement composé
    de bruit — on n'en a pas dans les données actuelles, mais rien ne
    l'interdit — sortirait vide et produirait un en-tête de CV blanc.
    Dans ce cas on rend l'original, qui est laid mais informatif.
    """
    if not titre or not titre.strip():
        return ""

    propre = MENTION_GENRE.sub(" ", titre)
    propre = DUREE.sub(" ", propre)
    propre = _nettoyer_separateurs(propre)

    return propre if propre else titre.strip()
