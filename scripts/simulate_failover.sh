#!/usr/bin/env bash
# =============================================================================
# Failover DEMONSTRATION on the local simulation (scripts/run_local_demo.sh or
# docker compose). Shows the service's DECISION changing; it does not (and
# cannot) move real traffic — see docs/FAILOVER.md.
#
#   1. nominal            → placements on primary
#   2. primary goes dark  → simulator stops answering (Prometheus up=0 / unreachable)
#   3. traffic "moves"    → we scale the secondary standby (what GitOps/ACM would do)
#   4. fail-back          → primary returns, standby scaled back
# =============================================================================
set -euo pipefail
API="${API:-http://localhost:8080}"
PRI="${PRIMARY_ADMIN:-http://localhost:8001}"
SEC="${SECONDARY_ADMIN:-http://localhost:8002}"
WAIT="${WAIT:-25}"   # > Prometheus scrape interval so up{} reflects the change

show() { curl -s "$API/api/clusters" | python3 "$(dirname "$0")/show_clusters.py"; }

echo "== 1. Nominal"; show
echo; echo "== 2. Primary cluster down (simulated)"
curl -s -XPOST "$PRI/cluster/down" >/dev/null; sleep "$WAIT"; show
echo; echo "== 3. Standby takes the load (manual/GitOps step, simulated)"
curl -s -XPOST "$SEC/workloads/ecommerce/frontend/replicas/2" >/dev/null
curl -s -XPOST "$SEC/workloads/ecommerce/frontend/load/8" >/dev/null
curl -s -XPOST "$SEC/workloads/banking/txn-processor/load/6" >/dev/null
sleep "$WAIT"; curl -s "$API/api/finops/namespace/ecommerce?cluster=secondary" | \
  python3 -c 'import json,sys; print("secondary ecommerce, used cost $/month:", json.load(sys.stdin)["actual_cost_usd"])'
echo; echo "== 4. Fail-back"
curl -s -XPOST "$PRI/cluster/up" >/dev/null
curl -s -XPOST "$SEC/workloads/ecommerce/frontend/replicas/1" >/dev/null
curl -s -XPOST "$SEC/workloads/ecommerce/frontend/load/1" >/dev/null
curl -s -XPOST "$SEC/workloads/banking/txn-processor/load/1" >/dev/null
sleep "$WAIT"; show
