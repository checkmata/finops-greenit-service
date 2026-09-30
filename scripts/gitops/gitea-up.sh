#!/usr/bin/env bash
# Starts the local Git server and creates the GitOps repository `finops/platform-gitops`.
# The admin password is generated once into .secrets/gitea_admin_password (git-ignored)
# and never printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
docker compose -p gitops -f docker-compose.gitops.yaml up -d
mkdir -p .secrets && chmod 700 .secrets
PW_FILE=.secrets/gitea_admin_password
[[ -s $PW_FILE ]] || { head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$PW_FILE"; chmod 600 "$PW_FILE"; }
for _ in $(seq 60); do curl -fs http://127.0.0.1:3001/api/healthz >/dev/null && break; sleep 2; done
docker exec gitops-gitea-1 gitea admin user list 2>/dev/null | grep -q ' finops ' || \
  docker exec gitops-gitea-1 gitea admin user create --username finops --email finops@localhost \
    --password "$(cat "$PW_FILE")" --admin --must-change-password=false >/dev/null
curl -fs -u "finops:$(cat "$PW_FILE")" http://127.0.0.1:3001/api/v1/repos/finops/platform-gitops >/dev/null || \
  curl -fs -u "finops:$(cat "$PW_FILE")" -H 'Content-Type: application/json' \
    -d '{"name":"platform-gitops","private":false,"default_branch":"main"}' \
    http://127.0.0.1:3001/api/v1/user/repos >/dev/null
echo "gitea ready: http://127.0.0.1:3001/finops/platform-gitops (in-cluster: http://gitea:3000/finops/platform-gitops.git)"
