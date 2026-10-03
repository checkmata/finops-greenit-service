#!/usr/bin/env bash
# =============================================================================
# REAL failover demonstration on the two kind clusters (V2).
#
# The primary cluster is made unavailable with `docker pause` on its node:
# every process of the cluster (API server, kubelet, Prometheus, workloads) is
# frozen — a whole-cluster outage — and fully reversible with `docker unpause`.
# Nothing is deleted.
#
# Requires: the compose stack with FAILOVER_MODE=auto (thresholds from .env).
#   scripts/k8s/failover-demo.sh | tee docs/evidence/failover-demo-$(date +%F).log
# =============================================================================
set -uo pipefail
API="${API:-http://127.0.0.1:8080}"
export PATH="$HOME/.local/bin:$PATH"
ts() { date -u +%H:%M:%S; }
state() { curl -s "$API/api/failover" | "$(dirname "$0")/../../.venv/bin/python" "$(dirname "$0")/../show_failover.py"; }
standby() { kubectl --context kind-secondary get deploy -n ecommerce -n banking 2>/dev/null >/dev/null;
  for ns in banking ecommerce; do kubectl --context kind-secondary -n $ns get deploy --no-headers \
    -o custom-columns=D:.metadata.name,R:.spec.replicas,A:.status.availableReplicas | tr -s ' ' | sed "s#^#   $ns/#"; done; }
wait_for() { # $1 = python condition on the /api/failover json, $2 = timeout seconds
  local end=$(( $(date +%s) + $2 )); while [ "$(date +%s)" -lt "$end" ]; do
    curl -s "$API/api/failover" | python3 -c "import json,sys; b=json.load(sys.stdin); sys.exit(0 if ($1) else 1)" && return 0
    echo "  [$(ts)] $(state)"; sleep 5; done; return 1; }

echo "== [$(ts)] 1. NOMINAL"; state; standby
echo "== [$(ts)] 2. PRIMARY OUTAGE: docker pause primary-control-plane"; docker pause primary-control-plane >/dev/null
echo "== [$(ts)] 3. detection + hysteresis (failure threshold from config)"
wait_for 'b["state"]["active_cluster"] == "secondary"' 180 || { echo "no failover"; docker unpause primary-control-plane; exit 1; }
echo "== [$(ts)] 4. FAILED OVER"; state
curl -s "$API/api/failover" | python3 -c 'import json,sys; e=json.load(sys.stdin)["events"][0]; print(" event:", e["kind"], e["action_status"], "|", e["reason"]); print(" scaled:", e["action_detail"].get("scaled")); print(" not_replicated:", e["action_detail"].get("not_replicated"))'
echo "== [$(ts)] 5. standby promoted — waiting for pods to be Available"
kubectl --context kind-secondary -n ecommerce wait --for=condition=Available deploy --all --timeout=300s >/dev/null && \
kubectl --context kind-secondary -n banking wait --for=condition=Available deploy --all --timeout=300s >/dev/null
standby
echo "   frontend on secondary answers HTTP: $(kubectl --context kind-secondary -n ecommerce run curl-probe --rm -i --restart=Never --image=curlimages/curl:8.10.1 -q -- -s -o /dev/null -w '%{http_code}' http://frontend 2>/dev/null)"
echo "   secondary metrics seen by the service:"; curl -s "$API/api/finops/namespace/ecommerce?cluster=secondary" | python3 -c 'import json,sys; b=json.load(sys.stdin); print("    ecommerce@secondary used $/month:", b["actual_cost_usd"], "| workloads:", len(b["workloads"]))'
echo "== [$(ts)] 6. PRIMARY BACK: docker unpause primary-control-plane"; docker unpause primary-control-plane >/dev/null
echo "== [$(ts)] 7. recovery hysteresis (recovery threshold from config)"
wait_for 'b["state"]["active_cluster"] == "primary"' 300 || { echo "no failback"; exit 1; }
echo "== [$(ts)] 8. FAILED BACK"; state
curl -s "$API/api/failover" | python3 -c 'import json,sys; e=json.load(sys.stdin)["events"][0]; print(" event:", e["kind"], e["action_status"], "|", e["reason"]); print(" restored:", e["action_detail"].get("restored"))'
sleep 5; standby
