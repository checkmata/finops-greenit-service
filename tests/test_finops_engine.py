"""
Unit tests for finops_engine — pure functions, no mocks needed.

Per AGENTS.md: domain engine tests use plain fixtures, no mocks (pure functions).
"""

from datetime import datetime, timezone

import pytest

from app.domain.finops_engine import (
    BYTES_PER_GB,
    HOURS_PER_MONTH,
    calculate_monthly_cost,
    calculate_namespace_waste,
    calculate_overprovisioning_ratio,
    calculate_requested_cost,
    generate_recommendations,
)
from app.domain.models.pod_metric import PodMetric, StorageMetric

NOW = datetime.now(timezone.utc)

# Default cost model (from config.py defaults)
COST_PER_CORE_HOUR = 0.03
COST_PER_GB_HOUR = 0.004
COST_PER_GB_MONTH = 0.10


def make_pod(
    cpu_core_hours: float = 4.0,
    memory_bytes: float = 8 * BYTES_PER_GB,
    cpu_requested: float = 8.0,
    memory_requested: float = 16 * BYTES_PER_GB,
    namespace: str = "team-alpha",
) -> PodMetric:
    return PodMetric(
        namespace=namespace,
        team="platform",
        cost_center="engineering",
        environment="prod",
        cpu_core_hours=cpu_core_hours,
        memory_bytes=memory_bytes,
        cpu_requested=cpu_requested,
        memory_requested=memory_requested,
        timestamp=NOW,
    )


def make_storage(
    used_bytes: float = 50 * BYTES_PER_GB,
    capacity_bytes: float = 100 * BYTES_PER_GB,
) -> StorageMetric:
    return StorageMetric(
        namespace="team-alpha",
        pvc_name="data-vol",
        used_bytes=used_bytes,
        capacity_bytes=capacity_bytes,
        is_bound=True,
        timestamp=NOW,
    )


class TestMonthlyCost:
    def test_cpu_only_cost(self):
        pod = make_pod(cpu_core_hours=4.0, memory_bytes=0)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        expected_cpu = 4.0 * COST_PER_CORE_HOUR * HOURS_PER_MONTH
        assert cost.cpu_cost_usd == pytest.approx(expected_cpu, rel=1e-4)
        assert cost.memory_cost_usd == 0.0

    def test_memory_only_cost(self):
        pod = make_pod(cpu_core_hours=0, memory_bytes=8 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        expected_memory = 8.0 * COST_PER_GB_HOUR * HOURS_PER_MONTH
        assert cost.memory_cost_usd == pytest.approx(expected_memory, rel=1e-4)

    def test_total_cost_is_sum(self):
        pod = make_pod(cpu_core_hours=4.0, memory_bytes=8 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        assert cost.total_cost_usd == pytest.approx(
            cost.cpu_cost_usd + cost.memory_cost_usd + cost.storage_cost_usd, rel=1e-6
        )

    def test_storage_cost_included_when_provided(self):
        pod = make_pod()
        storage = make_storage(used_bytes=50 * BYTES_PER_GB, capacity_bytes=100 * BYTES_PER_GB)
        cost = calculate_monthly_cost(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH, [storage]
        )
        expected_storage = 50.0 * COST_PER_GB_MONTH
        assert cost.storage_cost_usd == pytest.approx(expected_storage, rel=1e-4)


class TestRequestedCost:
    def test_requested_cost_uses_request_values(self):
        pod = make_pod(cpu_core_hours=2.0, cpu_requested=8.0)
        cost = calculate_requested_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        # Should use cpu_requested=8.0, not cpu_core_hours=2.0
        expected_cpu = 8.0 * COST_PER_CORE_HOUR * HOURS_PER_MONTH
        assert cost.cpu_cost_usd == pytest.approx(expected_cpu, rel=1e-4)


class TestOverprovisioningRatio:
    def test_well_utilized(self):
        pod = make_pod(cpu_core_hours=7.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.cpu_ratio == pytest.approx(7.0 / 8.0, rel=1e-4)
        assert not ratio.is_overprovisioned_cpu
        assert not ratio.is_underprovisioned_cpu

    def test_overprovisioned(self):
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.is_overprovisioned_cpu
        assert ratio.cpu_ratio == pytest.approx(3.0 / 8.0, rel=1e-4)

    def test_underprovisioned(self):
        pod = make_pod(cpu_core_hours=9.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.is_underprovisioned_cpu
        assert not ratio.is_overprovisioned_cpu

    def test_zero_requested_returns_zero(self):
        pod = make_pod(cpu_requested=0, cpu_core_hours=5.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.cpu_ratio == 0.0


class TestNamespaceWaste:
    def test_waste_is_gap_between_requested_and_actual(self):
        pod = make_pod(cpu_core_hours=4.0, cpu_requested=8.0)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.wasted_cost_usd > 0
        assert waste.waste_ratio > 0
        assert waste.waste_ratio < 1.0

    def test_zero_request_no_waste(self):
        pod = make_pod(cpu_requested=0, cpu_core_hours=0, memory_requested=0, memory_bytes=0)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.wasted_cost_usd == 0.0


class TestGenerateRecommendations:
    def test_overprovisioned_generates_recommendation(self):
        pods = [make_pod(cpu_core_hours=3.0, cpu_requested=8.0)]
        result = generate_recommendations(
            pods, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert result.total >= 1
        assert any("CPU" in r.message for r in result.recommendations)

    def test_underprovisioned_generates_critical(self):
        pods = [make_pod(cpu_core_hours=10.0, cpu_requested=8.0)]
        result = generate_recommendations(
            pods, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        critical = [r for r in result.recommendations if r.severity.value == "critical"]
        assert len(critical) >= 1

    def test_empty_pods_returns_empty_set(self):
        result = generate_recommendations(
            [], COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert result.total == 0

    def test_well_utilized_no_recommendations(self):
        pods = [make_pod(cpu_core_hours=7.5, cpu_requested=8.0, memory_bytes=15 * BYTES_PER_GB, memory_requested=16 * BYTES_PER_GB)]
        result = generate_recommendations(
            pods, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert result.total == 0