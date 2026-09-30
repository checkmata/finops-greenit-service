# FAILOVER — hystérésis, actuation contrôlée, démonstration réelle (V2, complété en V2.x)

> Retour : [README docs](README.md) · Code : `app/domain/failover_controller.py` (pur), `app/failover_service.py`, `app/adapters/outbound/kubernetes_actuator.py` · Preuve : [evidence/failover-demo-2026-09-29.log](evidence/failover-demo-2026-09-29.log)

## 1. Ce que fait le service, et ce qu'il ne fait pas

| Fait (V2) ✅ | Ne fait pas |
|---|---|
| détecte la santé de chaque cluster (Prometheus joignable, kube-state-metrics `up`, déclaration opérateur) | déplacer le trafic (DNS, load balancer global) |
| applique une **hystérésis** configurable avant de basculer, puis avant de revenir | répliquer les **données** (bases, volumes) : ce n'est pas un plan de reprise complet |
| journalise chaque transition avec sa raison (table `failover_events`) | créer des Deployments absents du standby (trading : `not_replicated`) |
| **promeut le standby** : met à l'échelle les Deployments autorisés sur le secondary (mode `auto` ou après approbation humaine) | supprimer quoi que ce soit |
| **restaure exactement** les réplicas au retour (fail-back) | |

## 2. Hystérésis (domaine pur)

```text
PRIMARY_ACTIVE ──(primary en échec ≥ FAILOVER_FAILURE_THRESHOLD_SECONDS et secondary sain)──► FAILED_OVER
FAILED_OVER    ──(primary sain   ≥ FAILOVER_RECOVERY_THRESHOLD_SECONDS)──────────────────────► PRIMARY_ACTIVE
```

Toute observation qui interrompt une série remet son chronomètre à zéro. Tests (`tests/unit/test_v2_failover_controller.py`) :
- une coupure brève ne déclenche rien ;
- le basculement a lieu exactement au seuil ;
- un primary qui oscille toutes les 10 s pendant 10 min ne cause **aucun** aller-retour ;
- le retour exige la stabilité.

## 3. Modes d'action (`FAILOVER_MODE`)

| Mode | Effet d'une transition |
|---|---|
| `decision_only` | décision journalisée, rien n'est modifié |
| `manual` (**défaut**) | événement `awaiting_approval` ; un humain exécute avec `POST /api/failover/events/{id}/approve` (ou `reject`) |
| `auto` | exécution immédiate (utilisée pour la démonstration) |

Garde-fous :
- namespaces autorisés (`FAILOVER_NAMESPACES`) ;
- plafond de réplicas (`FAILOVER_MAX_REPLICAS`) ;
- Deployments exclus (`FAILOVER_EXCLUDE_DEPLOYMENTS`, par défaut `loadgenerator`) ;
- identité Kubernetes **minimale** (`manifests/failover-actuator-rbac.yaml`), vérifiée avec `kubectl auth can-i` : elle ne peut que lire des Deployments et changer leur nombre de réplicas dans `banking`/`ecommerce`/`trading` (trading depuis V2.x). Ni création, ni suppression, ni accès aux Secrets ;
- **exécution sur le leader uniquement** (voir HA ci-dessous).

## 4. Démonstration réelle (kind, 29/09/2026, mode `auto`, seuils 30 s / 60 s)

```text
22:46:57  docker pause primary-control-plane      (tout le cluster primary gelé)
22:47:13  détection (probe Prometheus : timeout 10 s + tick 5 s)
22:47:13→22:47:43  hystérésis "primary failing …/30s"
22:47:43  FAILOVER exécuté : 7 Deployments promus sur secondary
          trading/market-feed, trading/order-matcher → not_replicated (non déployés sur le standby)
+30 s     tous les pods Available ; frontend du secondary → HTTP 200 ; le service lit les métriques du secondary
22:48:13  docker unpause primary-control-plane
22:48:24  oscillation RÉELLE pendant la reprise du nœud → chronomètre de retour remis à zéro
22:49:29  FAILBACK après 60 s de stabilité → les 7 Deployments reviennent exactement à leurs valeurs
```

Rejouer : `scripts/k8s/failover-demo.sh` (le journal complet est dans `docs/evidence/`).

La première exécution a révélé deux défauts, corrigés depuis :
1. une erreur 403 au milieu de la promotion faisait perdre la trace des changements déjà faits, et le fail-back ne restaurait rien. Désormais la progression partielle est toujours enregistrée ;
2. les images des Deployments à 0 réplica n'étaient pas présentes sur le standby, et la promotion attendait leur téléchargement plusieurs minutes. Désormais les images sont pré-tirées.

Les deux événements (échec puis succès) restent visibles dans l'historique : ils n'ont pas été effacés.

## 5. HA et exclusivité

Le contrôleur (et le scheduler) ne tournent que sur la réplique qui détient le verrou consultatif PostgreSQL. Sur 2 répliques réelles :
- un seul leader ;
- aucun cycle écrit deux fois ;
- arrêt du leader → reprise en ~2,8 s ;
- crash simulé (connexion tuée) → verrou libéré.

L'état du contrôleur est en base : un nouveau leader le reprend là où il en était.

## 6. Limites restantes

- Pas de réplication des manifestes ni des données. Promouvoir un workload suppose qu'il existe déjà sur le standby (trading ne l'est pas, d'où `not_replicated`). GitOps/ACM : V3+.
- Pas de bascule du trafic utilisateur (DNS / load balancer global).
- La détection dépend du timeout de la sonde Prometheus (10 s) et du tick : ~15 s avant le début de l'hystérésis.
- Les deux clusters partagent le même hôte : une vraie panne matérielle les toucherait tous les deux.
- Le token de l'actionneur dure 24 h (à renouveler ; en production : projected ServiceAccount token).

## 7. V2.x — données, GitOps et reprise complète

- **GitOps** : sur le standby, les `replicas` appartiennent au contrôleur (Argo CD les ignore, test C1) ; le reste est géré par Git. Trading est maintenant déclaré sur le standby, donc promu au failover (plus de `not_replicated`).
- **Données** : au failover, le registre banking du standby est **promu avant** le scaling. Une copie inscriptible restée sur un timeline plus ancien est **isolée** par la garde de données (boucle de 1 s, ≤ 1,7 s mesuré). Le **failback est conditionné** à l'état des données : il n'a lieu qu'une fois le primary redevenu la copie de référence (`scripts/dr/ledger-failback.sh`).
- **Actionneur** : Roles par namespace (plus de ClusterRole) ; jeton relu à chaque appel (rotation sans redémarrage) ; `GET /api/failover` expose `actuators` (sonde d'identité) et `data` (état de chaque copie).
- **Corrigé** : un failback n'annule plus que le failover de **son** épisode, jamais un plus ancien (bug trouvé en conditions réelles).
- Mesures complètes et limites : [DR.md](DR.md).
