"""V1 FinOps: per-component waste, bounded savings (no double counting), storage status."""

import itertools

import pytest

from app.domain.finops_engine import (
    calculate_monthly_cost,
    calculate_namespace_waste,
    calculate_requested_cost,
    rightsizing_savings_usd,
    storage_status,
)
from app.domain.models.data_quality import MetricStatus
from app.domain.models.recommendation import Category
from app.domain.recommendation_engine import generate_all_recommendations
from tests.helpers_v1 import GB, all_healthy, pod, pvc

P = dict(cost_per_core_hour=0.03, cost_per_gb_hour=0.004, cost_per_gb_month=0.10)


class TestNoDoubleCounting:
    def test_mvp_example_savings_do_not_exceed_waste(self):
        """The exact MVP case that produced 24.19 + 25.92 > 34.56."""
        p = pod(cpu_core_hours=0.6, memory_bytes=0.5 * GB, cpu_requested=2.0, memory_requested=2 * GB)
        waste = calculate_namespace_waste(p, **P)
        recs = generate_all_recommendations(p, None, None, all_healthy(), **P).recommendations
        savings = sum(r.estimated_savings_usd or 0 for r in recs if r.category is Category.COST_RIGHTSIZING)
        assert waste.wasted_cost_usd == pytest.approx(34.56)
        assert savings <= waste.wasted_cost_usd + 1e-9

    def test_cpu_saving_equals_released_reservation(self):
        # 2.0 → 0.72 cores released = 1.28 × 0.03 × 720
        assert rightsizing_savings_usd(2.0, 0.72, 0.03) == pytest.approx(27.648)

    def test_saving_never_negative(self):
        assert rightsizing_savings_usd(1.0, 2.0, 0.03) == 0.0

    @pytest.mark.parametrize(
        "cpu_used,cpu_req,mem_used,mem_req",
        list(itertools.product([0.0, 0.1, 0.9, 1.9, 3.0], [0.5, 2.0], [0.0, 0.3, 1.5, 4.0], [1.0, 2.0])),
    )
    def test_sum_of_savings_bounded_by_total_waste(self, cpu_used, cpu_req, mem_used, mem_req):
        p = pod(cpu_core_hours=cpu_used, cpu_requested=cpu_req,
                memory_bytes=mem_used * GB, memory_requested=mem_req * GB)
        waste = calculate_namespace_waste(p, **P)
        recs = generate_all_recommendations(p, None, None, all_healthy(), **P).recommendations
        cpu_s = sum(r.estimated_savings_usd or 0 for r in recs if r.resource == "cpu")
        mem_s = sum(r.estimated_savings_usd or 0 for r in recs if r.resource == "memory")
        assert cpu_s <= waste.cpu_waste_usd + 1e-6
        assert mem_s <= waste.memory_waste_usd + 1e-6
        assert cpu_s + mem_s <= waste.wasted_cost_usd + 1e-6


class TestComponentWaste:
    def test_components_sum_to_total(self):
        p = pod()
        w = calculate_namespace_waste(p, storage_metrics=[pvc(used_gb=2, cap_gb=10)], **P)
        assert w.cpu_waste_usd + w.memory_waste_usd + w.storage_waste_usd == pytest.approx(w.wasted_cost_usd, abs=1e-3)
        assert w.storage_waste_usd == pytest.approx(0.8)  # 8 GB unused × 0.10

    def test_under_provisioned_resource_does_not_cancel_other_waste(self):
        """CPU over-reserved, memory over-used: gross waste = CPU waste only (not net)."""
        p = pod(cpu_core_hours=0.5, cpu_requested=2.0, memory_bytes=3 * GB, memory_requested=2 * GB)
        w = calculate_namespace_waste(p, **P)
        assert w.memory_waste_usd == 0.0
        assert w.wasted_cost_usd == pytest.approx(w.cpu_waste_usd)
        req = calculate_requested_cost(p, **P).total_cost_usd
        act = calculate_monthly_cost(p, **P).total_cost_usd
        assert w.wasted_cost_usd > req - act  # net gap would under-report


class TestStorage:
    def test_status_values(self):
        assert storage_status(None) is MetricStatus.UNAVAILABLE
        assert storage_status([]) is MetricStatus.NOT_APPLICABLE
        assert storage_status([pvc()]) is MetricStatus.AVAILABLE
        assert storage_status([pvc(), pvc("b", stats=False)]) is MetricStatus.PARTIAL
        assert storage_status([pvc(stats=False)]) is MetricStatus.UNAVAILABLE

    def test_pvc_without_stats_is_not_priced(self):
        p = pod()
        cost = calculate_requested_cost(p, storage_metrics=[pvc(cap_gb=10), pvc("ghost", 0, 0, stats=False)], **P)
        assert cost.storage_cost_usd == pytest.approx(1.0)
        assert cost.storage_status is MetricStatus.PARTIAL

    def test_storage_contributes_to_total(self):
        p = pod()
        with_s = calculate_monthly_cost(p, storage_metrics=[pvc(used_gb=5)], **P)
        without = calculate_monthly_cost(p, **P)
        assert with_s.total_cost_usd - without.total_cost_usd == pytest.approx(0.5)
        assert without.storage_status is MetricStatus.UNAVAILABLE


class TestWorkloadScope:
    def test_recommendations_per_workload_carry_scope(self):
        ns = pod()
        wls = [pod(workload="ledger", cpu_core_hours=0.2, cpu_requested=1.0),
               pod(workload="api", cpu_core_hours=0.9, cpu_requested=1.0, memory_bytes=1.8 * GB)]
        recs = generate_all_recommendations(ns, None, None, all_healthy(), workloads=wls, **P).recommendations
        cpu = [r for r in recs if r.resource == "cpu"]
        assert {r.workload for r in cpu} == {"ledger"}
        r = cpu[0]
        assert (r.cluster, r.team, r.cost_center, r.environment) == ("primary", "payments", "cc-100", "prod")
        assert "ledger" in r.message
        assert r.fingerprint == "primary|banking|ledger|cost_right-sizing|cpu"


class TestMinimumHistory:
    def test_young_workload_is_not_right_sized(self):
        young = pod(workload="new-api", cpu_core_hours=0.0, cpu_requested=2.0, observation_hours=0.1)
        recs = generate_all_recommendations(pod(), None, None, all_healthy(), workloads=[young], **P).recommendations
        assert not [r for r in recs if r.category is Category.COST_RIGHTSIZING]
        info = [r for r in recs if r.resource == "history"]
        assert len(info) == 1 and info[0].workload == "new-api" and "deferred" in info[0].message

    def test_enough_history_allows_right_sizing(self):
        old = pod(workload="api", cpu_core_hours=0.2, cpu_requested=2.0, observation_hours=48.0)
        recs = generate_all_recommendations(pod(), None, None, all_healthy(), workloads=[old], **P).recommendations
        assert [r for r in recs if r.category is Category.COST_RIGHTSIZING and r.resource == "cpu"]

    def test_threshold_is_configurable(self):
        young = pod(cpu_core_hours=0.2, cpu_requested=2.0, observation_hours=0.5)
        recs = generate_all_recommendations(young, None, None, all_healthy(), min_observation_hours=0.25, **P)
        assert any(r.category is Category.COST_RIGHTSIZING for r in recs.recommendations)
