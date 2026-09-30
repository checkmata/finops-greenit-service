"""
Generate the 4 Grafana dashboards (grafana/dashboards/*.json).

    python scripts/generate_grafana_dashboards.py

WHY generated: hand-edited dashboard JSON drifts and breaks silently; a script
keeps queries next to their intent and always emits valid JSON.
Design rules: one measure per panel (no dual axis), color follows the series
name (palette-classic-by-name), stat tiles for headline figures, health shown
as text + color. Datasource uids come from grafana/provisioning/datasources.

Sources:
- Infrastructure → Prometheus of the selected cluster (datasource variable)
- FinOps / Green IT / Recommendations → TimescaleDB (written by the scheduler)
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
TS = {"type": "postgres", "uid": "timescaledb"}
PROM = {"type": "prometheus", "uid": "${ds}"}
_id = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def panel(title, kind, targets, x, y, w, h, *, unit=None, desc=None, overrides=None, options=None, mappings=None):
    field_defaults = {"color": {"mode": "palette-classic-by-name"}, "custom": {}}
    if kind == "stat":
        field_defaults["color"] = {"mode": "fixed", "fixedColor": "text"}
    if unit:
        field_defaults["unit"] = unit
    if mappings:
        field_defaults["mappings"] = mappings
        field_defaults["color"] = {"mode": "thresholds"}
    if kind == "timeseries":
        field_defaults["custom"] = {"lineWidth": 2, "fillOpacity": 0, "showPoints": "auto", "spanNulls": False}
    return {
        "id": _next_id(), "title": title, "type": kind, "description": desc or "",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": field_defaults, "overrides": overrides or []},
        "options": options or ({"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                                "tooltip": {"mode": "multi"}} if kind == "timeseries" else {}),
    }


def prom(expr, legend):
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": "A"}


def sql(query, fmt="time_series", ref="A"):
    return {"datasource": TS, "rawSql": query, "format": fmt, "rawQuery": True, "editorMode": "code", "refId": ref}


def text_panel(md, x, y, w, h):
    return {"id": _next_id(), "type": "text", "title": "", "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": md}}


def dashboard(uid, title, panels, variables, desc):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["finops-greenit"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": variables}, "panels": panels,
    }


def sql_var(name, query, multi=True):
    return {"name": name, "type": "query", "datasource": TS, "query": query, "definition": query,
            "multi": multi, "includeAll": True, "allValue": None, "refresh": 2,
            "current": {"selected": True, "text": "All", "value": "$__all"}}


CLUSTER_VAR = sql_var("cluster", "SELECT DISTINCT cluster FROM metric_snapshots ORDER BY 1")
NS_VAR = sql_var("namespace", "SELECT DISTINCT namespace FROM metric_snapshots WHERE cluster IN ($cluster) ORDER BY 1")
SCOPE = "cluster IN ($cluster) AND namespace IN ($namespace)"
UP_MAP = [{"type": "value", "options": {
    "1": {"text": "UP", "color": "green", "index": 0},
    "0": {"text": "DOWN", "color": "red", "index": 1}}}]


def series(column: str, group: str) -> str:
    """Average samples of one series inside a bucket, then sum across series (see app/domain/history.py)."""
    return f"""SELECT bucket AS time, {group} AS metric, SUM(v) AS value FROM (
  SELECT time_bucket('$__interval'::interval, time) AS bucket, cluster, namespace, AVG({column}) AS v
  FROM metric_snapshots WHERE $__timeFilter(time) AND {SCOPE} AND {column} IS NOT NULL
  GROUP BY 1, 2, 3) s
