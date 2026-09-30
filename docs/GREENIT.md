# GREENIT — énergie, CO₂ et provenance des chiffres (V2)

> Retour : [README docs](README.md) · Code : `app/domain/greenit_engine.py`, `PrometheusMetricsAdapter.get_energy()`

## 1. Formule (inchangée depuis le MVP)

```text
kWh    = joules (Kepler, sur 24 h) / 3 600 000
kgCO₂  = kWh × PUE × intensité_carbone / 1000        (PUE = 1,4 ; intensité = 475 gCO₂/kWh par défaut)
```

PUE et intensité sont des **constantes de configuration**, pas des mesures (choix du MVP, conservé : pas d'API externe). 🔴 Leur source (datacenter, pays) est à documenter.

## 2. Provenance de l'énergie (nouveau en V1)

Le MVP, faute de métrique par conteneur, prenait l'énergie de **tout le cluster** et l'attribuait au namespace demandé : chaque namespace « émettait » alors comme le cluster entier. V1 suit une hiérarchie et **enregistre la méthode utilisée** :

| Niveau | Série Kepler | Attribution | Précision |
|---|---|---|---|
| 1. Conteneur | `kepler_container_cpu_joules_total{namespace=…}` **ou** `{container_namespace=…}` (selon la version de Kepler) | `container` | mesurée |
| 2. Pod | `kepler_pod_cpu_joules_total{pod_namespace=…}` (Kepler récent) | `pod` | mesurée |
| 3. Nœud × part CPU | `kepler_node_cpu_joules_total` (ou `…package…`) × (CPU du namespace / CPU du cluster sur 24 h) | `node_cpu_share` | **estimée** |
| 4. Rien d'attribuable | — | `unavailable` | pas de valeur (jamais 0) |

Conséquences :
- l'API renvoie `energy_source` (attribution, précision, métrique source) et `carbon_footprint.precision` ;
- une recommandation basée sur une estimation le dit (message « [estimated: node-level fallback] » et champ `estimation_method`) ;
- sans énergie attribuable alors que Kepler est en ligne, on obtient une recommandation DATA_QUALITY « no energy series attributable », et **pas** de CO₂.

🟢 **Vérifié en V2 sur Kepler 0.8 réel** : la série est `kepler_container_joules_total{container_namespace, pod_name, mode}` (le label `namespace` désigne le pod de Kepler lui-même). Les requêtes ont été corrigées en conséquence.

### 2 bis. Source de puissance et périmètre (V2)

Kepler déclare lui-même comment il obtient la puissance : `kepler_node_info{components_power_source}`.

| `components_power_source` | Précision affichée | Série utilisée | `scope` |
|---|---|---|---|
| `rapl-sysfs` (kind sur l'hôte, bare-metal) | **mesurée** | `kepler_container_package_joules_total` + `…dram…` | `cpu_package+dram` |
| `estimator` (VM sans RAPL, ex. minikube/KVM) | **estimée** | `kepler_container_joules_total` | `platform` |
| aucune série attribuable | **indisponible** | — | — (pas de valeur, jamais 0) |

Pourquoi exclure la part `platform`/`other` en mode matériel : sur kind, `kepler_container_joules_total` ≈ RAPL + 83 % de « other » **modélisé**. L'additionner reviendrait à présenter une estimation comme une mesure. Le chiffre mesuré est donc plus petit, mais honnête. Chaque snapshot stocke `energy_power_source` et `energy_scope` (migration 003).

## 3. Énergie « idle »

- Le MVP écrivait `idle = 0.0` en dur, ce qui se lisait comme « aucun gaspillage ».
- V1 : l'idle n'est connu **que** si la série choisie porte une séparation `mode="idle"` / `mode="dynamic"` (certaines versions de Kepler, niveau conteneur). Sinon `idle_energy_kwh = null`.
- **Aucune recommandation `ENERGY_WASTE` n'est produite quand l'idle est inconnu.** Quand il est connu et dépasse 30 % (WARNING) ou 50 % (CRITICAL) de l'énergie, une recommandation propose de consolider.
- Interprétation honnête : l'« idle » Kepler par conteneur est la part de la **consommation de base du nœud** attribuée au workload. C'est un signal de sous-utilisation des nœuds (donc de consolidation), pas une énergie « jetée ».

## 4. CO₂ par workload et par pod (V2) 🟢

`GET /api/greenit/namespace/{ns}/workloads?cluster=` : cluster → namespace → workload → pod.
- Énergie par pod : `kepler_container_*_joules_total` groupé par `(container_namespace, pod_name)` ;
- pod → workload : via le propriétaire du pod (kube-state-metrics) ;
- chaque ligne porte sa précision (mesurée / estimée / indisponible).

Vérifié sur l'infrastructure réelle : la somme des workloads égale le total du namespace. Les valeurs sont historisées dans `workload_snapshots` (énergie, CO₂) et agrégées à l'heure, au jour, à la semaine et au mois (`GET /api/history/aggregate`).

## 4 bis. Ce qui n'est pas (encore) calculé

- **CO₂ évité par un right-sizing** : non chiffré (il faudrait un modèle énergie/cœur réservé ; réserver n'implique pas consommer).
- **Intensité carbone en temps réel** : volontairement exclue (constante).

## 5. Environnements

| Environnement | Source de l'énergie | Ce qu'on peut en conclure |
|---|---|---|
| Simulation (démo uniquement, jamais une validation) | modèle simple : 2 W par pod + 12 W par cœur, 40 W par nœud (`exporter/simulator.py`) | démontre la chaîne et la provenance, **pas** des chiffres réels |
| kind (exécuté en V2) | Kepler réel, privilégié, qui lit le RAPL **de l'hôte** (`rapl-sysfs`) | mesure réelle du package CPU + DRAM ; les deux clusters partagent le même hôte |
| minikube / KVM (exécuté en V2) | Kepler réel en mode `estimator` (pas de RAPL dans la VM) | estimé, affiché comme tel |
| OpenShift bare-metal / VM avec RAPL | Kepler avec compteurs matériels | la mesure la plus fiable disponible |

## 6. Où lire les résultats

- En direct : `GET /api/greenit/namespace/{ns}?cluster=` (404 si aucune énergie n'est attribuable).
- Historique : `GET /api/history/timeseries?metric=energy|co2&group_by=cluster,namespace`.
- Grafana : dashboard « 3. Green IT » (inclut un tableau de provenance).
