"""Nettoyage du titre affiché en tête de CV — `titre_offre.nettoyer`.

Le golden est composé de VINGT titres réels de `stages.db` — quatorze qui
changent, six qui ne doivent pas bouger. Les six comptent autant que les
quatorze : un nettoyeur qui abîme les titres déjà propres serait pire que
pas de nettoyeur du tout.

Sur les 294 titres réels, 163 changent, aucun ne devient vide, aucun ne
descend sous quinze caractères.
"""

from __future__ import annotations

import pytest

import titre_offre
from titre_offre import nettoyer

# =====================================================================
# Golden — vingt titres réels, sortie figée
# =====================================================================
GOLDEN = [
    (
        'Stage Assistant Marketing Digital & Stratégie de Contenu - Marketing (H/F)',
        'Stage Assistant Marketing Digital & Stratégie de Contenu - Marketing',
    ),
    (
        'Machine Learning Engineer  (H/F) | Stage',
        'Machine Learning Engineer | Stage',
    ),
    (
        'Stage - Analyste Sectoriel Electricité - H/F - 6 mois',
        'Stage - Analyste Sectoriel Electricité',
    ),
    (
        'Stage – Sustainability Data & ESG Reporting – Septembre 2026 (H/F)',
        'Stage – Sustainability Data & ESG Reporting – Septembre 2026',
    ),
    (
        'Excellence Track Program : stage ingénieur fin d’études – Parcours audit '
        'financier/tech – Paris La Défense F/H',
        'Excellence Track Program : stage ingénieur fin d’études – Parcours audit '
        'financier/tech – Paris La Défense',
    ),
    (
        'Stage Data Analyst Marketing (H/F) - Canada',
        'Stage Data Analyst Marketing - Canada',
    ),
    (
        'Analyste M&A - F/H - Stage',
        'Analyste M&A - Stage',
    ),
    (
        'Stagiaire Data & IA - Transformation Digitale (H/F)',
        'Stagiaire Data & IA - Transformation Digitale',
    ),
    (
        'Air Liquide - Stage - HR Data Analyst (H/F)',
        'Air Liquide - Stage - HR Data Analyst',
    ),
    (
        'Stage Analyste Investissement immobilier I&L - H/F',
        'Stage Analyste Investissement immobilier I&L',
    ),
    (
        'Stage – Analyste Cybersécurité & Gestion des Risques (H/F)',
        'Stage – Analyste Cybersécurité & Gestion des Risques',
    ),
    (
        'Stage - Talent acquisition Specialist (H/F)',
        'Stage - Talent acquisition Specialist',
    ),
    (
        'Stage en Droit des sociétés/financier/boursier/gouvernance H/F',
        'Stage en Droit des sociétés/financier/boursier/gouvernance',
    ),
    (
        'STAGE - INGÉNIEUR IA (GEN AI, LLM, …) - H/F',
        'STAGE - INGÉNIEUR IA (GEN AI, LLM, …)',
    ),
    # --- les six qui ne doivent PAS bouger ---------------------------
    (
        'Stage - Acculturation à l’usage de l’intelligence artificielle pour la '
        'prévention des risques',
        'Stage - Acculturation à l’usage de l’intelligence artificielle pour la '
        'prévention des risques',
    ),
    (
        'Stage – Impact des marchés de l’électricité sur les charges éoliennes',
        'Stage – Impact des marchés de l’électricité sur les charges éoliennes',
    ),
    (
        'Stage - Cloud et sécurité : vers une association gagnante ?',
        'Stage - Cloud et sécurité : vers une association gagnante ?',
    ),
    (
        'Stage Gouvernance et communication Digital Trust',
        'Stage Gouvernance et communication Digital Trust',
    ),
    ('Legal Intern', 'Legal Intern'),
    (
        'Stage Data Scientist IA – LLM & Agentic RAG',
        'Stage Data Scientist IA – LLM & Agentic RAG',
    ),
]


@pytest.mark.parametrize("brut,attendu", GOLDEN, ids=range(len(GOLDEN)))
def test_golden_sur_titres_reels(brut, attendu):
    assert nettoyer(brut) == attendu


# =====================================================================
# Ce qui distingue cette fonction de `dedup._normaliser_titre`
# =====================================================================
def test_la_casse_est_preservee():
    """`dedup._normaliser_titre` minuscule pour hacher. Ici on affiche."""
    assert nettoyer("STAGE Data Analyst (H/F)") == "STAGE Data Analyst"


