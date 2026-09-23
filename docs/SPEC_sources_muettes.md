# Spec — Les deux plus grosses sources françaises ne rapportent rien au mode stage

> **Statut : diagnostic, pas d'implémentation.** Ce document mesure et explique.
> Il ne propose pas de code et n'en écrit aucun. Il existe pour qu'une décision
> soit prise en connaissance de cause : ce chantier passe-t-il devant
> `job_etudiant`, ou après ?

## Contexte

`CP-J-obs` a doté la collecte d'un bilan par source (`observabilite.py`). À son
tout premier run réel, le tableau a montré ceci — **en mode stage, celui qui est
la priorité du projet** :

```
source             brut  normal.  gardées   détail
careerjet           224      224       70   exclu 2, hors-IDF 24, hors-stage 59, trop-vieux 69
jooble               86       86        0   hors-IDF 2, hors-stage 84
free_work             6        6        2   hors-IDF 1, hors-stage 1, trop-vieux 2
adzuna                2        2        2
france_travail        0        0        0
```

France Travail : **0 offre rendue**. Adzuna : **2**. Aucun incident réseau, donc
aucune panne — les deux sources répondent normalement et ne rapportent rien.

Le pipeline de stages tourne donc, depuis le 2026-07-14, sur **Careerjet et
JobSpy seuls**. La base le confirme sur toute l'historique :

| source | offres en base (2026-07-14 → 09-01) |
|---|---:|
| jobspy:linkedin | 252 |
| jobspy:indeed | 187 |
| careerjet | 122 |
| adzuna | **6** |
| free_work | 2 |
| france_travail | **1** |
| jooble | **0** |

Sept semaines, 1 offre France Travail et 6 Adzuna. Personne ne l'a vu parce que
rien ne l'affichait — c'est le même défaut de fond que Jooble, sur deux sources
de plus.

Et le corpus étiqueté hérite du biais : ses 57 étiquettes viennent de
careerjet (24), jobspy:linkedin (19), jobspy:indeed (12) et free_work (2).
**Aucune de France Travail ni d'Adzuna.** La baseline de ranking mesure donc la
qualité du classement sur un gisement amputé de ses deux plus grosses sources
françaises, sans que le rapport le dise.

---

## Ce qui a été mesuré

Toutes les mesures ci-dessous sont des appels réels aux API, en Île-de-France,
le 2026-09-05, avec `config.JOURS_FRAICHEUR = 7` sauf mention contraire.

### France Travail — recherche par mots-clés

Ce que fait l'adaptateur aujourd'hui (`_chercher_un_terme`, paramètre `motsCles`) :

| terme de `config.TERMES_RECHERCHE` | offres IdF / 7 j |
|---|---:|
| stage intelligence artificielle | **0** |
| stage machine learning | **0** |
| stage cybersécurité | **0** |
| stage data science | **0** |
| internship AI security | **0** |
| **total** | **0** |

Ce n'est pas une panne : le jeton OAuth2 est valide, l'API répond 204 « aucune
offre ». `market.py` documentait déjà le phénomène (« "stage cybersécurité" en
plein texte remonte 0 offre sur 31 jours, le ROME correspondant M1856 en remonte
9 ») — mais uniquement pour le COMPTAGE du tableau de bord. Personne n'avait
rapproché cette note du chemin de RÉCUPÉRATION, qui souffre exactement du même
mal.

### France Travail — recherche par code ROME

Le référentiel que `market.py` utilise déjà, appliqué cette fois à la
récupération :

| code | libellé | tous contrats / 7 j | avec `typeContrat=MIS` / 7 j | / 31 j |
|---|---|---:|---:|---:|
| M1405 | Data scientist | 14 | 4 | 5 |
| M1856 | Expert en cybersécurité | 11 | 1 | 3 |
| M1882 | Architecte sécurité informatique | 7 | 0 | 0 |
| M1889 | Ingénieur en Intelligence Artificielle | 5 | 0 | 3 |

Le ROME rapporte donc **37 offres tous contrats sur 7 jours**, là où les
mots-clés en rapportent 0. Le mécanisme est disponible : `sources/romeo.py`
résout déjà un intitulé libre en code ROME, `config.ROME_REPLI` fournit un repli
hors ligne, et `france_travail.compter_offres` sait déjà passer `codeROME`.
Seul `_chercher_un_terme` ne le fait pas.

### ⚠ `TYPE_CONTRAT_STAGE = "MIS"` ne sélectionne pas des stages

Vérifié sur M1405, 31 jours, IdF — les 5 offres que ce filtre rapporte :

```
typeContratLibelle : {'Intérim - 18 Mois': 3, 'Intérim - 3 Mois': 2}
natureContrat      : {'Contrat travail': 5}
alternance         : {False: 5}

   Data scientist (F/H)                     | Intérim - 18 Mois
   Technicien Data Center (H/F)             | Intérim - 18 Mois
   Data Scientist IA - (H/F)                | Intérim - 3 Mois
   DATA SCIENTIST (H/F)                     | Intérim - 18 Mois
   Annonce générique - - Data Scientist     | Intérim - 3 Mois
```

