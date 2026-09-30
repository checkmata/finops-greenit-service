# GITOPS — OCM + Argo CD sur les deux clusters (V2.x)

> Retour : [README docs](README.md) · Réplication : [REPLICATION](REPLICATION.md) · Reprise : [DR](DR.md) · Preuve : [evidence/gitops-validation-2026-09-30.log](evidence/gitops-validation-2026-09-30.log)

## 1. Choix de l'outillage

| Option | Multicluster / placement | GitOps | Politiques | OpenShift | Test local réel | Verdict |
|---|---|---|---|---|---|---|
| **Red Hat ACM** | ✅ | via OpenShift GitOps | ✅ | natif | ❌ exige OpenShift + souscription | **BLOCKED** en local ; architecture compatible |
| **Open Cluster Management** (amont d'ACM) | ✅ ManagedCluster, Placement | intégration Argo CD | addon (non installé) | ✅ | ✅ sur kind | **retenu** (gestion de flotte) |
| **Argo CD** (= OpenShift GitOps) | ApplicationSet | ✅ | — | ✅ | ✅ | **retenu** (moteur GitOps) |
| Flux | Kustomization par cluster | ✅ | — | ✅ | ✅ | écarté : plus léger, mais ce n'est pas ce qu'utilisent ACM ou OpenShift |

Ce qui a été exécuté : OCM 1.3.1 (`clusteradm`) + Argo CD **v3.5.3 en mode core** (sans API server, UI ni Dex : l'hôte n'a que ~300 Mo de RAM libre).

## 2. Topologie

```text
Gitea (docker, réseau kind)  ◄── scripts/gitops/publish.sh (dépôt finops/platform-gitops, LOCAL uniquement)
        │  poll 60 s
        ▼
HUB = cluster secondary (hub auto-géré, comme le local-cluster d'ACM)
  ├─ OCM cluster-manager ── ManagedCluster primary / secondary (label finops.io/role)
  │     Placement workload-clusters ─► PlacementDecision [primary, secondary]
  └─ Argo CD (core) ── ApplicationSet (clusterDecisionResource = la décision OCM)
        ├─► primary   : banking, ecommerce, trading, finops-platform
        └─► secondary : banking, ecommerce, trading, finops-platform
```

Limite assumée : le hub est sur `secondary`. Si `secondary` tombe, on perd la gestion GitOps (pas les workloads du primary). En production, le hub serait un cluster de management dédié.

## 3. Organisation du dépôt

| Chemin | Contenu |
|---|---|
| `workloads/<app>/k8s/base` + `overlays/{primary,secondary}` | les workloads (source unique, déjà existante) |
| `manifests/` | composants du service dans les clusters (RBAC de l'actionneur, proxy d'auth Prometheus) |
| `gitops/bootstrap/argocd/` | Argo CD core, AppProject `finops`, liaison OCM → ApplicationSet |
| `gitops/bootstrap/ocm/` | ManagedClusterSetBinding + Placement |
| `gitops/bootstrap/argocd-manager/` | identité **à droits minimaux** d'Argo CD sur primary (appliquée par un humain) |
| `gitops/platform/overlays/{primary,secondary}` | ce qui diffère entre les clusters côté plateforme |
| `gitops/apps/workloads.yaml` | ApplicationSets `workloads` et `finops-platform` |

Différences primary / secondary : réplicas (warm / cold standby), ressources, rôle du registre (`LEDGER_ROLE`), proxy d'authentification (secondary seulement). Chacune est visible dans un overlay.

## 4. Qui possède quoi (point critique avec le failover)

| Champ | Primary | Secondary (standby) |
|---|---|---|
| Tout, sauf les exceptions ci-dessous | Git (self-heal) | Git (self-heal) |
| `Deployment.spec.replicas` | Git | **contrôleur de failover** (`ignoreDifferences`) |
| `StatefulSet.volumeClaimTemplates[].status` | API server (non déclarable) | idem |
| Rôle effectif de la base (primaire / standby) | état des données. Git ne déclenche qu'une **reconstruction explicite** (`LEDGER_RESYNC_GENERATION`) | idem |

Sans cette répartition, Argo CD annulerait un failover en quelques secondes (test B : dérive corrigée en 2,3 s).

## 5. Sécurité

- **AppProject `finops`** : seulement notre dépôt, nos 2 clusters, et le seul type cluster-wide `Namespace`. Il n'existe pas de projet `default`.
- **Identité d'Argo CD sur primary** : rôle `admin` limité à banking, ecommerce, trading, monitoring et finops-system, plus la lecture des namespaces. Vérifié : `can-i create deployments -n kube-system` = **no**.
- `resource.inclusions` : Argo CD ne surveille que les types d'objets utilisés, et **pas les Secrets**.
- **Aucun Secret dans Git.** Les identifiants (Gitea, registre, jetons) sont générés dans `.secrets/`, ignoré par Git, puis créés dans les clusters par des scripts.
- **Actionneur** : Roles par namespace (plus de ClusterRole). Il peut scaler, mais `can-i delete` = **no**.
- ⚠️ Sur le hub, Argo CD utilise son compte de service in-cluster, qui a des droits larges d'après l'installation officielle. À réduire sur un hub dédié.

## 6. Validation réelle (30/09/2026) — `scripts/dr/gitops-validation.sh`

| Test | Résultat |
|---|---|
| A. commit dans Gitea → annotation présente dans primary | ✅ **88,7 s** (sondage toutes les 60 s ; un premier essai a pris 6,4 s) |
| B. `kubectl scale` manuel sur primary (Git = 1, cluster = 3) | ✅ dérive corrigée par self-heal en **2,3 s** |
| C1. `replicas` du standby modifiés comme le ferait le failover | ✅ **non** annulés après 90 s (l'application reste `Synced`) |
| C2. champ géré par Git (`env`) modifié sur le standby | ✅ restauré en **1,1 s** |
| A′. `git revert` | ✅ annotation retirée en **87,6 s** |

État permanent vérifié par `tests/infra/test_real_gitops_replication.py` : décision OCM = {primary, secondary}, 8/8 applications `Synced` / `Healthy`, et chaque workload du primary est déclaré sur le standby.

## 7. Reproduire

```bash
scripts/gitops/gitea-up.sh          # Gitea + dépôt (mot de passe dans .secrets/)
scripts/gitops/ocm-up.sh            # hub OCM sur secondary + enregistrement des 2 clusters
# étape HUMAINE (octroi de droits) : docs/GITOPS.md §5 + gitops/bootstrap/argocd-manager/rbac.yaml
scripts/gitops/publish.sh "message" # publie l'état déclaratif dans Gitea
scripts/gitops/argocd-up.sh         # Argo CD core + placement + ApplicationSets
scripts/dr/gitops-validation.sh     # tests A, B, C, A′ avec preuve
```

## 8. Non fait

- Pas de politiques OCM (addon governance-policy) : faute de RAM. **DEFERRED**.
- Pas de webhook Gitea → Argo CD : on sonde toutes les 60 s, d'où une latence commit → cluster de 6 à 90 s.
- Kube-prometheus-stack et Kepler restent installés par Helm (`kind-up.sh`) et ne sont pas encore gérés par Argo CD. **PARTIAL**.