GROUP BY 1, 2 ORDER BY 1"""


LATEST = f"""(SELECT DISTINCT ON (cluster, namespace) * FROM metric_snapshots
   WHERE $__timeFilter(time) AND {SCOPE} ORDER BY cluster, namespace, time DESC)"""


def infrastructure():
    ds_var = {"name": "ds", "label": "cluster", "type": "datasource", "query": "prometheus",
              "regex": "/Prometheus-.*/", "current": {"text": "Prometheus-primary", "value": "prom-primary"}}
    real = 'container!="",container!="POD"'
    p = [
        panel("Telemetry sources (Prometheus up)", "stat",
              [prom('up{job=~"kube-state-metrics|node-exporter|kepler|kubelet"}', "{{job}}")], 0, 0, 24, 4,
              mappings=UP_MAP, desc="DOWN = the scrape failed. FinOps needs kube-state-metrics; Green IT needs kepler.",
              options={"colorMode": "background", "textMode": "value_and_name", "graphMode": "none"}),
        panel("CPU used by namespace (cores)", "timeseries",
              [prom(f"sum by (namespace) (rate(container_cpu_usage_seconds_total{{{real}}}[5m]))", "{{namespace}}")],
              0, 4, 12, 8, unit="short"),
        panel("CPU requested by namespace (cores)", "timeseries",
              [prom('sum by (namespace) (kube_pod_container_resource_requests{resource="cpu"})', "{{namespace}}")],
              12, 4, 12, 8, unit="short"),
        panel("Memory working set by namespace", "timeseries",
              [prom(f"sum by (namespace) (container_memory_working_set_bytes{{{real}}})", "{{namespace}}")],
              0, 12, 12, 8, unit="bytes"),
        panel("PVCs — requested size (kube-state-metrics)", "bargauge",
              [prom("kube_persistentvolumeclaim_resource_requests_storage_bytes",
                    "{{namespace}}/{{persistentvolumeclaim}}"),
               {**prom("kubelet_volume_stats_used_bytes", "used {{namespace}}/{{persistentvolumeclaim}}"),
                "refId": "B"}], 12, 12, 12, 8, unit="bytes",
              desc="Requested size always shown. 'used' only appears when the kubelet reports volume stats — "
                   "hostPath/local-path provisioners (minikube, kind) do not, so FinOps marks storage "
                   "'unavailable' (never priced as 0)."),
        panel("Network received by namespace", "timeseries",
              [prom("sum by (namespace) (rate(container_network_receive_bytes_total[5m]))", "{{namespace}}")],
              0, 20, 12, 8, unit="Bps"),
        panel("Network transmitted by namespace", "timeseries",
              [prom("sum by (namespace) (rate(container_network_transmit_bytes_total[5m]))", "{{namespace}}")],
              12, 20, 12, 8, unit="Bps"),
        panel("Deployment availability (available / desired)", "table",
              [{**prom("kube_deployment_status_replicas_available", ""), "format": "table", "instant": True, "refId": "A"},
               {**prom("kube_deployment_spec_replicas", ""), "format": "table", "instant": True, "refId": "B"}],
              0, 28, 24, 8, desc="Input of the failover decision (GET /api/clusters)."),
    ]
    return dashboard("finops-infra", "FinOps/GreenIT — 1. Infrastructure", p, [ds_var],
                     "Raw telemetry of one cluster (select its Prometheus).")


def finops():
    p = [
        panel("Monthly cost — used resources", "stat", [sql(f"SELECT SUM(cost_usd) FROM {LATEST} l", "table")],
              0, 0, 6, 4, unit="currencyUSD", desc="Estimate: quantity × configured price × 720 h. Not an invoice."),
        panel("Monthly cost — requested resources", "stat",
              [sql(f"SELECT SUM(requested_cost_usd) FROM {LATEST} l", "table")], 6, 0, 6, 4, unit="currencyUSD"),
        panel("Waste (reserved, unused)", "stat", [sql(f"SELECT SUM(waste_cost_usd) FROM {LATEST} l", "table")],
              12, 0, 6, 4, unit="currencyUSD"),
        panel("Potential savings (right-sizing)", "stat",
              [sql(f"SELECT SUM(potential_savings_usd) FROM {LATEST} l", "table")], 18, 0, 6, 4, unit="currencyUSD",
              desc="Σ savings of current advice; always ≤ waste (V1 fix of double counting)."),
        panel("Used cost over time, by cluster", "timeseries", [sql(series("cost_usd", "cluster"))],
              0, 4, 12, 8, unit="currencyUSD"),
        panel("Waste over time, by namespace", "timeseries",
              [sql(series("waste_cost_usd", "cluster || '/' || namespace"))], 12, 4, 12, 8, unit="currencyUSD"),
        panel("Savings over time, by namespace", "timeseries",
              [sql(series("potential_savings_usd", "cluster || '/' || namespace"))], 0, 12, 12, 8, unit="currencyUSD"),
        panel("Storage status", "table",
              [sql(f"SELECT cluster, namespace, storage_status, storage_cost_usd FROM {LATEST} l ORDER BY 1, 2",
                   "table")], 12, 12, 12, 8,
              desc="'unavailable' = not priced (no kubelet stats) — it does NOT mean free."),
        panel("Workloads — latest cost view", "table", [sql("""SELECT cluster, namespace, workload, team,
  ROUND(cpu_cores_used::numeric, 3) AS cpu_used, cpu_cores_requested AS cpu_req,
  ROUND(requested_cost_usd::numeric, 2) AS requested_usd, ROUND(actual_cost_usd::numeric, 2) AS used_usd,
  ROUND(waste_cost_usd::numeric, 2) AS waste_usd, ROUND(potential_savings_usd::numeric, 2) AS savings_usd
