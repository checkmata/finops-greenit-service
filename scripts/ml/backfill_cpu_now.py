#!/usr/bin/env python3
"""
V3 — backfill workload_snapshots.cpu_cores_used_now from the REAL Prometheus history.

Before migration 006 only the long-window CPU average was stored. Prometheus still
holds the raw counters (retention ≥ 10 days), so the point-in-time value of every past
snapshot can be MEASURED again, with exactly the PromQL the adapter now uses
(PrometheusMetricsAdapter.get_workload_metrics → cpu_inner, 5-min rate, workload join).

Nothing is interpolated: a row gets a value only if Prometheus has a sample within
±30 s of the snapshot time; otherwise it stays NULL. Idempotent (only NULL rows).

Usage: .venv/bin/python scripts/ml/backfill_cpu_now.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.adapters.outbound.prometheus_adapter import ksm_label, usage_sum  # noqa: E402
from app.adapters.outbound.timescale_adapter import TimescaleAdapter  # noqa: E402

PROMETHEUS = {"primary": "http://127.0.0.1:19090", "secondary": "http://127.0.0.1:19091"}
NAMESPACES = ("banking", "ecommerce", "trading")


def cpu_now_query(ns: str, workload_label: str = "app") -> str:
    L = ksm_label(workload_label)
    join = (f"* on (namespace, pod) group_left({L}) max by (namespace, pod, {L}) "
            f'(kube_pod_labels{{namespace="{ns}", {L}!=""}})')
    inner = usage_sum("container_cpu_usage_seconds_total", f'namespace="{ns}"', by="namespace, pod", rate_window="5m")
    return f"sum by ({L}) ({inner} {join})", L


async def main() -> int:
    url = next(l.split("=", 1)[1] for l in (ROOT / ".env").read_text().splitlines() if l.startswith("DATABASE_URL="))
    storage = TimescaleAdapter(url.replace("@timescaledb:", "@127.0.0.1:"))
    total = 0
    async with httpx.AsyncClient(timeout=120) as http, storage._engine.begin() as conn:
        for cluster, prom in PROMETHEUS.items():
            span = (await conn.execute(text(
                "SELECT min(time), max(time) FROM workload_snapshots WHERE cluster = :c AND cpu_cores_used_now IS NULL"),
                {"c": cluster})).one()
            if span[0] is None:
                continue
            for ns in NAMESPACES:
                q, L = cpu_now_query(ns)
                start, end = span[0].timestamp(), span[1].timestamp()
                rows = []
                # Prometheus caps a range query at 11 000 points per series: 60 s steps, 6-hour chunks.
                t = start
                while t < end:
                    r = await http.get(f"{prom}/api/v1/query_range",
                                       params={"query": q, "start": t, "end": min(end, t + 6 * 3600), "step": 60})
                    r.raise_for_status()
                    for series in r.json()["data"]["result"]:
                        wl = series["metric"].get(L)
                        if wl:
                            rows += [{"c": cluster, "ns": ns, "wl": wl, "ts": datetime.fromtimestamp(float(ts), timezone.utc),
                                      "v": float(v)} for ts, v in series["values"]]
                    t += 6 * 3600
                if not rows:
                    continue
                await conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS bf (c text, ns text, wl text, ts timestamptz, v float8)"))
                await conn.execute(text("TRUNCATE bf"))
                await conn.execute(text("INSERT INTO bf VALUES (:c, :ns, :wl, :ts, :v)"), rows)
                res = await conn.execute(text("""
                    UPDATE workload_snapshots w SET cpu_cores_used_now = s.v
                    FROM (SELECT DISTINCT ON (w2.time, w2.workload) w2.time, w2.workload, bf.v
                          FROM workload_snapshots w2 JOIN bf
                            ON bf.c = w2.cluster AND bf.ns = w2.namespace AND bf.wl = w2.workload
                           AND bf.ts BETWEEN w2.time - interval '30 seconds' AND w2.time + interval '30 seconds'
                          WHERE w2.cluster = :c AND w2.namespace = :ns AND w2.cpu_cores_used_now IS NULL
                          ORDER BY w2.time, w2.workload, abs(extract(epoch FROM bf.ts - w2.time))) s
                    WHERE w.cluster = :c AND w.namespace = :ns AND w.workload = s.workload AND w.time = s.time"""),
                    {"c": cluster, "ns": ns})
                total += res.rowcount or 0
                print(f"{cluster}/{ns}: {len(rows)} Prometheus samples → {res.rowcount} snapshot rows filled")
    await storage.close()
    print(f"total rows filled: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
