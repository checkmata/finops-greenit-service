#!/usr/bin/env bash
# Generate a Prometheus basic-auth credential for <cluster>: htpasswd (bcrypt)
# into a Kubernetes Secret, clear password into .secrets/ (git-ignored, mounted
# into the api as a Docker secret). Nothing is printed.
#   scripts/k8s/make-prometheus-auth.sh secondary
set -euo pipefail
ROLE="${1:?cluster}"; ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$ROOT/.secrets" && chmod 700 "$ROOT/.secrets"
PW=$("$ROOT/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
printf '%s' "$PW" > "$ROOT/.secrets/prom_${ROLE}_password"; chmod 600 "$ROOT/.secrets/prom_${ROLE}_password"
HT=$(docker run --rm httpd:2.4-alpine htpasswd -nbB finops "$PW")
kubectl --context "kind-$ROLE" -n monitoring create secret generic prometheus-htpasswd \
  --from-literal=htpasswd="$HT" --dry-run=client -o yaml | kubectl --context "kind-$ROLE" apply -f - >/dev/null
kubectl --context "kind-$ROLE" apply -f "$ROOT/manifests/prometheus-auth-proxy.yaml" >/dev/null
kubectl --context "kind-$ROLE" -n monitoring rollout restart deploy/prometheus-auth-proxy >/dev/null
kubectl --context "kind-$ROLE" -n monitoring rollout status deploy/prometheus-auth-proxy --timeout=180s >/dev/null
echo "auth proxy ready on $ROLE-control-plane:30091 (user finops, password in .secrets/prom_${ROLE}_password)"
