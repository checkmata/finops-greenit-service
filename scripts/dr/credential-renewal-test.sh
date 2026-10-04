#!/usr/bin/env bash
# V2.x hardening — REAL test of the automatic actuator credential renewal (docs/DR.md §6).
#
# Run while the platform is FAILED OVER (secondary active) with a short actuator lifetime:
#   ACTUATOR_TOKEN_TTL_SECONDS=600 docker compose … up -d api api-2      (600 s = TokenRequest minimum)
#   scripts/dr/credential-renewal-test.sh [duration_s=1500]
#
# Proves, with the REAL actuator code and the REAL API server:
#   A. continuous actuation: every 20 s the actuator (KubernetesActuator, same token file as
#      the service) probes, reads and scales trading/market-feed on the standby (1→2→1)
#   B. ≥ 2 automatic renewals of the 10-min actuator token, no human action
#   C. the FIRST token is really rejected (401) once expired, while the current one works
#   D. ServiceAccount deleted & recreated mid-test (new UID → old tokens invalid): recovered
#      automatically by the issuer, time to recovery measured
# No registry (ledger) data is read or written. Token values are never printed.
set -uo pipefail
cd "$(dirname "$0")/../.."
DURATION=${1:-1500}
OUT=docs/evidence/credential-renewal-test-$(date +%F).log
API=http://127.0.0.1:8080; API2=http://127.0.0.1:8082
C=finops-greenit-service-api-1
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT"; }
read -r -d '' CREDS_PY <<'PY' || true
import json, sys
b = json.load(sys.stdin)
if b["this_replica"]["is_leader"]:
    c = b["credentials"]["secondary"]; a = c["actuator"]; i = c["issuer"]
    print(f"actuator {a['state']} exp_in={a['expires_in_seconds']}s renewals={a['renewals']} "
          f"reason={a['last_reason']!r} err={a['last_error']!r} | issuer {i['state']} "
          f"exp_in={i['expires_in_seconds']}s renewals={i['renewals']} | probe={b['actuators']['secondary']}")
PY
creds() { for u in $API $API2; do curl -s -m 30 "$u/api/failover" | python3 -c "$CREDS_PY" 2>/dev/null; done; }
renewals() { creds | sed -n 's/.*actuator [a-z]* exp_in=[0-9]*s renewals=\([0-9]*\).*/\1/p' | head -1; }
# the real actuator code, inside the service container, with the service's own config + token file
act() { docker exec -e STEP="$1" $C python -c '
import asyncio, os, yaml
from app.cluster_config import parse_clusters
from app.adapters.outbound.kubernetes_actuator import KubernetesActuator
k=[c for c in parse_clusters(yaml.safe_load(open("/app/config/clusters.kind.yaml"))) if c.kubernetes][0].kubernetes
async def main():
    a=KubernetesActuator(k); p=await a.probe("trading")
    try:
        r=await a.get_replicas("trading","market-feed"); tgt=2 if os.environ["STEP"]=="up" else 1
        await a.scale("trading","market-feed",tgt); r2=await a.get_replicas("trading","market-feed"); res=f"probe={p} replicas {r}->{r2}"
    except Exception as e: res=f"probe={p} ERROR {type(e).__name__}: {str(e)[:60]}"
    await a.close(); print(res)
asyncio.run(main())' 2>&1 | tail -1; }
raw_call() { docker exec -e T="$1" $C python -c '
import httpx, os
r=httpx.get("https://secondary-control-plane:6443/apis/apps/v1/namespaces/trading/deployments", params={"limit":1},
            headers={"Authorization":"Bearer "+os.environ["T"]}, verify="/run/secrets/k8s_secondary_ca"); print(r.status_code)'; }

: > "$OUT"
log "== Credential renewal test — REAL (failed-over platform, secondary active), ${DURATION}s"
log "start: $(creds)"
FIRST=$(docker exec $C cat /var/lib/finops/credentials/actuator.token)   # kept in memory, never printed
log "first actuator token captured (value not logged); direct API call with it → HTTP $(raw_call "$FIRST")"
R0=$(renewals); T0=$(date +%s); step=up; fails=0; ops=0; sa_done=no; first_rejected=no
while [ $(( $(date +%s) - T0 )) -lt "$DURATION" ]; do
  res=$(act $step); ops=$((ops+1)); [[ $res == *"probe=ok replicas"* && $res != *ERROR* ]] || fails=$((fails+1))
  [ $((ops % 6)) -eq 1 ] && log "t+$(( $(date +%s) - T0 ))s actuator: $res | $(creds)"
  [ $step = up ] && step=down || step=up
  if [ "$first_rejected" = no ] && [ $(( $(date +%s) - T0 )) -gt 640 ]; then
    code=$(raw_call "$FIRST"); log "t+$(( $(date +%s) - T0 ))s FIRST token (issued at start, 600 s) → HTTP $code ; current → $(act $step)"
    [ "$code" = 401 ] && first_rejected=yes
  fi
  if [ "$sa_done" = no ] && [ $(( $(date +%s) - T0 )) -gt 800 ]; then
    U0=$(kubectl --context kind-secondary -n finops-system get sa finops-failover-actuator -o jsonpath='{.metadata.uid}')
    kubectl --context kind-secondary -n finops-system delete sa finops-failover-actuator >/dev/null
    TD=$(date +%s); log "t+$(( TD - T0 ))s SA finops-failover-actuator DELETED (simulates the 2026-10-01 recreation)"
    until U1=$(kubectl --context kind-secondary -n finops-system get sa finops-failover-actuator -o jsonpath='{.metadata.uid}' 2>/dev/null) && [ -n "$U1" ] && [ "$U1" != "$U0" ]; do sleep 2; done
    log "t+$(( $(date +%s) - T0 ))s SA recreated by Argo CD self-heal with a NEW uid; actuator now: $(act $step)"
    until [[ $(act $step) == *"probe=ok replicas"* ]]; do sleep 5; done
    log "t+$(( $(date +%s) - T0 ))s actuator RECOVERED automatically $(( $(date +%s) - TD ))s after the deletion | $(creds)"
    sa_done=yes
  fi
  sleep 20
done
act down >/dev/null
R1=$(renewals)
log "end: $(creds)"
log "RESULT ops=$ops failed_ops=$fails automatic_renewals=$((R1-R0)) first_token_rejected_after_expiry=$first_rejected sa_recreation_recovered=$sa_done"
