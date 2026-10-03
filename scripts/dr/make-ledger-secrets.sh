#!/usr/bin/env bash
# Credentials of the replicated banking ledger — generated locally, NEVER in Git.
# Same values on both clusters (the standby authenticates to the primary with the
# replication password; after a promotion the app password must keep working).
# Files: .secrets/ledger_{postgres,replication,app}_password (git-ignored, 0600). Not printed.
set -euo pipefail
cd "$(dirname "$0")/../.."
mkdir -p .secrets && chmod 700 .secrets
for k in postgres replication app; do
  f=.secrets/ledger_${k}_password
  [[ -s $f ]] || { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$f"; chmod 600 "$f"; }
done
for c in primary secondary; do
  kubectl --context "kind-$c" -n banking create secret generic ledger-db-credentials \
    --from-file=POSTGRES_PASSWORD=.secrets/ledger_postgres_password \
    --from-file=REPLICATION_PASSWORD=.secrets/ledger_replication_password \
    --from-file=APP_PASSWORD=.secrets/ledger_app_password \
    --dry-run=client -o yaml | kubectl --context "kind-$c" apply -f - >/dev/null
  echo "$c: secret banking/ledger-db-credentials ready"
done
