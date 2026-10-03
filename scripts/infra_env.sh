#!/usr/bin/env bash
# Print the INFRA_* variables for `pytest -m infra` against the local REAL stack
# (kind clusters + docker compose). Reads the DB URL from .env (git-ignored).
#   eval "$(scripts/infra_env.sh)"; pytest -m infra -v
set -euo pipefail
cd "$(dirname "$0")/.."
db=$(grep -E '^DATABASE_URL=' .env | cut -d= -f2-)
echo "export INFRA_PROMETHEUS_PRIMARY_URL=http://127.0.0.1:19090"
echo "export INFRA_PROMETHEUS_SECONDARY_URL=http://127.0.0.1:19091"
echo "export INFRA_DATABASE_URL='$db'"
echo "export INFRA_API_URL=http://127.0.0.1:8080"
echo "export INFRA_API2_URL=http://127.0.0.1:8082"
# authenticated secondary Prometheus: NodePort 30091 is not published on the host → node IP on the kind network
ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"kind\").IPAddress}}" secondary-control-plane 2>/dev/null)
[ -n "$ip" ] && echo "export INFRA_PROMETHEUS_SECONDARY_AUTH_URL=http://$ip:30091"
# Steady-state start of the HA pair: the later StartedAt of the two replicas.
since=$(docker inspect -f '{{.State.StartedAt}}' finops-greenit-service-api-1 finops-greenit-service-api-2-1 2>/dev/null | sort | tail -1)
[ -n "$since" ] && echo "export INFRA_HA_SINCE='$since'"

# V2.x: replicated ledger (NodePort 30432 on each node, kind network) + its superuser password file
for c in primary secondary; do
  ipc=$(docker inspect -f "{{(index .NetworkSettings.Networks \"kind\").IPAddress}}" $c-control-plane 2>/dev/null)
  up=$(echo $c | tr a-z A-Z)
  [ -n "$ipc" ] && echo "export INFRA_LEDGER_${up}_HOST=$ipc"
done
[ -f .secrets/ledger_postgres_password ] && echo "export INFRA_LEDGER_PASSWORD_FILE=$PWD/.secrets/ledger_postgres_password"
command -v kubectl >/dev/null && echo "export INFRA_KUBE_CONTEXTS=kind-primary,kind-secondary"
