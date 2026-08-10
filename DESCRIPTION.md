# stage_finder — Description du projet

## En une phrase

**stage_finder** (aussi appelé *job-finder*) est un agrégateur **local** d'offres de
**stage** : il interroge en parallèle plusieurs sites d'emploi, dédoublonne les
résultats, puis les **classe par similarité sémantique** avec un profil idéal —
du plus pertinent au moins pertinent.

100 % local et gratuit : le classement repose sur des *embeddings* calculés sur la
machine avec `sentence-transformers`. Aucune donnée n'est envoyée dans le cloud
pour le ranking.

---

## Objectif

Trouver rapidement les stages les plus pertinents pour un profil précis
(ici : **ingénieur IA + cybersécurité, 6 mois, Paris, janvier 2027**) sans avoir à
parcourir manuellement des dizaines d'annonces sur plusieurs plateformes. L'outil
ratisse large côté sources, puis laisse un moteur de tri sémantique faire le
classement fin.

---

## Pipeline

```
Sources → Normalisation → Filtres durs → Extraction → Déduplication (exacte + floue)
        → Ranking sémantique → Persistance SQLite → Sortie (CLI + CSV + HTML)
```

| Étape | Rôle |
|-------|------|
| **Sources** | Adzuna, Jooble, France Travail (API, en parallèle) + JobSpy (Indeed FR / LinkedIn / Google Jobs, scraping séquentiel). Une source en échec n'interrompt pas les autres. |
| **Normalisation** | Chaque offre est ramenée à un schéma commun : `title, company, location, description, url, source, posted_at, salary`. |
| **Filtres durs** | Mot « stage » **dans le titre**, exclusion des alternances/apprentissages et postes séniors, localisation Île-de-France, publication récente. Volontairement permissifs. |
| **Extraction** | Signaux souples : durée du stage (mois), date de début. |
| **Déduplication** | Exacte (hash `titre normalisé + entreprise + ville`) puis floue (similarité d'embeddings pour les quasi-doublons). On garde la version la plus riche. |
| **Ranking** | Similarité cosinus entre la *requête de référence* (le profil idéal) et chaque offre, via un modèle `sentence-transformers` **multilingue et local**. |
| **Persistance** | Base **SQLite** (`stages.db`) : historise chaque run et repère les offres jamais vues (`--new-only`). |
| **Sortie** | Console (classement lisible), export **CSV** (`stages.csv`) et rapport **HTML** (`stages.html`). |

---

## Utilisation

```bash
python main.py                      # pipeline complet, affichage CLI + exports
python main.py --limit 20           # top 20 dans la console
python main.py --no-jobspy          # ignore le scraping JobSpy (plus rapide)
python main.py --min-score 0.4      # ne garde que les offres au score >= 0.4
python main.py --new-only           # uniquement les offres jamais vues (SQLite)
python main.py --no-db              # ne lit/écrit pas la base SQLite
python main.py --csv out.csv --html out.html   # chemins d'export personnalisés
```

Le fichier **`config.py`** est le point d'entrée pour adapter l'outil : requête de
référence (profil idéal), termes de recherche, localisation, filtres, modèle
d'embedding, mode de composition du score, etc. Aucune logique métier dans ce
fichier — que de la configuration.

---

## Configuration & secrets

- **`.env`** — clés API des sources (Adzuna, Jooble, France Travail). Un
  `.env.example` sert de modèle.
- Les clés/jetons sont **redactés dans les logs** (durcissement) pour éviter toute
  fuite accidentelle.
- La configuration est **validée au démarrage** (`config_schema.py`, via Pydantic) :
  échec rapide si un paramètre est incohérent.

---

## Structure du code

| Fichier / dossier | Contenu |
|-------------------|---------|
| `main.py` | Orchestrateur du pipeline complet. |
| `config.py` | Tous les critères de recherche et paramètres modifiables. |
| `config_schema.py` | Validation de la configuration (Pydantic). |
| `sources/` | Un module par source : `adzuna.py`, `jooble.py`, `france_travail.py`, `jobspy_source.py` + utilitaires (`RedactingFilter`). |
| `normalize.py` | Modèle `Offre` et mise au schéma commun. |
| `filters.py` | Filtres durs (titre, exclusions, localisation, fraîcheur). |
| `extract.py` | Extraction des signaux souples (durée, date de début). |
| `dedup.py` | Déduplication exacte (hash) et floue (embeddings). |
| `ranker.py` | Cœur du projet : encodage + classement sémantique. |
| `storage.py` | Persistance SQLite et détection des nouveautés. |
| `report.py` | Exports CSV et HTML. |
| `evaluation.py` | Métriques pures : precision@k, nDCG@k, rang médian des positives. |
| `etiqueter.py` | Corpus étiqueté de référence (CLI à l'aveugle, `etiquettes.json`). |
| `evaluer_ranking.py` | Mesure le ranking sur le corpus étiqueté (baseline reproductible). |
| `benchmark.py` | Mesure de performance du pipeline. |
| `console.py` | Force l'UTF-8 sur la console Windows (badges, emoji). |
| `tests/` | Suite `pytest` (filtres, normalisation, dédup, ranker, storage, secrets…). |

---

## Dépendances principales

- `requests` — appels HTTP vers les API.
- `python-jobspy` — scraping Indeed / LinkedIn / Google Jobs.
- `sentence-transformers` (+ `torch`) — embeddings multilingues locaux pour le ranking.
- `pandas` — manipulation de données / export.
- `python-dotenv` — chargement du `.env`.
- `pydantic` — validation de configuration.
- `truststore` — vérification TLS via le magasin de certificats de l'OS (utile derrière proxy/antivirus).
- `pytest` — tests (développement).

Installation : `pip install -r requirements.txt`

---

## Points remarquables

- **Résilience** : chaque source est isolée ; une panne ne fait pas tomber le run.
- **Parallélisme mesuré** : sources API en threads (I/O-bound), scraping séquentiel par politesse.
- **Embeddings calculés une seule fois** et réutilisés par la dédup floue *et* le ranking.
- **Modèle téléchargé une fois** puis mis en cache : les exécutions suivantes tournent hors-ligne.
- **Suivi dans le temps** grâce à SQLite (repérage des nouvelles offres entre deux runs).

---

*Voir aussi : `README.md` (guide détaillé), `PROJET.md` (conception) et
`AMELIORATIONS.md` (pistes d'évolution).*
