# ARCHITECTURE — composants, responsabilités, communications (V2.x · V3 · V4)

> Retour : [README docs](README.md) · Diagrammes : [architecture](diagrams/architecture.md) · [modules](diagrams/modules.md)
>
> Légende : 🟢 confirmé par le code · 🟡 fourni mais non exécuté · 🔴 à confirmer avec Marouane

## 1. Style : hexagonal (Ports & Adapters), renforcé en V1

Le cœur métier ne sait pas d'où viennent les données ni où elles vont. Il reçoit des objets Python et renvoie des objets Python.

```text
      Adaptateurs ENTRANTS                                Adaptateurs SORTANTS
  ┌──────────────────────────┐                        ┌──────────────────────────────┐
  │ FastAPI (6 routers)      │                        │ PrometheusMetricsAdapter ×N  │  un par cluster
  │ Scheduler (timer)        │──► PORTS ──► DOMAINE ◄──│ TimescaleAdapter             │
  └──────────────────────────┘   MetricsPort   pur     └──────────────────────────────┘
                                 StoragePort
          app/composition.py = racine de composition (instancie les adaptateurs une seule fois)
```

| Couche | Dossier | Imports autorisés | Exemples V1 |
|---|---|---|---|
| Domaine | `app/domain/` | stdlib + ses modèles | `finops_engine`, `greenit_engine`, `recommendation_engine`, `failover`, `history`, `namespace_selection`, `recommendation_lifecycle` |
| Ports | `app/ports/` | domaine | `MetricsPort` (10 méthodes), `StoragePort` (14 méthodes) |
| Entrants | `app/adapters/inbound/api/` | tout | routers finops, greenit, recommendations, clusters, history, health ; `dependencies.py` |
| Sortants | `app/adapters/outbound/` | ports, domaine, config | Prometheus (httpx), TimescaleDB (SQLAlchemy async) |
| Application | `app/composition.py`, `app/cluster_registry.py`, `app/cluster_config.py`, `app/scheduler/` | ports, domaine, config | assemblage, registre de clusters, cycle périodique |

🟢 Vérifié : `app/domain/` n'importe ni FastAPI, ni Pydantic, ni httpx, ni SQLAlchemy, ni `app.config`, ni `prometheus_client`.

## 2. Composants

### 2.1 Racine de composition — `app/composition.py` (nouveau)
Lit la liste des clusters (`load_clusters`). Crée **un** `PrometheusMetricsAdapter` par cluster et **un** `TimescaleAdapter`, puis les range dans un `Container(registry, storage)`. `lifespan()` le stocke dans `app.state.container`.
*Pourquoi* : le MVP créait un client HTTP et un pool de base de données **à chaque requête**, sans jamais les fermer.

### 2.2 Registre de clusters — `app/cluster_registry.py` (nouveau)
Donne le port d'un cluster par nom (le primary par défaut). Mémorise les déclarations opérateur et la dernière liste de Deployments observée de façon **fiable** (kube-state-metrics up).

### 2.3 Routers (`app/adapters/inbound/api/`)
| Router | Routes | Lit | Écrit |
|---|---|---|---|
| finops | `GET /api/finops/namespace/{ns}[/history]` | Prometheus du cluster choisi / DB | — |
| greenit | `GET /api/greenit/namespace/{ns}[/history]` | idem | — |
| recommendations | `GET /history`, `PATCH /{id}/status` | DB | statut uniquement (décision humaine) |
| clusters | `GET /api/clusters`, `PUT /api/clusters/{name}/availability` | tous les Prometheus | registre (en mémoire) |
| history | `GET /api/history/timeseries`, `/workloads` | DB | — |
| health | `GET /health`, `/health/live` | tous | — |

Les dépendances (`get_metrics_port`, `get_storage_port`, `get_cluster_registry`) sont centralisées dans `dependencies.py` et ré-exportées par chaque router, pour que les tests puissent les remplacer.

