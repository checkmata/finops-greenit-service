"""Pretty-print GET /api/clusters (used by scripts/simulate_failover.sh). Reads JSON on stdin."""

import json
import sys

body = json.load(sys.stdin)
print(f"active_cluster: {body['active_cluster']} | failover_active: {body['failover_active']}")
for c in body["clusters"]:
    extra = f" (workloads: {c['workloads_source']})" if c.get("workloads_source") else ""
    print(f"  cluster {c['name']:<10} healthy={c['healthy']} reachable={c['reachable']}{extra}")
for p in body["placements"]:
    target = p["serving_cluster"] or "-"
    print(f"  {p['namespace'] + '/' + p['workload']:<34} {p['status']:<9} -> {target:<10} {p['reason']}")
