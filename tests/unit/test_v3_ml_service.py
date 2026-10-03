"""V3 application service: history → series → analysis → recommendations (plain fixtures)."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models.recommendation import Category
from app.domain.models.snapshot import WorkloadSnapshot
from app.ml_service import MLService, MLSettings

NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)


def snaps(n=400, cpu_now=0.05, spike_at=None, request=1.0, mem=0.2, step=60):
    out = []
    for i in range(n):
        t = NOW - timedelta(seconds=(n - i) * step)
        c = cpu_now * (6 if spike_at is not None and spike_at <= i < spike_at + 10 else 1)
        out.append(WorkloadSnapshot(t, "primary", "banking", "txn-processor", "payments", "cc", "prod",
                                    cpu_cores_used=0.05, cpu_cores_requested=request, memory_gb_used=mem,
                                    memory_gb_requested=1.0, requested_cost_usd=0, actual_cost_usd=0,
                                    waste_cost_usd=0, potential_savings_usd=0,
                                    energy_interval_kwh=1e-6, co2_interval_kg=6.65e-7, cpu_cores_used_now=c))
    return out


class Storage:
    def __init__(self, rows):
        self.rows, self.upserts, self.obsolete_calls = rows, [], []

    async def query_workload_snapshots(self, *, start, end, cluster=None, namespace=None, workload=None):
        return [r for r in self.rows if start <= r.time <= end and (cluster is None or r.cluster == cluster)]

    async def upsert_recommendation(self, rec):
        self.upserts.append(rec)
        return None, "insert"

    async def mark_obsolete(self, **kw):
        self.obsolete_calls.append(kw)
        return 0


def svc(rows, **kw):
    return MLService(Storage(rows), MLSettings(**kw), clock=lambda: NOW)


def test_series_use_point_in_time_cpu_and_power_not_the_long_average():
    s = svc([]).series("primary", "banking", "txn-processor", snaps(20, cpu_now=0.3))
    assert s["cpu"].values[0] == pytest.approx(0.3)             # cpu_cores_used_now, not cpu_cores_used (0.05)
    assert s["energy"].unit == "W" and s["energy"].values[0] == pytest.approx(1e-6 * 3.6e6 / 60)
    assert s["cost"].values[0] == pytest.approx((0.3 * 0.04 + 0.2 * 0.005) * 720)


def test_rows_without_point_in_time_cpu_are_gaps_not_zeros():
    rows = snaps(20)
    rows = [r if i % 2 else WorkloadSnapshot(**{**r.__dict__, "cpu_cores_used_now": None}) for i, r in enumerate(rows)]
    s = svc([]).series("primary", "banking", "txn-processor", rows)
    assert all(v > 0 for v in s["cpu"].values)


def test_analysis_flags_the_spike_and_proposes_a_decrease_on_an_overprovisioned_workload():
    a = svc([]).analyze("primary", "banking", "txn-processor", snaps(1800, spike_at=1700))
    cpu_eps = [e for e in a.anomalies if e.metric == "cpu"]
    assert cpu_eps and cpu_eps[-1].direction == "spike"
    rs = a.rightsizing["cpu"]
    assert rs.action == "decrease" and rs.recommended_request < 1.0 and rs.expected_monthly_saving_usd > 0
    assert a.forecasts["cpu"] is not None                        # 30 h of 5-min points → validated


def test_recommendations_are_ml_categories_with_scope_and_provenance():
    service = svc([], recent_anomaly_hours=24)
    a = service.analyze("primary", "banking", "txn-processor", snaps(1800, spike_at=1780))
    recs = service.to_recommendations(a)
    cats = {r.category for r in recs}
    assert Category.COST_RIGHTSIZING_ML in cats and Category.ANOMALY in cats
    r = next(r for r in recs if r.category is Category.COST_RIGHTSIZING_ML)
    assert r.cluster == "primary" and r.workload == "txn-processor" and r.team == "payments"
    assert r.estimation_method.startswith("ml:v3") and "confidence" in r.message


@pytest.mark.asyncio
async def test_ml_cycle_upserts_and_only_closes_ml_categories():
    service = svc(snaps(1800, spike_at=1790), recent_anomaly_hours=24)
    counts = await service.run_once(["primary"])
    st = service.storage
    assert counts["insert"] == len(st.upserts) > 0
    assert st.obsolete_calls and all(c["categories"] == {"anomaly", "cost_right-sizing_ml"}
                                     for c in st.obsolete_calls)


@pytest.mark.asyncio
async def test_workload_without_history_is_none():
    assert await svc([]).analyze_workload("primary", "banking", "nothing") is None


@pytest.mark.asyncio
async def test_ml_loop_runs_as_soon_as_leadership_is_acquired():
    import asyncio

    from app.ml_service import run_ml_loop

    class Leader:
        is_leader = False
    service, leader = svc(snaps(300)), Leader()
    task = asyncio.create_task(run_ml_loop(service, ["primary"], leader, interval_seconds=0.05))
    await asyncio.sleep(0.02)
    assert service.storage.upserts == [] and service.storage.obsolete_calls == []   # follower: nothing
    leader.is_leader = True
    await asyncio.sleep(0.12)
    task.cancel()
    assert service.storage.obsolete_calls                                            # ran once leader


def test_standby_cluster_is_never_right_sized_on_its_idle_usage():
    service = MLService(Storage([]), MLSettings(standby_clusters=frozenset({"primary"})), clock=lambda: NOW)
    a = service.analyze("primary", "banking", "txn-processor", snaps(1800))
    assert a.rightsizing["cpu"].action == "decrease"                  # computed, visible in the API…
    assert not [r for r in service.to_recommendations(a) if r.category is Category.COST_RIGHTSIZING_ML]  # …never advised
    assert a.notes and "standby" in a.notes[0]


def test_an_increase_is_never_a_negative_saving_api_contract():
    """Regression (real infra): a −0.18 'saving' broke /api/recommendations/history (schema: ≥ 0)."""
    service = svc([])
    a = service.analyze("primary", "banking", "txn-processor", snaps(1800, cpu_now=0.5, request=0.2))
    rec = next(r for r in service.to_recommendations(a) if r.category is Category.COST_RIGHTSIZING_ML and r.resource == "cpu")
    assert a.rightsizing["cpu"].action == "increase"
    assert rec.estimated_savings_usd is None and "extra cost" in rec.message
