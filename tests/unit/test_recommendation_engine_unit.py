"""
Unit tests for recommendation_engine — pure functions, no mocks needed.

WHY zero mocks:
- recommendation_engine functions are pure composition: PodMetric + CarbonFootprint → Recommendations
- No I/O, no network, no database — just business logic on domain objects
- Fixtures are hardcoded PodMetric/CarbonFootprint/EnergyBreakdown dataclasses
- Tests run in milliseconds (no external dependencies)

WHAT "test the domain in isolation" buys us:
- Tests verify BUSINESS LOGIC, not infrastructure
- Refactoring adapters (Prometheus, TimescaleDB) doesn't break these tests
- Running tests offline (no Prometheus, no Kepler) is possible
- Catching threshold/heuristic bugs early (before they reach production)
"""

from datetime import datetime, timezone

from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.pod_metric import PodMetric
from app.domain.models.recommendation import Category, Severity
from app.domain.recommendation_engine import generate_all_recommendations

NOW = datetime.now(timezone.utc)

# Default cost model (from config.py defaults)
COST_PER_CORE_HOUR = 0.03
COST_PER_GB_HOUR = 0.004
COST_PER_GB_MONTH = 0.10
BYTES_PER_GB = 1024**3


# =============================================================================
# Fixtures — hardcoded domain objects (no mocking)
# =============================================================================


def make_pod(
    cpu_core_hours: float = 4.0,
    memory_bytes: float = 8 * BYTES_PER_GB,
    cpu_requested: float = 8.0,
    memory_requested: float = 16 * BYTES_PER_GB,
) -> PodMetric:
    """Create a PodMetric with sensible defaults for testing."""
    return PodMetric(
        namespace="team-alpha",
        team="platform",
        cost_center="engineering",
        environment="prod",
        cpu_core_hours=cpu_core_hours,
        memory_bytes=memory_bytes,
        cpu_requested=cpu_requested,
        memory_requested=memory_requested,
        timestamp=NOW,
    )


def make_carbon_footprint(energy_kwh: float = 5.0, co2_kg: float = 3.325) -> CarbonFootprint:
    """Create a CarbonFootprint with sensible defaults for testing."""
    return CarbonFootprint(
        namespace="team-alpha",
        team="platform",
        cost_center="engineering",
        environment="prod",
        energy_kwh=energy_kwh,
        co2_kg=co2_kg,
        timestamp=NOW,
    )


def make_energy_breakdown(
    total_energy_kwh: float = 10.0,
    idle_waste_kwh: float = 4.0,
) -> EnergyBreakdown:
    """Create an EnergyBreakdown with sensible defaults for testing."""
    return EnergyBreakdown(
        namespace="team-alpha",
        total_energy_kwh=total_energy_kwh,
        idle_waste_kwh=idle_waste_kwh,
        timestamp=NOW,
    )


def make_data_quality(healthy: set[TelemetrySource] | None = None) -> DataQuality:
    """Create a DataQuality with sensible defaults for testing."""
    if healthy is None:
        healthy = {
            TelemetrySource.KUBE_STATE_METRICS,
            TelemetrySource.NODE_EXPORTER,
            TelemetrySource.KEPLER,
        }
    return DataQuality(
        healthy_sources=frozenset(healthy),
        timestamp=NOW,
    )


# =============================================================================
# Test generate_all_recommendations — normal cases
# =============================================================================


