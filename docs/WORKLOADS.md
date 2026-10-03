# WORKLOADS — les charges métier de démonstration

> Retour : [README docs](README.md) · Voir aussi : [MULTICLUSTER](MULTICLUSTER.md)

## 1. Pourquoi des workloads « métier » ?

Le service mesure des ressources (CPU, mémoire, stockage, énergie). Pour le démontrer, il faut des applications qui **consomment réellement**. On veut en plus que la charge ait un sens métier et qu'elle varie (pics, tendances, sur- et sous-dimensionnement).

```text
Données synthétiques → application métier → charge Kubernetes → CPU / RAM / réseau / énergie → FinOps / GreenIT
```

**Aucune donnée réelle** : tout est généré, avec une graine (`SEED`) pour la reproductibilité.

## 2. Les trois secteurs

| Secteur | Namespace | Implémentation | Charge produite | Licence |
|---|---|---|---|---|
| Banking | `banking` | `workloads/banking/app.py` (maison) | Transactions synthétiques + scoring de type AML (règles + hachage SHA-256) | celle du dépôt |
| Trading | `trading` | `workloads/trading/app.py` (maison) | Flux d'ordres (arrivées de Poisson) + carnet d'ordres à priorité prix-temps | celle du dépôt |
| E-commerce | `ecommerce` | **Online Boutique** (GoogleCloudPlatform/microservices-demo **v0.10.1**, manifestes amont non modifiés) | 11 microservices + un load generator qui simule des acheteurs | Apache-2.0 |

### Banking — transactions synthétiques
- **Générateur** : comptes fictifs, montants log-normaux (médiane ≈ 90). Environ 2 % de motifs suspects inspirés des typologies AML : *fan-out* (un compte vers 8), *cycle* (A→B→C→D→A), *structuring* (montants juste sous 10 000).
- **Traitement** : règles (structuring, fan-out, gros montant). Un nombre configurable de tours SHA-256 (`WORK_ROUNDS`) simule le coût CPU d'un vrai scoring.
- **Métriques métier** : `banking_transactions_total`, `banking_transactions_flagged_total{rule}`, `banking_scoring_seconds`.
- **Déploiements** : `txn-processor` (3 réplicas, request 1 CPU, **volontairement sur-dimensionné**, PVC `ledger-db`) et `fraud-scorer` (2 réplicas, beaucoup de tours de hachage, bien dimensionné).

**AMLSim** (IBM, Apache-2.0) a été étudié. C'est un générateur **batch** Java qui produit des fichiers CSV de comptes et de transactions ; il ne sert pas une charge continue. Ses typologies ont inspiré les motifs ci-dessus, mais le code n'en dérive pas. Un Job Kubernetes qui pré-génère un jeu AMLSim pour alimenter le processeur est une évolution possible (non implémentée).

### Trading — carnet d'ordres
- **Flux** : prix moyen en marche aléatoire, 70 % d'ordres limites et 30 % d'ordres au marché, intensité oscillante (rafales).
- **Moteur** : carnet à priorité prix-temps (tas binaires), exécutions partielles. Testé : meilleur prix d'abord, un ordre limite qui ne croise pas reste dans le carnet, un ordre au marché ne reste jamais.
- **Métriques** : `trading_orders_total`, `trading_trades_total`, `trading_book_depth`, `trading_matching_seconds`.
- **Déploiements** : `order-matcher` (3000 ordres/s, request 500m, tend à dépasser sa request → **risque de throttling**) et `market-feed`.

Alternative étudiée : **ABIDES** (simulateur de marché multi-agents issu de la recherche). Il est plus réaliste mais lourd, et orienté expérimentation plutôt que service continu. 🔴 Sa licence exacte est à vérifier avant tout usage.

### E-commerce — Online Boutique
- Application Kubernetes réaliste et largement utilisée en démonstration. Son `loadgenerator` (Locust) simule des parcours d'achat.
- Les pods portent déjà `app: <service>`. Le kustomize ajoute `team`, `cost-center` et `environment` aux **templates** de pods, sans toucher aux sélecteurs.
- La version est **épinglée** (v0.10.1). Le build kustomize a été vérifié en V1.

## 3. Placement primary / secondary

| Workload | primary | secondary (standby) |
|---|---|---|
| banking/txn-processor, fraud-scorer | réplicas nominaux | 1 réplica chacun |
| ecommerce (tous services) | réplicas nominaux | 1 réplica, `loadgenerator` à 0 |
| trading/* | réplicas nominaux | **non déployé** → démontre le cas « OUTAGE » d'un failover |

Structure : `workloads/<secteur>/k8s/base` + `overlays/{primary,secondary}` (kustomize).

## 4. Labels nécessaires au service

| Label de pod | Utilisé pour | Métrique kube-state-metrics |
|---|---|---|
| `app` | workload (`WORKLOAD_LABEL`) | `kube_pod_labels{label_app=...}` |
| `team`, `cost-center`, `environment` | attribution / chargeback | `label_team`, `label_cost_center`, `label_environment` |

⚠️ kube-state-metrics v2 n'expose que les labels **autorisés** (`--metric-labels-allowlist`). Sans cette configuration (`scripts/k8s/values-kube-prometheus-stack.yaml`), `kube_pod_labels` est vide : les coûts restent calculés par namespace, mais sans détail par workload ni équipe (`unknown`).

## 5. Deux modes de démonstration

- **Simulation** (laptop, sans Kubernetes) : `exporter/profiles.py` reprend les **mêmes noms** de workloads, avec des profils de charge synthétiques. Le code des applis ci-dessus n'y tourne pas.
- **kind** : les vraies applis tournent ; cAdvisor et kube-state-metrics mesurent leur consommation réelle.
