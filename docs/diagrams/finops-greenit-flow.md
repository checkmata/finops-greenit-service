# Diagramme 4 — Flux métier FinOps / GreenIT (V1)

> Retour : [FINOPS](../FINOPS.md) · [GREENIT](../GREENIT.md)

```mermaid
flowchart TB
    subgraph M["1. Mesures (Prometheus, par cluster)"]
        CPU["CPU utilisé<br/>moyenne de rate 5m sur 30 j<br/>+ heures d'historique"]
        MEM["Mémoire utilisée"]
        REQ["Requests CPU / mémoire"]
        PVC["PVC : utilisé / capacité<br/>(sans stats → non valorisé)"]
        LAB["kube_pod_labels<br/>app · team · cost-center"]
        KEP["Kepler : conteneur → pod → nœud × part CPU"]
        UP["up{job}"]
    end
    subgraph F["2. FinOps (finops_engine)"]
        F1["coût réservé / utilisé<br/>CPU + mémoire + stockage"]
        F2["gaspillage par composant<br/>(brut, jamais négatif)"]
    end
    subgraph G["3. GreenIT (greenit_engine)"]
        G1["kWh × PUE × intensité → kgCO₂"]
        G2["provenance : measured / estimated / unavailable"]
        G3["idle : mesuré ou inconnu"]
    end
    subgraph R["4. Règles (recommendation_engine)"]
        R0["historique insuffisant → différé (INFO)"]
        R1["ratio sous 0,5 → réduire (+20 %)<br/>économie = réservation libérée ≤ gaspillage"]
        R2["ratio au-dessus de 1 → augmenter (+25 %)"]
        R3["idle mesuré au-dessus de 30 % → ENERGY_WASTE"]
        R4["CO₂ (INFO, marqué si estimé)"]
        R5["source en panne → DATA_QUALITY"]
    end
    subgraph O["5. Cycle de vie & restitution"]
        O1["upsert (dédoublonné) · obsolete"]
        O2["historique : namespace + workload"]
        O3["humain : approved / rejected / executed / failed"]
        O4["API · Grafana"]
    end
    CPU & MEM & REQ & PVC --> F1 --> F2
    LAB --> R1
    KEP --> G1
    KEP --> G2
    KEP --> G3
    CPU --> R0
    F2 --> R1
    REQ --> R2
    G3 --> R3
    G1 --> R4
    UP --> R5
    R0 & R1 & R2 & R3 & R4 & R5 --> O1 --> O3
    F1 & G1 --> O2 --> O4
```

Hors flux (V1) : actions automatiques, CO₂ par workload, CO₂ évité par un right-sizing, ML (V3), agent (V4).
