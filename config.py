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
# Requêtes larges : on préfère ratisser large et laisser le ranking trier.
# Chaque terme est interrogé séparément puis les résultats sont fusionnés.
TERMES_RECHERCHE = [
    "stage intelligence artificielle",
    "stage machine learning",
    "stage cybersécurité",
    "stage data science",
    "internship AI security",
]

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
#   jobspy         scraping, sans clé  — Indeed / LinkedIn / Google Jobs
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
    "france_travail",
    "careerjet",
    "free_work",
    "jobspy",
]

# Careerjet : nombre de pages parcourues PAR TERME de recherche (30 offres par
# page). 2 pages x 5 termes = jusqu'à 300 offres brutes, largement de quoi
# alimenter les filtres sans faire traîner la collecte.
CAREERJET_PAGES = 2

# Marché Careerjet interrogé (fr_FR = France, en français).
CAREERJET_LOCALE = "fr_FR"

# Free-Work : nombre de pages de stages parcourues (le site en publie peu).
FREE_WORK_PAGES = 2

# ---------------------------------------------------------------------------
# 2 ter) Métiers suivis par le tableau de bord marché (market.py)
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
MODELE_EMBEDDING = "paraphrase-multilingual-MiniLM-L12-v2"

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

# Que faire d'une offre dont la date de publication est absente ou illisible ?
#   False = on l'exclut (strict : "vraiment que du récent").
#   True  = on la garde (permissif).
GARDER_SI_DATE_INCONNUE = False

# ---------------------------------------------------------------------------
# 6) Déduplication floue par embeddings
# ---------------------------------------------------------------------------
# Après la dédup exacte (hash), on rapproche les quasi-doublons par similarité
# cosinus des embeddings (déjà calculés pour le ranking) : deux offres dont les
# vecteurs sont très proches sont considérées comme une seule (ex. « Sanofi » vs
# « Sanofi Group »), sans lib de fuzzy string matching.
DEDUP_FLOUE_ACTIVE = True

# Seuil de cosinus au-delà duquel deux offres sont jugées quasi-identiques.
# Volontairement haut (~0.9) pour ne fusionner que de vrais doublons.
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
# Prérequis :  ollama pull qwen3:8b   puis   ollama list
VERIFY_ENABLED = True                          # couche active ? (--no-verify court-circuite)
VERIFY_MODEL = "qwen3:1.7b"                     # tag Ollama du modèle de vérification (léger = rapide sur CPU)
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

# ---------------------------------------------------------------------------
# 10) Divers
# ---------------------------------------------------------------------------
# Timeout (secondes) pour tous les appels réseau.
TIMEOUT_HTTP = 20

# Délai (secondes) entre deux requêtes JobSpy pour rester poli / éviter le blocage.
DELAI_ENTRE_REQUETES = 4

# Nombre de threads pour la collecte parallèle des sources API (I/O-bound).
# À garder >= au nombre de sources API de SOURCES_ACTIVES (5 aujourd'hui), sinon
# les dernières attendent qu'un thread se libère alors qu'elles ne font
# qu'attendre le réseau. JobSpy reste séquentiel par politesse.
MAX_THREADS_COLLECTE = 6
