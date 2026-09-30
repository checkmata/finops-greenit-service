#!/usr/bin/env bash
# Create the failover actuator identity on a cluster and write its SHORT-LIVED
# token + CA into .secrets/ (git-ignored), mounted into the api as Docker secrets.
#   scripts/k8s/make-actuator-secrets.sh secondary [duration=24h]
set -euo pipefail
ROLE="${1:?cluster role}"; DUR="${2:-24h}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$ROOT/.secrets" && chmod 700 "$ROOT/.secrets"
# RBAC (ServiceAccount + namespaced Roles) is GitOps-managed since V2.x (finops-platform app);
# this script only issues the short-lived token.
kubectl --context "kind-$ROLE" -n finops-system get serviceaccount finops-failover-actuator >/dev/null
kubectl --context "kind-$ROLE" -n finops-system create token finops-failover-actuator --duration "$DUR" \
  > "$ROOT/.secrets/k8s_${ROLE}_token"
kind get kubeconfig --internal --name "$ROLE" | "$ROOT/.venv/bin/python" -c '
import sys, yaml, base64
cfg = yaml.safe_load(sys.stdin)
sys.stdout.write(base64.b64decode(cfg["clusters"][0]["cluster"]["certificate-authority-data"]).decode())' \
  > "$ROOT/.secrets/k8s_${ROLE}_ca.crt"
chmod 600 "$ROOT/.secrets/"*
echo "wrote .secrets/k8s_${ROLE}_token (valid $DUR) and .secrets/k8s_${ROLE}_ca.crt"
