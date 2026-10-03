#!/usr/bin/env bash
# V2.x — REAL long-outage regression test (incident of 2026-10-01, docs/DR.md §5).
#
# A short `docker pause` (~1 min, the DR runs) never let OCM mark a cluster unavailable.
# A long outage does: OCM taints the ManagedCluster (unreachable/unavailable, NoSelect).
# Before the fix this removed `primary` from the PlacementDecision → the ApplicationSet
# deleted its Applications → Argo CD cascade-deleted the failed site's workloads.
#
# This test freezes the primary node for OUTAGE_SECONDS (default 420 s) and asserts,
# DURING the outage, that:
#   1. OCM really sees the outage (taint present / Available != True)  — else the test is void
#   2. `primary` stays in the PlacementDecision                         (Placement tolerations)
#   3. the primary Applications are neither deleted nor recreated        (same UIDs)
# and AFTER it, that every workload object of the primary is the SAME object (same UIDs).
# Nothing is deleted; `docker pause` is reversible and does not touch any data.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUTAGE=${OUTAGE_SECONDS:-420}
OUT=${1:-docs/evidence/long-outage-test-$(date +%F).log}
S="kubectl --context kind-secondary"; P="kubectl --context kind-primary"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT"; }
apps_uids() { $S -n argocd get applications -o jsonpath='{range .items[*]}{.metadata.name}={.metadata.uid}{"\n"}{end}' | grep -- '-primary=' | sort; }
wl_uids() { for ns in banking ecommerce trading; do $P -n "$ns" get deploy,sts,svc,pvc -o jsonpath='{range .items[*]}{.kind}/{.metadata.name}={.metadata.uid}{"\n"}{end}'; done | sort; }
fail() { log "FAIL — $*"; docker unpause primary-control-plane >/dev/null 2>&1 || true; exit 1; }

: > "$OUT"
log "== Long-outage test — REAL — primary frozen for ${OUTAGE}s"
APPS0=$(apps_uids); WL0=$(wl_uids)
log "before: $(echo "$APPS0" | wc -l) primary Applications, $(echo "$WL0" | wc -l) primary workload objects; decision: $($S -n argocd get placementdecisions -o jsonpath='{.items[0].status.decisions[*].clusterName}')"
docker pause primary-control-plane >/dev/null
T0=$(date +%s); seen_outage=no
while [ $(( $(date +%s) - T0 )) -lt "$OUTAGE" ]; do
  taints=$($S get managedcluster primary -o jsonpath='{.spec.taints[*].key}')
  avail=$($S get managedcluster primary -o jsonpath='{.status.conditions[?(@.type=="ManagedClusterConditionAvailable")].status}')
  decision=$($S -n argocd get placementdecisions -o jsonpath='{.items[0].status.decisions[*].clusterName}')
  [ -n "$taints" ] && [ "$seen_outage" = no ] && { seen_outage=yes; log "t+$(( $(date +%s) - T0 ))s OCM marks primary down: Available=$avail taints=[$taints]"; }
  [[ " $decision " == *" primary "* ]] || fail "t+$(( $(date +%s) - T0 ))s primary REMOVED from the PlacementDecision (decision: $decision)"
  [ "$(apps_uids)" = "$APPS0" ] || fail "t+$(( $(date +%s) - T0 ))s primary Applications deleted/recreated"
  sleep 10
done
log "t+${OUTAGE}s end of outage: decision still [$($S -n argocd get placementdecisions -o jsonpath='{.items[0].status.decisions[*].clusterName}')], primary Applications unchanged"
[ "$seen_outage" = yes ] || fail "OCM never tainted primary during ${OUTAGE}s: the test did not reproduce a long outage (increase OUTAGE_SECONDS)"
docker unpause primary-control-plane >/dev/null
log "primary unpaused; waiting for OCM to clear the taint"
timeout 300 bash -c "until [ -z \"\$($S get managedcluster primary -o jsonpath='{.spec.taints[*].key}')\" ]; do sleep 5; done" || log "taint still present after 300 s"
timeout 300 bash -c "until $P get nodes >/dev/null 2>&1; do sleep 5; done"
WL1=$(wl_uids)
[ "$WL1" = "$WL0" ] || fail "primary workload objects changed: $(diff <(echo "$WL0") <(echo "$WL1") | head -5 | tr '\n' ' ')"
[ "$(apps_uids)" = "$APPS0" ] || fail "primary Applications changed after the outage"
log "PASS — after a ${OUTAGE}s outage seen by OCM, primary stayed a target, its Applications and all $(echo "$WL0" | wc -l) workload objects are the same objects"
log "apps: $($S -n argocd get applications --no-headers | awk '{print $1":"$2"/"$3}' | tr '\n' ' ')"
