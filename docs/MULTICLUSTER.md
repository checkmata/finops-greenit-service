# MULTICLUSTER — ce qui est réel, simulé, ou à venir

> Retour : [README docs](README.md) · Voir aussi : [FAILOVER](FAILOVER.md) · [ROADMAP](ROADMAP.md)
>
> Légende : 🟢 implémenté et testé · 🟡 fourni mais non exécuté pendant le développement · 🔵 prévu (V2+)

## 1. La bonne formulation

```text
Kubernetes / OpenShift            → orchestre les workloads DANS un cluster
ACM / Multicluster Engine / GitOps → gère une FLOTTE de clusters (déploiement, politiques, placement)
finops-greenit-service            → OBSERVE chaque cluster (son Prometheus) et DÉCIDE quel cluster
                                    devrait servir chaque workload. Il ne déplace rien.
```

Kubernetes seul n'orchestre pas plusieurs clusters. Ce service non plus : il n'est ni un gestionnaire de flotte, ni un load balancer global.

## 2. Ce que V1 apporte au code 🟢

| Élément | Fichier | Rôle |
|---|---|---|
| Liste des clusters | `config/clusters.*.yaml`, `app/cluster_config.py` | `name`, `role` (`primary` / `secondary`), `prometheus_url`. Validation : noms uniques et exactement un primary |
| Un adaptateur par cluster | `app/composition.py` | Un `PrometheusMetricsAdapter` par cluster, créé une seule fois au démarrage |
| Registre | `app/cluster_registry.py` | Accès aux ports par nom, déclarations opérateur (« indisponible »), dernière liste de workloads vue |
| Métriques distinguées | `PodMetric.cluster`, `Recommendation.cluster`, `metric_snapshots.cluster`, `workload_snapshots.cluster` | Chaque chiffre porte son cluster |
| API | `?cluster=` sur `/api/finops`/`/api/greenit`, `GET /api/clusters`, `PUT /api/clusters/{name}/availability` | Voir et déclarer |
| Scheduler | `run_multicluster_cycle()` | Parcourt tous les clusters ; un cluster injoignable est sauté (compté dans `/metrics`) |
| Décision de failover | `app/domain/failover.py` | Fonction pure, voir [FAILOVER](FAILOVER.md) |

Sans fichier de clusters, le service fonctionne comme le MVP : un seul cluster (`CLUSTER_NAME`, `PROMETHEUS_URL`).

## 3. Trois environnements, trois niveaux de réalisme

| | Simulation locale | kind (2 clusters) | OpenShift + ACM |
|---|---|---|---|
| Lancement | `scripts/run_local_demo.sh` ou `docker compose up` | `scripts/k8s/kind-up.sh` | manifestes `manifests/` + V2 |
| Clusters | 2 simulateurs Python (`exporter/`) | 2 vrais clusters Kubernetes | vrais clusters |
| Prometheus | 2 vrais Prometheus | kube-prometheus-stack par cluster | Prometheus / Thanos d'OpenShift |
| Workloads | charge **simulée** (profils) | vraies applis (`workloads/`) | vraies applis |
| Kepler | séries **simulées** (3 modes d'exposition) | vrai Kepler, **mesuré** (RAPL de l'hôte, `rapl-sysfs`) | vrai Kepler |
| Panne de cluster | API admin du simulateur | `docker pause` du nœud (démo de failover réelle) | incident réel |
| Statut | 🟢 exécuté (voir §5) | 🟢 **exécuté en V2** ([INFRASTRUCTURE](INFRASTRUCTURE.md)) | 🔵 V3+ |

**Ce que la simulation prouve** : toute la chaîne logicielle (PromQL réel, adaptateur, domaine, scheduler, stockage, API, décision de failover) contre de vrais Prometheus.
**Ce qu'elle ne prouve pas** : le comportement de vraies applications, de vrais nœuds, ni un basculement de trafic.

## 4. Simulation locale : comment ça marche

