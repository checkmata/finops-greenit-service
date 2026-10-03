"""One-line view of GET /api/failover (used by scripts/k8s/failover-demo.sh). JSON on stdin."""

import json
import sys

b = json.load(sys.stdin)
s = b.get("state") or {}
print(f"active={s.get('active_cluster')} mode={s.get('mode')} status=\"{b['this_replica']['last_status']}\"")
