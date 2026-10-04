#!/usr/bin/env bash
# V2.x — reconcile the transactions that exist ONLY on the stale (fenced) primary ledger copy,
# BEFORE a failback resync erases that copy (incident 2026-10-01: 10 150 rows written on the
# primary while replication was stalled by the host OOM).
#
#   default : DRY RUN — counts, time range, overlap check; nothing is written anywhere
#   --apply : 1. archive the rows (CSV.gz + sha256) under docs/evidence/
#             2. insert them into the AUTHORITATIVE (secondary) ledger in ONE transaction,
#                with new ids, origin='primary-reconciled', original created_at kept;
#                the transaction COMMITS only if the inserted count and the content checksum
#                (src,dst,amount,channel,pattern,created_at) equal the archive's — else ROLLBACK
# The primary copy is only READ. Checksums are computed in id buckets (a single string_agg
# over millions of rows OOM-killed both ledgers on 2026-10-03).
#
# Usage: scripts/dr/ledger-reconcile.sh <last_replicated_id> [--apply]
set -euo pipefail
cd "$(dirname "$0")/../.."
LAST=${1:?last id replicated to secondary (max(id) where origin='primary' on secondary)}
APPLY=${2:-}
P="kubectl --context kind-primary -n banking exec -i ledger-db-0 -- psql -U postgres -d ledger -v ON_ERROR_STOP=1 -At"
S="kubectl --context kind-secondary -n banking exec -i ledger-db-0 -- psql -U postgres -d ledger -v ON_ERROR_STOP=1 -At"
SEL="SELECT src, dst, amount, channel, pattern, created_at FROM ledger WHERE id > $LAST AND origin = 'primary'"
SUM="md5(string_agg(src||'|'||dst||'|'||amount||'|'||channel||'|'||pattern||'|'||created_at, ',' ORDER BY created_at, src, dst, amount))"

[ "$($P -c "SELECT pg_is_in_recovery()")" = f ] && [ "$($P -c "SELECT rolcanlogin FROM pg_roles WHERE rolname='ledger_app'")" = f ] \
  || { echo "ABORT: the primary copy is not the fenced stale copy"; exit 1; }
[ "$($S -c "SELECT pg_is_in_recovery()")" = f ] || { echo "ABORT: secondary is not the writable authoritative copy"; exit 1; }
N=$($P -c "SELECT count(*) FROM ledger WHERE id > $LAST")
NP=$($P -c "SELECT count(*) FROM ledger WHERE id > $LAST AND origin='primary'")
RANGE=$($P -c "SELECT min(created_at)||' → '||max(created_at) FROM ledger WHERE id > $LAST")
H=$($P -c "SELECT $SUM FROM ($SEL) x")
OVERLAP=$($S -c "SELECT count(*) FROM ledger WHERE created_at BETWEEN (SELECT min(created_at) FROM ledger WHERE id > $LAST AND origin='primary') AND (SELECT max(created_at) FROM ledger WHERE id > $LAST AND origin='primary') AND origin IN ('primary','primary-reconciled')" 2>/dev/null || echo "?")
echo "rows only on the primary copy: $N (origin=primary: $NP) | $RANGE | content md5 ${H:0:12}…"
echo "already present on secondary in that window (origin primary/primary-reconciled): $OVERLAP"
[ "$N" = "$NP" ] || { echo "ABORT: unexpected origins in the unreplicated rows"; exit 1; }
[ "$OVERLAP" = 0 ] || { echo "ABORT: secondary already holds rows of that window — reconciliation already done?"; exit 1; }
[ "$APPLY" = --apply ] || { echo "DRY RUN — nothing written. Re-run with --apply."; exit 0; }

ARCH=docs/evidence/ledger-reconciliation-2026-10-01.csv.gz
$P -c "\\copy ($SEL ORDER BY created_at) TO STDOUT WITH CSV HEADER" | gzip -n > "$ARCH"
echo "archived $(zcat "$ARCH" | tail -n +2 | wc -l) rows → $ARCH sha256 $(sha256sum "$ARCH" | cut -c1-16)…"
{ echo "BEGIN;"
  echo "CREATE TEMP TABLE r (src text, dst text, amount numeric(14,2), channel text, pattern text, created_at timestamptz);"
  echo "\\copy r FROM STDIN WITH CSV HEADER"
  zcat "$ARCH"; echo "\\."
  echo "INSERT INTO ledger (origin, src, dst, amount, channel, pattern, created_at) SELECT 'primary-reconciled', src, dst, amount, channel, pattern, created_at FROM r;"
  echo "DO \$\$ DECLARE n int; h text; BEGIN
          SELECT count(*), $SUM INTO n, h FROM ledger WHERE origin='primary-reconciled';
          IF n <> $NP OR h <> '$H' THEN RAISE EXCEPTION 'reconciliation check failed: % rows, md5 %', n, h; END IF;
        END \$\$;"
  echo "COMMIT;"; } | $S -q
echo "inserted & verified on secondary: $($S -c "SELECT count(*)||' rows, md5 '||left($SUM,12) FROM ledger WHERE origin='primary-reconciled'")… (expected $NP, ${H:0:12}…)"
