#!/usr/bin/env bash
# V2.x — CONTROLLED data failback of the banking ledger, then workload failback.
#
#   after a failover: secondary ledger = authoritative (promoted, newer timeline)
#                     primary ledger   = stale copy, fenced by the controller
#
#   1. Git: primary ledger becomes a STANDBY of the secondary (resync generation +1)
#      → Argo CD restarts it → it rebuilds from a base backup of the secondary.
#   2. wait until it streams; 3. quiesce writes on the secondary (fence its app role);
#   4. wait until the primary has replayed EVERYTHING (LSN equality → zero data loss);
#   5. promote the primary ledger, re-enable its app role;
#   6. Git: primary role back to primary, secondary becomes a standby again (generation +1);
#   7. wait until the secondary streams from the primary;
#   8. the failover controller, whose failback was gated on the data, now fails the
#      workloads back after its stability period; 9. consistency check.
#
# Refuses to run unless the preconditions prove the primary copy is the stale one.
# Usage: scripts/dr/ledger-failback.sh [evidence.log]
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=${1:-docs/evidence/dr-failback-$(date +%F).log}
PO=workloads/banking/k8s/overlays/primary/kustomization.yaml
SO=workloads/banking/k8s/overlays/secondary/kustomization.yaml
T0=$(date +%s.%N)
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$OUT"; }
el() { python3 -c "import sys,time; print(f'{time.time()-float(sys.argv[1]):.1f}')" "$1"; }
sqlp() { kubectl --context "kind-$1" -n banking exec ledger-db-0 -- psql -U postgres -d ledger -Atc "$2" 2>/dev/null; }
api() { curl -s -m 30 http://127.0.0.1:8080/api/failover; }
jq_() { python3 -c "import json,sys; b=json.load(sys.stdin); print($1)"; }
wait_until() { # <timeout> <description> <command>
  local end=$(( $(date +%s) + $1 ))
  until eval "$3"; do [ "$(date +%s)" -lt "$end" ] || { log "TIMEOUT: $2"; exit 1; }; sleep 2; done; }
setenv() { sed -i "s/- {name: $2, value: [^}]*}/- {name: $2, value: $3}/" "$1"; }
gen() { grep -o 'LEDGER_RESYNC_GENERATION, value: "[0-9]*"' "$1" | grep -o '[0-9]*'; }

: > "$OUT"
log "== Controlled failback (data first, then workloads) — REAL"
ACTIVE=$(api | jq_ 'b["state"]["active_cluster"]')
P_REC=$(sqlp primary "SELECT pg_is_in_recovery()"); P_LOGIN=$(sqlp primary "SELECT rolcanlogin FROM pg_roles WHERE rolname='ledger_app'")
S_REC=$(sqlp secondary "SELECT pg_is_in_recovery()")
P_TLI=$(sqlp primary "SELECT ('x'||substr(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::int")
S_TLI=$(sqlp secondary "SELECT ('x'||substr(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::int")
log "0. PRECONDITIONS active=$ACTIVE | primary ledger recovery=$P_REC login=$P_LOGIN timeline=$P_TLI | secondary recovery=$S_REC timeline=$S_TLI"
if [ "$ACTIVE" != secondary ] || [ "$P_LOGIN" != f ] || [ "$S_REC" != f ] || [ "$P_TLI" -ge "$S_TLI" ]; then
  log "ABORT: the primary copy is not provably the stale one — nothing changed"; exit 1; fi

G=$(( $(gen "$PO") + 1 ))
setenv "$PO" LEDGER_ROLE standby; setenv "$PO" LEDGER_RESYNC_GENERATION "\"$G\""
scripts/gitops/publish.sh "ledger failback 1/2: primary ledger resyncs from secondary (generation $G)" | tail -1 | tee -a "$OUT"
log "1. Git: primary ledger → standby of secondary (generation $G); waiting for Argo CD + base backup"
wait_until 600 "primary resync" '[ "$(sqlp primary "SELECT status FROM pg_stat_wal_receiver")" = streaming ]'
log "2. primary ledger rebuilt and STREAMING from secondary (t+$(el "$T0")s); rows: $(sqlp primary 'SELECT count(*) FROM ledger')"

TQ=$(date +%s.%N)
sqlp secondary "ALTER ROLE ledger_app NOLOGIN" >/dev/null
sqlp secondary "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename='ledger_app'" >/dev/null
S_LSN=$(sqlp secondary "SELECT pg_current_wal_lsn()")
log "3. QUIESCE secondary writes (app role NOLOGIN); last secondary LSN $S_LSN"
wait_until 120 "replay catch-up" '[ "$(sqlp primary "SELECT pg_last_wal_replay_lsn() >= '"'"'$S_LSN'"'"'::pg_lsn")" = t ]'
log "4. primary replayed up to $S_LSN (zero data loss switchover)"
sqlp primary "SELECT pg_promote(true, 60)" >/dev/null
sqlp primary "ALTER ROLE ledger_app LOGIN" >/dev/null
log "5. primary ledger PROMOTED (timeline $(sqlp primary "SELECT ('x'||substr(pg_walfile_name(pg_current_wal_lsn()),1,8))::bit(32)::int")), app role re-enabled — write unavailability $(el "$TQ")s"
wait_until 120 "first primary write" '[ "$(sqlp primary "SELECT count(*) FROM ledger WHERE origin='"'"'primary'"'"' AND created_at > to_timestamp('"$TQ"')")" -gt 0 ]'
log "   banking writes accepted again on the primary (t+$(el "$T0")s)"

SG=$(( $(gen "$SO") + 1 ))
setenv "$PO" LEDGER_ROLE primary; setenv "$SO" LEDGER_ROLE standby; setenv "$SO" LEDGER_RESYNC_GENERATION "\"$SG\""
scripts/gitops/publish.sh "ledger failback 2/2: primary is primary again, secondary resyncs as standby (generation $SG)" | tail -1 | tee -a "$OUT"
log "6. Git: primary role=primary, secondary → standby (generation $SG)"
wait_until 600 "secondary resync" '[ "$(sqlp secondary "SELECT status FROM pg_stat_wal_receiver")" = streaming ]'
log "7. secondary ledger rebuilt and STREAMING from primary (t+$(el "$T0")s)"

wait_until 600 "workload failback" '[ "$(api | jq_ '"'"'b["state"]["active_cluster"]'"'"')" = primary ]'
EV=$(api | python3 -c 'import json,sys; b=json.load(sys.stdin); e=next(e for e in b["events"] if e["kind"]=="failback"); print(e["kind"], e["action_status"], "restored=%d" % len(e["action_detail"].get("restored", [])), "at", e["at"])')
log "8. WORKLOAD FAILBACK by the controller: $EV (t+$(el "$T0")s)"

sleep 5
CUT=$(sqlp primary "SELECT now() - interval '3 seconds'")
QC="SELECT count(*)||' '||md5(string_agg(id||':'||origin||':'||amount, ',' ORDER BY id)) FROM ledger WHERE created_at < '$CUT'"
sleep 3
H1=$(sqlp primary "$QC"); H2=$(sqlp secondary "$QC")
log "9. CONSISTENCY rows<$CUT primary: ${H1%% *} rows ${H1##* } | secondary: ${H2%% *} rows ${H2##* } → $([ "$H1" = "$H2" ] && echo PASS || echo FAIL)"
log "   replication: $(sqlp primary "SELECT state||' '||sync_state||' replay_lag='||coalesce(replay_lag::text,'0') FROM pg_stat_replication")"
log "   standby deployments: $(for ns in banking ecommerce trading; do kubectl --context kind-secondary -n $ns get deploy --no-headers -o custom-columns=N:.metadata.name,R:.spec.replicas | tr -s ' ' | sed "s#^#$ns/#" | tr '\n' ','; done)"
log "TOTAL failback duration $(el "$T0")s"
