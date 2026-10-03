# REPLICATION — workloads et données (V2.x)

> Retour : [README docs](README.md) · GitOps : [GITOPS](GITOPS.md) · Reprise : [DR](DR.md)

On ne confond jamais **manifests répliqués** (le standby sait lancer le workload) et **données répliquées** (le standby a l'état métier).

## 1. Par workload (vérifié sur les clusters réels)

| Workload | Nature | Manifests | Pods sur le standby | Services | Config | Données | Statut |
|---|---|---|---|---|---|---|---|
| **banking** txn-processor, fraud-scorer | sans état (calcul) | ✅ GitOps | warm : 1 + 1 | ✅ | ✅ | — | **REPLICATED** |
| **banking** ledger-db (PostgreSQL 16) | **avec état** | ✅ GitOps | hot standby | ✅ + NodePort 30432 | ✅ | ✅ **réplication en flux, asynchrone** | **REPLICATED (données)** |
| **banking** PVC `ledger-db` (V1) | monté mais inutilisé | ✅ | ✅ | — | — | ❌ non répliqué (aucune donnée) | n/a |
| **ecommerce** (11 services) | sans état | ✅ GitOps | warm : 5 actifs, 6 à 0 | ✅ 12/12 | ✅ | — | **REPLICATED** |
| **ecommerce** redis-cart | état en `emptyDir` | ✅ | ✅ | ✅ | ✅ | ❌ **non répliqué** : les paniers en cours sont perdus au failover | manifests only |
| **trading** order-matcher, market-feed | carnet d'ordres **en mémoire** | ✅ GitOps (depuis V2.x) | cold : 0 | aucun service déclaré | ✅ | ❌ **non répliqué** : le carnet repart vide | **manifests only** |

Comptes d'objets identiques sur les deux clusters (Deployments, StatefulSets, Services, ConfigMaps, PVC) :
- banking : 2 / 1 / 2 / 1 / 2 ;
- ecommerce : 12 / 0 / 12 / 0 / 0 ;
- trading : 2 / 0 / 0 / 0 / 0.

## 2. Données : fiche par source

| Donnée | Source → destination | Mécanisme | Fréquence | RPO | RTO | Cohérence | En cas de panne |
|---|---|---|---|---|---|---|---|
| **Registre banking** | ledger-db primary → ledger-db secondary | réplication en flux PostgreSQL (asynchrone), NodePort 30432 | continue | **mesuré : 0 ligne perdue** avant le gel (retard ≤ 0,08 s) ; lignes écrites dans la fenêtre de split-brain : voir §4 | **53,6 s** (run 3) | par transaction, sur un seul timeline | promotion par le contrôleur ; ancienne copie isolée |
| **TimescaleDB du service** (historique FinOps) | — | **aucun** | — | depuis sa création | reconstruction depuis Prometheus (7 j de rétention) | — | ❌ **non répliqué** : plan de gestion sur un seul hôte |
| Paniers e-commerce (redis-cart) | — | aucun (`emptyDir`) | — | tout | immédiat (vide) | — | perdus |
| Carnet d'ordres trading | — | aucun (mémoire) | — | tout | au redémarrage | — | reconstruit vide |
| Manifests / configuration | Git (Gitea) → les deux clusters | Argo CD | sondage 60 s | dernier commit | 6–90 s | déclaratif | Gitea sur l'hôte : point unique de défaillance |

## 3. Conception du registre

- **Rôle déclaré dans Git** (`LEDGER_ROLE`), mais **aucune donnée n'est effacée sur un simple changement de rôle**. Un volume n'est reconstruit que si `role=standby` **et** que `LEDGER_RESYNC_GENERATION` change : c'est une décision explicite, dans un commit.
- **Failover** : le service **promeut** le standby (`pg_promote`) **avant** de scaler les workloads. Les writers du standby, qui ne faisaient rien tant que la base était en recovery, se mettent alors à écrire d'eux-mêmes.
- **Anti split-brain** (`app/domain/data_replication.py`) : si deux copies sont inscriptibles, celle du **timeline le plus ancien** est isolée. Son rôle applicatif passe en `NOLOGIN` et ses sessions sont coupées.
  - Cela tourne dans une boucle dédiée toutes les 1 s (`run_data_guard_loop`, sur le leader uniquement).
  - La première implémentation, dans la boucle de failover, attendait derrière les timeouts Prometheus : 11,3 s et 1 300 lignes exposées.
- **Failback conditionné aux données** : tant que la copie du primary n'est pas redevenue la copie inscriptible de référence, le primary compte comme « non rétabli » pour l'hystérésis. Les workloads ne reviennent donc jamais sur une base obsolète ou en reconstruction.
- **Failback des données** (`scripts/dr/ledger-failback.sh`), dans cet ordre :
  1. resync du primary depuis le secondary (Git) ;
  2. suspension des écritures sur le secondary ;
  3. attente de l'égalité des LSN (**zéro perte**) ;
  4. promotion du primary ;
  5. le secondary redevient standby (Git).

## 4. Ce que « RPO = 0 » veut dire ici (honnêteté)

- **Toutes les transactions validées sur le primary avant la panne sont sur la copie promue** : 0 perdue sur les 3 runs réels (69 500, 89 300 puis 113 000 lignes vérifiées par empreinte md5).
- La réplication est **asynchrone**. Le RPO garanti est donc « le retard de réplication au moment de la panne ». Mesuré à 0,002–0,08 s, il n'est pas garanti nul.
- **Fenêtre de split-brain** : quand l'ancien primary revient, ses writers locaux écrivent avant d'être isolés. Ces écritures ont été acquittées, puis sont effacées à la resynchronisation.

  | Run | Lignes concernées | Délai d'isolement |
  |---|---|---|
  | 1 | 100 | ~2 s |
  | 2 | 1 300 | 11,3 s, avant la boucle dédiée |
  | 3 | 100 | ≤ 1,7 s |

  **Ce sont des pertes pour le client.** Pour les supprimer, il faudrait l'un ou l'autre :
  - une réplication **synchrone** (l'ancien primary ne peut plus acquitter sans son standby), au prix de la disponibilité quand le standby est absent ;
  - un isolement au niveau du cluster (STONITH : couper les writers du primary au retour).
  
  Aucune de ces deux options n'est implémentée : **PARTIAL**.
