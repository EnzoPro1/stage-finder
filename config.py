"""
config.py — Tous les critères de recherche et paramètres modifiables.

C'est LE fichier à éditer pour adapter l'outil à ta recherche :
mots-clés, lieu, requête de référence pour le ranking, modèle d'embedding, etc.
Aucune logique ici, uniquement de la configuration.

Les valeurs portent leur VRAI TYPE, y compris les chemins (`Path`). C'est la
seule façon de ne convertir qu'une fois : une constante exportée en `str`
oblige chaque consommateur à se souvenir de la convertir, et il suffit qu'un
seul oublie pour que la panne n'apparaisse qu'à l'exécution, loin d'ici.
C'est arrivé — `CV_OUT_ROOT` en `str` a produit un `TypeError: unsupported
operand type(s) for /: 'str' and 'str'` dans le worker.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# 1) Requête de référence pour le ranking sémantique
# ---------------------------------------------------------------------------
# C'est le "profil idéal". Chaque offre sera comparée à ce texte par
# similarité cosinus. Plus une offre lui ressemble, plus elle remonte.
# Modifie librement ce paragraphe : c'est lui qui pilote tout le classement.
REQUETE_REFERENCE = (
    "Stage d'ingénieur en intelligence artificielle et en cybersécurité, "
    "idéalement un poste mêlant les deux domaines (IA appliquée à la sécurité, "
    "sécurité des systèmes d'IA, MLOps sécurisé). Forte préférence pour l'IA. "
    "Entreprise du CAC40 de préférence. Localisation Paris. "
    "Stage de 6 mois à partir de janvier 2027."
)

# ---------------------------------------------------------------------------
# 2) Termes de recherche envoyés aux sources
# ---------------------------------------------------------------------------
# Les termes vivent dans `recherche.yaml`, groupés par FAMILLE (ai_engineering,
# ml, mlops, inference, cyber), et chaque source compose sa requête selon ce
# que son moteur sait exprimer (cf. `recherche.py`). L'ancienne liste globale
# `TERMES_RECHERCHE` — cinq phrases conjonctives envoyées telles quelles
# partout — a disparu : « stage intelligence artificielle » exigeait les trois
# mots chez Adzuna et France Travail, et rapportait 0.

# Localisation (filtre "doux" côté API + filtre dur côté pipeline).
LIEU = "Paris"

# Nombre de résultats visés par terme et par source (plafond, best effort).
RESULTATS_PAR_TERME = 30

# ---------------------------------------------------------------------------
# 2 bis) Sources interrogées
# ---------------------------------------------------------------------------
# Le catalogue complet vit dans sources/registry.py ; ici on choisit seulement
# QUI tourne. Commente une ligne pour désactiver une source (par ex. si tu n'as
# pas la clé API correspondante, ou si une source est trop lente).
#
#   adzuna         API, clé requise    — agrégateur généraliste
#   jooble         API, clé requise    — DÉSACTIVÉE, voir ci-dessous
#   france_travail API, clé requise    — gisement public français
#   careerjet      API, sans clé       — méta-moteur (des centaines de sites FR)
#   free_work      API, sans clé       — job board tech/IT français
#   jobspy         scraping, sans clé  — Indeed / LinkedIn (Google Jobs retiré)
#   greenhouse     API publique        — pages carrières (liste dans recherche.yaml)
#   lever          API publique        — pages carrières (liste dans recherche.yaml)
#   ashby          API publique        — pages carrières (liste dans recherche.yaml)
#
# JOOBLE EST RETIRÉE, et son module reste au catalogue (`sources/registry.py`)
# pour qu'on puisse la retester sans la réécrire.
#
# Mesuré le 2026-09-05 : son paramètre `location: "Paris"` est résolu par l'API
# en **Paris, Texas**. 86 offres sur 86 américaines — « Director, Solutions
# Engineering », « CDL A Truck Driver », « Machine Learning Engineer » à
# Powderly, Telephone et Arthur City, Texas. Bilan sur toute l'historique du
# projet (2026-07-14 → 09-01) : 0 offre Jooble dans `stages.db`, 0 dans le
# corpus étiqueté.
#
# Ce qui protégeait le pipeline n'était pas la géographie mais le filtre de
# TITRE : 84 des 86 mouraient sur « pas un stage », et 2 seulement
# atteignaient le filtre de lieu. Le mode `job_etudiant` relâchera ce filtre de
# titre — la source deviendrait alors franchement dangereuse.
#
# L'API répond en outre 403 après quelques dizaines d'appels, ce que
# l'adaptateur convertissait en `[]` muet (corrigé depuis : `observabilite`).
SOURCES_ACTIVES = [
    "adzuna",
    "careerjet",
    "free_work",
    "jobspy",
    # Pages carrières d'entreprises (recherche.yaml, bloc `entreprises`). Une
    # liste vide les laisse tourner à vide : 0 appel, et une ligne MUETTE au
    # bilan qui le rappelle.
    "greenhouse",
    "lever",
    "ashby",
]

# Sources du catalogue VOLONTAIREMENT absentes de `SOURCES_ACTIVES` pour les
# stages, avec la raison. Le bilan de chaque run les imprime en tête : une
# source retirée ne doit pas ressembler à une source oubliée, ni une source
# oubliée à une source retirée. `config_schema` refuse qu'une source figure à
# la fois ici et dans `SOURCES_ACTIVES`.
#
# FRANCE TRAVAIL : 319 offres brutes, 0 stage gardé (contrôle en réel du
# 2026-09-17, requêtes par terme sur les familles de recherche.yaml). Conforme
# au diagnostic du 2026-09-05 (SPEC_sources_muettes.md) : le gisement ne porte
# presque pas de stages — aucune nature de contrat « stage », et les rares
# stages y sont déclarés en CDD. L'adaptateur reste au catalogue : il est
# central pour les jobs étudiants.
SOURCES_ECARTEES_STAGES = {
    "france_travail": (
        "0 stage gardé sur 319 offres (2026-09-17) — le gisement ne publie "
        "presque pas de stages ; conservée pour les jobs étudiants"
    ),
    "jooble": (
        "« Paris » résolu en Paris, Texas : 86 offres américaines sur 86 "
        "(2026-09-05)"
    ),
    # Un SITE de JobSpy, pas une source du catalogue : écrit « jobspy:<site> ».
    "jobspy:google": (
        "0 offre en 36 runs (2026-07-14 → 09-17) ; Google répond 200 avec une "
        "page qui exige JavaScript, sans offre ni curseur — le scraper JobSpy "
        "ne la lit plus, quelle que soit la requête (3 formes essayées)"
    ),
}

# --- Adzuna : construction de la requête ------------------------------------
# Le paramètre `what` d'Adzuna est CONJONCTIF : il exige TOUS les mots.
# Ablation mesurée le 2026-09-05, à Paris :
#
#   what='stage intelligence artificielle' + fenêtre 7 j ....      0
#   idem sans la fenêtre de fraîcheur .......................      4
#   idem sans `where` .......................................     43
#   what='stage' (trop large : tous domaines) ............... 1 276
#
# La requête est donc, PAR FAMILLE :
#
#   title_only  un mot de contrat DANS LE TITRE — ce que `filters.est_un_stage`
#               exige ensuite : la requête et le filtre ne se contredisent pas.
#   what_or     les MOTS des termes de la famille, en OU. Adzuna ne sait pas
#               faire un OU de phrases : « machine learning » devient
#               « machine » OU « learning ».
#
# `title_only` n'accepte qu'un mot : une chaîne par mot de contrat. « stage »
# couvre les annonces françaises, « internship » les annonces anglaises
# publiées à Paris, fréquentes en IA.
ADZUNA_TITRES_EXIGES = ["stage", "internship"]

# Mots retirés du `what_or` : trop génériques pour signifier une famille. Sans
# eux, « ingénieur machine learning » ferait entrer « ingénieur », et toute
# offre d'ingénieur porterait l'étiquette « ml ». « ia » / « ai » y sont aussi :
# « sécurité de l'IA » (famille cyber) étiquetterait sinon cyber toutes les
# offres d'IA, et « ai » attrape « j'ai ».
ADZUNA_MOTS_IGNORES = [
    "de", "du", "des", "la", "le", "les", "par", "of", "the", "and", "et",
    "ia", "ai", "ingénieur", "engineer", "intelligence", "apprentissage",
    "automatique", "traitement", "langage", "data", "science", "recherche",
    "research", "applied", "computer", "plateforme", "platform",
    "infrastructure", "optimisation", "optimization", "model", "sécurité",
    "security",
    # « agents » n'est interrogé qu'en phrase (« agents IA », « AI agents »).
    # Adzuna ne sait pas faire de phrase : une fois « ia » / « ai » retirés, le
    # mot seul attrapait toute description qui parle d'« agents ». Mesuré le
    # 2026-09-17 : 121 -> 112 stages gardés, ai_engineering 61 -> 51 ; les 9
    # offres perdues n'étaient trouvées QUE par « agents » (contrôle de gestion,
    # marketing, juriste, front office…). Une seule citait l'IA dans son titre
    # (« Marketing Digital & Automatisation IA ») : Adzuna ne peut pas interroger
    # « IA », retiré pour les raisons ci-dessus.
    "agents",
]

# Pages Adzuna par chaîne (famille × mot de contrat), RESULTATS_PAR_TERME
# offres par page. 5 familles × 2 mots × 2 pages = 20 appels au pire ; une
# famille étroite s'arrête dès la première page incomplète.
ADZUNA_PAGES = 2

# Careerjet : nombre de pages parcourues PAR FAMILLE (30 offres par page).
# Careerjet comprend OU, guillemets et parenthèses (sondé le 2026-09-17) : une
# requête par famille, « (stage OR internship …) ("machine learning" OR …) ».
CAREERJET_PAGES = 2

# Marché Careerjet interrogé (fr_FR = France, en français).
CAREERJET_LOCALE = "fr_FR"

# Free-Work : nombre de pages de stages parcourues (le site en publie peu).
FREE_WORK_PAGES = 2

# --- France Travail : un appel par terme -------------------------------------
# `motsCles` est CONJONCTIF, virgule comme espace (sondé le 2026-09-17 :
# vendeur 1 750, caissier 218, « vendeur,caissier » 8). Pas de OU possible.
# Hors `SOURCES_ACTIVES` pour les stages (cf. SOURCES_ECARTEES_STAGES).
#
# Nombre d'offres rapatriées par appel (plafond de l'API : 150).
FRANCE_TRAVAIL_RESULTATS = 150

# Décompte d'appels par run, stages :
#
#   avant (5 termes globaux)   adzuna ≤4, france_travail 5, careerjet ≤10,
#                              free_work ≤2, jobspy 15 ............. ≈ 36
#   familles                   adzuna ≤20, careerjet ≤10, free_work ≤2,
#                              jobspy 15 ........................... ≈ 47

# --- JobSpy : une requête par famille, en OU --------------------------------
# Sites interrogés. Chacun a sa ligne dans le bilan (`jobspy:indeed`…).
#
# GOOGLE JOBS EST RETIRÉ (cf. SOURCES_ECARTEES_STAGES). Sondé le 2026-09-17 :
# trois formes de requête — terme + ville, `google_search_term` en langage
# naturel (« … jobs near Paris, France in the last week », la syntaxe que
# JobSpy documente), et sans `google_search_term` — rendent 0 offre, JobSpy
# journalisant « initial cursor not found ». La page brute : statut 200,
# 92 Ko, ni données d'offres ni curseur de pagination, pas de mur de
# consentement ni de page anti-robot, mais une demande de JavaScript. Le
# scraper lit un HTML que Google ne sert plus à un client sans navigateur :
# aucune formulation de requête n'y change rien.
JOBSPY_SITES = ["indeed", "linkedin"]

# Indeed et LinkedIn comprennent OU, guillemets et parenthèses (documenté par
# JobSpy) : 2 sites × 5 familles = 10 appels. Une requête de famille ramène
# davantage qu'une requête de terme, d'où un plafond par SITE.
#
# Contrôle en réel du 2026-09-17 aux plafonds PRÉCÉDENTS (indeed 100,
# linkedin 30) — lignes rendues / gardées par les filtres :
#
#   indeed    ai_engineering 100 / 55   ml 100 / 68   mlops 24 / 5
#             inference 27 / 10         cyber 100 / 35          ~1 s par requête
#   linkedin  30 / 14 à 20 pour chaque famille                  ~35-40 s par requête
#
# Aucun blocage ni statut d'erreur. Les plafonds étaient ATTEINTS (Indeed 3
# familles sur 5, LinkedIn 5 sur 5) : la collecte était tronquée par ce
# réglage, pas par le marché. Relevés à 300 / 60 :
#
#   indeed 300   ~1 s par requête et aucun refus à 100 : marge large.
#   linkedin 60  chaque résultat coûte un appel de plus (description) et
#                LinkedIn bloque vers la 10e page par IP. Si un run enregistre
#                un 429, le bilan le dit en tête et propose d'abaisser ce
#                plafond — sans l'ajuster tout seul.
#
# Contrôle en réel du 2026-09-17 à 300 / 60 — lignes / gardées :
#
#   indeed    ai_engineering 246 / 121  ml 226 / 121  mlops 24 / 5
#             inference 27 / 10         cyber 300 / 72          1 à 4 s par requête
#   linkedin  60 / 31 à 39 pour chaque famille                  ~80 s par requête
#
# Aucun blocage, aucun statut d'erreur, aucun conseil émis. JobSpy complet :
# 455 s, pauses comprises (contre ~240 s à 100 / 30). Plafond encore atteint :
# Indeed sur cyber (300), LinkedIn sur les 5 familles (60).
JOBSPY_RESULTATS_PAR_SITE = {"indeed": 300, "linkedin": 60}

# ---------------------------------------------------------------------------
# 2 ter) Jobs étudiants
# ---------------------------------------------------------------------------
# Origines, rayon et termes vivent dans recherche.yaml (bloc `student_jobs`).
# Ici : quelles sources, et jusqu'où elles paginent.
#
# Run réel du 2026-09-17 (3 origines, 15 km, 16 termes) — 222 s au total :
#
#   source          appels HTTP  brut   gardées  après dédup  sans terme retrouvé
#   france_travail  23 (+1 jeton) 1195   1195     1059          0
#   jobspy:indeed    9            600    578      456           86
#   adzuna           9            352    352      335          270
#   careerjet        8            308    141      118           41
#
# Adzuna est BRUYANT : `what_or` de mots isolés (« partiel », « caisse »,
# « extra »…) remonte 6 135 offres autour de Noisy-le-Grand, dont 270 des 335
# gardées ne contiennent aucun des termes (« Commercial Immobilier »…). Elle
# est aussi TRONQUÉE (150 lues). Careerjet : 167 offres trop vieilles sur 308
# (pas de fenêtre de date côté API), 3 pages lues sur 6 autour de Noisy.
SOURCES_ACTIVES_JOBS = [
    "france_travail",
    "careerjet",
    "jobspy",
]

# Retirées volontairement des jobs étudiants, avec la raison — imprimées en
# tête du bilan, comme pour les stages.
SOURCES_ECARTEES_JOBS = {
    # ADZUNA — gardée pour les stages. Deux formes de requête mesurées en réel
    # le 2026-09-17 : `what_or` des mots des seize termes (81 % des offres
    # gardées sans aucun terme, 6 135 annoncées autour de Noisy-le-Grand), puis
    # une liste fermée de six mots sans ambiguïté (89 %). Cause sondée : Adzuna
    # élargit par synonymie, même en `title_only` (« étudiant » -> « Apprenti
    # élagueur » ; « vendeur » -> 697 « Conseiller commercial »). À 30 km :
    # 11 326 / 3 561 / 18 608 offres annoncées par origine — rien qu'une
    # pagination puisse couvrir. L'adaptateur « jobs étudiants » est retiré ;
    # l'historique git le garde (commits e83e6b1, 40c3be3).
    "adzuna": (
        "élargit les mots-clés par synonymie, même dans le titre (« étudiant » -> "
        "« Apprenti élagueur », « vendeur » -> « Conseiller commercial ») : 81 à "
        "89 % des offres sans aucun terme recherché (2026-09-17)"
    ),
    "free_work": (
        "job board tech/IT : ni recherche par lieu, ni mot-clé pris en compte "
        "côté serveur — rien à y chercher en jobs étudiants locaux"
    ),
    "jooble": (
        "« Paris » résolu en Paris, Texas (2026-09-05) ; non réévaluée sur des "
        "communes de Seine-et-Marne"
    ),
    "jobspy:linkedin": (
        "gisement de postes qualifiés, pas de jobs étudiants locaux ; ~80 s par "
        "requête et risque de blocage — non interrogé ici"
    ),
    "jobspy:google": (
        "Google répond 200 avec une page qui exige JavaScript, sans offre ni "
        "curseur (2026-09-17)"
    ),
}

# Base SÉPARÉE de stages.db, même schéma. `stages.db` est lue par le tableau de
# bord marché, la génération de CV, `etiqueter.py` (qui présente ses offres à
# l'étiquetage à l'aveugle) et `evaluer_ranking.py` — tous écrits pour des
# STAGES. Y verser des jobs étudiants les ferait entrer dans le corpus
# d'évaluation du ranking. Le rapport unifié lit les deux bases.
CHEMIN_BASE_JOBS = Path("jobs_etudiants.db")

# Fraîcheur des jobs étudiants, en jours — SÉPARÉE de JOURS_FRAICHEUR (stages).
# Une annonce de job étudiant reste ouverte plus longtemps : au run du
# 2026-09-17 à 7 jours, Careerjet perdait 279 offres sur 420 pour ancienneté.
# Poussée côté API là où elle existe (France Travail `publieeDepuis`, Indeed
# `hours_old`), appliquée par le filtre partout. Le rapport affiche l'âge de
# chaque offre pour trier ou ignorer les plus anciennes.
JOURS_FRAICHEUR_JOBS = 21

# Mots de contrat qui, dans le TITRE, marquent une alternance. Les jobs
# étudiants ne l'EXCLUENT pas : l'offre reste, avec un drapeau « alternance ».
MOTS_CLES_ALTERNANCE = [
    "alternance", "alternant", "alternante", "apprentissage", "apprenti",
    "apprentie", "professionnalisation",
]

# France Travail : pages par requête (FRANCE_TRAVAIL_RESULTATS offres chacune).
# Au-delà, un conseil « tronquée » est imprimé en tête du bilan. À 30 km
# (2026-09-17), plus gros total annoncé : 693 (temps partiel sans mot-clé,
# ESIEE) ; par terme, 504 (« vendeur », ESIEE). 5 pages = 750.
FRANCE_TRAVAIL_PAGES_JOBS = 5

# Careerjet : une requête OU par origine. Pas de rayon côté API : pages
# annoncées à 50 par page (2026-09-17) — Meaux 3, Évry 2, ESIEE 6.
CAREERJET_PAGES_JOBS = 6
CAREERJET_TAILLE_PAGE_JOBS = 50

# JobSpy : sites interrogés pour les jobs étudiants, autour de chaque origine.
JOBSPY_SITES_JOBS = ["indeed"]
# Plafond de résultats par requête, par site. Indeed à 30 km (19 miles),
# requête OU des seize termes, results_wanted=1000 (2026-09-17) : 998 / 726 /
# 995 lignes, 6 à 10 s par origine — le plafond des stages (300) tronquait.
JOBSPY_RESULTATS_PAR_SITE_JOBS = {"indeed": 1000}

# --- Trajets (trajets.py) ----------------------------------------------------
# OpenRouteService, clé ORS_API_KEY dans le .env. Seuil et facteurs : bloc
# `student_jobs.trajet` de recherche.yaml.
#
# Quota vérifié le 2026-09-17 : 3 500 routes par requête matrix (refus 400
# code 6004 au-delà), 50 requêtes par jour (`X-Ratelimit-Limit: 50`, reset
# 24 h après le premier appel ; une requête refusée compte).
ORS_ROUTES_MAX_PAR_APPEL = 3500
# Plafond d'appels par run : un run ne doit pas pouvoir épuiser la journée.
ORS_APPELS_MAX_PAR_RUN = 10
# Cache (origine, commune) -> durée et distance calculées. Artefact de run.
CHEMIN_CACHE_TRAJETS = Path("trajets.json")

# ---------------------------------------------------------------------------
# 2 quater) Métiers suivis par le tableau de bord marché (market.py)
# ---------------------------------------------------------------------------
# Intitulés en TEXTE LIBRE, convertis en codes métier ROME par l'API ROMEO de
# France Travail (sources/romeo.py). Mesurer un marché par code ROME plutôt que
# par mot-clé change tout : « stage cybersécurité » en plein texte remonte 0
# offre sur 31 jours en IdF, le ROME correspondant (M1856) en remonte 9.
#
# Écris-les comme un intitulé de poste, pas comme une requête de recherche.
METIERS_SUIVIS = [
    "ingénieur en intelligence artificielle",
    "ingénieur cybersécurité",
    "data scientist",
]

# Nombre de codes ROME retenus par intitulé (ROMEO en propose plusieurs, par
# score décroissant). 1 = le métier le plus probable seulement ; 2 élargit un
# peu sans trop diluer.
ROME_PAR_METIER = 2

# Codes ROME de repli, utilisés si ROMEO est indisponible. Ils viennent d'un
# appel réel à ROMEO sur les intitulés ci-dessus : le tableau de bord reste
# donc juste, même hors ligne.
ROME_REPLI = {
    "ingénieur en intelligence artificielle": [
        {"code": "M1889", "libelle": "Ingénieur en Intelligence Artificielle (IA)"},
    ],
    "ingénieur cybersécurité": [
        {"code": "M1882", "libelle": "Architecte sécurité informatique"},
        {"code": "M1856", "libelle": "Expert en cybersécurité"},
    ],
    "data scientist": [
        {"code": "M1405", "libelle": "Data scientist"},
    ],
}

# ---------------------------------------------------------------------------
# 3) Filtres durs (volontairement permissifs — le ranking fait le tri fin)
# ---------------------------------------------------------------------------
# Une offre doit contenir au moins un de ces mots DANS LE TITRE pour être
# considérée comme un stage. On cible le titre (et plus la description) car
# c'est le seul signal fiable du type de poste : un CDI senior peut mentionner
# le mot « stage » dans son texte sans être un stage. Recherche par mot entier.
MOTS_CLES_STAGE = ["stage", "stagiaire", "internship", "intern", "trainee"]

# Mots qui, présents DANS LE TITRE, disqualifient l'offre : on ne veut ni
# alternance/apprentissage, ni postes séniors/expérimentés (tu cherches
# uniquement un stage). Recherche par mot entier également.
MOTS_CLES_EXCLUS = [
    "alternance", "alternant", "apprentissage", "apprenti",
    "professionnalisation", "senior", "confirmé", "confirme",
]

# Une offre doit être localisée dans l'un de ces secteurs (sur le champ "location").
# Large volontairement : Paris + Île-de-France + codes départements franciliens.
LIEUX_ACCEPTES = [
    "paris", "île-de-france", "ile-de-france", "idf",
    "75", "77", "78", "91", "92", "93", "94", "95",
    "boulogne", "nanterre", "saint-denis", "montreuil", "courbevoie",
    "issy-les-moulineaux", "la défense", "la defense",
]

# Pays/villes qui, cités DANS LE TITRE, trahissent un poste à l'étranger même
# quand le champ « lieu » dit « Paris » (cas courant des cabinets de placement :
# « Stage Data Scientist - IT (H/F) - Canada », publié depuis Paris). Recherche
# par mot entier ; liste volontairement courte pour éviter les faux positifs
# (on n'y met pas « suisse » seul, qui apparaît dans « Banque Suisse » à Paris).
MARQUEURS_ETRANGERS_TITRE = [
    "canada", "québec", "quebec", "montréal", "montreal",
    "united states", "usa", "new york", "san francisco",
    "maroc", "morocco", "casablanca", "tunisie", "tunisia", "sénégal", "senegal",
    "londres", "london", "dubai", "dubaï", "singapore", "singapour",
    "allemagne", "germany", "berlin", "espagne", "spain", "madrid", "barcelone",
    "belgique", "bruxelles", "luxembourg", "genève", "geneve", "lausanne",
]

# Marqueurs qui trahissent un lieu HORS France (ex. « Paris, TX » = Paris, Texas).
# Une offre dont la localisation contient l'un d'eux est rejetée, même si elle
# contient aussi « paris ».
MARQUEURS_ETRANGERS = [
    ", tx", ", ca ", ", ca,", ", ny", ", il ", ", oh", ", on ",  # états US / Canada
    "texas", "canada", "united states", "usa", "ontario",
    "belgi", "luxembourg", "suisse", "switzerland", "maroc", "morocco",
    "tunisie", "tunisia", "deutschland", "germany", "españa", "spain",
]

# ---------------------------------------------------------------------------
# 4) Ranking sémantique
# ---------------------------------------------------------------------------
# Modèle multilingue OBLIGATOIRE : les offres sont en français.
# Tourne 100 % en local, aucune donnée ne sort de la machine.
#
# Alternatives à benchmarker (voir benchmark.py) :
#   - "paraphrase-multilingual-MiniLM-L12-v2" : léger, rapide, fenêtre ~128 tok.
#   - "intfloat/multilingual-e5-large"        : plus lourd, meilleur retrieval.
#   - "BAAI/bge-m3"                           : contexte 8k, multilingue solide.
# Source de vérité au moment du test : leaderboard MTEB (onglets français /
# retrieval), plutôt qu'un « gagnant » figé.
#
# Préfixe « ollama: » = modèle servi par Ollama (GPU) au lieu de
# sentence-transformers (CPU), avec cache disque des vecteurs
# (cache_embeddings.py) : « ollama:bge-m3 ». À ne passer en défaut qu'après
# mesure sur l'instantané (benchmark.py) : au moins aussi bon en P@10 ET nDCG@10.
MODELE_EMBEDDING = "paraphrase-multilingual-MiniLM-L12-v2"

# Modèle de la DÉDUPLICATION FLOUE, distinct de celui du classement. Le seuil
# SEUIL_DEDUP_FLOU (0,90) a été calibré sur les cosinus de MiniLM : un autre
# modèle distribue ses similarités autrement, et le même seuil y fusionnerait
# (ou raterait) d'autres paires. Changer MODELE_EMBEDDING ne doit donc PAS
# changer la dédup. Quand les deux sont égaux, les vecteurs sont calculés une
# seule fois et partagés.
MODELE_EMBEDDING_DEDUP = "paraphrase-multilingual-MiniLM-L12-v2"

# Cache disque des embeddings servis par Ollama (voir cache_embeddings.py).
CHEMIN_CACHE_EMBEDDINGS = Path("embeddings_cache.db")

# Longueur maximale de séquence (en tokens) imposée au modèle. Les descriptions
# longues sont TRONQUÉES au-delà : sans borne explicite, la troncature se fait en
# silence sur la valeur par défaut du modèle (souvent 128). On la fixe pour
# rendre le comportement visible et reproductible. None = valeur du modèle.
MAX_SEQ_LENGTH: int | None = 256

# Certains modèles (famille E5, BGE) attendent un préfixe d'instruction.
# Renseigner ici (ex. "query: " pour la requête, "passage: " pour les offres)
# selon le modèle choisi ; laisser "" pour MiniLM.
PREFIXE_REQUETE = ""
PREFIXE_DOCUMENT = ""

# --- Composition du score ---------------------------------------------------
# Comment combiner la similarité cosinus et le boost mots-clés.
#   "additif"            : cosinus + boost (historique, plafonné à 1.0).
#   "additif_normalise"  : on NORMALISE d'abord le cosinus sur le run (min-max ou
#                          z-score) puis on ajoute le boost — le boost ajuste au
#                          lieu d'écraser le signal sémantique.
#   "multiplicatif"      : cosinus × (1 + boost) — module sans jamais dominer.
#   "lexicographique"    : trie d'abord par présence de combo/boost, puis par
#                          cosinus décroissant — garantit les combos en tête.
MODE_COMPOSITION_SCORE = "additif_normalise"

# Méthode de normalisation du cosinus (mode "additif_normalise") : "minmax" ou
# "zscore". min-max ramène le run dans [0, 1] ; z-score centre-réduit.
METHODE_NORMALISATION = "minmax"

# --- Profils de référence multiples -----------------------------------------
# Tout le classement pivote historiquement sur un SEUL vecteur cible, ce qui est
# fragile. On peut ancrer sur plusieurs profils et agréger leurs similarités.
# Chaque profil : {"texte": ..., "poids": ...}. Laisser la liste vide pour
# retomber sur REQUETE_REFERENCE seule.
PROFILS_REFERENCE = [
    {
        "texte": (
            "Stage d'ingénieur en intelligence artificielle : machine learning, "
            "deep learning, LLM, RAG, NLP, MLOps, data science. Paris."
        ),
        "poids": 1.0,
    },
    {
        "texte": (
            "Stage d'ingénieur en cybersécurité : test d'intrusion, SOC, SIEM, "
            "sécurité des systèmes d'information, red team, cryptographie. Paris."
        ),
        "poids": 0.7,
    },
    {
        "texte": (
            "Stage à l'intersection IA et cybersécurité : IA appliquée à la "
            "détection d'intrusion, sécurité des systèmes d'IA, MLSecOps. Paris."
        ),
        "poids": 1.2,
    },
]

# Agrégation des similarités des profils : "max" (le meilleur profil décide) ou
# "moyenne" (moyenne pondérée par les poids).
AGGREGATION_PROFILS = "max"

# --- Boost par mots-clés (re-ranking par-dessus la similarité) --------------
# Une offre qui mentionne ces termes (titre + description) reçoit un bonus de
# score. Les acronymes sont cherchés en MOT ENTIER (\b) pour éviter les faux
# positifs (ex. on n'inclut pas « ai » qui matcherait « j'ai »).
MOTS_CLES_IA = [
    "ia", "intelligence artificielle", "machine learning", "apprentissage automatique",
    "deep learning", "llm", "large language model", "rag", "retrieval augmented",
    "nlp", "traitement du langage", "natural language", "genai", "generative",
    "générative", "mlops", "computer vision", "vision par ordinateur",
    "transformer", "transformers", "pytorch", "tensorflow", "hugging face",
    "réseaux de neurones", "neural network", "fine-tuning", "embeddings",
    "data science", "agentic", "agentique", "diffusion", "prompt engineering",
    # Intitulés de poste : « data science » ne matche PAS « data scientist »
    # (frontière de mot), or c'est l'intitulé le plus courant. Sans eux, un
    # titre « Data Scientist » ne recevait aucun boost IA de titre, et le
    # garde-fou « métier support » de verifier.py le classait à tort.
    "data scientist", "machine learning engineer", "ml engineer",
    # Apprentissage par renforcement : absent de la liste, un stage de recherche
    # « Semantic Communication MARL » ne portait AUCUN signal IA de titre.
    "reinforcement learning", "apprentissage par renforcement", "marl",
]

MOTS_CLES_CYBER = [
    "cyber", "cybersécurité", "cybersecurity", "sécurité informatique",
    "sécurité des systèmes", "test d'intrusion", "tests d'intrusion",
    "pentest", "penetration testing", "intrusion", "vulnérabilité",
    "vulnerability", "soc", "siem", "malware", "forensic", "red team",
    "blue team", "owasp", "cryptographie", "cryptography", "threat",
    "ransomware", "edr", "menace", "sécurité offensive", "sécurité défensive",
]

# Poids des boosts (ajoutés à la similarité cosinus, score final plafonné à 1.0).
BOOST_IA = 0.05        # mot-clé IA présent DANS LE TITRE
BOOST_CYBER = 0.05     # mot-clé cyber présent DANS LE TITRE
BOOST_COMBO = 0.15     # bonus SUPPLÉMENTAIRE si IA ET cyber (le stage rêvé)

# Un mot-clé présent SEULEMENT dans la description (pas le titre) compte moins :
# on évite ainsi de sur-noter le blabla corporate des grands groupes (ex. Thales
# qui mentionne « Cyber » et « IA » dans sa présentation quel que soit le poste).
BOOST_FACTEUR_DESCRIPTION = 0.4

# Le bonus combo exige qu'au moins un signal (IA ou cyber) soit dans le TITRE :
# un vrai stage à l'intersection l'annonce dans son intitulé.
COMBO_EXIGE_SIGNAL_TITRE = True

# Une offre "combo" (IA + cyber) est-elle toujours affichée, même sous le
# seuil --min-score ? True = oui, elle ne peut jamais être masquée.
COMBO_TOUJOURS_AFFICHE = True

# ---------------------------------------------------------------------------
# 5) Fraîcheur des offres
# ---------------------------------------------------------------------------
# On ne garde que les offres publiées dans les N derniers jours.
JOURS_FRAICHEUR = 7

# Sources EXEMPTÉES du filtre de fraîcheur des stages (date absente comprise).
# Les pages carrières ne listent que les postes OUVERTS : une date ancienne dit
# que le poste est affiché depuis longtemps, pas qu'il est pourvu. Le 2026-09-17,
# la fenêtre de 7 j en rejetait 11 sur 36 (dont « Applied Science Intern »
# chez Datadog, 9 j). L'âge reste affiché dans le rapport pour juger soi-même.
SOURCES_SANS_FRAICHEUR = ["greenhouse", "lever", "ashby"]

# Que faire d'une offre dont la date de publication est absente ou illisible ?
#   False = on l'exclut (strict : "vraiment que du récent").
#   True  = on la garde (permissif).
GARDER_SI_DATE_INCONNUE = False

# ---------------------------------------------------------------------------
# 6) Déduplication floue par embeddings
# ---------------------------------------------------------------------------
# Après la dédup exacte (hash), on rapproche les quasi-doublons par similarité
# cosinus des embeddings (déjà calculés pour le ranking), sans lib de fuzzy
# string matching.
#
# DEUX CONDITIONS, pas une. Le cosinus juge le TEXTE ; il est assorti d'une
# condition d'identité (`dedup._identite_fusionnable`) : même entreprise, même
# commune, mêmes horaires annoncés dans le titre. La raison est que
# `ranker._texte_a_encoder` encode titre + description — ni l'entreprise ni le
# lieu n'entrent dans le vecteur, donc deux annonces au même gabarit sont à
# cosinus ~1 chez deux employeurs différents.
#
# Mesuré le 2026-09-05 sur 447 offres de type job étudiant : le cosinus seul
# fusionnait 79 offres, dont 37 entre communes différentes et 22 entre
# employeurs différents. Avec le garde-fou : 21 fusions, aucune des deux.
#
# CE QUI A ÉTÉ PERDU AU PASSAGE, et c'est assumé : « Sanofi » et « Sanofi
# Group » ne se rapprochent plus (c'était l'exemple d'origine de ce réglage).
# Accepter qu'un nom en contienne un autre ferait fusionner « Tripletta Pizza -
# Latin » et « Tripletta Pizza - Guy Môquet », deux restaurants distincts que
# la commune ne sépare pas — tous les arrondissements parisiens partagent le
# code INSEE 75056. Une fusion manquée coûte une ligne en double ; une fusion
# abusive supprime une offre réelle sans laisser de trace.
DEDUP_FLOUE_ACTIVE = True

# Seuil de cosinus au-delà duquel deux offres sont jugées quasi-identiques.
# Volontairement haut (~0.9) pour ne fusionner que de vrais doublons.
#
# NE PAS le monter en espérant corriger les fusions abusives : mesuré, à 0.99
# il restait 17 fusions entre employeurs distincts et 19 entre communes
# distinctes. Un seuil ne sépare pas ce que le vecteur ne contient pas — c'est
# le garde-fou d'identité qui s'en charge.
SEUIL_DEDUP_FLOU = 0.90

# ---------------------------------------------------------------------------
# 7) Extraction durée / date de début (signaux souples, pas un filtre dur)
# ---------------------------------------------------------------------------
# On extrait par regex la durée du stage et la date de début quand elles sont
# lisibles. Ce ne sont PAS des filtres durs (on jetterait de vraies offres) :
# on en fait des colonnes filtrables + un petit boost/malus.
DUREE_CIBLE_MOIS = 6          # durée idéale recherchée
DUREE_MIN_ACCEPTABLE = 4      # en-dessous : léger malus (trop court)
BONUS_DUREE_CIBLE = 0.03      # boost si la durée est proche de la cible
MALUS_DUREE_COURTE = 0.03     # malus si la durée est nettement trop courte

# Mois/année de début visés (pour un petit boost si l'offre les mentionne).
DATE_DEBUT_CIBLE_ANNEE = 2027
DATE_DEBUT_CIBLE_MOIS = 1     # janvier
BONUS_DATE_DEBUT = 0.02

# ---------------------------------------------------------------------------
# 8) Persistance SQLite
# ---------------------------------------------------------------------------
# Une petite base locale débloque : cache des offres vues (n'afficher que les
# nouveautés), dédup dans le temps (offre republiée chaque lundi), historique.
CHEMIN_BASE = "stages.db"

# Rapport HTML UNIFIÉ (rapport.py) : stages et jobs étudiants, lus dans leurs
# deux bases, au dernier run de chacune. Réécrit à la fin de chaque run.
CHEMIN_RAPPORT = Path("flux.html")

# ---------------------------------------------------------------------------
# 8 bis) Génération de CV (cv_forge)
# ---------------------------------------------------------------------------
# stage_finder ne connaît de cv_forge qu'UNE fonction : `generate_cv`. Aucun
# autre symbole du paquet n'est importé, et le sens de la dépendance est
# strictement à sens unique — cv_forge n'importe rien d'ici.
#
# Le master est le RÉSERVOIR de CV : il est lu, jamais écrit, ni par
# stage_finder ni par le LLM. Chemin ABSOLU et propre à ce poste : cv_forge
# est un dépôt voisin, pas un sous-dossier.
CV_MASTER_PATH = Path(r"C:\Users\toi\cv_forge\data\master.yaml")

# Racine des CV produits : un sous-dossier par offre, nommé d'après sa clé.
CV_OUT_ROOT = Path(r"C:\Users\toi\cv_forge\output\stages")

# ---------------------------------------------------------------------------
# 9) Vérification par LLM local (Ollama) — couche « retrieve-then-verify »
# ---------------------------------------------------------------------------
# Le ranking cosinus est un tri grossier bon marché : il rapproche des textes
# mais ne RAISONNE pas. On ajoute une étape qui fait relire la shortlist top-N
# par un LLM 100 % local (Ollama), qui juge chaque offre et rend un verdict
# explicable (score, alternance ?, niveau, drapeaux rouges, justification).
#
# Principe non négociable : on ne passe JAMAIS le LLM sur toutes les offres, on
# vérifie seulement les VERIFY_TOP_N premières. Le LLM enrichit, il ne supprime
# jamais une offre en dur (risque d'hallucination) : son score est simplement
# combiné au cosinus. Si Ollama est injoignable, on retombe sur le cosinus.
#
# Le profil recherché réutilise REQUETE_REFERENCE ci-dessus (pas de duplication).
#
# Prérequis :  ollama pull qwen3:4b   puis   ollama list
#
# Ces valeurs sont les DÉFAUTS. Les réglages effectivement utilisés sont ceux
# de `llm.charger_reglages()`, qui les surcharge par variables d'environnement
# (SF_LLM_MODEL, SF_LLM_TOP_N… — liste complète dans .env.example).
VERIFY_ENABLED = True                          # couche active ? (--no-verify court-circuite)
VERIFY_MODEL = "qwen3:4b"                       # tag Ollama du modèle de vérification (tient en VRAM 6 Go)
VERIFY_OLLAMA_URL = "http://localhost:11434"    # serveur Ollama local
VERIFY_TOP_N = 30                               # taille de la shortlist vérifiée
VERIFY_TIMEOUT_S = 180                          # timeout (s) par appel LLM (large : 8B sur CPU ≈ 6 tok/s)
VERIFY_SCORE_WEIGHT = 0.5                       # poids du score LLM dans le score final (0-1)
VERIFY_MIN_SCORE = 0.0                          # seuil d'affichage du score LLM (0 = tout afficher)

# Budget de génération par verdict, en tokens. La justification est le seul champ
# à longueur libre : c'est lui qui consomme ce budget. TROP BAS = le JSON est
# coupé avant la fin et le verdict entier est perdu (parsing impossible) ;
# trop haut = latence inutile sur CPU. ~500 laisse la place à 3 phrases.
VERIFY_MAX_TOKENS = 500

# Nombre de phrases demandées au modèle pour la justification. 1 phrase tient
# sur une ligne mais n'explique rien ; 3 donnent un vrai avis lisible (domaine
# du poste, adéquation durée/date, réserves) sans faire exploser la latence.
VERIFY_JUSTIF_PHRASES = 3

# Options d'inférence passées à Ollama à chaque appel (llm.py).
#
# LLM_NUM_CTX : fenêtre de contexte. Elle dimensionne le cache KV, alloué en
# VRAM À LA CHARGE du modèle : qwen3:4b à 8192 tokens ≈ 2,5 Go de poids +
# 1,2 Go de cache, ce qui tient sur une carte de 6 Go. La monter fait déborder
# des couches sur le CPU, et la latence s'effondre sans aucun message d'erreur.
#
# LLM_KEEP_ALIVE : durée de résidence du modèle après un appel (syntaxe
# Ollama : "10m", "30s", un nombre de secondes, -1 = indéfini). Elle garde le
# modèle chaud d'une offre à la suivante ; le déchargement explicite en fin de
# lot reste assuré par `ollama_pool.decharger`.
#
# LLM_NOUVELLES_TENTATIVES : nombre de NOUVELLES tentatives quand la sortie
# structurée est illisible ou non conforme au schéma (0 = un seul essai).
LLM_THINK = False
LLM_TEMPERATURE = 0.0
LLM_NUM_CTX = 8192
LLM_KEEP_ALIVE = "10m"
LLM_NOUVELLES_TENTATIVES = 2

# ---------------------------------------------------------------------------
# 10) Divers
# ---------------------------------------------------------------------------
# Timeout (secondes) pour tous les appels réseau.
TIMEOUT_HTTP = 20

# Délai (secondes) entre deux requêtes JobSpy pour rester poli / éviter le blocage.
DELAI_ENTRE_REQUETES = 4

# Nombre de threads pour la collecte parallèle des sources API (I/O-bound).
# À garder >= au nombre de sources API de SOURCES_ACTIVES (6 aujourd'hui), sinon
# les dernières attendent qu'un thread se libère alors qu'elles ne font
# qu'attendre le réseau. JobSpy reste séquentiel par politesse.
MAX_THREADS_COLLECTE = 6
