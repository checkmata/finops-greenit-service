# Diagramme 1 — Architecture globale (V1)

> Retour : [ARCHITECTURE](../ARCHITECTURE.md) · [MULTICLUSTER](../MULTICLUSTER.md)

## 1.a Cible : plusieurs clusters, un Prometheus par cluster

```mermaid
flowchart LR
    subgraph A["Cluster primary"]
        WA["Workloads<br/>banking · trading · ecommerce"]
        SA["cAdvisor · kube-state-metrics<br/>node-exporter · Kepler"]
        PA[("Prometheus A")]
        WA --> SA --> PA
    end
    subgraph B["Cluster secondary (standby)"]
        WB["Workloads standby<br/>banking · ecommerce"]
        SB["cAdvisor · kube-state-metrics<br/>node-exporter · Kepler"]
        PB[("Prometheus B")]
        WB --> SB --> PB
    end
    subgraph SVC["finops-greenit-service"]
        REG["ClusterRegistry<br/>1 adaptateur / cluster"]
        SCH["Scheduler<br/>cycle multicluster"]
        DOM["Domaine pur<br/>FinOps · GreenIT · Recos · Failover · History"]
        API["API FastAPI"]
    end
    DB[("TimescaleDB<br/>recommendations<br/>metric_snapshots<br/>workload_snapshots")]
    GRAF["Grafana<br/>4 dashboards"]
    HUMAN["Humain<br/>approuve / rejette"]

    PA -- PromQL --> REG
    PB -- PromQL --> REG
    REG --> SCH --> DOM
    REG --> API --> DOM
    SCH -- seul écrivain --> DB
    API -- lecture --> DB
    GRAF --> DB
    GRAF --> PA
    GRAF --> PB
    HUMAN -- PATCH status --> API
```

Le service **observe** et **décide**. Aucune flèche ne va du service vers les clusters : il ne modifie rien.

## 1.b Démo locale (simulation)

```mermaid
flowchart LR
    S1["sim-primary<br/>exporter/ (4 composants + admin)"] --> P1[("Prometheus :9090")]
    S2["sim-secondary"] --> P2[("Prometheus :9091")]
    P1 --> APP["API + scheduler<br/>(uvicorn)"]
    P2 --> APP
    APP --> ST[("SQLite (processus)<br/>ou TimescaleDB (compose)")]
    OPS["simulate_failover.sh"] -- "POST /cluster/down" --> S1
    OPS -- "GET /api/clusters" --> APP
```
