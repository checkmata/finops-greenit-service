"""
END-TO-END on real components, no mock anywhere:

    real pods → cAdvisor/KSM/Kepler → REAL Prometheus (×2 clusters)
      → PrometheusMetricsAdapter → domain → scheduler cycle
      → TimescaleAdapter → REAL TimescaleDB → read back
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.scheduler.snapshot_job import run_multicluster_cycle
from tests.infra.conftest import env_or_skip


@pytest.mark.asyncio
async def test_full_cycle_real_prometheus_to_real_timescaledb(monkeypatch):
    primary_url, secondary_url = env_or_skip("prometheus")
    (db_url,) = env_or_skip("database")
    from app.config import settings
    monkeypatch.setattr(settings, "MIN_OBSERVATION_HOURS", 0.0)  # clusters are hours old, not days

    tag = uuid.uuid4().hex[:6]
    names = {f"e2e-primary-{tag}": primary_url, f"e2e-secondary-{tag}": secondary_url}
    ports = {n: PrometheusMetricsAdapter(u, 30, cluster_name=n) for n, u in names.items()}
    storage = TimescaleAdapter(db_url)
    start = datetime.now(timezone.utc) - timedelta(seconds=5)
    try:
        count = await run_multicluster_cycle(ports, storage, namespaces=["banking", "trading", "ecommerce"])
        assert count > 0
        snaps = await storage.query_snapshots(start=start, end=datetime.now(timezone.utc) + timedelta(minutes=1))
        mine = {(s.cluster, s.namespace): s for s in snaps if s.cluster in names}
        p = mine[(f"e2e-primary-{tag}", "banking")]
        assert p.cost_usd > 0 and p.requested_cost_usd > p.cost_usd          # real over-reservation
        assert p.energy_attribution == "container" and p.co2e_kg and p.co2e_kg > 0
        assert p.storage_status == "unavailable"                               # PVC without kubelet stats
        assert (f"e2e-secondary-{tag}", "ecommerce") in mine
        # V2 intent: never a fabricated zero-cost snapshot for a namespace that is not running.
        # V2.x: trading IS now replicated to the standby by GitOps (0 replicas, cold) and ran
        # there during the DR scenarios — a snapshot is legitimate only with REAL usage.
        st = mine.get((f"e2e-secondary-{tag}", "trading"))
        assert st is None or (st.cpu_core_hours or 0) > 0

        wl = await storage.query_workload_snapshots(start=start, end=datetime.now(timezone.utc) + timedelta(minutes=1),
                                                    cluster=f"e2e-primary-{tag}")
        assert {"txn-processor", "fraud-scorer", "order-matcher", "frontend"} <= {w.workload for w in wl}
        recs = await storage.list_recommendation_records(cluster=f"e2e-primary-{tag}", limit=500)
        assert any(r.recommendation.workload == "txn-processor" and r.recommendation.resource == "cpu" for r in recs)
        # CO2 advice is flagged "estimated" exactly when the namespace energy is not a measurement.
        for r in recs:
            if r.recommendation.category.value != "carbon_intensity":
                continue
            snap = mine[(r.recommendation.cluster, r.recommendation.namespace)]
            measured = snap.energy_power_source == "hardware" and snap.energy_attribution in ("container", "pod")
            assert bool(r.recommendation.estimation_method) is (not measured), (snap, r.recommendation)
    finally:
        async with storage._engine.begin() as conn:
            for table in ("recommendations", "metric_snapshots", "workload_snapshots"):
                await conn.execute(text(f"DELETE FROM {table} WHERE cluster = ANY(:c)"), {"c": list(names)})
        await storage.close()
        for p in ports.values():
            await p.close()
