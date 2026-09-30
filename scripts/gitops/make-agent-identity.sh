#!/usr/bin/env bash
# V4: the agent's OWN Git identity on the local Gitea — a collaborator with WRITE access to
# finops/platform-gitops ONLY (not admin). Its token goes to .secrets/gitea_agent_token
# (git-ignored, 0600, never printed) and is mounted in the API as a Docker secret.
# The agent never gets a Kubernetes write identity: Argo CD applies what it commits.
set -euo pipefail
cd "$(dirname "$0")/../.."
ADMIN_PW=$(cat .secrets/gitea_admin_password)
API=http://127.0.0.1:3001/api/v1
if ! docker exec gitops-gitea-1 gitea admin user list 2>/dev/null | grep -q ' finops-agent '; then
  PW=$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n')
  docker exec gitops-gitea-1 gitea admin user create --username finops-agent --email agent@localhost \
    --password "$PW" --must-change-password=false >/dev/null
  unset PW
fi
curl -fs -u "finops:$ADMIN_PW" -X PUT -H 'Content-Type: application/json' -d '{"permission":"write"}' \
  "$API/repos/finops/platform-gitops/collaborators/finops-agent" >/dev/null
if [[ ! -s .secrets/gitea_agent_token ]]; then
  docker exec gitops-gitea-1 gitea admin user generate-access-token --username finops-agent \
    --token-name "finops-agent-$(date +%s)" --scopes write:repository --raw > .secrets/gitea_agent_token
  chmod 600 .secrets/gitea_agent_token
fi
echo "finops-agent: write collaborator on finops/platform-gitops; token in .secrets/gitea_agent_token"
