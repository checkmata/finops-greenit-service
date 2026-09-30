#!/usr/bin/env bash
# =============================================================================
# Create ONE real Kubernetes cluster for the demo and install everything on it.
# EXECUTED in V2 (minikube + KVM, no root needed — user in the libvirt/kvm groups).
#
#   scripts/k8s/cluster-up.sh <primary|secondary> <cpus> <memory_mb>
#
# Steps: minikube VM (kvm2, Docker runtime, joined to the shared libvirt
# "default" network so clusters can reach each other) → kube-prometheus-stack
# (NodePort 30090) → Kepler (model mode in VMs: no RAPL) → workload images built
# in the node's Docker → kustomize overlay of this role.
# =============================================================================
set -euo pipefail
ROLE="${1:?role: primary|secondary}"; CPUS="${2:-2}"; MEM="${3:-3072}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
K8S_VERSION="${K8S_VERSION:-v1.31.4}"
export PATH="$HOME/.local/bin:$PATH"

minikube status -p "$ROLE" >/dev/null 2>&1 || minikube start -p "$ROLE" --driver=kvm2 --kvm-network=default \
  --container-runtime=docker --cpus="$CPUS" --memory="$MEM" --disk-size=20g --kubernetes-version="$K8S_VERSION"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add kepler https://sustainable-computing-io.github.io/kepler-helm-chart >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version 65.5.0 \
  --kube-context "$ROLE" -n monitoring --create-namespace \
  -f "$ROOT/scripts/k8s/values-kube-prometheus-stack.yaml" \
  --set prometheus.prometheusSpec.externalLabels.cluster="$ROLE" --wait --timeout 10m

# Kepler mounts /usr/src (kernel headers) as a hostPath; the minikube image has
# none. Kepler 0.8 uses the kernel BTF instead, so an empty dir is enough.
minikube -p "$ROLE" ssh -- sudo mkdir -p /usr/src
helm upgrade --install kepler kepler/kepler --version 0.6.2 --kube-context "$ROLE" -n kepler \
  --create-namespace --set serviceMonitor.enabled=true --wait --timeout 8m

eval "$(minikube -p "$ROLE" docker-env --shell bash)"
docker build -q -t finops-demo/banking:local "$ROOT/workloads/banking" >/dev/null
docker build -q -t finops-demo/trading:local "$ROOT/workloads/trading" >/dev/null
eval "$(minikube -p "$ROLE" docker-env --unset --shell bash)"

for w in banking trading ecommerce; do
  overlay="$ROOT/workloads/$w/k8s/overlays/$ROLE"
  [ -d "$overlay" ] && kubectl --context "$ROLE" apply -k "$overlay" >/dev/null
done
for ns in banking trading ecommerce; do
  kubectl --context "$ROLE" get ns "$ns" >/dev/null 2>&1 && \
    kubectl --context "$ROLE" -n "$ns" wait --for=condition=Available deploy --all --timeout=10m >/dev/null
done

SHARED_IP=$(minikube -p "$ROLE" ssh -- "ip -4 -o addr show eth1 | awk '{print \$4}' | cut -d/ -f1" | tr -d '\r')
echo "cluster=$ROLE ready — Prometheus: http://${SHARED_IP}:30090 (shared libvirt network)"
