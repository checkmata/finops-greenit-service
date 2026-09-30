# DATA_FLOW — qui appelle qui, avec quelles données (V1)

> Retour : [README docs](README.md) · Diagrammes : [main-flow](diagrams/main-flow.md)

| # | Déclencheur | Lit | Écrit |
|---|---|---|---|
| A | démarrage | fichier de clusters | — |
| B | timer (scheduler) | Prometheus de chaque cluster | TimescaleDB |
| C | `GET /api/finops|greenit/namespace/{ns}?cluster=` | Prometheus du cluster | — |
| D | `GET /api/clusters`, `PUT …/availability` | tous les Prometheus | registre (mémoire) |
| E | `GET /api/history/*`, `…/history` | TimescaleDB | — |
| F | `PATCH /api/recommendations/{id}/status` | TimescaleDB | TimescaleDB |

## A. Démarrage

```text
uvicorn app.adapters.inbound.api.main:app
 └─ create_app()            6 routers + /metrics (prometheus_client)
 └─ lifespan()
     ├─ configure_logging(settings)                  LOG_LEVEL / LOG_FORMAT
     ├─ build_container(settings)                    app/composition.py
     │    ├─ load_clusters()                         CLUSTERS_CONFIG_FILE ou 1 cluster (CLUSTER_NAME, PROMETHEUS_URL)
     │    ├─ PrometheusMetricsAdapter(url, cluster_name, workload_label) × N
     │    ├─ ClusterRegistry(roles, ports)
     │    └─ TimescaleAdapter(DATABASE_URL)
     ├─ [SQLite uniquement] ensure_tables_exist()     mode démo
     └─ create_task(start_snapshot_scheduler(storage, cluster_ports, interval))
```

## B. Cycle du scheduler (flux principal)

```text
start_snapshot_scheduler                         boucle, sleep(SCHEDULER_INTERVAL_SECONDS)
 └─ run_multicluster_cycle(ports, storage)
     └─ pour chaque cluster :
         ├─ port.ping()                            injoignable → cycle sauté, compteur "unreachable"
         └─ run_snapshot_cycle(port, storage, cluster=nom)
             ├─ resolve_namespaces()               SCHEDULER_NAMESPACES, sinon port.list_namespaces()
             │                                     + select_namespaces(include/exclude regex)   [domaine]
             └─ pour chaque namespace : _process_namespace()
```

`_process_namespace` en 3 phases :

**1. Collecte** (chaque appel est isolé dans un try/except ; un échec donne une valeur « inconnue », jamais 0)

| Appel | Résultat | En cas d'échec |
|---|---|---|
| `get_pod_metrics(ns)` | `PodMetric` agrégé (usage, requests, équipe majoritaire, `observation_hours`) | liste vide → namespace sauté |
| `check_data_quality()` | sources `up` | ensemble vide (« rien de sain ») |
| `get_workload_metrics(ns, namespace_total)` | un `PodMetric` par valeur du label `app` (+ `_unattributed`) | `[]` → FinOps au niveau namespace |
| `get_storage_metrics(ns)` | PVC (`has_usage_stats`) | `None` → stockage `unavailable` |
| `get_energy(ns)` | `EnergyMeasurement` (total, idle, attribution) | `UNAVAILABLE` |

**2. Calculs** (domaine pur)

```text
calculate_requested_cost / calculate_monthly_cost (avec stockage)   → CostBreakdown × 2
summarize_waste                                                       → gaspillage par composant
carbon_footprint_from_measurement                                     → CarbonFootprint | None
build_energy_breakdown (si énergie disponible)                        → idle éventuellement None
generate_all_recommendations(pod, carbone, énergie, qualité, prix,
                             workloads, min_observation_hours)        → RecommendationSet
```

Dans `generate_all_recommendations` :
- **FinOps**, pour chaque workload (ou pour le namespace s'il n'y a pas de détail) : historique < minimum → INFO « deferred » ; sinon ratio < 0,5 → réduire (économie = réservation libérée), ratio > 1 → augmenter ;
- **GreenIT** : pas d'énergie attribuable → DATA_QUALITY ; idle connu et > 30 % → ENERGY_WASTE ; CO₂ > 0 → INFO (marquée « estimated » si l'attribution est `node_cpu_share`) ;
- **qualité des données** : une recommandation par source en panne.

**3. Persistance**

```text
upsert_recommendation(rec) × N       decide_upsert : insert | refresh (pending) | skip (approved, failed, rejected < 7 j)
mark_obsolete(cluster, ns, fingerprints produits)   pending non reproduits → obsolete (seulement si tous les upserts ont réussi)
save_workload_snapshots([...])       une ligne par workload
save_snapshot(MetricSnapshot)        coût réel/réservé/gaspillage/économies, stockage, énergie, CO₂, attribution
```

## C. Lecture temps réel

`GET /api/finops/namespace/{ns}?cluster=secondary` :
`get_metrics_port` (registre → port du cluster, 404 si inconnu) → `get_pod_metrics` (503 si Prometheus injoignable, 404 si vide) → `get_storage_metrics` → `get_workload_metrics` → `calculate_*` + `summarize_waste` → JSON (`cost_breakdown`, `storage_status`, `workloads`).

`GET /api/greenit/namespace/{ns}` : `get_energy` (404 si inattribuable) → `carbon_footprint_from_measurement` → JSON avec `energy_source`.

## D. Clusters et failover

```text
GET /api/clusters
 └─ pour chaque cluster : observe_cluster()
      ping → check_data_quality (KSM up ?) → get_deployment_availability
      KSM up           → la liste observée est mémorisée (registry.remember_deployments)
      sinon, liste vide → dernière liste fiable, disponibilité forcée à 0 ("workloads_source": "last_seen")
      + déclaration opérateur éventuelle
 └─ plan_failover([ClusterState…])  [domaine] → active_cluster, placements (nominal/degraded/failover/outage + raison)
```

## E. Historique

`GET /api/history/timeseries?metric=cost&scope=namespace&group_by=cluster&days=7&bucket_minutes=60`
→ `query_snapshots(start, end, cluster, namespace)` (ou `query_workload_snapshots`) → `history.aggregate()` [domaine] : **moyenne** des échantillons d'une série dans un intervalle, puis **somme** des séries du groupe ; `None` reste `None`.

## F. Décision humaine

`PATCH /api/recommendations/{id}/status {"status": "approved"}` : statut connu ? (400) → `get_recommendation_record` (404) → `can_transition(actuel, nouveau)` (**409** sinon, avec les transitions permises) → `update_recommendation_status` → réponse avec le nouveau statut.
Transitions : pending → approved | rejected ; approved → executed | failed | rejected ; failed → approved | rejected ; executed, rejected, obsolete : terminaux. `obsolete` n'est jamais posé via l'API.