`MIS` = **mission d'intérim**. Pas un stage, pas un seul sur cinq.

Le commentaire de `sources/france_travail.py:187` affirme : « Vérifié à la
mesure : sur M1856, `typeContrat=MIS` remonte 9 stages sur 31 jours là où
`natureContrat=E2` en remonte 0. » La mesure était bonne, sa lecture ne l'était
pas : le filtre remontait 9 **offres**, qui n'étaient pas des stages. Et `E2`
n'est pas non plus le code du stage — vérifié, il rend « Alternance Data
Scientist / IA Engineer », c'est-à-dire l'apprentissage.

**Conséquence hors périmètre de ce document, mais à traiter :** la colonne
« stages » du tableau de bord marché (`market.py`, appels
`compter_offres(..., stages_seulement=True)`) compte des missions d'intérim
depuis toujours. C'est un second défaut silencieux, de la même famille, dans un
autre module.

### France Travail porte-t-il seulement des stages ?

Question décisive pour estimer le gain, et la réponse est gênante. Sur **233
offres** rapatriées en IdF (`motsCles` = « stage », « stagiaire », « ingénieur ») :

```
   180  Contrat travail
    34  Contrat apprentissage
    14  Cont. professionnalisation
     2  Emploi non salarié
     2  CDI de chantier ou d'opération
     1  Contrat d'usage
```

**Aucune nature « stage ».** Elle n'existe pas dans ce gisement. Les stages que
France Travail publie sont déclarés en CDD, voire en CDI, avec le mot « stage »
uniquement dans l'intitulé :

```
Contrôleur(e) / Auditeur(trice) Conformité (STAGE 6 MOIS)  | CDD - 6 Mois | Contrat travail
Offre générique - Stage fin d'études/césure (22 semaines)  | CDI          | Contrat travail
Stagiaire Chargé du Recrutement (h/f)                      | CDD - 6 Mois | Contrat travail
```

Volume réel, tous métiers confondus, IdF, **31 jours** : 23 titres contenant
« stage » hors alternance sur la requête « stage », 35 sur « stagiaire ». Pour la
niche IA/cybersécurité spécifiquement, sur 7 jours : proche de zéro.

### Adzuna — pourquoi 2 offres

Trois causes qui se composent, isolées par ablation sur le terme témoin
« stage intelligence artificielle » :

| requête | `count` |
|---|---:|
| `what` + `where=Paris` + `max_days_old=7` — **ce que fait l'adaptateur** | **0** |
| `what` + `where=Paris`, sans fenêtre de fraîcheur | 4 |
| `what` seul, sans `where` ni fenêtre | 43 |
| `what_phrase` + `where=Paris` | 2 |
| `what_or` + `where=Paris` | 7 554 |
| `what='stage'` + `where=Paris` | 5 204 |
| `what='stage'` + `where=Paris` + `max_days_old=7` | **1 267** |

1. **`what` est conjonctif.** Adzuna exige TOUS les mots. « stage intelligence
   artificielle » demande les trois, dans une annonce française qui écrira
   plutôt « Stage — Data Scientist (H/F) ». Les cinq termes de
   `config.TERMES_RECHERCHE` font tous 3 à 4 mots.
2. **La fenêtre de 7 jours achève le peu qui reste** : 4 → 0.
3. Ce n'est **ni un filtre de catégorie ni un marché vide** : la même requête
   réduite à un seul mot rapporte 1 267 offres dans la même fenêtre et la même
   ville.

Détail des 5 termes actuels : seul « stage cybersécurité » rapporte quoi que ce
soit (2 offres), les quatre autres rendent 0.

### Ce que je n'ai pas pu mesurer

Le rendement d'une requête Adzuna corrigée **après passage des filtres durs**.
Le quota de l'API a été épuisé pendant la mesure : Adzuna répond alors `400`
(et non `429`), y compris sur des requêtes qui fonctionnaient une minute plus
tôt. J'ai arrêté plutôt que d'insister.

Ce qui reste à mesurer, quand le quota est reconstitué :
`what='stage'`, `where=Paris`, `max_days_old=7`, 3 pages → `filters.filtrer` →
combien de survivantes, dont combien portent un tag IA/cyber. Sans ce chiffre,
l'estimation ci-dessous reste une estimation.

*(Note d'exploitation : ce `400` d'étranglement est désormais capté par
`observabilite` en `http: HTTP 400`. Avant CP-J-obs, il devenait `[]`.)*

---

## Ce que chaque correctif rapporterait

