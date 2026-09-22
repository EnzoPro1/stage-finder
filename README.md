# job-finder 🎯

Agrégateur **local** d'offres de **stage** qui interroge plusieurs sites d'emploi
en parallèle, dédoublonne les résultats, puis les **classe par similarité
sémantique** avec ton profil idéal — du plus pertinent au moins pertinent.

100 % local, gratuit, aucune donnée n'est envoyée dans le cloud pour le ranking
(les embeddings sont calculés sur ta machine avec `sentence-transformers`).

---

## 🧭 Pipeline

```
Sources → Normalisation → Filtres durs → Extraction → Déduplication (exacte + floue)
        → Ranking sémantique → Persistance SQLite → Sortie
```

- **Sources** : Adzuna (API), Jooble (API), France Travail (API), **Careerjet**
  (API, méta-moteur qui indexe des centaines de sites français), **Free-Work**
  (API, job board tech/IT français), JobSpy (Indeed FR / LinkedIn / Google
  Jobs). Les sources API sont interrogées **en parallèle** (I/O-bound) ; JobSpy
  reste séquentiel par politesse.

  Le catalogue vit dans `sources/registry.py` : **une source = une ligne**, et
  `config.SOURCES_ACTIVES` décide lesquelles tournent. Ajouter un site ne
  demande plus de toucher à `main.py`.
- **Normalisation** : chaque offre est ramenée au schéma commun
  `{ title, company, location, description, url, source, posted_at, salary }`.
- **Filtres durs** : mot-clé « stage » **dans le titre**, exclusion des
  alternances/apprentissages et des postes séniors, localisation Île-de-France,
  et **offres publiées dans les 7 derniers jours** uniquement.
- **Déduplication** : hash `(titre normalisé + entreprise + ville)`, on garde la
  version la plus riche.
- **Ranking** : similarité cosinus entre la *requête de référence* (ton profil
  idéal) et chaque offre, via le modèle multilingue
  `paraphrase-multilingual-MiniLM-L12-v2`, **plus un boost de mots-clés** (voir
  ci-dessous).

### 🚀 Boost par mots-clés (re-ranking)