### 2.4 Scheduler — `app/scheduler/snapshot_job.py`
`start_snapshot_scheduler` lance `run_multicluster_cycle` toutes les `SCHEDULER_INTERVAL_SECONDS`. Chaque cycle fait `ping` de chaque cluster, puis `run_snapshot_cycle` (namespaces découverts ou listés), puis `_process_namespace`. C'est **toujours le seul écrivain** en base.

### 2.5 Domaine
| Module | Rôle | Pur ? |
|---|---|---|
| `finops_engine` | coûts réservé/utilisé (CPU, mémoire, stockage), gaspillage par composant, `rightsizing_savings_usd`, `storage_status` | 🟢 |
| `greenit_engine` | joules → kWh → kgCO₂ ; `carbon_footprint_from_measurement` (None si inattribuable) | 🟢 |
| `recommendation_engine` | règles par workload, garde d'historique minimal, idle, provenance, qualité des données | 🟢 |
| `recommendation_lifecycle` | statuts, transitions, `decide_upsert` (dédoublonnage) | 🟢 |
| `failover` | `plan_failover(ClusterState…) → FailoverPlan` | 🟢 |
| `history` | agrégation de séries (moyenne dans une série, somme entre séries) | 🟢 |
| `namespace_selection` | liste explicite ou découverte + regex include/exclude | 🟢 |
| `models/` | `PodMetric` (+cluster, workload, observation_hours), `StorageMetric` (+has_usage_stats), `EnergyMeasurement`, `CarbonFootprint` (+attribution), `EnergyBreakdown` (idle optionnel), `Recommendation` (+scope, fingerprint), `StoredRecommendation`, `MetricSnapshot`, `WorkloadSnapshot`, `ClusterState`, `FailoverPlan`, `MetricStatus` | 🟢 |

### 2.6 `PrometheusMetricsAdapter`
| Méthode | Requête(s) principale(s) |
|---|---|
| `get_pod_metrics` | `avg_over_time(sum(rate(cpu[5m]))[30d:5m])`, `count_over_time(...)`, mémoire, requests, `kube_pod_labels` |
| `get_workload_metrics` | mêmes mesures, jointes à `kube_pod_labels` par `(namespace, pod)` ; reste `_unattributed` |
| `get_storage_metrics` | `kubelet_volume_stats_*` + `kube_persistentvolumeclaim_info` |
| `get_energy` | conteneur → pod → nœud × part CPU → indisponible |
| `get_deployment_availability` | `kube_deployment_spec_replicas`, `…status_replicas_available` |
| `check_data_quality` / `ping` / `list_namespaces` | `up{job=~…}` / `vector(1)` / `count by (namespace)(requests)` |

### 2.7 `TimescaleAdapter` et schéma
| Table | Type | Clé logique | Migration |
|---|---|---|---|
| `recommendations` | relationnelle | `id`, `fingerprint` | 001 + 002 |
| `metric_snapshots` | hypertable (7 j) | time, cluster, namespace | 001 + 002 |
| `workload_snapshots` | hypertable (7 j) | time, cluster, namespace, workload | 002 |

`upsert_recommendation` applique `decide_upsert` (insérer / rafraîchir / ignorer). `mark_obsolete` clôt les recommandations *pending* qui ne sont plus produites.

### 2.8 Infrastructure
| Élément | Fichiers | Statut |
|---|---|---|
| Simulation de 2 clusters | `exporter/`, `prometheus/prometheus-{primary,secondary}.yml`, `scripts/run_local_demo.sh` | 🟢 exécutée |
| Compose (DB, 2 sims, 2 Prometheus, API, Grafana) | `docker-compose.yaml`, `grafana/` | 🟡 |
| 2 clusters kind + workloads | `scripts/k8s/`, `workloads/*/k8s/` | 🟡 (overlays construits et validés) |
| OpenShift (service, DB, Kepler) | `manifests/` | 🟡 validés par kubeconform |
| CI | `.github/workflows/ci.yml` | 🟡 |

