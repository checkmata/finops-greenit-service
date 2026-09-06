"""
load_test_data.py — Seed the local TimescaleDB with realistic test data.

WHY this script exists:
- Manual testing via Grafana dashboards needs data in TimescaleDB
- Running the API endpoints against an empty DB is useless for demo/debug
- Seeds both recommendations and metric_snapshots tables with realistic patterns

HOW to use:
    docker compose up -d timescaledb   # start the DB
    python scripts/load_test_data.py   # seed it

No external dependencies beyond what's already in requirements.txt.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.ports.storage_port import MetricSnapshot


async def seed_recommendations(adapter: TimescaleAdapter) -> int:
    """Generate and persist realistic recommendations. Returns count."""
    namespaces = [
        ("team-alpha", "platform", "engineering", "prod"),
        ("team-beta", "data", "analytics", "prod"),
        ("team-gamma", "backend", "infra", "staging"),
    ]

    templates = [
        (Category.COST_RIGHTSIZING, Severity.HIGH, "cpu",
         "CPU requests over-provisioned by {pct}%", 8.0, 4.0, 87.60, 12.5),
        (Category.COST_RIGHTSIZING, Severity.WARNING, "memory",
         "Memory requests over-provisioned by {pct}%", 16.0, 8.0, 45.20, 6.8),
        (Category.COST_IDLE_RESOURCE, Severity.WARNING, "cpu",
         "CPU utilization below 10% for 7+ days", 0.5, 0.0, 35.40, 5.1),
        (Category.ENERGY_WASTE, Severity.INFO, "energy",
         "Idle workload consuming {kwh} kWh/day unnecessarily", 0.0, 0.0, 0.0, 8.2),
        (Category.CARBON_INTENSITY, Severity.INFO, "carbon",
         "Carbon footprint: {kg} kg CO2e/month (INFO — informational only)",
         0.0, 0.0, 0.0, 3.5),
        (Category.DATA_QUALITY, Severity.WARNING, "data_quality",
         "Kepler data unavailable — energy/carbon estimates based on model only",
         0.0, 0.0, 0.0, 0.0),
    ]

    count = 0
    for ns, team, cost_center, env in namespaces:
        for cat, sev, resource, msg_tpl, cur, rec, usd, co2 in templates:
            pct = random.randint(30, 70)
            kwh = random.uniform(5.0, 25.0)
            kg = random.uniform(1.0, 10.0)
            msg = msg_tpl.format(pct=pct, kwh=f"{kwh:.1f}", kg=f"{kg:.1f}")
            rec_obj = Recommendation(
                namespace=ns, category=cat, severity=sev, resource=resource,
                message=msg, current_value=cur, recommended_value=rec,
                estimated_savings_usd=usd, estimated_savings_kg_co2=co2,
            )
            await adapter.save_recommendation(rec_obj)
            count += 1

    return count


async def seed_snapshots(adapter: TimescaleAdapter) -> int:
    """Generate and persist 7 days of hourly metric snapshots. Returns count."""
    now = datetime.now(timezone.utc)
    namespaces = [
        ("team-alpha", "platform", "engineering", "prod"),
        ("team-beta", "data", "analytics", "prod"),
    ]

    count = 0
    for ns, team, cc, env in namespaces:
        for hours_ago in range(168, 0, -1):  # 7 days * 24 hours
            ts = now - timedelta(hours=hours_ago)
            snap = MetricSnapshot(
                time=ts,
                namespace=ns,
                team=team,
                cost_center=cc,
                environment=env,
                cpu_core_hours=random.uniform(2.0, 8.0),
                memory_gb_hours=random.uniform(4.0, 16.0),
                cost_usd=random.uniform(20.0, 120.0),
                energy_kwh=random.uniform(10.0, 50.0),
                co2e_kg=random.uniform(2.0, 12.0),
                waste_pct=random.uniform(5.0, 40.0),
            )
            await adapter.save_snapshot(snap)
            count += 1

    return count


async def main() -> None:
    """Connect to local TimescaleDB and seed test data."""
    adapter = TimescaleAdapter("postgresql://finops:finops@localhost:5432/finops")
    await adapter.ensure_tables_exist()

    print("Seeding recommendations...")
    rec_count = await seed_recommendations(adapter)
    print(f"  Created {rec_count} recommendations")

    print("Seeding metric snapshots (7 days, hourly)...")
    snap_count = await seed_snapshots(adapter)
    print(f"  Created {snap_count} snapshots across 2 namespaces")

    await adapter.close()
    print("Done. Query via Grafana or the API: http://localhost:8000/docs")


if __name__ == "__main__":
    asyncio.run(main())