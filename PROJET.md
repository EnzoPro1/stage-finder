# 📋 job-finder — Rapport complet du projet

> Agrégateur local d'offres de **stage**, dédoublonnage, et **classement par
> similarité sémantique** avec un profil idéal. 100 % local, gratuit, aucune
> donnée envoyée dans le cloud pour le ranking.

---

## 🎯 Objectif

Construire un outil Python qui interroge plusieurs sites d'emploi en parallèle,
normalise et dédoublonne les offres, puis les classe du plus pertinent au moins
pertinent par rapport à un **profil de référence**.

**Cible de recherche :**

| Critère | Valeur |
|---------|--------|
| Type | Stage de 6 mois |
| Début | Janvier 2027 |
| Lieu | Paris / Île-de-France |
| Domaines | Ingénierie IA **et** cybersécurité (préférence marquée pour l'IA) |
| Idéal | Un poste à l'**intersection IA × Cyber**, entreprise du CAC40 |

---

## 🔄 Pipeline

```
Sources → Normalisation → Filtres durs → Déduplication → Ranking sémantique + boost → Sortie
 (3)        (schéma          (4 règles)     (hash,          (embeddings locaux         (CLI +
            commun)                          plus riche)      + boost mots-clés)         CSV + HTML)
```

---

## 🗂️ Structure du projet

```
stage_finder/
├── .env / .env.example        # clés API (masquées dans les logs)
├── .gitignore
├── requirements.txt           # dépendances (Python 3.11/3.12)
├── config.py                  # ⭐ TOUT est réglable ici
├── sources/
│   ├── __init__.py            # injection truststore (TLS) + masquage secrets
│   ├── adzuna.py              # API Adzuna (France)
│   ├── jooble.py              # API Jooble (France)
│   └── jobspy_source.py       # scraping Indeed / LinkedIn / Google Jobs
├── normalize.py               # schéma commun + normaliseurs par source
├── filters.py                 # filtres durs (stage, exclusions, IDF, fraîcheur)
├── dedup.py                   # déduplication (hash, version la plus riche)
├── ranker.py                  # 🧠 cœur : embeddings + cosinus + boost mots-clés
├── report.py                  # exports CSV + HTML
├── main.py                    # orchestrateur + options CLI
├── README.md                  # doc utilisateur (clés API, install, dépannage)
└── PROJET.md                  # ce rapport
```

≈ 1300 lignes de Python, entièrement commentées en français.

| Fichier | Rôle |
|---------|------|
| `config.py` | Requête de référence, termes de recherche, filtres, mots-clés de boost, poids |
| `sources/adzuna.py` | Source API Adzuna, testable seule |
| `sources/jooble.py` | Source API Jooble, testable seule |
| `sources/jobspy_source.py` | Scraping sans authentification, un site à la fois, délais polis |
| `sources/__init__.py` | Injection `truststore` + `masquer_secrets()` |
| `normalize.py` | `dataclass Offre` + un normaliseur par source |
| `filters.py` | Les 4 filtres durs |
| `dedup.py` | Déduplication par hash |
| `ranker.py` | Similarité cosinus + boost IA/Cyber |
| `report.py` | Exports CSV et rapport HTML |
| `main.py` | Orchestration de bout en bout |

---

## ⚙️ Fonctionnement détaillé par étape

### 1. Sources
Chaque source est un **module indépendant**, testable isolément
(`python -m sources.adzuna`). Garanties communes :
- Clé API manquante → **warning + liste vide**, jamais de crash.
- Chaque requête est isolée dans un `try/except` (timeout, 429, blocage, réponse
  vide gérés) : une requête qui échoue ne fait pas tomber la source.
- **JobSpy** : jamais authentifié, un site à la fois, une pause entre chaque
  requête, gestion propre du rate-limit / captcha.

### 2. Normalisation
Toutes les offres sont ramenées au schéma commun (`dataclass Offre`) :

```python
{ title, company, location, description, url, source, posted_at, salary, tags }
```

Un normaliseur par source + table de dispatch → ajouter une source = ajouter une
fonction. Un item cassé est ignoré, pas fatal.

### 3. Filtres durs (permissifs — le ranking fait le tri fin)

| Filtre | Règle |
|--------|-------|
| **Stage** | mot-clé « stage / stagiaire / internship » **dans le titre** (mot entier) |
| **Exclusions** | rejette alternance / apprentissage / senior dans le titre |
| **Localisation** | Île-de-France uniquement ; rejette les « Paris » étrangers (Paris, TX) |
| **Fraîcheur** | publié dans les **7 derniers jours** (poussé aux sources + filtre date) |

On **ne filtre pas** sur la durée (6 mois) ni la date de début : ces infos sont
souvent seulement en texte libre → laissées au ranking.

### 4. Déduplication
Clé = hash de `(titre normalisé + entreprise + ville)`. Le titre est nettoyé
(minuscules, sans accents, mentions H/F retirées), les arrondissements parisiens
regroupés. En cas de doublon → on garde la **version la plus riche** (description
la plus longue, présence d'un salaire et d'une date).

### 5. Ranking sémantique (le cœur)
- Modèle **multilingue** `paraphrase-multilingual-MiniLM-L12-v2`, 100 % local
  (indispensable : les offres sont en français).
- Embeddings normalisés → la similarité cosinus se réduit à un produit scalaire.
- On encode `titre + description` (le titre sauve les offres à description vide).

**Boost par mots-clés** ajouté par-dessus la similarité :

```
score_final = cosinus + boost_IA + boost_Cyber + boost_combo   (plafonné à 1.0)
```

- Termes **IA** : LLM, RAG, NLP, MLOps, deep learning, embeddings, agentique…
- Termes **Cyber** : test d'intrusion, pentest, SIEM, red team, OWASP…
- Bonus **combo IA × Cyber** → badge `★ IA+CYBER`, **toujours affiché** même sous
  un seuil de score.
- **Anti-boilerplate** : un mot-clé dans le titre compte plein pot ; seulement
  dans la description, il est atténué (évite de sur-noter le blabla corporate des
  grands groupes type Thales).

### 6. Sortie
- **Console** : rang, score, badges, titre, entreprise, lieu, source, URL.
- **`stages.csv`** : ouvrable dans Excel (accents gérés), colonne `match`.
- **`stages.html`** : rapport lisible, trié, badges colorés, liens cliquables.

---

## 🧗 Obstacles techniques résolus

| Problème | Solution |
|----------|----------|
| **Python 3.14** : torch/numpy/jobspy ne compilent pas (wheels absents) | environnement virtuel en **Python 3.12** |
| **Interception SSL** (antivirus/proxy) → `CERTIFICATE_VERIFY_FAILED` | **`truststore`** (magasin de certificats Windows) au runtime, + bundle PEM pour pip |
| **Clés API en clair dans les logs** | fonction `masquer_secrets()` → `app_key=***` |
| **« Paris, Texas »** remonté par Jooble | liste `MARQUEURS_ETRANGERS` |
| **Postes senior / CDI** pris pour des stages | exiger le mot « stage » **dans le titre** |
| **Faux combo** (boilerplate Thales) | boost titre > description + combo exige un signal en titre |

> ⚠️ **Note sécurité** : les clés Adzuna ont transité en clair dans les logs lors
> des premiers tests. Il est recommandé de les **régénérer** sur le portail Adzuna.

---

## 🔑 Sources de données

| Source | Clé requise | Obtention |
|--------|-------------|-----------|
| Adzuna | `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | https://developer.adzuna.com/ (gratuit) |
| Jooble | `JOOBLE_API_KEY` | https://jooble.org/api/about (gratuit) |
| JobSpy (Indeed/LinkedIn/Google) | aucune | — |

Clé absente = source ignorée (warning), le reste continue.

---

## ▶️ Utilisation

```powershell
# Activer l'environnement
.\.venv\Scripts\Activate.ps1

python main.py                  # run complet → console + CSV + HTML
python main.py --no-jobspy      # rapide (API seules, sans scraping)
python main.py --limit 20       # top 20 en console
python main.py --min-score 0.5  # haut du panier + tous les combos garantis
python main.py --csv out.csv --html out.html   # chemins personnalisés

# Tester une source isolément
python -m sources.adzuna
python -m ranker                # démo du ranking
```

---

## 📊 Dernier run réel

```
367 offres collectées  →  87 après filtres  →  62 stages uniques classés
dont 2 combos IA + Cyber badgés en tête
```

Exemples de têtes de classement (combos) :
- ★ **Stagiaire Cybersécurité DevSecOps**
- ★ **Internship on Artificial Intelligence and Network Intrusion Detection**

Le système fonctionne de bout en bout. ✅

---

## 🔧 Réglages rapides (dans `config.py`)

| Paramètre | Effet |
|-----------|-------|
| `REQUETE_REFERENCE` | Le profil idéal qui pilote tout le classement |
| `TERMES_RECHERCHE` | Requêtes envoyées aux sources |
| `JOURS_FRAICHEUR` | Fenêtre de fraîcheur (7 jours) |
| `MOTS_CLES_IA` / `MOTS_CLES_CYBER` | Termes qui déclenchent les boosts |
| `BOOST_IA` / `BOOST_CYBER` / `BOOST_COMBO` | Poids des bonus |
| `GARDER_SI_DATE_INCONNUE` | Garder ou non les offres sans date |

---

## 🚧 Améliorations possibles

- Déduplication **floue** (rapprochement approximatif des titres) pour fusionner
  les quasi-doublons (ex. « Sanofi » vs « Sanofi Group »).
- Filtres **interactifs** dans le rapport HTML (tri par colonne, recherche).
- **Planification automatique** d'un run quotidien.
- Cache des offres déjà vues pour ne montrer que les **nouveautés**.

---

## 🔐 Confidentialité

- Le **ranking est 100 % local** : ni le profil, ni les offres ne quittent la
  machine (aucune clé, aucun appel externe pour les embeddings).
- Les clés API sont lues depuis `.env` (non commité) et **masquées** dans les logs.
- JobSpy est utilisé **sans compte**, avec des délais entre requêtes.
