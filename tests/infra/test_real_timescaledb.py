"""TimescaleAdapter against the REAL PostgreSQL/TimescaleDB (migrations applied by the container).

Each test writes under a unique cluster name and deletes its rows afterwards.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.domain.models.snapshot import MetricSnapshot, WorkloadSnapshot
from tests.infra.conftest import env_or_skip


@asynccontextmanager
async def real_db():
    """Async context manager rather than an async fixture (pytest-asyncio 0.23 bug with pytest 8)."""
    (url,) = env_or_skip("database")
    adapter = TimescaleAdapter(url)
    cluster = f"infra-test-{uuid.uuid4().hex[:8]}"
    try:
        yield adapter, cluster
    finally:
        async with adapter._engine.begin() as conn:
            for table in ("recommendations", "metric_snapshots", "workload_snapshots"):
                await conn.execute(text(f"DELETE FROM {table} WHERE cluster = :c"), {"c": cluster})
        await adapter.close()


@pytest.mark.asyncio
async def test_schema_is_timescale():
    async with real_db() as (adapter, _):
        async with adapter._engine.connect() as conn:
            hyper = {r[0] for r in await conn.execute(text(
                "SELECT hypertable_name FROM timescaledb_information.hypertables"))}
            retention = {r[0] for r in await conn.execute(text(
                "SELECT hypertable_name FROM timescaledb_information.jobs WHERE proc_name='policy_retention'"))}
            check = (await conn.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='recommendations_status_check'"
            ))).scalar()
            cols = {r[0] for r in await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='metric_snapshots'"))}
    assert {"metric_snapshots", "workload_snapshots"} <= hyper
    assert {"metric_snapshots", "workload_snapshots"} <= retention
    assert "obsolete" in check and "rejected" in check and "dismissed" not in check
    assert {"energy_power_source", "energy_scope", "cluster", "waste_cost_usd"} <= cols  # migrations 002 + 003


@pytest.mark.asyncio
async def test_recommendation_upsert_dedup_and_lifecycle_on_postgres():
    async with real_db() as (adapter, cluster):
        rec = Recommendation(category=Category.COST_RIGHTSIZING, severity=Severity.WARNING, namespace="banking",
                             resource="cpu", message="m1", current_value="1", recommended_value="0.5",
                             cluster=cluster, workload="txn-processor", team="payments")
        i1, a1 = await adapter.upsert_recommendation(rec)
        i2, a2 = await adapter.upsert_recommendation(rec)
        assert (a1, a2) == ("insert", "refresh") and i1 == i2
        assert await adapter.update_recommendation_status(i1, "approved")
        assert (await adapter.upsert_recommendation(rec))[1] == "skip"
        stored = await adapter.get_recommendation_record(i1)
        assert stored.status == "approved" and stored.recommendation.team == "payments"


@pytest.mark.asyncio
async def test_invalid_status_rejected_by_database_constraint():
    async with real_db() as (adapter, cluster):
        rec = Recommendation(category=Category.COST_RIGHTSIZING, severity=Severity.INFO, namespace="x",
                             resource="cpu", message="m", current_value="1", recommended_value="1", cluster=cluster)
        rid, _ = await adapter.upsert_recommendation(rec)
        with pytest.raises(Exception):
            await adapter.update_recommendation_status(rid, "dismissed")  # legacy value, refused by CHECK


@pytest.mark.asyncio
async def test_snapshots_roundtrip_with_nulls_and_provenance():
    async with real_db() as (adapter, cluster):
        now = datetime.now(timezone.utc)
        await adapter.save_snapshot(MetricSnapshot(
            time=now, cluster=cluster, namespace="banking", team="t", cost_center="c", environment="e",
            cpu_core_hours=0.05, memory_gb_hours=0.3, cost_usd=1.5, requested_cost_usd=25.0, waste_cost_usd=23.5,
            waste_pct=94.0, energy_kwh=None, co2e_kg=None, storage_status="unavailable",
            energy_attribution="unavailable", energy_power_source="hardware", energy_scope="cpu_package+dram"))
        await adapter.save_workload_snapshots([WorkloadSnapshot(
            now, cluster, "banking", "txn-processor", "t", "c", "e", 0.02, 0.8, 0.1, 0.5, 18.0, 0.5, 17.5, 15.0)])
        window = dict(start=now - timedelta(minutes=1), end=now + timedelta(minutes=1), cluster=cluster)
        snaps = await adapter.query_snapshots(**window)
        assert len(snaps) == 1 and snaps[0].energy_kwh is None and snaps[0].cost_usd == pytest.approx(1.5)
        assert (snaps[0].energy_power_source, snaps[0].energy_scope) == ("hardware", "cpu_package+dram")
        wl = await adapter.query_workload_snapshots(**window)
        assert [w.workload for w in wl] == ["txn-processor"]
