"""Tiny PromQL client for operators: python scripts/promql.py <prometheus_url> '<query>' [label]"""

import json
import sys
import urllib.parse
import urllib.request

url, query = sys.argv[1], sys.argv[2]
label = sys.argv[3] if len(sys.argv) > 3 else None
with urllib.request.urlopen(f"{url}/api/v1/query?{urllib.parse.urlencode({'query': query})}", timeout=15) as r:
    data = json.load(r)["data"]["result"]
for s in data:
    key = s["metric"].get(label, "") if label else json.dumps(s["metric"], sort_keys=True)
    print(f"  {key:<40} {float(s['value'][1]):.4f}")
if not data:
    print("  (empty result)")