def test_les_accents_sont_preserves():
    assert nettoyer("Stage Ingénieur Cybersécurité F/H") == \
        "Stage Ingénieur Cybersécurité"


def test_la_ponctuation_utile_survit():
    """`&`, `:`, `?`, `«»`, `|` font partie du titre, pas du bruit."""
    assert nettoyer("Stagiaire « Data Miner » H/F") == 'Stagiaire « Data Miner »'
    assert nettoyer("Stage - Cloud : gagnante ? (H/F)") == \
        "Stage - Cloud : gagnante ?"


def test_les_cles_de_dedup_ne_bougent_pas():
    """La garantie qui protège les 294 offres en base : ce module n'est
    PAS branché sur `dedup`, donc l'identité des offres est intacte."""
    from dedup import _normaliser_titre

    titre = "Stage Ingénieur Cybersécurité F/H"
    assert _normaliser_titre(titre) == "stage ingenieur cybersecurite"
    assert nettoyer(titre) != _normaliser_titre(titre)


# =====================================================================
# Mentions de genre — toutes les formes vues dans les données réelles
# =====================================================================
@pytest.mark.parametrize("mention", [
    "H/F", "F/H", "(H/F)", "(F/H)", "h/f", "F/H/X", "H/F/X",
    "(f/m/d)", "(x/f/m)", "M/F/Mx", "[H/F]", "(F/H/X)",
])
def test_toutes_les_formes_de_mention_de_genre(mention):
    assert nettoyer(f"Stage Data Analyst {mention}") == "Stage Data Analyst"


def test_une_mention_doublee_part_entierement():
    """Vu en base : « Ingénieurs civils F/H F/H - L - DIV »."""
    assert nettoyer("Ingénieurs civils F/H F/H - L - DIV") == \
        "Ingénieurs civils - L - DIV"


def test_la_mention_au_milieu_ne_tronque_pas_la_fin():
    """LE piège. La mention n'est pas toujours en fin de titre : couper au
    marqueur perdrait l'employeur ou le pays."""
    assert nettoyer("Stage Data Scientist - IT (H/F) - Canada") == \
        "Stage Data Scientist - IT - Canada"
    assert nettoyer("Stage IA F/H - SAFRAN ELECTRONICS & DEFENSE") == \
        "Stage IA - SAFRAN ELECTRONICS & DEFENSE"


# =====================================================================
# Faux positifs : un « / » n'est pas une mention de genre
# =====================================================================
@pytest.mark.parametrize("titre", [
    "Stage - Analyste financier/Assistant Portfolio Manager",
    "Applied Scientist / Research Engineer (Internship)",
    "Consultante / Consultant Data, Risque & Conformité - Stage",
    "SRE / DevOps - Stage de fin d'études - Paris",
    "Stage Analyste Business Plan (Paris/Lyon ou Marseille)",
    "Software Engineering / Machine Learning Engineering Intern",
    "Stage Innovation : Ingénieur Génie Logiciel / Intelligence Artificielle",
    "STAGE IA/LLM pour la Transformation de l'Ingénierie",
])
def test_un_slash_ordinaire_est_laisse_tranquille(titre):
    assert nettoyer(titre) == titre


def test_seule_la_mention_part_quand_les_deux_coexistent():
    """« SEO/SEA » reste, « (H/F) » part, « Canada » reste."""
    assert nettoyer("Stage Web Marketing & SEO/SEA - Marketing (H/F) - Canada") == \
        "Stage Web Marketing & SEO/SEA - Marketing - Canada"


# =====================================================================
# Durées
# =====================================================================
@pytest.mark.parametrize("brut,attendu", [
    ("Stage Consultant IA & Data - 6 mois H/F", "Stage Consultant IA & Data"),
    ("Stage - 6 mois - Risk ESG Analyst F/H", "Stage - Risk ESG Analyst"),
    ("STAGE 6 MOIS ANALYSTE ASSET MANAGER H/F", "STAGE ANALYSTE ASSET MANAGER"),
    ("Stage de 6 mois - Analyste stratégique F/H (SFD)",
     "Stage - Analyste stratégique (SFD)"),
    ("internship 6 months - data science", "internship - data science"),
    ("DATA SCIENTIST - STAGE 2 MOIS", "DATA SCIENTIST - STAGE"),
])
def test_les_durees_sont_retirees(brut, attendu):
    assert nettoyer(brut) == attendu


