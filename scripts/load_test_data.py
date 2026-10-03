"""
load_test_data.py — seed a database with SYNTHETIC history (demo / dashboards).

V1 fixes: the MVP script used Severity.HIGH (does not exist), numeric
current_value (column is TEXT), a hard-coded URL that did not match
docker-compose, and inserted duplicate recommendations.

    python -m scripts.load_test_data                   # uses DATABASE_URL (.env)
    python -m scripts.load_test_data --database-url sqlite+aiosqlite:///.demo/seed.db

Every value is synthetic and random — never present it as measured data.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from datetime import datetime, timedelta, timezone

from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.config import settings
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.ports.storage_port import MetricSnapshot
from app.domain.models.snapshot import WorkloadSnapshot

SCOPES = [
    ("primary", "banking", "payments", "cc-banking", ["txn-processor", "fraud-scorer"]),
    ("primary", "trading", "markets", "cc-trading", ["order-matcher", "market-feed"]),
    ("primary", "ecommerce", "digital", "cc-ecommerce", ["frontend", "cartservice"]),
    ("secondary", "ecommerce", "digital", "cc-ecommerce", ["frontend"]),
]


async def seed(adapter: TimescaleAdapter, days: int, rng: random.Random) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    snaps = 0
    for hours_ago in range(days * 24, 0, -1):
        t = now - timedelta(hours=hours_ago)
        diurnal = 1 + 0.3 * math.sin(2 * math.pi * t.hour / 24)
        for cluster, ns, team, cc, workloads in SCOPES:
            factor = 0.15 if cluster == "secondary" else 1.0
            requested = 60.0 * factor
            used = requested * rng.uniform(0.25, 0.45) * diurnal
            waste = requested - used
            energy = 3.0 * factor * diurnal
            await adapter.save_snapshot(MetricSnapshot(
                time=t, cluster=cluster, namespace=ns, team=team, cost_center=cc, environment="prod",
                cpu_core_hours=used / 21.6, memory_gb_hours=4.0 * factor, cost_usd=round(used, 2),
                requested_cost_usd=requested, waste_cost_usd=round(waste, 2),
                waste_pct=round(100 * waste / requested, 1), potential_savings_usd=round(waste * 0.6, 2),
                storage_cost_usd=0.5 if ns == "banking" else None,
                storage_status="available" if ns == "banking" else "not_applicable",
                energy_kwh=round(energy, 3), co2e_kg=round(energy * 1.4 * 0.475, 3),
                idle_energy_kwh=round(energy * 0.35, 3) if cluster == "primary" else None,
                energy_attribution="container" if cluster == "primary" else "node_cpu_share",
            ))
            await adapter.save_workload_snapshots([
                WorkloadSnapshot(t, cluster, ns, w, team, cc, "prod", 0.3 * factor, 1.0 * factor, 0.3, 1.0,
                                 21.6 * factor, round(used / len(workloads), 2),
                                 round(waste / len(workloads), 2), round(waste * 0.6 / len(workloads), 2))
                for w in workloads
            ])
            snaps += 1

    recs = 0
    for cluster, ns, team, cc, workloads in SCOPES:
        for w in workloads:
            rec = Recommendation(
                category=Category.COST_RIGHTSIZING, severity=rng.choice([Severity.WARNING, Severity.CRITICAL]),
                namespace=ns, resource="cpu", message=f"[SYNTHETIC] Workload '{w}': CPU ~30% utilized.",
                current_value="1.0", recommended_value="0.4", estimated_savings_usd=round(rng.uniform(5, 40), 2),
                cluster=cluster, workload=w, team=team, cost_center=cc, environment="prod",
            )
            _, action = await adapter.upsert_recommendation(rec)  # idempotent: re-running does not duplicate
            recs += action == "insert"
    return snaps, recs


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=settings.DATABASE_URL)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    adapter = TimescaleAdapter(args.database_url)
    try:
        if args.database_url.startswith("sqlite"):
            await adapter.ensure_tables_exist()  # PostgreSQL: tables come from the SQL migrations
        snaps, recs = await seed(adapter, args.days, random.Random(args.seed))
        print(f"Seeded {snaps} namespace snapshots (+ workload rows) and {recs} new recommendations.")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
