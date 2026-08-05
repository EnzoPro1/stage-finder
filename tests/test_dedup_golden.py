"""Golden de `dedup._normaliser_titre` sur un corpus d'annonces RÉELLES.

# Pourquoi ce fichier existe

`_normaliser_titre` alimente `dedup._cle`, qui est la CLÉ PRIMAIRE de la
table `offres` — et, depuis le CP2, le `offer_id` de `generation_jobs`.

Une édition de cette fonction ne casse aucun test « logique » : les 33
tests de `test_dedup.py` vérifient que les variantes connues d'une mention
de genre se replient bien sur le titre nu, ce qui reste vrai après presque
n'importe quelle réécriture. Aucun d'eux ne détecte qu'une édition déplace
les clés d'annonces DÉJÀ EN BASE.

C'est pourtant exactement ce qui est arrivé : le correctif des mentions
M/F a déplacé 65 des 295 clés, soit 22 % de la table. À l'époque il n'y
avait aucun job. Maintenant qu'il y en a, le même déplacement produirait
des jobs et des PDF rattachés à des `offer_id` ne désignant plus aucune
ligne — orphelins, silencieux, et impossibles à rattacher après coup.

Ce golden est donc un DÉTECTEUR DE MIGRATION, pas un test de correction.
Il fige la sortie de la fonction sur un corpus d'annonces réellement
présentes dans stages.db, y compris les cas qui avaient révélé les défauts
de l'ancien motif (Freshfields, « Data RH F/H », « (x/f/m) », M&A, R&D).

# Que faire quand il tombe

Il ne dit PAS que la modification est fausse. Il dit qu'elle change des
clés persistées, et donc qu'elle a un coût que le test ne peut pas payer
à votre place :

  1. mesurer l'impact réel :  python migration_recalc_cles.py
  2. si le volume est acceptable, appliquer :  --apply
  3. seulement ensuite, mettre ce corpus à jour.

Corpus extrait de stages.db le 2026-08-05.
"""

from __future__ import annotations

import pytest

from dedup import _normaliser_titre

# (titre réel, sortie normalisée figée)
CORPUS: tuple[tuple[str, str], ...] = (
    ('AI & Automation Intern',
     'ai automation intern'),
    ('Air Liquide - Stage - HR Controller  (H/F)',
     'air liquide stage hr controller'),
    ('Analyste M&A - F/H - Stage',
     'analyste m a stage'),
    ("Chargé (e) d'études Innovation Logistique en stage F/H",
     'charge e d etudes innovation logistique en stage'),
    ('Consultant stagiaire IT M&A (H/F)',
     'consultant stagiaire it m a'),
    ('Data Analyst Intern',
     'data analyst intern'),
    ('Final Year Internship - Insurance',
     'final year internship insurance'),
    ('Ingénieur Qualité Produit & Maintenance N3 H/F - Stage',
     'ingenieur qualite produit maintenance n3 stage'),
    ('Internship - Développement de Carrières & Projets Alumni (Septembre 2026)',
     'internship developpement de carrieres projets alumni septembre 2026'),
    ('Product Data Scientist Intern',
     'product data scientist intern'),
    ('STAGE - ANALYSTE REPORTING SUSTAINABILITY - CORPORATE (H/F/X)',
     'stage analyste reporting sustainability corporate'),
    ('STAGE - Consultant communications/Consultante communications',
     'stage consultant communications consultante communications'),
    ('STAGE - Valorisation des produits Data RH F/H',
     'stage valorisation des produits data rh'),
    ("STAGE-Analyser l'impact d'une intégration d'éléments méthodologiques issus du programme confiance.ia F/H",
     'stage analyser l impact d une integration d elements methodologiques issus du programme confiance ia'),
    ('Stage - 6 mois - Data Scientist Generative AI F/H',
     'stage 6 mois data scientist generative ai'),
    ('Stage - Analyste Excellence Opérationnelle (F/M/X)',
     'stage analyste excellence operationnelle'),
    ('Stage - Analyste Recherche ESG - 6 mois - Paris (f/m/d)',
     'stage analyste recherche esg 6 mois paris'),
    ('Stage - Assistant Innovation Digitale',
     'stage assistant innovation digitale'),
    ('Stage - Comment sécuriser nos moyens de transports et garantir le maintien de leur conformité tout au long de leur cycle de vie ?',
     'stage comment securiser nos moyens de transports et garantir le maintien de leur conformite tout au long de leur cycle de vie'),
    ('Stage - DevSecOps et agilité',
     'stage devsecops et agilite'),
    ('Stage - Intelligence artificielle & Cybersécurité : Comment s’adapter pour garantir la sécurité de ces nouveaux systèmes ?',
     'stage intelligence artificielle cybersecurite comment s adapter pour garantir la securite de ces nouveaux systemes'),
    ('Stage - Juriste Data Privacy (x/f/m) - Janvier 2027',
     'stage juriste data privacy janvier 2027'),
    ('Stage 6 mois - Septembre 2026 – Global Advocacy & Influence Tech & Analytics (Bac+3)',
     'stage 6 mois septembre 2026 global advocacy influence tech analytics bac 3'),
    ('Stage Big Data, data visualisation, développement de tableaux de bords sous Power Bi.',
     'stage big data data visualisation developpement de tableaux de bords sous power bi'),
    ('Stage Data/Cyber – Second semestre 2026 (septembre-décembre) - Freshfields LLP',
     'stage data cyber second semestre 2026 septembre decembre freshfields llp'),
    ('Stage Django / React (6 mois - démarrage fin 2026)',
     'stage django react 6 mois demarrage fin 2026'),
    ('Stage Ingénieur Data IA H/F',
     'stage ingenieur data ia'),
    ('Stage Software Engineer - IT (H/F) - Canada',
     'stage software engineer it canada'),
    ('Stage en Droit des sociétés/financier/boursier/gouvernance H/F',
     'stage en droit des societes financier boursier gouvernance'),
    ('Stage – Data Analyst',
     'stage data analyst'),
    ('Stagiaire Analyste/Consultant en Cybersécurité',
     'stagiaire analyste consultant en cybersecurite'),
    ('Stagiaire Mécénat & Fundraising',
     'stagiaire mecenat fundraising'),
)