def test_une_duree_dans_une_parenthese_porteuse_rééquilibre_les_parentheses():
    """« (6 mois - démarrage fin 2026) » : retirer la durée ne doit pas
    laisser une parenthèse ouvrante orpheline ni un tiret pendu."""
    assert nettoyer("Stage Django / React (6 mois - démarrage fin 2026)") == \
        "Stage Django / React (démarrage fin 2026)"
    assert nettoyer("Sales Analyst (Stage 6 mois sept 2026) - H/F/X") == \
        "Sales Analyst (Stage sept 2026)"


def test_une_parenthese_videe_de_sa_duree_disparait():
    assert nettoyer("Stage Data Scientist (6 mois) – Conseil (f/m/d)") == \
        "Stage Data Scientist – Conseil"


def test_un_nombre_qui_n_est_pas_une_duree_reste():
    assert nettoyer("Stage – Septembre 2026 (H/F)") == "Stage – Septembre 2026"
    assert nettoyer("Stage Bac+3 (H/F)") == "Stage Bac+3"


# =====================================================================
# Bords
# =====================================================================
@pytest.mark.parametrize("vide", ["", "   ", None])
def test_une_entree_vide_rend_une_chaine_vide(vide):
    assert nettoyer(vide) == ""


def test_un_titre_entierement_bruit_rend_l_original():
    """Filet : un en-tête de CV blanc serait pire qu'un en-tête laid.
    Aucun des 294 titres réels n'est dans ce cas, mais rien ne l'interdit."""
    assert nettoyer("H/F") == "H/F"
    assert nettoyer("(F/H)") == "(F/H)"


def test_les_espaces_multiples_sont_reduits():
    assert nettoyer("Stage –  Commercial (tech)") == "Stage – Commercial (tech)"


def test_la_fonction_est_idempotente():
    """La rappliquer sur son propre résultat ne doit plus rien changer."""
    for brut, attendu in GOLDEN:
        assert nettoyer(attendu) == attendu, brut


# =====================================================================
# Balayage sur les vraies données
# =====================================================================
def test_aucun_titre_reel_n_est_vide_ni_tronque(tmp_path):
    """Le differ, figé en test : il tourne sur `stages.db` si elle est là,
    et se skippe proprement sinon (la base n'est pas versionnée)."""
    import os

    import config
    import storage

    if not os.path.exists(config.CHEMIN_BASE):
        pytest.skip("stages.db absente (non versionnée)")

    conn = storage.ouvrir(config.CHEMIN_BASE)
    titres = [l["title"] or "" for l in conn.execute("SELECT title FROM offres")]
    conn.close()
    if not titres:
        pytest.skip("aucune offre en base")

    vides = [t for t in titres if t.strip() and not nettoyer(t).strip()]
    assert not vides, f"{len(vides)} titre(s) vidé(s) : {vides[:3]}"

    courts = [(t, nettoyer(t)) for t in titres
              if len(t.strip()) > 25 and len(nettoyer(t)) < 15]
    assert not courts, f"titre(s) tronqué(s) : {courts[:3]}"


def test_le_module_n_est_reference_qu_a_un_seul_endroit():
    """« Appliquée au moment de construire l'`OfferInput`, nulle part
    ailleurs. » Ce test est ce qui empêche l'essaimage."""
    import subprocess
    import sys
    from pathlib import Path

    racine = Path(titre_offre.__file__).resolve().parent
    sortie = subprocess.run(
        [sys.executable, "-c",
         "import pathlib,re;"
         "print('|'.join(sorted(p.name for p in pathlib.Path('.').glob('*.py')"
         " if 'titre_offre' in p.read_text(encoding='utf-8')"
         " and p.name != 'titre_offre.py')))"],
        cwd=racine, capture_output=True, text=True, timeout=60,
    )
    consommateurs = [n for n in sortie.stdout.strip().split("|") if n]
    assert consommateurs == ["worker.py"], (
        f"`titre_offre` est utilisé par {consommateurs}. Le nettoyage doit "
        f"rester au seul point de construction de l'OfferInput."
    )
