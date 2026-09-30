#!/usr/bin/env bash
# Open Cluster Management (upstream of Red Hat ACM) on the kind clusters.
# Hub = `secondary` (self-managed hub, like ACM's local-cluster); both clusters
# are registered as ManagedClusters and labelled with their role.
# The bootstrap token goes to .secrets/ocm_join_cmd (git-ignored) and is never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for c in primary secondary; do kubectl config view --minify --flatten --context "kind-$c" > "$TMP/$c"; done
# clusteradm ignores --context with kind kubeconfigs → one kubeconfig file per cluster.
KUBECONFIG=$TMP/secondary clusteradm init --wait --output-join-command-file .secrets/ocm_join_cmd >/dev/null
chmod 600 .secrets/ocm_join_cmd
TOKEN=$(grep -o -- '--hub-token [^ ]*' .secrets/ocm_join_cmd | awk '{print $2}')
HUB=$(kubectl config view --minify --context kind-secondary -o jsonpath='{.clusters[0].cluster.server}')
for c in primary secondary; do
  # --force-internal-endpoint-lookup: klusterlets use https://secondary-control-plane:6443 (kind network)
  KUBECONFIG=$TMP/$c clusteradm join --hub-token "$TOKEN" --hub-apiserver "$HUB" --cluster-name "$c" \
    --force-internal-endpoint-lookup --wait >/dev/null
done
KUBECONFIG=$TMP/secondary clusteradm accept --clusters primary,secondary --wait >/dev/null
for c in primary secondary; do
  kubectl --context kind-secondary label managedcluster "$c" "finops.io/role=$c" --overwrite >/dev/null
done
kubectl --context kind-secondary get managedclusters