Par-dessus la similarité sémantique, un bonus de score est ajouté aux offres qui
mentionnent des termes IA (LLM, RAG, NLP, MLOps, deep learning…) et/ou cyber
(test d'intrusion, pentest, SIEM, red team…). Une offre à l'**intersection
IA × Cyber** reçoit un gros bonus « combo » et un badge `★ IA+CYBER`.

```
score_final = similarité_cosinus + boost_IA + boost_cyber + boost_combo   (plafonné à 1.0)
```

- Un mot-clé dans le **titre** compte plein pot ; seulement dans la
  **description**, il est atténué (`BOOST_FACTEUR_DESCRIPTION`) pour éviter de
  sur-noter le blabla corporate des grands groupes.
- Le bonus **combo** exige qu'au moins un signal IA/cyber soit dans le titre.
- Une offre combo est **toujours affichée**, même sous le seuil `--min-score`
  (`COMBO_TOUJOURS_AFFICHE`).

Tout est réglable dans `config.py` (`MOTS_CLES_IA`, `MOTS_CLES_CYBER`,
`BOOST_IA`, `BOOST_CYBER`, `BOOST_COMBO`).

---

## ✅ Prérequis

- **Python 3.11 ou 3.12** — ⚠️ **PAS Python 3.13/3.14** : `torch`, `numpy` et
  `python-jobspy` n'ont pas encore de wheels compatibles avec ces versions et
  l'installation échouera à la compilation.
- Windows / macOS / Linux.

Vérifie ta version :
```powershell
py -0p          # liste les Python installés (Windows)
python --version
```

---

## 📦 Installation

```powershell
# 1) Se placer dans le dossier du projet
cd C:\Users\toi\stage_finder

# 2) Créer un environnement virtuel en Python 3.12 (adapte le chemin si besoin)
py -3.12 -m venv .venv

# 3) L'activer
.\.venv\Scripts\Activate.ps1        # PowerShell
# ou  .venv\Scripts\activate.bat    # cmd

# 4) Installer les dépendances
pip install -r requirements.txt
```

> La première exécution télécharge le modèle d'embedding (~470 Mo) depuis
> HuggingFace, **une seule fois**. Ensuite tout tourne hors-ligne.

### 🔒 Souci de certificat SSL ? (antivirus / proxy d'entreprise sous Windows)

Si `pip install` ou le téléchargement du modèle échoue avec
`CERTIFICATE_VERIFY_FAILED … unable to get local issuer certificate`, c'est
qu'un antivirus (Kaspersky, ESET…) ou un proxy intercepte le trafic TLS avec sa
propre autorité racine, absente du bundle Python.

Le code gère ça **au runtime** via `truststore` (il utilise le magasin de
certificats de Windows — installé automatiquement par `requirements.txt`).

Mais **`pip` lui-même** peut buter avant. Solution : exporter les autorités
racines de Windows dans un fichier `.pem` et le donner à pip.

```powershell
# Générer un bundle depuis le magasin Windows :
$pem = ".\windows-ca-bundle.pem"
$sb = New-Object System.Text.StringBuilder
foreach ($store in @('Cert:\LocalMachine\Root','Cert:\CurrentUser\Root','Cert:\LocalMachine\CA')) {
  Get-ChildItem $store -ErrorAction SilentlyContinue | ForEach-Object {
    $b = [System.Convert]::ToBase64String($_.RawData, 'InsertLineBreaks')
    [void]$sb.AppendLine("-----BEGIN CERTIFICATE-----")
    [void]$sb.AppendLine($b)
    [void]$sb.AppendLine("-----END CERTIFICATE-----")
  }
}
[System.IO.File]::WriteAllText((Resolve-Path .).Path + "\windows-ca-bundle.pem", $sb.ToString())

# Puis installer en pointant pip dessus :
$env:PIP_CERT = ".\windows-ca-bundle.pem"
pip install -r requirements.txt
```

---

## 🔑 Obtenir les clés API

Copie `.env.example` en `.env` puis remplis les valeurs. **Une clé absente = la
source correspondante est simplement ignorée** (un warning est loggé, aucun
crash). JobSpy ne demande aucune clé.

### Adzuna
1. Va sur **https://developer.adzuna.com/** et crée un compte (gratuit).
2. Dans ton dashboard, crée une application : tu obtiens un **App ID** et une
   **App Key**.
3. Renseigne-les dans `.env` :
   ```
   ADZUNA_APP_ID=ton_app_id
   ADZUNA_APP_KEY=ta_app_key
   ```

### Jooble
1. Va sur **https://jooble.org/api/about** et demande une clé API (gratuite).
2. Renseigne-la dans `.env` :
   ```
   JOOBLE_API_KEY=ta_cle
   ```

### France Travail (ex-Pôle emploi)
API **officielle et gratuite**, elle couvre un gisement français que
Indeed/LinkedIn ratent en partie.
1. Crée un compte sur **https://francetravail.io/** et déclare une application
   avec accès à l'API « Offres d'emploi v2 ».
2. Récupère l'**identifiant client** et la **clé secrète**, puis renseigne :
   ```
   FRANCE_TRAVAIL_ID=ton_identifiant_client
   FRANCE_TRAVAIL_KEY=ta_cle_secrete
   ```
L'authentification se fait par **jeton OAuth2** (la clé secrète ne transite
jamais en query string : elle sert à obtenir un jeton, envoyé ensuite en en-tête
`Authorization: Bearer`). L'URL demandée à la création de l'application n'est
jamais appelée : le flux `client_credentials` est serveur-à-serveur, sans
redirection. `http://localhost:5000` convient.

Deux contraintes de l'API, gérées dans le code :
- **5 départements maximum** par recherche — l'Île-de-France en compte 8, donc on
  filtre par `region=11` et non par liste de départements ;
- **10 appels/seconde** — le module sérialise ses requêtes derrière un verrou
  (`_respecter_debit`, marge à 8/s).

### Careerjet — *aucune clé obligatoire*
Méta-moteur qui indexe des centaines de job boards français (sites d'entreprises,
cabinets, boards de niche) que ni Indeed ni LinkedIn ne couvrent complètement.
C'est aujourd'hui la source qui rapporte le plus de stages IA/cyber en IdF.

