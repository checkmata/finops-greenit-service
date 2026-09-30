#!/usr/bin/env bash
# =============================================================================
# REAL local multicluster on kind + Docker (V2 — EXECUTED).
#
#   scripts/k8s/kind-up.sh primary      # then: scripts/k8s/kind-up.sh secondary
#
# Per cluster: kind node → kube-prometheus-stack (Prometheus NodePort 30090)
# → Kepler (privileged DaemonSet: reads the HOST's RAPL counters when available)
# → workload images loaded into the node → kustomize overlay of this role.
# =============================================================================
set -euo pipefail
ROLE="${1:?role: primary|secondary}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CTX="kind-$ROLE"
export PATH="$HOME/.local/bin:$PATH"

kind get clusters | grep -qx "$ROLE" || kind create cluster --config "$ROOT/scripts/k8s/kind-$ROLE.yaml" --image kindest/node:v1.31.0

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add kepler https://sustainable-computing-io.github.io/kepler-helm-chart >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version 65.5.0 \
  --kube-context "$CTX" -n monitoring --create-namespace \
  -f "$ROOT/scripts/k8s/values-kube-prometheus-stack.yaml" \
  --set prometheus.prometheusSpec.externalLabels.cluster="$ROLE" --wait --timeout 10m

helm upgrade --install kepler kepler/kepler --version 0.6.2 --kube-context "$CTX" -n kepler \
  --create-namespace --set serviceMonitor.enabled=true --wait --timeout 8m

docker build -q -t finops-demo/banking:local "$ROOT/workloads/banking" >/dev/null
docker build -q -t finops-demo/trading:local "$ROOT/workloads/trading" >/dev/null
kind load docker-image finops-demo/banking:local finops-demo/trading:local --name "$ROLE" >/dev/null

for w in banking trading ecommerce; do
  overlay="$ROOT/workloads/$w/k8s/overlays/$ROLE"
  [ -d "$overlay" ] || continue
  kubectl --context "$CTX" apply -k "$overlay" >/dev/null
  # WARM STANDBY = images already on the node. Deployments kept at 0 replicas
  # never pull their image; on failover the promotion then waits minutes for
  # downloads (observed in the V2 real demo). Pre-pull every image of the overlay.
  kubectl kustomize "$overlay" | awk '/image: / {print $NF}' | tr -d '"' | sort -u | while read -r img; do
    docker exec "$ROLE-control-plane" crictl pull "$img" >/dev/null || echo "WARN: pre-pull failed for $img"
  done
done
for ns in banking trading ecommerce; do
  kubectl --context "$CTX" get ns "$ns" >/dev/null 2>&1 && \
    kubectl --context "$CTX" -n "$ns" wait --for=condition=Available deploy --all --timeout=10m >/dev/null
done
echo "cluster=$ROLE ready — Prometheus on the kind network: http://$ROLE-control-plane:30090"