MESSAGE = """
    La normalisation des titres a changé pour une annonce réelle.

        titre    : {titre!r}
        figé     : {attendu!r}
        obtenu   : {obtenu!r}

    `_normaliser_titre` alimente `dedup._cle`, clé primaire de `offres` et
    `offer_id` de `generation_jobs`. Ce changement DÉPLACE des clés déjà en
    base : les jobs et les PDF qui les référencent deviendront orphelins,
    en silence.

    Ce n'est pas forcément une erreur — le correctif des mentions M/F a
    légitimement déplacé 22 % des clés. Mais il exige une MIGRATION :

        python migration_recalc_cles.py            # mesure l'impact
        python migration_recalc_cles.py --apply    # applique, avec sauvegarde

    Mettez ce corpus à jour SEULEMENT après, jamais avant.
"""


@pytest.mark.parametrize("titre, attendu", CORPUS,
                         ids=[t[:40] for t, _ in CORPUS])
def test_la_normalisation_d_un_titre_reel_ne_bouge_pas(titre, attendu):
    obtenu = _normaliser_titre(titre)
    assert obtenu == attendu, MESSAGE.format(
        titre=titre, attendu=attendu, obtenu=obtenu)


def test_le_corpus_couvre_les_cas_qui_avaient_revele_les_defauts():
    """Garde-fou du garde-fou : un corpus régénéré sans ces cas
    laisserait repasser exactement les bugs déjà corrigés.

    « R&D » n'y figure pas, et c'est volontaire : aucune annonce de
    stages.db n'en contient. Il est couvert par le test synthétique
    `test_ce_qui_ressemble_a_une_mention_sans_en_etre_une_est_intact`
    de `test_dedup.py`. Ce fichier-ci ne fige QUE du réel — y glisser un
    titre inventé lui ferait perdre ce qui fait sa valeur : chaque ligne
    correspond à une clé réellement présente en base.
    """
    titres = " | ".join(t for t, _ in CORPUS).lower()
    for motif in ("freshfields", "rh f/h", "x/f/m", "m&a"):
        assert motif in titres, f"cas de régression « {motif} » absent du corpus"


def test_le_corpus_est_de_taille_utile():
    assert len(CORPUS) >= 30
