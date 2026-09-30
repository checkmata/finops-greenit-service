# FINOPS — calculs de coûts, gaspillage et économies (V1)

> Retour : [README docs](README.md) · Code : `app/domain/finops_engine.py`, `app/domain/recommendation_engine.py`

Tous les chiffres sont des **estimations déterministes** : quantités mesurées par Prometheus × tarifs de configuration. Ce n'est pas une facture.

## 1. Données d'entrée

| Donnée | Source (PromQL) | Remarque V1 |
|---|---|---|
| CPU utilisé (cœurs moyens) | `avg_over_time(sum(rate(container_cpu_usage_seconds_total{…, container!="", container!="POD"}[5m]))[30d:5m])` | voir §4 : moyenne de taux courts, séries du cgroup pod exclues |
| Historique disponible | `count_over_time(<même expression>[30d:5m])` × 5 min | `PodMetric.observation_hours` |
| Mémoire utilisée | `sum(container_memory_working_set_bytes{…, container!="", container!="POD"})` | instantané |
| CPU / mémoire réservés | `sum(kube_pod_container_resource_requests{resource="cpu"/"memory"})` | |
| Stockage | `kubelet_volume_stats_used_bytes`, `…capacity_bytes`, `kube_persistentvolumeclaim_info` | PVC sans statistiques kubelet → non valorisé |
| Workload / équipe | jointure `* on (namespace, pod) group_left(label_app) kube_pod_labels` | label configurable (`WORKLOAD_LABEL`) |

Tarifs (`app/config.py`, surchargeables) : `COST_PER_VCPU_HOUR_USD=0.03`, `COST_PER_GB_HOUR_USD=0.004`, `COST_PER_GB_MONTH_USD=0.10`. 🔴 Leur origine métier reste à documenter.

## 2. Formules

```text
coût réservé / mois = CPU_req × p_cpu × 720 + Go_req × p_mem × 720 + Σ capacité_PVC(Go) × p_sto
coût utilisé / mois = CPU_used × p_cpu × 720 + Go_used × p_mem × 720 + Σ utilisé_PVC(Go) × p_sto

gaspillage_cpu      = max(0, réservé_cpu − utilisé_cpu)        (idem mémoire, stockage)
gaspillage total    = gaspillage_cpu + gaspillage_mem + gaspillage_sto
waste_ratio         = gaspillage total / coût réservé
```

**Gaspillage « brut »** (somme des écarts positifs). Le MVP calculait « réservé − utilisé », donc un CPU sur-réservé pouvait être compensé par une mémoire sur-utilisée. Or le CPU réservé et inutilisé est payé quoi qu'il arrive. Quand aucune ressource n'est sous-dimensionnée, les deux définitions coïncident.

## 3. Recommandations et économies (correction du double comptage)

| Condition (par workload si le détail existe, sinon par namespace) | Recommandation | Sévérité |
|---|---|---|
| historique < `MIN_OBSERVATION_HOURS` | aucune ; INFO « right-sizing deferred » | INFO |
| usage/request < 0,5 | réduire à usage × 1,2 | waste_ratio > 0,7 → CRITICAL, > 0,5 → WARNING, sinon INFO |
| usage/request > 1,0 | augmenter à usage × 1,25 | CRITICAL |

**Économie** d'une réduction = réservation libérée :

```text
économie = (request_actuelle − request_recommandée) × prix × 720
```

Comme `recommandée ≥ usage` (marge de 20 %), l'économie est toujours ≤ le gaspillage de **cette** ressource. Donc :

```text
économie_cpu + économie_mem (+ économie_sto = 0 en V1) ≤ gaspillage total
```

Le MVP utilisait `gaspillage_total × (1 − ratio)` pour **chaque** ressource. Exemple réel : 24,19 $ + 25,92 $ = 50,11 $ d'économies pour 34,56 $ de gaspillage. Cet invariant est vérifié par un test paramétré sur 80 combinaisons (`tests/unit/test_v1_finops.py`).

Le stockage entre dans le coût et le gaspillage, mais **aucune réduction de PVC n'est recommandée** : Kubernetes ne sait pas réduire un PVC. La détection de PVC orphelins viendra plus tard.

## 4. Corrections trouvées en V1 <a id="4"></a>

| Problème du MVP | Conséquence | Correction |
|---|---|---|
| `rate(cpu[30d])` | `rate` divise par 30 jours entiers : une série de moins de 30 jours (nouveau déploiement) paraît quasi inactive → conseil « réduire » **dangereux** | moyenne temporelle de `rate[5m]` (sous-requête) + `MIN_OBSERVATION_HOURS` (24 h par défaut, 168 h conseillé en prod). **Trouvé en exécutant la démo contre un vrai Prometheus** |
| somme cAdvisor sans filtre `container` | la série « pod » (`container=""`) et le conteneur pause s'ajoutent aux conteneurs → usage ≈ ×2 sur un vrai cluster | `container!="", container!="POD"` |
| économies calculées sur le gaspillage total | double comptage | réservation libérée (§3) |
| `cost_usd` historisé à 0 | pas d'historique de coût | coût réel + réservé + gaspillage + économies historisés |
| stockage jamais valorisé | FinOps limité à CPU+mémoire | PVC intégrés, avec `storage_status` |
| équipe = 1ʳᵉ série trouvée | attribution arbitraire | équipe majoritaire, et détail par workload |

## 5. Statut du stockage

| `storage_status` | Signification |
|---|---|
| `available` | tous les PVC ont leurs statistiques kubelet |
| `partial` | certains PVC n'ont pas de statistiques ; seuls les autres sont valorisés |
| `unavailable` | requête en échec, ou aucun PVC mesurable → **non valorisé** (≠ gratuit) |
| `not_applicable` | aucun PVC : 0 $ est la vraie valeur |

## 6. Où lire les résultats

- En direct : `GET /api/finops/namespace/{ns}?cluster=` → `cost_breakdown` (requested/actual/waste par ressource), `storage_status`, `workloads[]`.
- Historique : `GET /api/history/timeseries?metric=cost|requested_cost|waste|savings&scope=namespace|workload&group_by=cluster,namespace,workload&days=7`.
- Grafana : dashboard « 2. FinOps ».

Règle d'agrégation de l'historique : les échantillons d'une même série dans un intervalle sont **moyennés** (un coût mensuel échantillonné toutes les 5 min ne s'additionne pas), puis les séries sont **sommées**.

## 7. Changement de contrat API (additif)

`GET /api/finops/namespace/{ns}` garde tous les champs du MVP et ajoute `cluster`, `cost_breakdown`, `storage_status` et `workloads`. Seule `cost_savings_usd` change de sens : elle vaut désormais le **gaspillage brut**.