FROM (SELECT DISTINCT ON (cluster, namespace, workload) * FROM workload_snapshots
      WHERE $__timeFilter(time) AND cluster IN ($cluster) AND namespace IN ($namespace)
      ORDER BY cluster, namespace, workload, time DESC) w
ORDER BY waste_usd DESC""", "table")], 0, 20, 24, 10),
    ]
    return dashboard("finops-cost", "FinOps/GreenIT — 2. FinOps", p, [CLUSTER_VAR, NS_VAR],
                     "Cost estimates from TimescaleDB history (per cluster / namespace / workload).")


def greenit():
    p = [
        panel("Energy, last 24 h (kWh)", "stat", [sql(f"SELECT SUM(energy_kwh) FROM {LATEST} l", "table")],
              0, 0, 8, 4, unit="kwatth"),
        panel("CO₂, last 24 h (kg)", "stat", [sql(f"SELECT SUM(co2e_kg) FROM {LATEST} l", "table")],
              8, 0, 8, 4, unit="masskg", desc="kWh × PUE × grid intensity / 1000 (constants in config)."),
        panel("Namespaces with ESTIMATED energy", "stat",
              [sql(f"""SELECT COUNT(*) FROM {LATEST} l
WHERE energy_attribution = 'node_cpu_share' OR energy_power_source = 'model'""", "table")],
              16, 0, 8, 4, desc="Estimated = Kepler power MODEL (no hardware counters) or node-level share. "
                                "Measured = RAPL package+dram attributed per container."),
        panel("Energy over time, by namespace", "timeseries",
              [sql(series("energy_kwh", "cluster || '/' || namespace"))], 0, 4, 12, 8, unit="kwatth"),
        panel("CO₂ over time, by namespace", "timeseries",
              [sql(series("co2e_kg", "cluster || '/' || namespace"))], 12, 4, 12, 8, unit="masskg"),
        panel("Energy provenance (latest)", "table", [sql(f"""SELECT cluster, namespace,
  CASE WHEN energy_kwh IS NULL THEN 'unavailable'
       WHEN energy_power_source = 'hardware' AND energy_attribution IN ('container','pod') THEN 'measured'
       ELSE 'estimated' END AS precision,
  energy_power_source AS power_source, energy_scope AS scope, energy_attribution AS attribution,
  ROUND((energy_kwh * 1000)::numeric, 3) AS energy_wh, ROUND((idle_energy_kwh * 1000)::numeric, 3) AS idle_wh,
  ROUND((co2e_kg * 1000)::numeric, 3) AS co2_g FROM {LATEST} l ORDER BY 1, 2""", "table")], 0, 12, 24, 8,
              desc="Wh and g (small local clusters). idle empty = not measurable with this Kepler exposure; "
                   "no ENERGY_WASTE advice is emitted then."),
        panel("CO₂ per workload (per cycle, g)", "timeseries", [sql("""SELECT time_bucket('$__interval'::interval, time) AS time,
  cluster || '/' || namespace || '/' || workload AS metric, sum(co2_interval_kg) * 1000 AS value
FROM workload_snapshots WHERE $__timeFilter(time) AND cluster IN ($cluster) AND namespace IN ($namespace)
  AND co2_interval_kg IS NOT NULL GROUP BY 1, 2 ORDER BY 1""")], 0, 20, 24, 9, unit="massg",
              desc="Kepler per container, joined to the pod's app label. Sum of per-cycle amounts (exact)."),
        panel("Energy & CO₂ per workload — period total", "table", [sql("""SELECT cluster, namespace, workload,
  max(energy_precision) AS precision, max(energy_power_source) AS power_source,
  ROUND((sum(energy_interval_kwh) * 1000)::numeric, 3) AS energy_wh, ROUND((sum(co2_interval_kg) * 1000)::numeric, 3) AS co2_g
