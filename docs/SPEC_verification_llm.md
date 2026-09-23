# Spec — Couche de vérification LLM locale pour `stage_finder`

## Contexte

`stage_finder` est un agrégateur **local** d'offres de stage. Le pipeline actuel est :

```
Sources → Normalisation → Filtres durs → Extraction → Déduplication
        → Ranking sémantique (embeddings sentence-transformers)
        → Persistance SQLite → Sortie (CLI + CSV + HTML)
```

Le ranking actuel repose sur la **similarité cosinus** entre un profil de référence et
chaque offre. C'est de la proximité topique : ça ne *raisonne* pas. On veut ajouter une
étape de **vérification par LLM local** qui lit chaque offre pré-sélectionnée et juge si
elle correspond vraiment au stage recherché, avec un verdict **explicable**.

Profil de référence : **stage 6 mois, ingénieur IA + cybersécurité, Île-de-France,
début janvier 2027**.

## Objectif de la fonctionnalité

Attraper les cas que le cosinus rate :
- alternances / apprentissages déguisés non filtrés en amont,
- postes séniors truffés de mots-clés IA/cyber,
- durée ≠ 6 mois, date de début incompatible avec janvier 2027,
- drift de domaine (boîte d'IA qui recrute un commercial ; « cyber » = GRC pur, pas technique).

Le LLM rend, pour chaque offre vérifiée : un score de pertinence, les champs extraits
(durée, date de début, niveau), des **drapeaux rouges**, et une justification courte.

## Principe : *retrieve-then-verify*

**Ne jamais** passer le LLM sur toutes les offres. On garde le ranking cosinus comme tri
grossier bon marché, puis on vérifie **uniquement la shortlist top-N** :

```
... → Ranking sémantique → Shortlist top-N (défaut 30)
    → Vérification LLM (par offre) → verdict enrichi
    → Combinaison des scores → Sortie
```

## Contraintes de conception (non négociables)

1. **Optionnel.** Un flag `--no-verify` doit court-circuiter toute la couche : le pipeline
   se comporte alors exactement comme aujourd'hui.
2. **Dégradation gracieuse.** Si Ollama est injoignable ou qu'un appel timeout/échoue, le
   run **ne casse pas** : on log un warning et on retombe sur le score cosinus pour cette
   offre. Même philosophie que l'isolation des sources.
3. **Cache SQLite.** Un verdict est calculé **une seule fois** par offre et par version de
   modèle. Les runs suivants ne re-vérifient que les offres jamais vues.
4. **Sortie structurée.** On demande à Ollama du JSON contraint par schéma (`format`),
   `temperature = 0` pour la reproductibilité. Parsing défensif quand même.
5. **Enrichir, pas supprimer.** Le LLM ne *drop* jamais une offre en dur (risque
   d'hallucination). Son verdict est un signal affiché ; un seuil configurable décide de la
   mise en avant. Cohérent avec les « filtres volontairement permissifs » du projet.
6. **100 % local / gratuit.** Aucune API cloud. Appels vers `http://localhost:11434`.
7. **Style du code existant.** Respecter l'archi actuelle : un module par responsabilité,
   config centralisée dans `config.py`, validation Pydantic dans `config_schema.py`,
   secrets redactés, tests pytest.

## Découpage des tâches

### 1. `config.py` — nouveaux paramètres (config pure, aucune logique)

Ajouter un bloc « vérification LLM » :

```python
VERIFY_ENABLED      = True
VERIFY_MODEL        = "qwen3:8b"              # tag Ollama
VERIFY_OLLAMA_URL   = "http://localhost:11434"
VERIFY_TOP_N        = 30                       # taille de la shortlist vérifiée
VERIFY_TIMEOUT_S    = 60                       # timeout par appel
VERIFY_SCORE_WEIGHT = 0.5                      # poids du score LLM dans le score final
VERIFY_MIN_SCORE    = 0.0                      # seuil d'affichage (0 = tout afficher)
```

Le **profil de référence** existe déjà (requête de référence du ranker) : **réutiliser
la même constante**, ne pas la dupliquer.

### 2. `config_schema.py` — validation Pydantic

Valider les nouveaux champs : `VERIFY_TOP_N > 0`, `0 <= VERIFY_SCORE_WEIGHT <= 1`,
`0 <= VERIFY_MIN_SCORE <= 1`, `VERIFY_TIMEOUT_S > 0`, URL non vide. Échec rapide au
démarrage si incohérent.

### 3. `verifier.py` — **nouveau module** (cœur de la fonctionnalité)

Responsabilité : prendre une `Offre` normalisée + le profil de référence, appeler Ollama,
retourner un `Verdict` (dataclass/pydantic) ou `None` en cas d'échec.

Schéma JSON attendu d'Ollama :

```python
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "pertinent":       {"type": "boolean"},
        "score":           {"type": "number"},          # 0.0 à 1.0
        "duree_mois":      {"type": ["integer", "null"]},
        "date_debut":      {"type": ["string", "null"]}, # ISO ou null
        "est_alternance":  {"type": "boolean"},
        "niveau":          {"type": "string"},           # "stage" | "junior" | "senior" | "inconnu"
        "domaine_match":   {"type": "boolean"},          # IA/cyber réellement au cœur du poste ?
        "drapeaux_rouges": {"type": "array", "items": {"type": "string"}},
        "justification":   {"type": "string"},
    },
    "required": ["pertinent", "score", "est_alternance", "niveau", "justification"],
}
```

Prompt (indicatif, à durcir contre les **faux négatifs** — mieux vaut signaler que
rejeter) :

```
Tu vérifies si une offre d'emploi correspond à un profil de stage précis.
Sois prudent : en cas de doute, marque l'offre comme pertinente mais ajoute un drapeau
rouge expliquant le doute. Ne rejette que si un critère est clairement violé.

PROFIL RECHERCHÉ:
- Stage (pas alternance, pas apprentissage, pas CDI)
- Durée ~6 mois
- Domaine: ingénierie IA ET/OU cybersécurité, au cœur du poste
- Localisation: Île-de-France
- Début: vers janvier 2027

OFFRE:
Titre: {title}
Entreprise: {company}
Lieu: {location}
Description: {description}

Réponds UNIQUEMENT en JSON conforme au schéma. Signale explicitement toute alternance,
poste senior, durée éloignée de 6 mois, date incompatible, ou domaine hors IA/cyber.
```

Appel Ollama (endpoint `/api/generate`, `stream=false`, `format=VERDICT_SCHEMA`,
`options={"temperature": 0}`). Accès aux champs de l'`Offre` **explicite** (title,
company, location, description — pas de `__dict__`). Envelopper dans try/except : tout
échec → retour `None` (dégradation gracieuse), jamais d'exception qui remonte.

> ⚠️ Aucune clé/secret ici, mais rester cohérent avec le `RedactingFilter` du projet si
> quoi que ce soit est loggué.

### 4. `storage.py` — cache des verdicts

Nouvelle table SQLite `verdicts` :

- **clé** = `hash_offre` (sha256 de `title|company|location|description`, normalisés) **+**
  `model` (tag Ollama).
- colonnes : le verdict JSON sérialisé, `created_at`.

Fonctions : `get_verdict(hash, model) -> dict | None` et
`save_verdict(hash, model, verdict)`. Le changement de modèle invalide naturellement le
cache (clé différente). Migration idempotente (`CREATE TABLE IF NOT EXISTS`).

### 5. `main.py` — intégration

Après le ranking, si `VERIFY_ENABLED` et pas `--no-verify` :

1. prendre les `VERIFY_TOP_N` premières offres,
2. pour chacune : cache hit → réutiliser ; sinon appeler `verifier.verifier(...)`, puis
   `save_verdict(...)`,
3. séquentiel (Ollama sérialise de toute façon), avec une **barre de progression** /
   compteur lisible, à l'image du reste de la CLI,
4. attacher le verdict à l'offre (ou `None`),
5. calculer le **score final** (voir plus bas) et re-trier.

Si Ollama est injoignable au premier appel : warning global unique + on garde le tri
cosinus pour tout le batch.

### 6. Combinaison des scores

Quand un verdict existe :

```
score_final = (1 - VERIFY_SCORE_WEIGHT) * score_cosinus
            +      VERIFY_SCORE_WEIGHT  * score_llm
```

Sans verdict (échec/désactivé) : `score_final = score_cosinus`. Ne jamais faire disparaître
une offre faute de verdict.

### 7. `report.py` + sortie CLI

Exposer, quand disponibles : `score_llm`, `pertinent`, `niveau`, `est_alternance`,
`drapeaux_rouges`, `justification`. En CLI, afficher au moins les drapeaux rouges (badge)
et la justification tronquée. Ajouter les colonnes correspondantes au CSV et une section
au rapport HTML. Ne rien casser pour les offres sans verdict (valeurs vides).

### 8. Flags CLI (dans `main.py`)

- `--no-verify` : désactive la couche.
- `--verify-model MODEL` : override `VERIFY_MODEL`.
- `--verify-top-n N` : override `VERIFY_TOP_N`.

### 9. `tests/` — pytest

- `verifier` avec appel Ollama **mocké** (pas de dépendance réseau en test) : parsing d'un
  JSON valide → `Verdict` correct.
- échec réseau / JSON invalide → retour `None` (dégradation).
- cache : deuxième lecture ne déclenche **aucun** appel (mock non rappelé).
- combinaison des scores (pondération, cas sans verdict).
- `--no-verify` : pipeline identique à l'existant.

### 10. (Bonus) `evaluation.py`

Si les labels de référence existent, ajouter une mesure comparant la **précision@k** avec
et sans vérification LLM, pour quantifier le gain.

## Non-objectifs / pièges à éviter

- Ne **pas** appeler le LLM sur toutes les offres (seulement la shortlist).
- Ne **pas** supprimer d'offres sur la seule foi du LLM.
- Ne **pas** bloquer/casser le run si Ollama est absent.
- Ne **pas** dupliquer le profil de référence : réutiliser celui du ranker.
- Ne **pas** ajouter de logique métier dans `config.py`.

## Critères d'acceptation

1. `python main.py --no-verify` produit le même résultat qu'avant la fonctionnalité.
2. Ollama arrêté → le run se termine avec un warning et un classement cosinus valide.
3. Deux runs successifs sur les mêmes offres → **zéro** appel LLM au second (cache).
4. CLI / CSV / HTML affichent `score_llm`, drapeaux rouges et justification quand présents.
5. Changer `VERIFY_MODEL` recalcule les verdicts (cache invalidé par la clé).
6. `pytest` passe, y compris le test à Ollama mocké.

## Prérequis

```bash
ollama pull qwen3:8b     # modèle par défaut (config)
ollama list              # vérifier la présence
```

Dépendance : réutiliser `requests` (déjà présent). Aucune nouvelle dépendance lourde requise.
