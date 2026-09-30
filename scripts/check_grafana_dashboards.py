"""
Run EVERY panel query of the provisioned dashboards through Grafana's own
datasource API (/api/ds/query) and report how many data points come back.

    GRAFANA_URL=http://127.0.0.1:3000 GRAFANA_PASSWORD=... python scripts/check_grafana_dashboards.py

Dashboard variables are substituted like the UI would ($cluster/$namespace = all
values, $ds = each Prometheus datasource, $__interval = 1m). Exit code 1 if a
data panel returns nothing.
"""

import base64
import json
import os
import sys
import urllib.request

G = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['GRAFANA_PASSWORD']}".encode()).decode()


def call(path, body=None):
    req = urllib.request.Request(G + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def sql_values(q):
    res = call("/api/ds/query", {"from": "now-7d", "to": "now", "queries": [
        {"refId": "A", "datasource": {"uid": "timescaledb"}, "rawSql": q, "format": "table"}]})
    frame = res["results"]["A"]["frames"][0]
    return [str(v) for v in frame["data"]["values"][0]] if frame["data"]["values"] else []


clusters = sql_values("SELECT DISTINCT cluster FROM metric_snapshots")
namespaces = sql_values("SELECT DISTINCT namespace FROM metric_snapshots")
lst = lambda xs: ",".join(f"'{x}'" for x in xs) or "''"  # noqa: E731
failures = 0
for d in call("/api/search?type=dash-db"):
    dash = call(f"/api/dashboards/uid/{d['uid']}")["dashboard"]
    print(f"\n## {dash['title']}")
    for p in dash["panels"]:
        if p.get("type") == "text":
            continue
        for ds_uid in (["prom-primary", "prom-secondary"] if "${ds}" in json.dumps(p) else [None]):
            queries = []
            for t in p["targets"]:
                q = dict(t)
                q["datasource"] = {"uid": ds_uid or t["datasource"]["uid"]}
                if "rawSql" in q:
                    q["rawSql"] = (q["rawSql"].replace("$cluster", lst(clusters))
                                   .replace("$namespace", lst(namespaces)).replace("$__interval", "1m"))
                queries.append(q)
            res = call("/api/ds/query", {"from": "now-1h", "to": "now", "queries": queries})
            points = sum(len(f["data"]["values"][-1]) if f["data"]["values"] else 0
                         for r in res["results"].values() for f in r.get("frames", []))
            errors = [r.get("error") for r in res["results"].values() if r.get("error")]
            label = f"{p['title']}" + (f" [{ds_uid}]" if ds_uid else "")
            status = "OK " if points and not errors else "EMPTY" if not errors else "ERROR"
            failures += status != "OK "
            print(f"  {status} {points:5d} pts  {label}" + (f"  → {errors[0][:120]}" if errors else ""))
sys.exit(1 if failures else 0)