## 3. Choix techniques (et pourquoi)

| Choix | Raison |
|---|---|
| Un Prometheus par cluster (pas de Prometheus central) | c'est la réalité d'OpenShift (monitoring embarqué par cluster) ; le service agrège |
| Contrôleur de failover pur (V2 : `failover_controller.step`), état persisté | testable, explicable ; l'actuation passe par `FailoverActuatorPort` (scaling uniquement, RBAC minimal), jamais le domaine |
| Élection de leader par verrou consultatif PostgreSQL (V2) | pas de dépendance supplémentaire (ni etcd ni Lease K8s), la base est déjà là ; libéré automatiquement si la connexion meurt |
| Secrets par référence (`*_env`, `*_file`) (V2) | aucune valeur dans Git ni dans la config ; compatible Docker/K8s Secrets |
| `None` plutôt que 0 pour l'inconnu | un 0 inventé fausse les coûts et le CO₂ |
| Moyenne de taux courts pour le CPU | `rate(x[30d])` sous-estime les séries jeunes |
| Économie = réservation libérée | bornée par le gaspillage : pas de double comptage |
| Déduplication par fingerprint | le scheduler tourne toutes les 5 min |
| Simulation plutôt que faux chiffres | démontrer toute la chaîne sur un laptop, en le disant |
| Pas d'OpenTelemetry en V1 | pas de besoin de traces ; ne pas ajouter une technologie « pour faire moderne » |

## 3 bis. Ajouts V2 (ports et adaptateurs)

| Port | Adaptateur | Rôle |
|---|---|---|
| `FailoverStatePort` | `SqlFailoverStore` | état du contrôleur + journal des événements |
| `FailoverActuatorPort` | `KubernetesActuator` (httpx, sous-ressource `/scale`) | promotion / restauration du standby |
| `LeaderElectionPort` | `PostgresAdvisoryLeader` / `AlwaysLeader` (SQLite) | une seule réplique écrit et agit |

Boucles (leader uniquement) : scheduler de snapshots (5 min) et `run_failover_loop` (`FAILOVER_TICK_SECONDS`). Les routers restent en lecture, sauf `approve`/`reject`. Le domaine n'importe toujours ni FastAPI, ni SQLAlchemy, ni httpx.

## 3 ter. Ajouts V2.x · V3 · V4

| Couche | V2.x | V3 | V4 |
|---|---|---|---|
| Domaine (pur) | `data_replication` (isolement, conditions de failback) | `ml/series`, `ml/anomaly`, `ml/forecasting`, `ml/rightsizing` | `agent/model`, `agent/reasoning` (explain, prioritize, propose), `agent/policy`, `agent/lifecycle` |
| Ports | `DataReplicationPort` | — (lit via `StoragePort`) | `ActionStorePort`, `GitOpsWriterPort`, `ObservationPort` |
| Adaptateurs sortants | `PostgresReplicationAdapter` (asyncpg) | — | `SqlActionStore`, `GiteaWriter` (API REST), `PrometheusObservation` |
| Services applicatifs | `FailoverService` (+ promotion, garde de données) | `MLService` (+ `run_ml_loop`) | `AgentService` (+ `agent_gitops` : planificateur / dry-run hors ligne) |
| API | `GET /api/failover` (+ `data`, `actuators`) | `/api/ml/*` (lecture seule) | `/api/agent/briefing`, `/api/agent/actions*` |
| Boucles (leader seulement) | garde de données 1 s | cycle ML horaire | exécution des actions validées (asynchrone) |
| Migrations | 005 (événement `fence`) | 006 (`cpu_cores_used_now`) | 007 (`agent_actions`, `agent_audit`) |

Le domaine n'importe toujours ni FastAPI, ni SQLAlchemy, ni httpx, ni numpy. L'agent n'a **aucun** accès en écriture à Kubernetes : son seul chemin d'écriture est Git.

## 4. Limites
Voir [LIMITATIONS.md](LIMITATIONS.md).
