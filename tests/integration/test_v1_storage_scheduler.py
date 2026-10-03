"""V1 end-to-end without network: InMemoryMetricsPort (2 clusters) → scheduler → TimescaleAdapter on SQLite."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.models.energy import EnergyAttribution
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.scheduler.snapshot_job import run_multicluster_cycle
from tests.helpers_v1 import GB, InMemoryMetricsPort, energy, pod, pvc, storage_ctx


def world():
    primary = InMemoryMetricsPort("primary", {
        "banking": {
            "total": pod("banking", cluster="primary", cpu_core_hours=1.0, cpu_requested=3.0),
            "workloads": [
                pod("banking", "txn-processor", cluster="primary", cpu_core_hours=0.2, cpu_requested=2.0),
                pod("banking", "fraud-scorer", cluster="primary", cpu_core_hours=0.8, cpu_requested=1.0,
                    memory_bytes=0.9 * GB, memory_requested=1 * GB),
            ],
            "pvcs": [pvc(used_gb=6, cap_gb=20)],
            "energy": energy("banking", 5.0),
        },
        "kube-system": {"total": pod("kube-system", cluster="primary")},
    })
    secondary = InMemoryMetricsPort("secondary", {
        "banking": {
            "total": pod("banking", cluster="secondary", cpu_core_hours=0.05, cpu_requested=1.0),
            "energy": energy("banking", 1.0, attribution=EnergyAttribution.NODE_CPU_SHARE),
        },
    })
    return primary, secondary


def rec(**kw):
    base = dict(category=Category.COST_RIGHTSIZING, severity=Severity.WARNING, namespace="banking",
                resource="cpu", message="m", current_value="2", recommended_value="1", cluster="primary",
                workload="api", team="payments")
    base.update(kw)
    return Recommendation(**base)


class TestUpsertDedup:
    @pytest.mark.asyncio
    async def test_insert_refresh_skip(self):
        async with storage_ctx() as st:
            id1, a1 = await st.upsert_recommendation(rec(message="v1"))
            id2, a2 = await st.upsert_recommendation(rec(message="v2"))
            assert (a1, a2) == ("insert", "refresh") and id1 == id2
            stored = await st.get_recommendation_record(id1)
            assert stored.recommendation.message == "v2" and stored.recommendation.team == "payments"
            await st.update_recommendation_status(id1, "approved")
            _, a3 = await st.upsert_recommendation(rec(message="v3"))
            assert a3 == "skip"
            await st.update_recommendation_status(id1, "executed")
            id4, a4 = await st.upsert_recommendation(rec(message="v4"))
            assert a4 == "insert" and id4 != id1

    @pytest.mark.asyncio
    async def test_rejected_is_respected(self):
        async with storage_ctx() as st:
            i, _ = await st.upsert_recommendation(rec())
            await st.update_recommendation_status(i, "rejected")
            _, action = await st.upsert_recommendation(rec())
            assert action == "skip"

    @pytest.mark.asyncio
    async def test_list_records_returns_real_ids_and_filters(self):
        async with storage_ctx() as st:
            i, _ = await st.upsert_recommendation(rec())
            await st.upsert_recommendation(rec(cluster="secondary"))
            recs = await st.list_recommendation_records(cluster="primary")
            assert [r.id for r in recs] == [i] and recs[0].status == "pending"


class TestMulticlusterCycle:
    @pytest.mark.asyncio
    async def test_full_cycle_persists_per_cluster_history(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "SCHEDULER_NAMESPACES", "")
        async with storage_ctx() as st:
            primary, secondary = world()
            n = await run_multicluster_cycle({"primary": primary, "secondary": secondary}, st)
            assert n > 0

            now = datetime.now(timezone.utc)
            snaps = await st.query_snapshots(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
            by = {(s.cluster, s.namespace): s for s in snaps}
            assert set(by) == {("primary", "banking"), ("secondary", "banking")}  # kube-system excluded

            p = by[("primary", "banking")]
            assert p.cost_usd > 0 and p.requested_cost_usd > p.cost_usd      # MVP stored 0.0
            assert p.storage_status == "available" and p.storage_cost_usd == pytest.approx(0.6)
            assert p.energy_attribution == "container" and p.co2e_kg == pytest.approx(5 * 1.4 * 0.475)
            assert p.idle_energy_kwh is None
            assert 0 < p.potential_savings_usd <= p.waste_cost_usd
            s = by[("secondary", "banking")]
            assert s.energy_attribution == "node_cpu_share" and s.storage_status == "unavailable"

            wl = await st.query_workload_snapshots(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
            assert {w.workload for w in wl} == {"txn-processor", "fraud-scorer"}

            recs = await st.list_recommendation_records(limit=500)
            cpu = [r.recommendation for r in recs if r.recommendation.resource == "cpu" and r.recommendation.cluster == "primary"]
            assert {r.workload for r in cpu} == {"txn-processor"}
            assert any(r.recommendation.estimation_method for r in recs if r.recommendation.cluster == "secondary")

            # Second cycle: no duplicate recommendations
            before = len(recs)
            await run_multicluster_cycle({"primary": primary, "secondary": secondary}, st)
            assert len(await st.list_recommendation_records(limit=500)) == before

    @pytest.mark.asyncio
    async def test_unreachable_cluster_is_skipped(self):
        async with storage_ctx() as st:
            primary, secondary = world()
            secondary.reachable = False
            await run_multicluster_cycle({"primary": primary, "secondary": secondary}, st, namespaces=["banking"])
            now = datetime.now(timezone.utc)
            snaps = await st.query_snapshots(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
            assert {s.cluster for s in snaps} == {"primary"}


class TestObsolete:
    @pytest.mark.asyncio
    async def test_resolved_advice_becomes_obsolete_but_human_states_untouched(self):
        async with storage_ctx() as st:
            primary, _ = world()
            await run_multicluster_cycle({"primary": primary}, st, namespaces=["banking"])
            recs = await st.list_recommendation_records(limit=500)
            cpu = [r for r in recs if r.recommendation.resource == "cpu"][0]
            # a human approves the memory advice (if any) — must stay approved
            others = [r for r in recs if r.id != cpu.id and r.status == "pending"]
            await st.update_recommendation_status(others[0].id, "approved")

            # Someone fixes txn-processor's CPU request by hand → CPU advice no longer produced
            fixed = [w if w.workload != "txn-processor" else
                     type(w)(**{**w.__dict__, "cpu_requested": 0.25}) for w in primary.data["banking"]["workloads"]]
            primary.data["banking"]["workloads"] = fixed
            await run_multicluster_cycle({"primary": primary}, st, namespaces=["banking"])

            after = {r.id: r.status for r in await st.list_recommendation_records(limit=500)}
            assert after[cpu.id] == "obsolete"
            assert after[others[0].id] == "approved"

    def test_api_cannot_set_obsolete(self):
        from app.domain.recommendation_lifecycle import can_transition
        assert not can_transition("pending", "obsolete")
        assert not can_transition("obsolete", "pending")


class TestStorageCostNeverFakeZero:
    @pytest.mark.asyncio
    async def test_pvc_without_stats_stores_null_cost(self):
        """Regression from the live Grafana check: 'unavailable' storage must not be stored as $0."""
        async with storage_ctx() as st:
            primary, _ = world()
            primary.data["banking"]["pvcs"] = [pvc(used_gb=0, cap_gb=0, stats=False)]
            await run_multicluster_cycle({"primary": primary}, st, namespaces=["banking"])
            now = datetime.now(timezone.utc)
            snap = (await st.query_snapshots(start=now - timedelta(hours=1), end=now + timedelta(hours=1)))[0]
            assert snap.storage_status == "unavailable" and snap.storage_cost_usd is None