Elle fonctionne telle quelle avec l'identifiant d'affiliation de **démonstration**
publié dans la doc Careerjet — mais il est partagé, donc soumis à un quota commun.
Pour un usage régulier, crée le tien sur **https://www.careerjet.fr/partners/** et
ajoute-le au `.env` :
```
CAREERJET_AFFID=ton_affid
```

### Free-Work — *aucune clé*
Job board 100 % tech/IT français (ex Carrière Info). Petit volume mais faible
recouvrement avec les autres sources. Rien à configurer.

### JobSpy (Indeed / LinkedIn / Google Jobs)
Rien à configurer. ⚠️ Le scraping est fait **sans authentification** (jamais avec
un compte perso), avec des délais entre requêtes. Certains sites peuvent
ponctuellement bloquer ou renvoyer 0 résultat (notamment Google Jobs et
LinkedIn) : c'est géré proprement, ça ne bloque pas le reste.

---

## ⚙️ Configuration

Tout est dans **`config.py`** :

- `REQUETE_REFERENCE` : **ton profil idéal**. C'est LE texte qui pilote le
  classement. Modifie-le librement.
- `TERMES_RECHERCHE` : les requêtes envoyées aux sources.
- `SOURCES_ACTIVES` : quelles sources tournent (commente une ligne pour en
  couper une). `CAREERJET_PAGES`, `CAREERJET_LOCALE`, `FREE_WORK_PAGES` règlent
  la profondeur de pagination des nouvelles sources.
- `LIEU`, `LIEUX_ACCEPTES`, `MARQUEURS_ETRANGERS` : filtrage géographique.
- `MARQUEURS_ETRANGERS_TITRE` : pays/villes qui, cités **dans le titre**,
  disqualifient l'offre même si le lieu affiché est « Paris » (cas des cabinets
  de placement : « Stage Data Scientist - IT (H/F) - Canada »).
- `MOTS_CLES_STAGE` : mots-clés du filtre « stage » (cherchés **dans le titre**).
- `MOTS_CLES_EXCLUS` : titres à écarter (alternance, apprentissage, sénior…).
- `JOURS_FRAICHEUR` : ne garder que les offres publiées depuis N jours (7 par défaut).
- `GARDER_SI_DATE_INCONNUE` : garder ou non une offre sans date détectable
  (`False` = strict).
- `MODELE_EMBEDDING`, `MAX_SEQ_LENGTH` : modèle de ranking et borne de troncature.
- `MODE_COMPOSITION_SCORE` : `additif` / `additif_normalise` / `multiplicatif` /
  `lexicographique` — comment combiner similarité et boost (voir AMELIORATIONS §1.1).
- `PROFILS_REFERENCE`, `AGGREGATION_PROFILS` : plusieurs profils cibles (IA pur,
  cyber pur, IA×cyber) agrégés en `max` ou `moyenne`.
- `DEDUP_FLOUE_ACTIVE`, `SEUIL_DEDUP_FLOU` : dédup des quasi-doublons par embeddings.
- `DUREE_CIBLE_MOIS`, `DATE_DEBUT_CIBLE_*`, `BONUS_*` / `MALUS_*` : signaux souples
  durée / date de début.
- `CHEMIN_BASE` : chemin de la base SQLite.
- `VERIFY_*` : couche de vérification LLM. `VERIFY_MAX_TOKENS` est le budget de
  génération d'un verdict — **trop bas, le JSON est coupé et le verdict entier
  est perdu** (pas seulement la fin du texte) ; `VERIFY_JUSTIF_PHRASES` fixe la
  longueur de l'analyse écrite (3 phrases par défaut).

### 🎯 Faux positifs de la vérification LLM

Trois garde-fous dans [verifier.py](verifier.py), issus d'un cas réel : un
**« Legal intern » publié par un éditeur de cybersécurité** était noté **0.90**.

