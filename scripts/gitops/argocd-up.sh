#!/usr/bin/env bash
# Argo CD (core) + OCM placement integration on the hub, then the ApplicationSets.
# Cluster credentials / RBAC grants are NOT handled here: they are a human step
# (see docs/GITOPS.md § "Enregistrement des clusters").
set -euo pipefail
cd "$(dirname "$0")/../.."
HUB=kind-secondary
kubectl --context $HUB apply --server-side -k gitops/bootstrap/argocd >/dev/null
kubectl --context $HUB -n argocd rollout restart deploy/argocd-repo-server statefulset/argocd-application-controller >/dev/null
kubectl --context $HUB -n argocd rollout status deploy/argocd-repo-server --timeout=300s
kubectl --context $HUB -n argocd rollout status statefulset/argocd-application-controller --timeout=300s
kubectl --context $HUB apply -f gitops/bootstrap/ocm/placements.yaml >/dev/null
for c in primary secondary; do kubectl --context $HUB label managedcluster "$c" "finops.io/role=$c" --overwrite >/dev/null; done
kubectl --context $HUB apply -f gitops/apps/workloads.yaml >/dev/null
kubectl --context $HUB -n argocd get placementdecisions,applicationsets,applications