Chaque cluster simulé est un processus (`python -m exporter.fake_metrics`) qui expose 4 composants sur 4 ports. Il tourne une horloge : l'usage CPU suit une sinusoïde « journalière », plus une tendance, plus du bruit ; les compteurs augmentent donc réellement.

```text
sim-primary  :9101 kube-state-metrics  :9102 node-exporter  :9103 kepler  :9104 kubelet  :8000 admin
     ▲ scrape (4 jobs)                                                    
prometheus-primary (:9090)  ◄─── PrometheusMetricsAdapter(cluster="primary") ───┐
                                                                                 ├── finops-greenit-service
prometheus-secondary (:9091) ◄── PrometheusMetricsAdapter(cluster="secondary") ─┘
     ▼ scrape
sim-secondary (même structure, profil « standby »)
```

- Un composant « down » répond 503. Prometheus enregistre alors un **vrai** `up{job="kepler"} = 0`. Le MVP exposait une fausse métrique `up`, que Prometheus renommait en `exported_job`.
- Profils (`exporter/profiles.py`) : le **primary** porte tout (banking, trading, ecommerce) et expose Kepler par conteneur avec la séparation idle/dynamique. Le **secondary** porte des copies standby (banking, ecommerce, sans trading) et n'expose Kepler qu'au niveau nœud. Il montre ainsi l'attribution *estimée*, et un PVC sans statistiques kubelet (stockage `unavailable`).

## 5. Validation effectuée (V1 puis V2)

| Vérification | Résultat |
|---|---|
| Tests automatisés multicluster (ports en mémoire + SQLite) | 🟢 inclus dans les 375 tests |
| Démo locale : 2 simulateurs + 2 **vrais** Prometheus 2.53.2 + API | 🟢 exécutée. `up{}` réel, détection des namespaces, coûts, énergie (mesurée sur primary, estimée sur secondary), recommandations dédupliquées, `/api/clusters`. C'est cette exécution qui a révélé le bug `rate(x[30d])` (voir [FINOPS](FINOPS.md#4)) |
| Manifestes Kubernetes (kubeconform) + overlays kustomize | 🟢 tous valides (Online Boutique v0.10.1 récupéré et construit) |
| `docker compose up` | 🟢 exécuté en V2 (TimescaleDB, 2 répliques d'API, Grafana) |
| `scripts/k8s/kind-up.sh` | 🟢 exécuté en V2 : primary 31/31 pods, secondary 22/22 pods `Running` |
| Dashboards Grafana | 🟢 V2 : 5 dashboards affichés, chaque panneau vérifié avec des données réelles |

## 6. Limites connues (multicluster)

- ✅ V2 : authentification par cluster (basic ou bearer, `*_env` / `*_file` uniquement) et TLS configurable. Vérifié en réel avec un proxy basic-auth devant le Prometheus secondary.
- ✅ V2 : hystérésis et actuation contrôlée du failover, état du contrôleur persisté ([FAILOVER](FAILOVER.md)).
- ✅ V2 : plusieurs répliques possibles (élection de leader PostgreSQL).
- Les déclarations « cluster indisponible » et la dernière liste de workloads vue restent **en mémoire**.
- Les deux clusters kind partagent le même hôte.

## 7. ACM / GitOps — fait en V2.x avec OCM + Argo CD 🟢

- Open Cluster Management (amont de RHACM) : hub sur `secondary`, `primary` et `secondary` enregistrés comme ManagedClusters ; un **Placement** sélectionne les clusters par label de rôle.
- Argo CD (moteur d'OpenShift GitOps) : un **ApplicationSet** génère une Application par (workload × cluster choisi par OCM), synchronisée depuis le Gitea local.
- Validé en réel (Git → K8s, dérive, retour arrière) : [GITOPS.md](GITOPS.md). RHACM lui-même reste **BLOCKED** (OpenShift requis).
- Réplication des données et reprise complète : [REPLICATION.md](REPLICATION.md), [DR.md](DR.md).
