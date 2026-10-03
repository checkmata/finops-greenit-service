#!/usr/bin/env bash
# =============================================================================
# Local demo WITHOUT Docker/Kubernetes: 2 simulated clusters + 2 real Prometheus
# + the API (SQLite storage). Everything runs as local processes.
#
#   PROMETHEUS_BIN=/path/to/prometheus scripts/run_local_demo.sh
#
# What is REAL here: Prometheus (scraping, PromQL, up{}), the service code path
# (adapter → domain → scheduler → storage → API).
# What is SIMULATED: the clusters (exporter/simulator.py) and their load.
# For TimescaleDB + Grafana use docker compose; for real Kubernetes use
# scripts/k8s/kind-up.sh (see docs/MULTICLUSTER.md).
# =============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROM="${PROMETHEUS_BIN:-prometheus}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
WORK="${DEMO_DIR:-$ROOT/.demo}"
mkdir -p "$WORK"
cd "$ROOT"

declare -A BASE=([primary]=91 [secondary]=92)
declare -A ADMIN=([primary]=8001 [secondary]=8002)
declare -A PROMPORT=([primary]=9090 [secondary]=9091)
PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

for c in primary secondary; do
  b=${BASE[$c]}
  CLUSTER_NAME=$c CLUSTER_PROFILE=$c ADMIN_PORT=${ADMIN[$c]} SIM_DAY_SECONDS=${SIM_DAY_SECONDS:-3600} \
  KSM_PORT=${b}01 NODE_EXPORTER_PORT=${b}02 KEPLER_PORT=${b}03 KUBELET_PORT=${b}04 \
    "$PY" -m exporter.fake_metrics >"$WORK/sim-$c.log" 2>&1 &
  PIDS+=($!)
  sed -e "s/sim-$c:91/localhost:${b}/g" -e "s/localhost:9090/localhost:${PROMPORT[$c]}/" \
      "prometheus/prometheus-$c.yml" >"$WORK/prometheus-$c.yml"
  "$PROM" --config.file="$WORK/prometheus-$c.yml" --storage.tsdb.path="$WORK/tsdb-$c" \
          --web.listen-address=":${PROMPORT[$c]}" >"$WORK/prometheus-$c.log" 2>&1 &
  PIDS+=($!)
done

export CLUSTERS_CONFIG_FILE=config/clusters.local.yaml
export DATABASE_URL="sqlite+aiosqlite:///$WORK/demo.db"
export SCHEDULER_INTERVAL_SECONDS="${SCHEDULER_INTERVAL_SECONDS:-30}"
# Demo only: the simulated "day" lasts SIM_DAY_SECONDS (1 h), so we accept
# 10 minutes of history instead of the production default (24 h).
export MIN_OBSERVATION_HOURS="${MIN_OBSERVATION_HOURS:-0.17}"
export LOG_FORMAT=text
"$PY" -m uvicorn app.adapters.inbound.api.main:app --port "${API_PORT:-8080}" >"$WORK/api.log" 2>&1 &
PIDS+=($!)

echo "Simulators admin: http://localhost:8001/state  http://localhost:8002/state"
echo "Prometheus:       http://localhost:9090  http://localhost:9091"
echo "API:              http://localhost:${API_PORT:-8080}/docs   (logs in $WORK)"
echo "Failover demo:    scripts/simulate_failover.sh"
wait