FROM workload_snapshots WHERE $__timeFilter(time) AND cluster IN ($cluster) AND namespace IN ($namespace)
  AND energy_interval_kwh IS NOT NULL GROUP BY 1, 2, 3 ORDER BY co2_g DESC""", "table")], 0, 29, 24, 10),
    ]
    return dashboard("finops-greenit", "FinOps/GreenIT — 3. Green IT", p, [CLUSTER_VAR, NS_VAR],
                     "Energy and CO₂ with their provenance (measured vs estimated).")


def recommendations():
    rec_scope = "cluster IN ($cluster) AND namespace IN ($namespace)"
    stat = lambda s, x: panel(f"{s.capitalize()}", "stat",  # noqa: E731
                              [sql(f"SELECT COUNT(*) FROM recommendations WHERE status = '{s}' AND {rec_scope}", "table")],
                              x, 0, 3, 4)
    p = [
        stat("pending", 0), stat("approved", 3), stat("rejected", 6), stat("executed", 9), stat("failed", 12),
        stat("obsolete", 15),
        panel("Pending savings", "stat", [sql(f"""SELECT COALESCE(SUM(estimated_savings_usd), 0) FROM recommendations
WHERE status = 'pending' AND {rec_scope}""", "table")], 18, 0, 6, 4, unit="currencyUSD"),
        panel("Pending by category", "barchart", [sql(f"""SELECT category, COUNT(*) AS count FROM recommendations
WHERE status = 'pending' AND {rec_scope} GROUP BY 1 ORDER BY 2 DESC""", "table")], 0, 4, 10, 8,
              options={"orientation": "horizontal", "showValue": "auto", "legend": {"showLegend": False}}),
        panel("Pending recommendations (highest savings first)", "table", [sql(f"""SELECT severity, cluster, namespace,
  workload, category, resource, message, estimated_savings_usd AS savings_usd, estimation_method, updated_at
FROM recommendations WHERE status = 'pending' AND {rec_scope}
ORDER BY estimated_savings_usd DESC NULLS LAST, updated_at DESC LIMIT 200""", "table")], 10, 4, 14, 16),
        text_panel("**Confidence** and **anomalies** columns arrive with the analytics layer (V3). "
                   "Status changes go through the API (`PATCH /api/recommendations/{id}/status`), "
                   "which enforces the allowed transitions.", 0, 12, 10, 8),
    ]
    return dashboard("finops-recos", "FinOps/GreenIT — 4. Recommendations", p, [CLUSTER_VAR, NS_VAR],
                     "Human-in-the-loop view of the recommendation lifecycle.")


def multicluster():
    p = [
        panel("Failover controller state", "table", [sql("""SELECT mode, active_cluster, primary_unhealthy_since,
  primary_healthy_since, last_transition_at, updated_at FROM failover_state""", "table")], 0, 0, 24, 4),
        panel("Failover / failback events", "table", [sql("""SELECT at, kind, from_cluster, to_cluster, reason,
  action_status, decided_by, action_detail::text AS detail FROM failover_events ORDER BY at DESC LIMIT 50""",
                                                           "table")], 0, 4, 24, 9),
        panel("Used cost by cluster (hourly aggregate)", "timeseries", [sql("""SELECT bucket AS time, cluster AS metric,
  sum(cost_usd) AS value FROM metric_snapshots_hourly WHERE $__timeFilter(bucket) GROUP BY 1, 2 ORDER BY 1""")],
              0, 13, 12, 8, unit="currencyUSD", desc="Continuous aggregate: primary vs secondary."),
        panel("Measured CO₂ by cluster (hourly aggregate, g)", "timeseries", [sql("""SELECT bucket AS time,
  cluster AS metric, sum(co2_kg) * 1000 AS value FROM metric_snapshots_hourly
WHERE $__timeFilter(bucket) AND co2_kg IS NOT NULL GROUP BY 1, 2 ORDER BY 1""")], 12, 13, 12, 8, unit="massg"),
    ]
    return dashboard("finops-multicluster", "FinOps/GreenIT — 5. Multicluster & failover", p, [],
                     "Failover controller (hysteresis) and cluster comparison from long-term aggregates.")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("1-infrastructure", infrastructure), ("2-finops", finops),
                        ("3-greenit", greenit), ("4-recommendations", recommendations),
                        ("5-multicluster", multicluster)):
        (OUT / f"{name}.json").write_text(json.dumps(build(), indent=2) + "\n")
        print("wrote", OUT / f"{name}.json")


if __name__ == "__main__":
    main()
