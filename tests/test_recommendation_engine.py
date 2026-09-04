"""
Unit tests for recommendation_engine — pure functions, no mocks needed.

Per AGENTS.md: domain engine tests use plain fixtures, no mocks (pure functions).
"""

from datetime import datetime, timezone

from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.pod_metric import PodMetric
from app.domain.models.recommendation import Category, Severity
from app.domain.recommendation_engine import (
    IDLE_WASTE_CRITICAL_PCT,
    IDLE_WASTE_WARNING_PCT,
    OVERPROVISIONED_CPU_THRESHOLD,
    WASTE_CRITICAL_THRESHOLD,
    generate_all_recommendations,
)

NOW = datetime.now(timezone.utc)

# Default cost model (from config.py defaults)
COST_PER_CORE_HOUR = 0.03
COST_PER_GB_HOUR = 0.004
COST_PER_GB_MONTH = 0.10
BYTES_PER_GB = 1024 ** 3


def make_pod(
    cpu_core_hours: float = 4.0,
    memory_bytes: float = 8 * BYTES_PER_GB,
    cpu_requested: float = 8.0,
    memory_requested: float = 16 * BYTES_PER_GB,
) -> PodMetric:
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
    return EnergyBreakdown(
        namespace="team-alpha",
        total_energy_kwh=total_energy_kwh,
        idle_waste_kwh=idle_waste_kwh,
        timestamp=NOW,
    )


def make_data_quality(healthy: set[TelemetrySource] | None = None) -> DataQuality:
    if healthy is None:
        healthy = {TelemetrySource.KUBE_STATE_METRICS, TelemetrySource.NODE_EXPORTER, TelemetrySource.KEPLER}
    return DataQuality(
        healthy_sources=frozenset(healthy),
        timestamp=NOW,
    )


class TestGenerateAllRecommendations:
    def test_overprovisioned_cpu_generates_rightsize(self):
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
        assert any("throttling" in r.message.lower() for r in critical)

    def test_idle_waste_generates_energy_recommendation(self):
        # 60% idle → CRITICAL
        result = generate_all_recommendations(
            pod=make_pod(),
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(total_energy_kwh=10.0, idle_waste_kwh=6.0),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        energy_recs = [r for r in result.recommendations if r.category == Category.ENERGY_WASTE]
        assert len(energy_recs) == 1
        assert energy_recs[0].severity == Severity.CRITICAL

    def test_idle_waste_warning_threshold(self):
        # 35% idle → WARNING
        result = generate_all_recommendations(
            pod=make_pod(),
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(total_energy_kwh=10.0, idle_waste_kwh=3.5),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        energy_recs = [r for r in result.recommendations if r.category == Category.ENERGY_WASTE]
        assert len(energy_recs) == 1
        assert energy_recs[0].severity == Severity.WARNING

    def test_carbon_footprint_always_informational(self):
        result = generate_all_recommendations(
            pod=make_pod(),
            carbon_footprint=make_carbon_footprint(co2_kg=3.325),
            energy_breakdown=make_energy_breakdown(),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        carbon_recs = [r for r in result.recommendations if r.category == Category.CARBON_INTENSITY]
        assert len(carbon_recs) == 1
        assert carbon_recs[0].severity == Severity.INFO

    def test_kepler_down_skips_carbon_adds_data_quality(self):
        quality = make_data_quality(healthy={TelemetrySource.KUBE_STATE_METRICS, TelemetrySource.NODE_EXPORTER})
        result = generate_all_recommendations(
            pod=make_pod(),
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=quality,
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        dq_recs = [r for r in result.recommendations if r.category == Category.DATA_QUALITY]
        assert len(dq_recs) >= 1
        assert any("kepler" in r.message.lower() for r in dq_recs)
        # No carbon recommendations when Kepler is down.
        carbon_recs = [r for r in result.recommendations if r.category == Category.CARBON_INTENSITY]
        assert len(carbon_recs) == 0

    def test_ksm_down_skips_finops_adds_data_quality(self):
        quality = make_data_quality(healthy={TelemetrySource.KEPLER})
        result = generate_all_recommendations(
            pod=make_pod(),
            carbon_footprint=make_carbon_footprint(),
            energy_breakdown=make_energy_breakdown(),
            data_quality=quality,
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        dq_recs = [r for r in result.recommendations if r.category == Category.DATA_QUALITY]
        assert any("kube-state-metrics" in r.message.lower() for r in dq_recs)

    def test_well_utilized_no_recommendations(self):
        pod = make_pod(cpu_core_hours=7.5, cpu_requested=8.0, memory_bytes=15 * BYTES_PER_GB, memory_requested=16 * BYTES_PER_GB)
        result = generate_all_recommendations(
            pod=pod,
            carbon_footprint=make_carbon_footprint(co2_kg=0.0),
            energy_breakdown=make_energy_breakdown(total_energy_kwh=10.0, idle_waste_kwh=1.0),
            data_quality=make_data_quality(),
            cost_per_core_hour=COST_PER_CORE_HOUR,
            cost_per_gb_hour=COST_PER_GB_HOUR,
            cost_per_gb_month=COST_PER_GB_MONTH,
        )
        # Only informational carbon rec (if co2 > 0) or nothing
        assert result.total <= 1


class TestThresholds:
    def test_overprovisioned_threshold(self):
        assert OVERPROVISIONED_CPU_THRESHOLD == 0.5

    def test_waste_critical_threshold(self):
        assert WASTE_CRITICAL_THRESHOLD == 0.7

    def test_idle_waste_thresholds(self):
        assert IDLE_WASTE_WARNING_PCT == 30.0
        assert IDLE_WASTE_CRITICAL_PCT == 50.0