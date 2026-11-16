# AlphaForge AI — README (français)

**Équipe 18 :** Kevin Xu, Jeremy Vong, Sheng Hege, Song Ning Lan

AlphaForge AI est un **simulateur** agentique d'analyse de placements, capable d'expliquer lui-même ses résultats, conçu pour le PolyFinances Datathon 2026 (Banque Nationale × AWS). Pour un profil d'investisseur fictif, il :

- évalue dans quelle mesure chacun des 31 actifs convient au profil (« Profile-Fit Score » — score d'adéquation au profil, de 0 à 100, neuf composantes);
- compare le portefeuille à une cible propre au profil sur un radar à neuf axes et propose des étapes de rééquilibrage avec leur effet simulé (« what-if »);
- vérifie toute position proposée au regard des contraintes du profil et **conteste** les demandes contradictoires (p. ex. les « 30 % dans Tesla » de Robert);
- répond aux questions en texte libre (« Ask AlphaForge ») au moyen d'un planificateur (mode local) ou d'un agent Amazon Bedrock AgentCore (mode agent) qui ne peut utiliser que des chiffres produits par des outils déterministes — chaque chiffre d'une réponse est vérifié par rapport aux sorties des outils.

Chaque résultat est accompagné de ses preuves (valeur, source, période), de ses hypothèses, de ses limites, de son niveau de confiance ainsi que des étapes et appels d'outils qui l'ont produit.

> **Simulation seulement.** Profils et portefeuilles fictifs, données publiques, aucune transaction réelle, pas un conseil financier personnalisé.

---

## 1. Structure du dépôt

```
workshop/
  README.md                       ce fichier (version anglaise)
  README_FR.md                    ce fichier (version française)
  TECHNICAL_DOCUMENTATION.pdf     documentation technique
  TECHNICAL_DOCUMENTATION_FR.pdf  documentation technique (version française)
  ONE_PAGER.pdf                   résumé d'une page
  ONE_PAGER_FR.pdf                résumé d'une page (version française)
  alphaforge_starter/             CSV du jeu de données de départ + build_dataset.py
  alphaforge-ai/                  l'application (les chemins ci-dessous sont relatifs à ce dossier)
alphaforge-ai/
  backend/            paquet Python « alphaforge » (bibliothèque standard seulement à l'exécution)
    alphaforge/
      data/           chargeur de lot (fixture | dir: | s3://), données fictives provisoires, corrélations de repli
      features/       moteur de variables : rendements, risque, indicateurs techniques, GARCH, régimes HMM, Hurst, facteurs, données fondamentales, nouvelles
      ingest/         cours/change/taux en direct, SEC EDGAR (dépôts, XBRL, Form 4), RSS des banques centrales, nouvelles de sources autorisées
      monitoring/     fréquences des sources (EventBridge Scheduler), alertes
      profiles.py     bandes de risque, plafonds, cibles du radar, profils types prédéfinis, validation des profils
      profile_features.py  variables de l'investisseur : tolérances, besoins, pondérations des composantes
      scoring.py      Profile-Fit Score : 9 composantes, seuils bloquants, niveau de confiance, test de stabilité du classement
      governance.py   vérification des contraintes pour une position ou une répartition proposée, solutions de rechange
      radar.py        radar du portefeuille à neuf axes par rapport à la cible
      suggestions.py  suggestions fondées sur des règles avec effet simulé; meilleurs choix
      portfolio.py    pondérations, volatilité fondée sur la covariance, contributions au risque, reconstitution de crises
      reasoning.py    planificateur local : intentions, collecte de preuves, citations, étapes de raisonnement
      ask.py          Ask : mode local / mode agent AgentCore, garde-fous, vérification des chiffres
      agent_tools.py  les 14 outils exposés à l'agent (schéma Gateway généré à partir d'ici)
      api/            routeur et routes sans cadriciel
      handlers/       points d'entrée AWS Lambda (API, outils Gateway, ingestion)
    tests/            45 tests, sans réseau
  data_pipeline/      ETL des CSV de départ vers le lot organisé (+ 31 tests)
  frontend/           React 19 + Vite 8 + Tailwind 4 + Recharts 3
  agents/             invite système du conseiller, schéma d'outils Gateway, configuration du harness, notes de conception de l'agent
  infra/iam/          modèles de politiques IAM à privilège minimal
  scripts/            empaquetage Lambda, génération de la configuration du harness
  docs/               guide de déploiement AWS, contrat de données, suivi des livrables
```

---

## 2. Dépendances

| Composant | Exigence | Remarques |
|---|---|---|
| Exécution du backend | **Python ≥ 3.12** | `backend/pyproject.toml` : `dependencies = []` — bibliothèque standard seulement. |
| Tests du backend | `pytest >= 8` | extra optionnel `dev`. |
| Code AWS | `boto3 >= 1.44` | extra optionnel `aws`; importé à la demande, seulement pour S3, DynamoDB, AgentCore, Bedrock Guardrails, Comprehend, EventBridge. Fourni par l'environnement d'exécution Lambda / intégré par `scripts/package_lambda.sh`. |
| Pipeline de données | Python (bibliothèque standard) | importe `backend/alphaforge.features`; utilisez donc le même Python ≥ 3.12. |
| Frontend | **Node.js 20.19+ ou 22.12+** (exigé par Vite 8) | react ^19.3, react-router-dom ^7.18, recharts ^3.10, vite ^8.3, tailwindcss ^4.3. |
| Reconstruction du jeu de données de départ (optionnelle) | `pandas`, `yfinance` | seulement pour `alphaforge_starter/build_dataset.py`; les CSV sont déjà fournis. |

Aucune clé d'API n'est stockée nulle part. L'accès à AWS passe par la chaîne d'identifiants par défaut (SSO, `aws login` ou un rôle IAM).

---

## 3. Sources de données

| Source | Données utilisées | Période | Conditions |
|---|---|---|---|
| **Jeu de données de départ** (`alphaforge_starter/`, fourni par les organisateurs) | 31 actifs (10 actions canadiennes, 10 actions américaines, 4 FNB d'actions dont un avec filtrage ESG, 4 FNB d'obligations, 1 FNB d'or, Bitcoin, Ether) | 2021-11-12 → 2026-11-12, extrait le 2026-11-13 | voir `alphaforge_starter/sources_metadata.csv` |
| ↳ Yahoo Finance au moyen de `yfinance` | `prices_daily.csv` (40 049 lignes : OHLC, clôture, clôture ajustée, volume, devise); `corporate_actions.csv` (dividendes, fractionnements) | quotidienne | conditions d'utilisation de Yahoo |
| ↳ API Valet de la Banque du Canada | `fx_usdcad_daily.csv` (FXUSDCAD); taux directeur V39079, rendements des obligations du GdC à 2 ans et 10 ans, IPC sur 12 mois | quotidienne / mensuelle | conditions de la Banque du Canada |
| ↳ FRED (Fed de St. Louis) | DFF, DGS2, DGS10, CPIAUCSL, UNRATE | quotidienne / mensuelle | conditions juridiques de FRED |
| **Métadonnées de l'équipe** (`backend/alphaforge/data/fixtures/assets.json`, constituées à la main) | groupe d'actifs, région, étiquettes (p. ex. `high_profile`, `established`, `dividend`), indicateur défensif, indicateurs ESG sectoriels (`fossil_fuels` pour ENB.TO, CNQ.TO, XOM; `unscreened_index` pour XIU.TO, SPY, QQQ) | statique | hypothèse de l'équipe — pas une cote ESG d'un tiers |
| **Ingestion en direct optionnelle** (réseau) | API de graphiques Yahoo (cours postérieurs à la fenêtre de départ, raccordés aux séries de départ); mises à jour des taux et du change de la BdC et de FRED | après le 2026-11-12 | comme ci-dessus |
| ↳ SEC EDGAR | soumissions, faits XBRL des sociétés (données fondamentales, valeurs du premier dépôt), éléments 8-K, transactions d'initiés Form 4 — 18 symboles inscrits auprès de la SEC (ATD.TO et CSU.TO sont seulement sur SEDAR+) | depuis le 2021-11-01 | politique d'accès équitable de la SEC (User-Agent avec coordonnées exigé) |
| ↳ RSS de la Réserve fédérale et de la Banque du Canada | communiqués de politique monétaire | en continu | publiques |
| ↳ GDELT DOC 2.0 / Google News RSS | **titre, éditeur, date et lien seulement** (aucun texte d'article), limités à une liste de 20 éditeurs autorisés; le titre doit nommer la société | en continu + rattrapage autour des fortes variations passées | conditions respectives |
| ↳ Amazon Comprehend (optionnel) | sentiment des titres au lieu du lexique intégré | — | AWS |

Sans ingestion, les données fondamentales et les nouvelles sont signalées comme **manquantes, avec une raison** (jamais complétées artificiellement), et les composantes de score correspondantes sont exclues.

La méthode de traitement complète se trouve dans `alphaforge-ai/docs/data_contract.md` et dans `TECHNICAL_DOCUMENTATION.pdf` (§4) — en français : `TECHNICAL_DOCUMENTATION_FR.pdf` (§4).

---

## 4. Configuration

Les paramètres du backend sont des variables d'environnement (`backend/alphaforge/config.py` et les modules indiqués).

| Variable | Valeur par défaut | Rôle |
|---|---|---|
| `ALPHAFORGE_DATA_SOURCE` | `fixture` | `fixture` (indicateurs provisoires, niveau de confiance forcé à faible), `dir:<absolute path>` ou `s3://bucket/prefix` (lot organisé) |
| `ALPHAFORGE_DATA_TTL_SECONDS` | `900` | durée de réutilisation d'un lot chargé avant sa relecture (pour que les mises à jour de l'ingestion atteignent l'API) |
| `ALPHAFORGE_DEFAULT_WINDOW` | `3y` | fenêtre des indicateurs (`3y` ou `5y`; une fenêtre manquante entraîne un repli, accompagné d'une note) |
| `ALPHAFORGE_STORAGE` | `local` | `local` (fichiers JSON) ou `dynamodb` |
| `ALPHAFORGE_LOCAL_STORE_DIR` | `backend/.localdata` | dossier du stockage JSON local |
| `ALPHAFORGE_TABLE_PREFIX` | `alphaforge-` | préfixe des noms de tables DynamoDB |
| `ALPHAFORGE_AUTH_MODE` | `header` | `header` fait confiance à `X-User-Id` (développement seulement); `cognito` lit les revendications JWT transmises par API Gateway — **réglez-la à `cognito` sur Lambda** |
| `AGENTCORE_HARNESS_ARN` | non définie | si elle est définie, Ask utilise le conseiller AgentCore; sinon, le planificateur local |
| `ALPHAFORGE_AGENT_TOOLS` | `inline` | `inline` : les outils sont envoyés avec chaque appel `InvokeHarness` et exécutés dans l'API; `gateway` : le harness appelle ses outils AgentCore Gateway |
| `AGENTCORE_MODEL_ID` | non définie | modèle Bedrock / profil d'inférence remplaçant le modèle du harness |
| `AGENTCORE_TIMEOUT_SECONDS` | `120` | limite d'exécution du harness par appel |
| `BEDROCK_GUARDRAIL_ID` / `BEDROCK_GUARDRAIL_VERSION` | non définie / `DRAFT` | garde-fou d'utilisation responsable appliqué à la question et à la réponse |
| `ALPHAFORGE_SCHEDULER` | `local` | `eventbridge` applique les changements de fréquence des sources à EventBridge Scheduler |
| `ALPHAFORGE_SCHEDULER_GROUP` / `_PREFIX` | `alphaforge-ingest` / `alphaforge-ingest-` | groupe et préfixe de nom des planifications EventBridge |
| `ALPHAFORGE_HTTP_USER_AGENT` | valeur provisoire | User-Agent des requêtes d'ingestion (SEC EDGAR exige une organisation et une adresse courriel de contact) |
| `ALPHAFORGE_SENTIMENT` | lexicon | `comprehend` évalue le sentiment des titres avec Amazon Comprehend |
| `ALPHAFORGE_STALE_DAYS` | `10` | le niveau de confiance baisse lorsque les données de marché sont plus anciennes que ce nombre de jours |

Frontend (Vite) : `VITE_API_BASE` (URL de l'API en production), `VITE_AUTH_MODE` (`header` | `cognito`), `VITE_BASE` (préfixe du mandataire de chemin), `VITE_ALLOWED_HOST`.

---

## 5. Installation et reproduction

Les commandes sont données pour macOS/Linux (bash) et Windows (PowerShell). Partez de la racine du dépôt (`workshop/`) et exécutez d'abord `cd alphaforge-ai`; tous les chemins ci-dessous sont relatifs à `alphaforge-ai/`. Le jeu de données de départ est déjà en place dans `workshop/alphaforge_starter/`, là où le pipeline et ses tests le cherchent par défaut.

### 5.1 Backend + tests (sans réseau)

```bash
# bash
cd backend
python3.12 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest                       # 45 tests
.venv/bin/python -m pytest ../data_pipeline/tests # 31 tests (construit le lot à partir des CSV de départ dans un dossier temporaire)
```

```powershell
# PowerShell
cd backend
py -3.12 -m venv .venv; .\.venv\Scripts\pip install pytest
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m pytest ..\data_pipeline\tests
```

Résultat attendu : `45 passed` et `31 passed` (vérifié le 2026-11-15 avec Python 3.14).

### 5.2 Construire le lot organisé à partir des données de départ

```bash
python data_pipeline/build_bundle.py            # optionnel : --starter DIR --out DIR ; sortie par défaut = data_pipeline/out
```

Sorties (chacune avec sa traçabilité : SHA-256 de la source, heure de génération, version de la transformation) : `assets.json`, `asset_metrics.json` (fenêtres de 3 ans et de 5 ans en CAD, y compris le rendement de crise de l'année civile 2022), `correlations.json`, `macro_snapshot.json`, `macro_series.json`, `fx_usdcad.json`, `dividends.json`, `data_quality_report.json`, `prices/<ticker>.json` et `asset_features.json`. La construction échoue de façon explicite en cas de doublons, de symboles inconnus, de cours non positifs ou de devises incohérentes.

### 5.3 Exécuter l'application localement

```bash
# terminal 1 — API sur http://127.0.0.1:8000, avec le lot réel
cd backend
ALPHAFORGE_DATA_SOURCE=dir:$(cd ../data_pipeline/out && pwd) .venv/bin/python -m alphaforge.devserver
```

```powershell
# équivalent PowerShell
cd backend
$env:ALPHAFORGE_DATA_SOURCE = "dir:" + (Resolve-Path ..\data_pipeline\out)
.\.venv\Scripts\python -m alphaforge.devserver
```

```bash
# terminal 2 — frontend sur http://127.0.0.1:5173 (redirige /api vers :8000)
cd frontend
npm install
npm run dev
```

Sans `ALPHAFORGE_DATA_SOURCE`, l'API utilise les **données fictives provisoires** (« placeholder fixture ») : l'interface affiche une bannière et chaque niveau de confiance est « faible ».

Si vous accédez au serveur de développement par un mandataire de chemin (par exemple code-server), lancez le frontend avec `VITE_BASE=/absproxy/5173/ npm run dev`; s'il tourne sous un autre nom d'hôte, ajoutez `VITE_ALLOWED_HOST=<host>`.

Utilisez le sélecteur **Viewing as** pour passer d'Amira à Mateo et à Robert (chacun doté d'un portefeuille fictif conçu pour illustrer une tension différente) ou choisissez **My own profile** pour ouvrir l'assistant de création de profil. La question de démonstration de Robert est « Should I put 30% of my portfolio in Tesla? » (« Devrais-je mettre 30 % de mon portefeuille dans Tesla? »).

### 5.4 Optionnel : données en direct, dépôts réglementaires, données fondamentales et nouvelles (réseau)

```bash
cd backend
export ALPHAFORGE_HTTP_USER_AGENT="AlphaForge-AI datathon <contact-email>"   # SEC EDGAR exige un contact
.venv/bin/python -m alphaforge.ingest.run --bundle ../data_pipeline/out \
    --jobs market_data,sec_edgar,official_rss,gdelt                         # ~5 min
.venv/bin/python -m alphaforge.ingest.run --bundle ../data_pipeline/out \
    --jobs news_backfill --max-queries 400                                  # ~10 min, peut être repris
```

La tâche `features` s'exécute toujours en dernier et reconstruit `asset_features.json`. Chaque exécution écrit `ingest_status.json`.

### 5.5 Optionnel : Ask avec le véritable agent AgentCore

```bash
cd backend
.venv/bin/pip install boto3
AGENTCORE_HARNESS_ARN=arn:aws:bedrock-agentcore:<region>:<account>:harness/<id> \
AGENTCORE_MODEL_ID=<inference-profile-id> \
ALPHAFORGE_DATA_SOURCE=dir:$(cd ../data_pipeline/out && pwd) \
.venv/bin/python -m alphaforge.devserver
```

Vos identifiants doivent disposer de `bedrock-agentcore:InvokeHarness` sur le harness, et le rôle du harness doit avoir accès au modèle. Dans le mode d'outils par défaut, `inline`, le harness se met en pause à chaque appel d'outil; l'API exécute l'outil sur le profil de l'utilisateur connecté, puis reprend la même session. Chaque appel apparaît dans l'onglet **Trace** de la réponse, et les chiffres de la réponse sont vérifiés par rapport à ces sorties.

### 5.6 Déploiement sur AWS

Suivez `docs/aws_deployment.md` : S3 (lot organisé, frontend), DynamoDB (5 tables), Lambda (API, outils, ingestion), Cognito, API Gateway HTTP API, AgentCore Gateway + Harness, Bedrock Guardrails, CloudFront et EventBridge Scheduler. Les modèles IAM se trouvent dans `infra/iam/`. Utilisez `scripts/package_lambda.sh` pour construire le fichier zip Lambda et `scripts/render_harness_config.py` pour remplir `agents/harness_config.json`.

---

## 6. Comment les résultats sont produits (version abrégée)

- **Les chiffres proviennent de code déterministe, jamais du LLM.** En mode agent, chaque pourcentage, nombre décimal et score sur 100 de la réponse est comparé aux sorties des outils; les chiffres sans correspondance sont énumérés et le niveau de confiance passe à faible.
- **Profile-Fit Score** = somme pondérée de neuf composantes (adéquation à la volatilité, adéquation aux pertes et au rétablissement, rendement ajusté au risque, qualité de l'entreprise, revenu, tendance et moment, adéquation aux préférences, contribution à la diversification, nouvelles et événements). Les pondérations proviennent du profil; les composantes non applicables sont retirées et leur poids est redistribué. Seuils bloquants : les actifs exclus sont plafonnés à 20; une adéquation à la volatilité ou aux pertes inférieure à 30 plafonne le score à 45.
- **Stabilité du classement** : le classement est recalculé 300 fois, chaque pondération étant multipliée par un facteur aléatoire dans l'intervalle [0,75 ; 1,25].
- **Les seuils sont des hypothèses explicitement identifiées** (bandes, tolérances, plafonds, pondérations, cibles) et sont retournés avec chaque résultat.
- **Les données manquantes abaissent le niveau de confiance au lieu d'être complétées artificiellement.**

Consultez `TECHNICAL_DOCUMENTATION.pdf` (dans ce dossier) — ou sa version française, `TECHNICAL_DOCUMENTATION_FR.pdf` — pour la méthode complète, un exemple détaillé de bout en bout et les preuves de validation.

---

## 7. État d'avancement et lacunes connues

| Partie | État |
|---|---|
| Pipeline de données, moteur de variables, notation, radar, suggestions, gouvernance, reconstitution de crises, Ask local | Fonctionnel, testé (76 tests) |
| Tâches d'ingestion (EDGAR, nouvelles, cours en direct) | Fonctionnelles en local; gestionnaire Lambda prêt |
| Mode agent AgentCore (outils inline et Gateway), garde-fous, vérification des chiffres | Code et configuration prêts; testé avec un harness simulé |
| Déploiement AWS | Guide, modèles IAM et scripts d'empaquetage prêts (voir `docs/aws_deployment.md`) |
| Écran de connexion Cognito dans le frontend | Non réalisé (volet API terminé) |