class TestGenerateAllRecommendationsNormal:
    """Normal cases for recommendation generation."""

    def test_overprovisioned_cpu_generates_rightsize(self):
        """Over-provisioned CPU should generate a right-size recommendation."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)  # ratio = 0.375 < 0.5
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        cpu_recs = [r for r in result.recommendations if r.resource == "cpu"]
        assert len(cpu_recs) >= 1
        assert any("CPU is" in r.message and "utilized" in r.message for r in cpu_recs)

    def test_overprovisioned_memory_generates_rightsize(self):
        """Over-provisioned memory should generate a right-size recommendation."""
        pod = make_pod(memory_bytes=4 * BYTES_PER_GB, memory_requested=16 * BYTES_PER_GB)  # ratio = 0.25
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        mem_recs = [r for r in result.recommendations if r.resource == "memory"]
        assert len(mem_recs) >= 1

    def test_underprovisioned_cpu_generates_critical(self):
        """Under-provisioned CPU should generate a critical recommendation."""
        pod = make_pod(cpu_core_hours=10.0, cpu_requested=8.0)  # ratio = 1.25 > 1.0
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        critical = [r for r in result.recommendations if r.severity == Severity.CRITICAL]
        assert len(critical) >= 1

    def test_carbon_footprint_always_informational(self):
        """Carbon footprint recommendation should always be INFO severity."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)  # overprovisioned
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        carbon_recs = [r for r in result.recommendations if r.category == Category.CARBON_INTENSITY]
        assert all(r.severity == Severity.INFO for r in carbon_recs)

    def test_well_utilized_no_finops_recommendations(self):
        """Well-utilized pod should generate no FinOps recommendations (carbon info is always generated)."""
        pod = make_pod(
            cpu_core_hours=7.5,
            cpu_requested=8.0,
            memory_bytes=15 * BYTES_PER_GB,
            memory_requested=16 * BYTES_PER_GB,
        )
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(total_energy_kwh=10.0, idle_waste_kwh=0.0),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        # Should have no FinOps recommendations (right-sizing, waste, etc.)
        finops_recs = [r for r in result.recommendations if r.category in (Category.COST_RIGHTSIZING, Category.COST_IDLE_RESOURCE)]
        assert len(finops_recs) == 0
        # Carbon footprint recommendation is always generated (INFO severity)


# =============================================================================
# Test generate_all_recommendations — edge cases
# =============================================================================


class TestGenerateAllRecommendationsEdgeCases:
    """Edge cases for recommendation generation."""

    def test_kepler_down_skips_carbon_adds_data_quality(self):
        """When Kepler is down, carbon recommendations should be skipped."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        quality = make_data_quality(healthy={TelemetrySource.KUBE_STATE_METRICS})
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=quality,
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        # Should have data quality recommendation but no carbon recommendations
        data_quality_recs = [r for r in result.recommendations if r.category == Category.DATA_QUALITY]
        carbon_recs = [r for r in result.recommendations if r.category == Category.CARBON_INTENSITY]
        assert len(data_quality_recs) >= 1
        assert len(carbon_recs) == 0

    def test_ksm_down_skips_finops_adds_data_quality(self):
        """When kube-state-metrics is down, FinOps recommendations should be skipped."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        quality = make_data_quality(healthy={TelemetrySource.KEPLER})
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=quality,
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        # Should have data quality recommendation but no FinOps recommendations
        data_quality_recs = [r for r in result.recommendations if r.category == Category.DATA_QUALITY]
        finops_recs = [r for r in result.recommendations if r.category in (Category.COST_RIGHTSIZING,)]
        assert len(data_quality_recs) >= 1
        assert len(finops_recs) == 0

    def test_zero_energy_generates_no_carbon(self):
        """Zero energy should not generate carbon recommendations."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(energy_kwh=0.0, co2_kg=0.0),
            energy_breakdown=make_energy_breakdown(total_energy_kwh=0.0, idle_waste_kwh=0.0),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        # Carbon footprint should still be generated (INFO), but no energy waste
        energy_recs = [r for r in result.recommendations if r.resource == "energy"]
        assert len(energy_recs) == 0

    def test_recommendations_have_correct_namespace(self):
        """All recommendations should have the correct namespace."""
        pod = make_pod(cpu_core_hours=3.0, cpu_requested=8.0)
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        for rec in result.recommendations:
            assert rec.namespace == "team-alpha"