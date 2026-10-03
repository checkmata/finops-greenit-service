# CODE_OBSERVATIONS — Limites et écarts constatés dans le code (audit du MVP)

> **Document historique** : audit du MVP (commit `c34f8b7`), avant V1. Le statut de chaque point après V1 est dans [LIMITATIONS §3](LIMITATIONS.md#3-statut-des-limites-de-laudit-mvp).

> Retour : [PROJECT_GUIDE](PROJECT_GUIDE.md)
>
> Ce fichier liste ce que l'analyse a **observé** (aucune correction n'a été faite dans le code). Utile pour :
> 1. ne pas affirmer en entretien qu'une fonctionnalité marche alors qu'elle n'est pas branchée ;
> 2. avoir des exemples concrets de « difficultés / pistes d'amélioration » **issues du code**.
>
> 🟢 = vérifié par lecture **et** exécution · 🟡 = déduction (non exécuté : Docker indisponible pendant l'analyse)

## 1. Résultat des tests

🟢 `pytest` : **185 tests passent** (couverture 88 % du dossier `app/`), à une condition : installer `aiosqlite`, qui manque dans `requirements-dev.txt`. Sans lui, **22 tests** de `tests/integration/test_timescale_adapter_integration.py` échouent avec `ModuleNotFoundError: No module named 'aiosqlite'`.
🟢 `app/adapters/inbound/api/main.py` est à 0 % de couverture (le `lifespan` et le scheduler réel ne sont pas testés de bout en bout).

## 2. Écarts fonctionnels

| # | Constat | Preuve | Effet |
|---|---|---|---|
| 1 | Le scheduler n'analyse que le namespace `default` 🟢 | `run_snapshot_cycle()` : `if namespaces is None: namespaces = ["default"]` ; `main.py` ne passe pas `namespaces` | Les namespaces métiers (ex. `team-alpha`) ne sont jamais historisés automatiquement |
| 2 | `cost_usd` du snapshot vaut toujours 0 🟢 | `snapshot_job.py` : `pod.total_cost_usd if hasattr(pod, "total_cost_usd") else 0.0` ; `PodMetric` n'a pas cet attribut | L'historique de coût est vide |
| 3 | Plantage du repli qualité de données 🟢 | `DataQuality(healthy_sources=frozenset(TelemetrySource))` sans `timestamp` → `TypeError` (reproduit) | Si `check_data_quality()` lève une exception, tout le namespace est ignoré pour ce cycle |
| 4 | `/health` ne voit pas une panne Prometheus 🟢 | `check_data_quality()` attrape les erreurs réseau et renvoie un ensemble vide ; testé avec une URL injoignable → `"healthy"` | Les probes OpenShift ne détectent que les pannes de base |
| 5 | IDs factices dans `/api/recommendations/history` 🟢 | `placeholder_id = UUID(int=i)` | L'`id` renvoyé ne permet pas d'appeler le `PATCH` |
| 6 | Transitions de statut non contrôlées 🟢 | `validate_status_transition()` et `ALLOWED_TRANSITIONS` ne sont appelés nulle part | On peut passer de `executed` à `pending` |
| 7 | Énergie idle toujours 0 🟢 | `idle_energy_kwh=0.0  # P1 metric — not queried yet` | Les recommandations `ENERGY_WASTE` ne peuvent jamais être générées hors tests |
| 8 | Coût de stockage jamais calculé 🟢 | `get_storage_metrics()` n'est appelé par aucun router ni par le scheduler | FinOps limité au CPU et à la mémoire |
| 9 | Économies estimées additionnées deux fois 🟢 | Reco CPU et mémoire utilisent chacune `wasted_cost_usd × (1 − ratio)` où `wasted_cost_usd` est le gaspillage **total** (CPU+mémoire). Exemple exécuté : gaspillage total 34,56 $ mais économies CPU 24,19 $ + mémoire 25,92 $ = 50,11 $ | `total_estimated_savings_usd` peut dépasser le gaspillage réel |
| 10 | Nom trompeur `cpu_core_hours` 🟡 | `rate(...[30d])` donne un **nombre moyen de cœurs**, pas des heures. Les formules (`× 720 h`) sont cohérentes avec « cœurs », donc le calcul reste juste ; seul le nom induit en erreur | Attention en entretien |
| 11 | Repli Kepler au niveau nœud 🟢 | Si la métrique par conteneur est absente, `kepler_node_cpu_joules_total` (tout le cluster) est attribué au namespace demandé | Surestimation possible du CO₂ d'un namespace |
| 12 | Attribution par la 1ʳᵉ série 🟢 | `labels_result[0]` pour `team`/`cost_center`/`environment` | Un namespace multi-équipes est attribué à une seule équipe |
| 13 | Équipe non stockée avec les recos 🟢 | `recommendation_to_record()` écrit `team="unknown"` | L'index `idx_recommendations_team` n'a pas de valeur utile |

## 3. Écarts techniques / exploitation

| # | Constat | Preuve |
|---|---|---|
| 14 | Adaptateurs créés à chaque requête 🟢 | `get_metrics_port()` / `get_storage_port()` instancient un `httpx.AsyncClient` et un moteur SQLAlchemy (pool) **par requête**, jamais fermés → fuite de connexions probable sous charge |
| 15 | Script de seed cassé 🟢 | `scripts/load_test_data.py` utilise `Severity.HIGH` (n'existe pas → `AttributeError` vérifié), passe des `float` pour `current_value` (colonne `TEXT`), et une URL `finops:finops@localhost:5432/finops` différente du `docker-compose.yaml` ; il affiche aussi `http://localhost:8000/docs` alors que 8000 est le port du faux exporteur |
| 16 | Métriques `up` du faux exporteur probablement inutilisables 🟡 | Prometheus ajoute son propre label `job="fake-exporter"` ; sans `honor_labels: true` dans `prometheus/prometheus.yml`, le label `job` exposé devient `exported_job`. `up{job=~"kube-state-metrics|…"}` ne trouverait alors rien → toutes les sources « en panne » → uniquement des recos `DATA_QUALITY` en local |
| 17 | Données locales à 0 🟡 | Le faux exporteur incrémente ses compteurs **une seule fois** au démarrage : `rate(...[30d])` et `increase(...[24h])` valent alors 0 → CPU utilisé = 0, énergie = 0 |
| 18 | Annotations `/metrics` sans endpoint 🟢 | `deployment.yaml` annonce `prometheus.io/path: /metrics`, mais l'app n'expose pas `/metrics` (`prometheus-client` est en dépendance mais jamais importé dans `app/`) |
| 19 | Réglages non utilisés 🟢 | `LOG_FORMAT`, `LOG_LEVEL`, `METRICS_CACHE_TTL_SECONDS`, `API_WORKERS`, `COST_PER_GB_MONTH_USD` (sauf stockage) sont définis mais pas lus ; `python-json-logger` et `pyyaml` ne sont pas importés |
| 20 | Pas de dashboard Grafana 🟢 | `grafana/provisioning/dashboards/dashboard.yml` pointe vers `/var/lib/grafana/dashboards`, aucun JSON n'est fourni |
| 21 | Pas de README ni de CI 🟢 | Aucun `README.md`, aucun dossier `.github/` ou autre pipeline |
| 22 | Secrets de dev dans le repo 🟢 | `manifests/secret.yaml`, `timescaledb-secret.yaml`, `docker-compose.yaml` contiennent le mot de passe de dev (encodé base64 ou en clair). Acceptable en local, à remplacer avant tout déploiement |
| 23 | Doublon de tests 🟢 | `tests/test_*.py` et `tests/unit/test_*_unit.py` testent largement les mêmes fonctions |
| 24 | Script de smoke test Windows 🟢 | `scripts/smoke_test.ps1` est en PowerShell |

## 4. Comment en parler en entretien

Ne pas présenter ces points comme des échecs, mais comme la réalité d'un **MVP d'un mois** (`AGENTS.md` : « 1-month MVP ») :
- l'architecture a été pensée pour que ces extensions soient simples (ajouter une méthode au port, brancher une fonction déjà écrite) ;
- les limites 1, 7, 8 correspondent à des éléments explicitement classés « P1 / Phase 2 » dans `docs/metrics-reference.md`.

🔴 **À confirmer avec Marouane** : quels points étaient connus, lesquels ont été corrigés après ce commit (`c34f8b7`), et lesquels relevaient de son périmètre.
