"""
Unit tests for greenit_engine — pure functions, no mocks needed.

Per AGENTS.md: domain engine tests use plain fixtures, no mocks (pure functions).
"""

from datetime import datetime, timezone

import pytest

from app.domain.greenit_engine import (
    build_carbon_footprint,
    build_energy_breakdown,
    calculate_carbon_footprint,
    calculate_carbon_overhead_from_overprovisioning,
    calculate_carbon_intensity,
    calculate_facility_energy,
    joules_to_kwh,
    kwh_to_joules,
)
from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown

NOW = datetime.now(timezone.utc)


class TestJoulesToKwh:
    def test_basic_conversion(self):
        assert joules_to_kwh(3_600_000) == pytest.approx(1.0)

    def test_half_kwh(self):
        assert joules_to_kwh(1_800_000) == pytest.approx(0.5)

    def test_zero(self):
        assert joules_to_kwh(0) == 0.0

    def test_large_value(self):
        assert joules_to_kwh(18_000_000) == pytest.approx(5.0)


class TestKwhToJoules:
    def test_roundtrip(self):
        for value in [0, 1.5, 100, 1_000_000]:
            assert kwh_to_joules(joules_to_kwh(value)) == pytest.approx(value)


class TestFacilityEnergy:
    def test_pue_1_no_overhead(self):
        assert calculate_facility_energy(5.0, 1.0) == pytest.approx(5.0)

    def test_pue_14(self):
        assert calculate_facility_energy(5.0, 1.4) == pytest.approx(7.0)

    def test_pue_2_doubles(self):
        assert calculate_facility_energy(5.0, 2.0) == pytest.approx(10.0)


class TestCarbonFootprint:
    def test_basic_calculation(self):
        # 5 kWh × 1.4 PUE × (475 / 1000) = 3.325 kgCO2
        co2 = calculate_carbon_footprint(5.0, 1.4, 475.0)
        assert co2 == pytest.approx(3.325, rel=1e-4)

    def test_zero_energy(self):
        assert calculate_carbon_footprint(0.0, 1.4, 475.0) == 0.0

    def test_clean_grid(self):
        # France (50 gCO2/kWh)
        co2 = calculate_carbon_footprint(5.0, 1.0, 50.0)
        assert co2 == pytest.approx(0.25, rel=1e-4)

    def test_dirty_grid(self):
        # Poland (700 gCO2/kWh)
        co2 = calculate_carbon_footprint(5.0, 1.0, 700.0)
        assert co2 == pytest.approx(3.5, rel=1e-4)


class TestCarbonIntensity:
    def test_basic(self):
        assert calculate_carbon_intensity(3.325, 5.0) == pytest.approx(0.665, rel=1e-4)

    def test_zero_energy(self):
        assert calculate_carbon_intensity(0.0, 0.0) == 0.0


class TestBuildCarbonFootprint:
    def test_produces_domain_object(self):
        fp = build_carbon_footprint(
            namespace="team-alpha",
            team="platform",
            cost_center="engineering",
            environment="prod",
            energy_joules=18_000_000,
            pue=1.4,
            grid_intensity_gco2_per_kwh=475.0,
            timestamp=NOW,
        )
        assert isinstance(fp, CarbonFootprint)
        assert fp.namespace == "team-alpha"
        assert fp.energy_kwh == pytest.approx(5.0, rel=1e-4)
        assert fp.co2_kg == pytest.approx(3.325, rel=1e-4)
        assert fp.timestamp == NOW

    def test_co2_grams_property(self):
        fp = build_carbon_footprint(
            "ns", "t", "cc", "prod",
            energy_joules=3_600_000,  # 1 kWh
            pue=1.0,
            grid_intensity_gco2_per_kwh=475.0,
            timestamp=NOW,
        )
        assert fp.co2_grams == pytest.approx(475.0, rel=1e-4)


class TestBuildEnergyBreakdown:
    def test_produces_domain_object(self):
        eb = build_energy_breakdown("team-alpha", 10.0, 4.0, timestamp=NOW)
        assert isinstance(eb, EnergyBreakdown)
        assert eb.total_energy_kwh == 10.0
        assert eb.idle_waste_kwh == 4.0
        assert eb.active_energy_kwh == pytest.approx(6.0)
        assert eb.waste_pct == pytest.approx(40.0)

    def test_zero_idle(self):
        eb = build_energy_breakdown("ns", 10.0, 0.0)
        assert eb.active_energy_kwh == pytest.approx(10.0)
        assert eb.waste_pct == 0.0


class TestCarbonOverhead:
    def test_overprovisioned_wastes_carbon(self):
        co2 = calculate_carbon_overhead_from_overprovisioning(
            requested_kwh=10.0,
            actual_kwh=5.0,
            pue=1.4,
            grid_intensity_gco2_per_kwh=475.0,
        )
        # 5 kWh wasted × 1.4 PUE × (475/1000) = 3.325 kgCO2
        assert co2 == pytest.approx(3.325, rel=1e-4)

    def test_no_overprovisioning(self):
        co2 = calculate_carbon_overhead_from_overprovisioning(
            requested_kwh=5.0,
            actual_kwh=5.0,
            pue=1.4,
            grid_intensity_gco2_per_kwh=475.0,
        )
        assert co2 == 0.0