1. **Extraction centrée sur les missions.** Les 600 caractères envoyés au modèle
   ne sont plus le DÉBUT de l'annonce mais la section « missions » (`Vos
   missions`, `Votre rôle`, `Responsibilities`…). Mesuré sur un run complet :
   **24 annonces sur 76 placent leurs missions au-delà du 600ᵉ caractère** — le
   modèle ne lisait donc que la plaquette de la société, saturée de vocabulaire
   cyber/IA quel que soit le poste.
2. **Règle de prompt explicite** : *juge le POSTE, pas l'EMPLOYEUR*. Une société
   d'IA recrute aussi des juristes et des commerciaux.
3. **Garde-fou déterministe** : un métier support annoncé dans le TITRE
   (juridique, RH, marketing, vente, finance…) force `domaine_match=false` et
   plafonne le score — sauf si le titre porte un signal IA/cyber, auquel cas la
   règle ne s'applique pas (« Stage Data Scientist - Marketing Analytics » reste
   un poste data). Les termes ambigus sont écrits en **expression** et jamais en
   mot isolé : « talent » seul attrapait « STAGE TALENT DAY » sur un poste de
   Data Engineer, « communication » seul attrapait « Semantic Communication MARL
   Internship » (apprentissage par renforcement).

> ⚠️ **Le cache des verdicts est versionné** par `verifier.VERSION_REGLES`.
> Sans ça, durcir le prompt ne changeait rien aux offres déjà vérifiées : elles
> ressortaient du cache avec leur ancien jugement. **Incrémente cette constante**
> à chaque modification de `_PROMPT`, `_extrait_pertinent` ou `_plafonner_*`.

Résultat mesuré sur le top 12 : « Legal intern » passe de **0.90 à 0.30** avec un
drapeau rouge explicite, et les 9 offres techniques gardent leur score (0.85-0.95).
- `MAX_THREADS_COLLECTE`, `TIMEOUT_HTTP`, `DELAI_ENTRE_REQUETES` : réglages réseau.

Toute la config est **validée au démarrage** par `config_schema.py` (pydantic) :
un poids négatif, un seuil > 1 ou une liste vide arrêtent le programme avec un
message clair, au lieu d'un classement silencieusement faussé.

---

## ▶️ Lancer l'outil

```powershell
python main.py                     # pipeline complet + exports CSV et HTML
python main.py --limit 20          # n'affiche que le top 20 en console
python main.py --no-jobspy         # ignore le scraping (API seules, rapide)
python main.py --min-score 0.4     # ne garde que les offres au score >= 0.40
python main.py --new-only          # n'affiche que les offres jamais vues (SQLite)
python main.py --no-db             # ne lit/écrit pas la base SQLite
python main.py --csv out.csv --html out.html   # chemins d'export personnalisés
```

Sorties :
- **Console** : rang / score / badges / durée+début / entreprise / lieu / source / URL.
- **`stages.csv`** : ouvrable dans Excel (accents gérés), colonnes `duree_mois`,
  `date_debut`, `nouvelle`.
- **`stages.html`** : rapport lisible, filtrable (champ de recherche), offres
  triées et cliquables, badge **🆕 Nouveau** sur les offres inédites.
- **`stages.db`** : base SQLite (cache des offres vues, nouveautés, historique
  des runs). Supprimable sans risque ; recréée au run suivant.

### 🖥️ L'app web (recommandée)

```powershell
python app.py       # puis ouvre http://localhost:5000
```

Le classement cosinus s'affiche **immédiatement** (rechargé d'un cache disque),
la vérification IA se déclenche à la demande, et les colonnes `cos | IA | Δ`
montrent ce que l'IA a changé au classement.

Trois boutons :

| Bouton | Ce qu'il fait |
|---|---|
| **🚀 Tout lancer (recherche + IA)** | Enchaîne **tout seul** la recherche sur toutes les sources **puis** la vérification IA du top N. Un clic, plus rien à surveiller — la barre de progression annonce l'étape 1/2 puis 2/2. |
| 🔎 Vérification IA seule | Ne relance que le LLM sur le classement déjà présent. |
| 🔄 Recherche seule | Ne relance que la collecte (classement cosinus, sans IA). |

### 📊 État du marché

Le panneau **« État du marché »** en haut de l'app répond à une autre question que
le reste de l'outil : *est-ce le bon moment, et ma niche est-elle porteuse ?*

- **Volumes temps réel par métier**, et non par mot-clé. Les intitulés de
  `config.METIERS_SUIVIS` sont convertis en **codes ROME** par l'IA ROMEO de
  France Travail ([sources/romeo.py](sources/romeo.py)), puis les offres sont
  comptées par code. L'écart est énorme : « stage cybersécurité » en texte libre
  remonte **0 offre** sur 31 jours en IdF, le ROME M1856 en remonte **9**. Le
  mot-clé ne matche que les intitulés reprenant le terme exact, le code ROME
  rassemble tout un métier.
- Mesurés **sans rapatrier les offres** — l'en-tête `Content-Range` et le champ
  `hits` suffisent, donc un appel par indicateur.
- **Colonne « rythme »** : les 7 derniers jours rapportés à la moyenne des 31.
  `▲ ×1.9` = ça publie presque deux fois plus vite que la moyenne du mois.
- **Colonne « ts contrats »** : le métier recrute-t-il en général, même sans
  stage ouvert ? Une valeur haute avec 0 stage désigne une cible de
  **candidature spontanée**.
- **Colonne « tension »** : l'indicateur officiel France Travail de difficulté
  de recrutement (PERSP_2, par métier × région, millésime annuel), via
  [sources/marche_travail.py](sources/marche_travail.py). C'est la **seule
  mesure du côté demande** — tout le reste compte des annonces.
  **Positive = les employeurs peinent à recruter, donc favorable au candidat** ;
  les couleurs suivent cette lecture, pas celle du recruteur.

> ⚠️ **Le scope de cette API est DOUBLE.** `api_stats-offres-demandes-emploiv1`
> seul délivre un jeton parfaitement valide… et renvoie `403` vide sur tous les
> endpoints. Il faut `api_stats-offres-demandes-emploiv1 offresetdemandesemploi`
> (bloc `security` de la spec OpenAPI). Aucun message d'erreur ne le signale.
- **Dynamique de ta recherche**, lue dans `stages.db` : offres connues, part déjà
  revue d'un run à l'autre, taux de nouveautés au dernier run, entreprises les
  plus présentes.

Deux garde-fous assumés dans [market.py](market.py) :

- les colonnes **ne s'additionnent jamais** — France Travail voit le gisement
  public, Careerjet le privé, avec recouvrement partiel ;
- si aucune mesure n'aboutit (API en panne, clés absentes), le verdict est
  **« inconnu »**, jamais « marché étroit » : une absence de chiffre n'est pas
  un chiffre nul.

Si ROMEO est indisponible, on retombe sur `config.ROME_REPLI` (codes obtenus par
un appel réel) — et `config_schema.py` refuse de démarrer si un métier suivi n'a
pas de repli, pour qu'une panne ne fasse pas disparaître une ligne en silence.

En ligne de commande :
```powershell
python -m market                  # tableau de bord complet
python -m sources.romeo           # codes ROME déduits de ton profil
python -m sources.marche_travail  # indicateur de tension par métier
```

L'analyse rédigée par le LLM se déplie sous chaque offre (**« ▸ lire l'analyse
IA »**, ou la case *déplier les analyses IA* pour tout ouvrir) : elle s'affiche
**sur toute la largeur**, en taille de lecture — avant, elle était compressée
dans une colonne étroite et rognée à trois lignes.

---

## 📄 Générer un CV pour une offre

Chaque ligne de la liste porte un bouton **« Générer CV »**. Une génération coûte
des minutes et monopolise Ollama : elle ne se fait donc pas dans le fil d'une
requête HTTP. Le clic **enfile un job**, un **worker unique** le consomme, et la
page suit l'avancement (`idle → pending → running → done`).

Le pipeline lui-même vit dans **[cv_forge](../cv_forge)**, appelé par sa seule
fonction publique `generate_cv`. La dépendance est strictement
`stage_finder → cv_forge` : `cv_forge` ne connaît ni `stages.db`, ni la file.

### En ligne de commande — `cv_cli.py`

Tout ce que fait le bouton se pilote sans Flask, **par le même code** :

```powershell
python cv_cli.py etat                    # la file + la couverture en texte
python cv_cli.py sans-texte              # ce qui bloque, et ce qui est récupérable
python cv_cli.py enfiler <cle|préfixe>   # demande un CV pour UNE offre
python cv_cli.py batch                   # les mieux classées, puis consomme
python cv_cli.py travailler              # worker continu (Ctrl+C)
python cv_cli.py travailler --une-passe  # vide la file puis rend la main
python cv_cli.py orphelins --purger      # jobs dont l'offre a disparu
```

#### `batch` — la fournée

```powershell
python cv_cli.py batch --limite 12          # défaut : les 12 mieux classées
python cv_cli.py batch --enfiler-seulement  # enfile sans consommer
```

Il **n'appelle ni `generate_cv` ni `cv_forge`** : il pose des lignes dans la file
avec le même point d'entrée que le bouton, puis consomme avec le même worker.
L'idempotence lui vient donc gratuitement — une offre déjà générée sous le même
`offer_hash` (texte + empreinte du master + version de config) retrouve son job
et **n'est pas régénérée**.

**Qui porte le worker ?** Le batch *tente* de prendre le verrou de worker et se
règle sur le résultat :

| Situation | Ce qui se passe |
|---|---|
| L'app web ne tourne pas | Le batch prend le verrou, **consomme lui-même**, décharge le modèle à la vidange. |
| L'app web tourne | Le verrou est déjà pris : le batch **enfile et s'arrête**. L'autre worker traitera. |

La décision se prend à l'exécution, d'après ce qui tourne. Le verrou est une
transaction `BEGIN EXCLUSIVE` tenue ouverte sur `stages.db.worker-lock` : il est
relâché par l'OS à la mort du processus, donc un worker tué ne laisse rien
derrière lui. Ollama local ne supporte pas deux générations concurrentes.

#### `sans-texte` — pourquoi un bouton peut être grisé

Le CV se construit à partir du **texte brut de l'annonce**, stocké dans
`offres_texte`. Ce texte est persisté **au scrape** : toute offre revue en ligne
repart avec le sien. Une offre qui a disparu des sources, elle, garde une ligne
dans `offres` mais plus de texte récupérable — son bouton reste `TEXT_MISSING`.

```powershell
python cv_cli.py sans-texte          # les 25 premières
python cv_cli.py sans-texte --tout   # la liste complète
```

La sortie sépare le **récupérable** (revu au dernier run → pourvu au prochain
scrape) du **perdu** (plus revu depuis : son texte n'existe plus nulle part).
La commande **ne supprime rien** : une annonce republiée à l'identique se
rattache toute seule à sa ligne, la clé étant dérivée du contenu.

### Tester une source isolément
```powershell
python -m sources.adzuna
python -m sources.jooble
python -m sources.france_travail
python -m sources.careerjet
python -m sources.free_work
python -m sources.jobspy_source
python -m ranker            # démo du ranking sur 2 offres factices
python -m verifier          # démo du verdict LLM (nécessite Ollama)
```

### Évaluer et régler le ranking
Le réglage des poids/modèle n'est plus « à l'aveugle » : on mesure.
```powershell
python config_schema.py                    # valide config.py (fail-fast)
python etiqueter.py                        # étiquette des offres réelles de stages.db
python evaluer_ranking.py                  # precision@10 / nDCG@10 / rang médian
python benchmark.py                        # compare plusieurs modèles d'embedding
python -m pytest tests/ -q                 # suite de tests (fixtures figées)
```
Le jeu de référence est constitué **sur les vraies offres de `stages.db`**, pas
sur des annonces inventées : `etiqueter.py` les présente à l'aveugle (ni source,
ni score, ni rang) dans un ordre mélangé à graine fixe, et enregistre un
jugement binaire. La vérité vit dans `etiquettes.json`, versionné ; la table
`etiquettes` n'en est qu'un cache, reconstructible par `--import`.

`evaluer_ranking.py` classe **le corpus étiqueté seul** avec le ranking cosinus
(sans LLM, donc strictement déterministe) et rend quatre chiffres :
`precision@10`, `nDCG@10`, rang médian des positives, nombre de négatives dans
le top-10 — plus la ventilation du corpus par source et par longueur de texte,
sans laquelle les chiffres ne se lisent pas. Deux exécutions consécutives
produisent une sortie identique octet pour octet.

---

## 🗂️ Structure du projet

```
job-finder/
├── .env.example            # modèle de clés API (à copier en .env)
├── requirements.txt        # dépendances
├── config.py               # critères de recherche + requête de référence
├── config_schema.py        # validation pydantic de config.py (fail-fast)
├── console.py              # force UTF-8 sur la console Windows (badge ★, emoji)
├── sources/
│   ├── __init__.py         # truststore + masquage secrets + RedactingFilter
│   ├── registry.py         # catalogue des sources (1 source = 1 ligne)
│   ├── adzuna.py           # source API Adzuna
│   ├── jooble.py           # source API Jooble
│   ├── france_travail.py   # source API France Travail (OAuth2, gisement FR)
│   ├── romeo.py            # ROMEO v2 : intitulé libre -> code métier ROME
│   ├── marche_travail.py   # API Marché du travail : indicateur de tension
│   ├── careerjet.py        # source API Careerjet (méta-moteur, sans clé)
│   ├── free_work.py        # source API Free-Work (job board tech FR, sans clé)
│   └── jobspy_source.py    # source JobSpy (Indeed / LinkedIn / Google)
├── normalize.py            # schéma commun + normaliseurs par source
├── filters.py              # filtres durs (stage + Île-de-France)
├── extract.py              # extraction durée / date de début (signaux souples)
├── dedup.py                # dédup exacte (hash) + floue (embeddings)
├── ranker.py               # ranking sémantique (multi-profils, composition du score)
├── storage.py              # persistance SQLite (cache, nouveautés, historique)
├── report.py               # exports CSV + HTML (filtrable)
├── evaluation.py           # métriques pures : precision@k, nDCG@k, rang médian
├── etiqueter.py            # corpus étiqueté de référence (CLI à l'aveugle)
├── evaluer_ranking.py      # mesure le ranking sur le corpus étiqueté
├── etiquettes.json         # LE corpus — source de vérité, versionnée
├── reference.py            # instantané figé de la base + estampille de config
├── feedback.py             # journal append-only des retours sur les offres
├── benchmark.py            # comparaison de modèles d'embedding
├── verifier.py             # vérification LLM locale (Ollama), verdict explicable
├── market.py               # indicateurs « le marché est-il favorable ? »
├── main.py                 # orchestrateur du pipeline (collecte parallélisée)
├── app.py                  # app web locale (Flask) : « Tout lancer », analyses IA
├── jobs.py                 # file de génération de CV + texte des offres
├── worker.py               # consommateur UNIQUE de la file (verrou inter-processus)
├── ollama_pool.py          # jeton d'appel LLM + déchargement du modèle
├── titre_offre.py          # nettoyage du titre pour l'en-tête du CV
├── cv_cli.py               # pilotage de la file sans Flask (batch, sans-texte…)
├── static/cv_etats.js      # machine à états du bouton « Générer CV »
├── tests/                  # suite pytest + fixtures JSON par source
└── README.md
```

> `filters.py`, `extract.py`, `storage.py`, `report.py`, `evaluation.py` isolent
> chaque responsabilité pour garder `main.py` mince et chaque brique testable
> seule.
>
> Côté CV, `jobs.py` ne connaît QUE la file — ni `cv_forge`, ni Flask, ni le
> worker — et `worker.py` est le seul module à importer `cv_forge`, dont il ne
> touche qu'une fonction : `generate_cv`.
>
> Côté jugements, **deux pools qui ne se mélangent jamais** : `etiqueter.py`
> étiquette À L'AVEUGLE et sert à MESURER le ranking ; `feedback.py` recueille
> des retours EN VOYANT le classement et servira à l'AMÉLIORER. Chaque
> étiquette porte une `provenance`, et `evaluer_ranking.charger_corpus` REFUSE
> de mesurer un corpus qui en contiendrait une d'origine in-app —
> `tests/test_etancheite.py` échoue si la barrière saute.

---

## 🔐 Confidentialité & bonnes pratiques

- Le **ranking est 100 % local** : ni ton profil, ni les offres ne sortent de ta
  machine (aucune clé API, aucun appel externe pour les embeddings).
- Les clés API sont lues depuis `.env` (à **ne pas** committer) et **masquées**
  dans les logs.
- JobSpy est utilisé **sans compte**, avec des délais entre requêtes pour rester
  respectueux des sites.
