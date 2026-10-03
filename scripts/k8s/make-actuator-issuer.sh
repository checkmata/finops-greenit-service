#!/usr/bin/env bash
# ONE-TIME BOOTSTRAP of the actuator credential issuer (V2.x hardening, docs/DR.md §6).
#
#   1. applies gitops/bootstrap/actuator-issuer/rbac.yaml on the cluster (issuer SA + the
#      two TokenRequest-only Roles) — a permission grant, run by a human;
#   2. issues ONE bootstrap token for the issuer (TokenRequest, 24 h) into
#      .secrets/k8s_<cluster>_issuer_token (git-ignored, 0600, never printed).
#
# From then on the service (app/credential_service.py, leader only) renews by itself:
#   issuer token (24 h) renewed every ~hour with its own TokenRequest right,
#   actuator token (1 h) re-issued by the issuer at 50 % of its lifetime or on any 401.
# Re-run this script only if the issuer token was lost/expired (management plane down
# for more than its lifetime): /api/failover reports `credentials.issuer = expired`.
#   scripts/k8s/make-actuator-issuer.sh secondary
set -euo pipefail
ROLE="${1:?cluster role}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$ROOT/.secrets" && chmod 700 "$ROOT/.secrets"
kubectl --context "kind-$ROLE" apply -f "$ROOT/gitops/bootstrap/actuator-issuer/rbac.yaml"
kubectl --context "kind-$ROLE" -n finops-credentials create token finops-actuator-issuer --duration 24h \
  > "$ROOT/.secrets/k8s_${ROLE}_issuer_token"
chmod 600 "$ROOT/.secrets/k8s_${ROLE}_issuer_token"
echo "issuer bootstrapped: .secrets/k8s_${ROLE}_issuer_token (24 h; renewed automatically by the service from now on)"