| # | Correctif | Gain estimé (mode stage) | Coût | Confiance |
|---|---|---|---|---|
| 1 | Adzuna : requêtes larges (mots simples) au lieu de phrases conjonctives | **Élevé** — 1 267 offres dans la fenêtre, contre 2 | Faible | Volume mesuré ; rendement après filtres **non mesuré** |
| 2 | France Travail : récupération par `codeROME` au lieu de `motsCles` | **Faible** — 37 offres tous contrats / 7 j sur les 4 codes, dont très peu de stages | Faible — la mécanique existe (`romeo.py`, `ROME_REPLI`, `compter_offres`) | Élevée |
| 3 | France Travail : cesser de filtrer sur `typeContrat=MIS` | Correction, pas volume — le filtre actuel sélectionne de l'intérim | Faible | Élevée |
| 4 | `market.py` : la colonne « stages » compte de l'intérim | Correction d'un chiffre faux affiché | Faible | Élevée |

Le déséquilibre est net : **Adzuna est le vrai gisement perdu, France Travail
ne l'est pas.** France Travail est structurellement pauvre en stages tech en
Île-de-France — non pas mal interrogé, mais dépourvu du contrat qu'on cherche.
Le corriger vaut pour la justesse (#3, #4), pas pour le volume.

Rappel utile pour arbitrer, mesuré au CP-J0 : **en mode `job_etudiant`, ces
deux sources s'inversent.** France Travail y rapporte 151 offres brutes sur 6
termes, et 1 962 offres à temps partiel en IdF sur 14 jours ; Adzuna 141. Le
diagnostic « ces sources sont muettes » est donc propre au mode stage, et ne
doit pas être généralisé.

---

## Points de conception à trancher

**`config.TERMES_RECHERCHE` est global.** Élargir les termes pour Adzuna les
élargit pour Careerjet, JobSpy et Jooble, qui n'ont pas la même sémantique de
recherche (Careerjet rapporte déjà 224 offres avec ces mêmes termes ; l'élargir
le noierait). Le correctif #1 suppose donc une **politique de requête par
source** — exactement la structure prévue par `Recherche` au CP-J1 (§4 de la
décision du 2026-09-05, « per-source policy, not a global flag »).

Deux voies, et elles n'ont pas le même ordonnancement :

- **A — corriger Adzuna maintenant**, avec un réglage propre à la source, avant
  `Recherche`. Rapide, mais pose un second endroit où vit une politique de
  requête, à réunifier ensuite.
- **B — attendre CP-J1**, où `Recherche` porte déjà la politique par source, et
  y traiter Adzuna comme un cas parmi d'autres. Plus propre, mais laisse le mode
  stage amputé le temps du chantier `job_etudiant`.

Les correctifs #3 et #4 sont indépendants de ce choix et peuvent être faits
séparément : ce sont des corrections de justesse, pas de volume.

---

## Non-objectifs

- **Aucune implémentation ici.** Aucun fichier de code n'est modifié par ce
  document.
- **Ne pas retirer France Travail des sources.** Elle est marginale en mode
  stage et centrale en mode `job_etudiant` : la juger sur un seul mode
  reproduirait l'erreur de lecture qui a produit `TYPE_CONTRAT_STAGE = "MIS"`.
- **Ne pas ré-étiqueter le corpus** en réaction à ce constat. Le biais de
  provenance est réel et il est noté ; le corriger est une décision de mesure,
  pas un correctif de collecte, et il touche `etiquettes.json` qui est la seule
  donnée non régénérable du projet.

---

## Décision demandée

1. Ce chantier passe-t-il **devant** `job_etudiant`, ou après ?
2. Si devant : voie **A** (correctif Adzuna isolé, tout de suite) ou voie **B**
   (attendre `Recherche` au CP-J1) ?
3. Les correctifs #3 et #4 (`typeContrat=MIS`), qui sont des corrections de
   justesse indépendantes, peuvent-ils être faits sans attendre cet arbitrage ?
4. Le biais de provenance du corpus étiqueté (0 étiquette sur 57 venant des deux
   sources) doit-il être inscrit dans le rapport de `evaluer_ranking`, pour que
   la baseline cesse de se présenter comme représentative ?

---

## Annexe — reproduire les mesures

Les sondes utilisées sont dans le répertoire de travail temporaire de la session
et ne sont pas versionnées (elles frappent les API réelles avec les clés du
`.env`). Chaque mesure du tableau se reproduit avec un appel direct :

```
France Travail   GET /v2/offres/search?region=11&motsCles=…&publieeDepuis=7&range=0-0
                 (le total est dans l'en-tête Content-Range)
                 …&codeROME=M1405   pour la variante ROME
Adzuna           GET /v1/api/jobs/fr/search/1?what=…&where=Paris&max_days_old=7
                 (le total est le champ "count")
```

Quota : Adzuna étrangle en `400` après quelques dizaines d'appels ; France
Travail impose un débit maximal, déjà respecté par `_respecter_debit()`.
