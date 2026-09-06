"""
Unit tests for finops_engine — pure functions, no mocks needed.

WHY zero mocks:
- finops_engine functions are pure math formulas: same inputs → same outputs
- No I/O, no network, no database — just arithmetic on domain objects
- Fixtures are hardcoded PodMetric/StorageMetric dataclasses
- Tests run in milliseconds (no external dependencies)

WHAT "test the domain in isolation" buys us:
- Tests verify BUSINESS LOGIC, not infrastructure
- Refactoring adapters (Prometheus, TimescaleDB) doesn't break these tests
- Running tests offline (no Prometheus) is possible
- Catching logic bugs early (before they reach production)
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
)
from app.domain.models.pod_metric import PodMetric, StorageMetric

NOW = datetime.now(timezone.utc)

# Default cost model (from config.py defaults)
COST_PER_CORE_HOUR = 0.03
COST_PER_GB_HOUR = 0.004
COST_PER_GB_MONTH = 0.10


# =============================================================================
# Fixtures — hardcoded domain objects (no mocking)
# =============================================================================


def make_pod(
    cpu_core_hours: float = 4.0,
    memory_bytes: float = 8 * BYTES_PER_GB,
    cpu_requested: float = 8.0,
    memory_requested: float = 16 * BYTES_PER_GB,
    namespace: str = "team-alpha",
) -> PodMetric:
    """Create a PodMetric with sensible defaults for testing."""
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
    """Create a StorageMetric with sensible defaults for testing."""
    return StorageMetric(
        namespace="team-alpha",
        pvc_name="data-vol",
        used_bytes=used_bytes,
        capacity_bytes=capacity_bytes,
        is_bound=True,
        timestamp=NOW,
    )


# =============================================================================
# Test calculate_monthly_cost — normal cases
# =============================================================================


class TestMonthlyCostNormal:
    """Normal cases for monthly cost calculation."""

    def test_cpu_only_cost(self):
        """CPU-only workload should calculate correct cost."""
        pod = make_pod(cpu_core_hours=4.0, memory_bytes=0)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        expected_cpu = 4.0 * COST_PER_CORE_HOUR * HOURS_PER_MONTH
        assert cost.cpu_cost_usd == pytest.approx(expected_cpu, rel=1e-4)
        assert cost.memory_cost_usd == 0.0

    def test_memory_only_cost(self):
        """Memory-only workload should calculate correct cost."""
        pod = make_pod(cpu_core_hours=0, memory_bytes=8 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        expected_memory = 8.0 * COST_PER_GB_HOUR * HOURS_PER_MONTH
        assert cost.memory_cost_usd == pytest.approx(expected_memory, rel=1e-4)

    def test_total_cost_is_sum(self):
        """Total cost should equal sum of component costs."""
        pod = make_pod(cpu_core_hours=4.0, memory_bytes=8 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        assert cost.total_cost_usd == pytest.approx(
            cost.cpu_cost_usd + cost.memory_cost_usd + cost.storage_cost_usd, rel=1e-6
        )

    def test_storage_cost_included_when_provided(self):
        """Storage cost should be included when storage metrics are provided."""
        pod = make_pod()
        storage = make_storage(used_bytes=50 * BYTES_PER_GB, capacity_bytes=100 * BYTES_PER_GB)
        cost = calculate_monthly_cost(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH, [storage]
        )
        expected_storage = 50.0 * COST_PER_GB_MONTH
        assert cost.storage_cost_usd == pytest.approx(expected_storage, rel=1e-4)


# =============================================================================
# Test calculate_monthly_cost — edge cases
# =============================================================================


class TestMonthlyCostEdgeCases:
    """Edge cases for monthly cost calculation."""

    def test_zero_usage_returns_zero_cost(self):
        """Zero resource usage should return zero cost."""
        pod = make_pod(cpu_core_hours=0, memory_bytes=0)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        assert cost.total_cost_usd == 0.0
        assert cost.cpu_cost_usd == 0.0
        assert cost.memory_cost_usd == 0.0

    def test_zero_cost_parameters_returns_zero(self):
        """Zero cost parameters should return zero cost."""
        pod = make_pod(cpu_core_hours=10.0, memory_bytes=16 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, 0.0, 0.0, 0.0)
        assert cost.total_cost_usd == 0.0

    def test_large_values_no_overflow(self):
        """Large resource values should not cause overflow."""
        pod = make_pod(cpu_core_hours=1000.0, memory_bytes=1000 * BYTES_PER_GB)
        cost = calculate_monthly_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        assert cost.total_cost_usd > 0
        assert cost.total_cost_usd < float("inf")


# =============================================================================
# Test calculate_requested_cost — normal cases
# =============================================================================


class TestRequestedCostNormal:
    """Normal cases for requested cost calculation."""

    def test_requested_cost_uses_request_values(self):
        """Requested cost should use requested resources, not actual usage."""
        pod = make_pod(cpu_core_hours=2.0, cpu_requested=8.0)
        cost = calculate_requested_cost(pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH)
        # Should use cpu_requested=8.0, not cpu_core_hours=2.0
        expected_cpu = 8.0 * COST_PER_CORE_HOUR * HOURS_PER_MONTH
        assert cost.cpu_cost_usd == pytest.approx(expected_cpu, rel=1e-4)


# =============================================================================
# Test calculate_overprovisioning_ratio — normal cases
# =============================================================================


class TestOverprovisioningRatioNormal:
    """Normal cases for overprovisioning ratio calculation."""

    def test_well_utilized(self):
        """Well-utilized pod should not be overprovisioned."""
        pod = make_pod(cpu_core_hours=7.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.cpu_ratio == pytest.approx(7.0 / 8.0, rel=1e-4)
        assert not ratio.is_overprovisioned_cpu
        assert not ratio.is_underprovisioned_cpu

    def test_overprovisioned(self):
        """Over-provisioned pod should be detected."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.is_overprovisioned_cpu
        assert ratio.cpu_ratio == pytest.approx(3.0 / 8.0, rel=1e-4)

    def test_underprovisioned(self):
        """Under-provisioned pod should be detected."""
        pod = make_pod(cpu_core_hours=9.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.is_underprovisioned_cpu
        assert not ratio.is_overprovisioned_cpu


# =============================================================================
# Test calculate_overprovisioning_ratio — edge cases
# =============================================================================


class TestOverprovisioningRatioEdgeCases:
    """Edge cases for overprovisioning ratio calculation."""

    def test_zero_requested_returns_zero(self):
        """Zero requested resources should return zero ratio."""
        pod = make_pod(cpu_requested=0, cpu_core_hours=5.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.cpu_ratio == 0.0

    def test_zero_usage_and_zero_requested(self):
        """Both zero usage and zero requested should return zero ratio."""
        pod = make_pod(cpu_requested=0, cpu_core_hours=0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.cpu_ratio == 0.0

    def test_exactly_50_percent_is_not_overprovisioned(self):
        """Exactly 50% utilization should NOT be considered overprovisioned (threshold is < 50%)."""
        pod = make_pod(cpu_core_hours=4.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert not ratio.is_overprovisioned_cpu  # 4.0/8.0 = 0.5, not < 0.5 threshold

    def test_49_percent_is_overprovisioned(self):
        """49% utilization should be considered overprovisioned (below 50% threshold)."""
        pod = make_pod(cpu_core_hours=3.92, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert ratio.is_overprovisioned_cpu  # 3.92/8.0 = 0.49 < 0.5 threshold

    def test_exactly_100_percent_is_neither(self):
        """Exactly 100% utilization should be neither over nor under provisioned."""
        pod = make_pod(cpu_core_hours=8.0, cpu_requested=8.0)
        ratio = calculate_overprovisioning_ratio(pod)
        assert not ratio.is_overprovisioned_cpu
        assert not ratio.is_underprovisioned_cpu


# =============================================================================
# Test calculate_namespace_waste — normal cases
# =============================================================================


class TestNamespaceWasteNormal:
    """Normal cases for namespace waste calculation."""

    def test_waste_is_gap_between_requested_and_actual(self):
        """Waste should be the gap between requested and actual cost."""
        pod = make_pod(cpu_core_hours=4.0, cpu_requested=8.0)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.wasted_cost_usd > 0
        assert waste.waste_ratio > 0
        assert waste.waste_ratio < 1.0


# =============================================================================
# Test calculate_namespace_waste — edge cases
# =============================================================================


class TestNamespaceWasteEdgeCases:
    """Edge cases for namespace waste calculation."""

    def test_zero_request_no_waste(self):
        """Zero requested resources should return zero waste."""
        pod = make_pod(cpu_requested=0, cpu_core_hours=0, memory_requested=0, memory_bytes=0)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.wasted_cost_usd == 0.0

    def test_full_utilization_no_waste(self):
        """Full utilization should return zero waste."""
        pod = make_pod(cpu_core_hours=8.0, cpu_requested=8.0, memory_bytes=16 * BYTES_PER_GB, memory_requested=16 * BYTES_PER_GB)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.wasted_cost_usd == 0.0
        assert waste.waste_ratio == 0.0

    def test_100_percent_waste_when_zero_usage(self):
        """Zero usage with non-zero requests should return 100% waste."""
        pod = make_pod(cpu_core_hours=0, cpu_requested=8.0, memory_bytes=0, memory_requested=16 * BYTES_PER_GB)
        waste = calculate_namespace_waste(
            pod, COST_PER_CORE_HOUR, COST_PER_GB_HOUR, COST_PER_GB_MONTH
        )
        assert waste.waste_ratio == pytest.approx(1.0, rel=1e-4)