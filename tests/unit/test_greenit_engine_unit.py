"""
Unit tests for greenit_engine — pure functions, no mocks needed.

WHY zero mocks:
- greenit_engine functions are pure physics formulas: joules → kWh → kgCO2
- No I/O, no network, no database — just arithmetic on domain objects
- Fixtures are hardcoded CarbonFootprint/EnergyBreakdown dataclasses
- Tests run in milliseconds (no external dependencies)

WHAT "test the domain in isolation" buys us:
- Tests verify BUSINESS LOGIC, not infrastructure
- Refactoring adapters (Prometheus, TimescaleDB) doesn't break these tests
- Running tests offline (no Kepler) is possible
- Catching physics/calculation bugs early (before they reach production)
"""

from datetime import datetime, timezone

import pytest

from app.domain.greenit_engine import (
    build_carbon_footprint,
    build_energy_breakdown,
    calculate_carbon_footprint,
    calculate_carbon_intensity,
    calculate_facility_energy,
    joules_to_kwh,
    kwh_to_joules,
)
from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown

NOW = datetime.now(timezone.utc)


# =============================================================================
# Test joules_to_kwh — normal cases
# =============================================================================


class TestJoulesToKwhNormal:
    """Normal cases for joules to kWh conversion."""

    def test_basic_conversion(self):
        """3,600,000 joules should equal 1.0 kWh."""
        assert joules_to_kwh(3_600_000) == pytest.approx(1.0)

    def test_half_kwh(self):
        """1,800,000 joules should equal 0.5 kWh."""
        assert joules_to_kwh(1_800_000) == pytest.approx(0.5)

    def test_large_value(self):
        """18,000,000 joules should equal 5.0 kWh."""
        assert joules_to_kwh(18_000_000) == pytest.approx(5.0)


# =============================================================================
# Test joules_to_kwh — edge cases
# =============================================================================


class TestJoulesToKwhEdgeCases:
    """Edge cases for joules to kWh conversion."""

    def test_zero_joules(self):
        """Zero joules should equal zero kWh."""
        assert joules_to_kwh(0) == 0.0

    def test_negative_joules(self):
        """Negative joules should return negative kWh (physically invalid but mathematically correct)."""
        assert joules_to_kwh(-3_600_000) == pytest.approx(-1.0)


# =============================================================================
# Test kwh_to_joules — normal cases
# =============================================================================


class TestKwhToJoulesNormal:
    """Normal cases for kWh to joules conversion."""

    def test_roundtrip(self):
        """Converting joules → kWh → joules should return original value."""
        for value in [0, 1.5, 100, 1_000_000]:
            assert kwh_to_joules(joules_to_kwh(value)) == pytest.approx(value)


# =============================================================================
# Test calculate_facility_energy — normal cases
# =============================================================================


class TestFacilityEnergyNormal:
    """Normal cases for facility energy calculation."""

    def test_pue_1_no_overhead(self):
        """PUE of 1.0 means no overhead (perfect efficiency)."""
        assert calculate_facility_energy(5.0, 1.0) == pytest.approx(5.0)

    def test_pue_14(self):
        """PUE of 1.4 should add 40% overhead."""
        assert calculate_facility_energy(5.0, 1.4) == pytest.approx(7.0)

    def test_pue_2_doubles(self):
        """PUE of 2.0 should double the energy."""
        assert calculate_facility_energy(5.0, 2.0) == pytest.approx(10.0)


# =============================================================================
# Test calculate_facility_energy — edge cases
# =============================================================================


class TestFacilityEnergyEdgeCases:
    """Edge cases for facility energy calculation."""

    def test_zero_energy(self):
        """Zero IT energy should result in zero facility energy."""
        assert calculate_facility_energy(0.0, 1.4) == 0.0

    def test_very_small_pue(self):
        """Very small PUE (physically impossible but mathematically valid)."""
        assert calculate_facility_energy(5.0, 0.1) == pytest.approx(0.5)


# =============================================================================
# Test calculate_carbon_footprint — normal cases
# =============================================================================


class TestCarbonFootprintNormal:
    """Normal cases for carbon footprint calculation."""

    def test_basic_calculation(self):
        """5 kWh × 1.4 PUE × 475 gCO2/kWh should equal 3.325 kgCO2."""
        co2 = calculate_carbon_footprint(5.0, 1.4, 475.0)
        assert co2 == pytest.approx(3.325, rel=1e-4)

    def test_clean_grid(self):
        """France's grid (50 gCO2/kWh) should produce low emissions."""
        co2 = calculate_carbon_footprint(5.0, 1.0, 50.0)
        assert co2 == pytest.approx(0.25, rel=1e-4)

    def test_dirty_grid(self):
        """Poland's grid (700 gCO2/kWh) should produce high emissions."""
        co2 = calculate_carbon_footprint(5.0, 1.0, 700.0)
        assert co2 == pytest.approx(3.5, rel=1e-4)


# =============================================================================
# Test calculate_carbon_footprint — edge cases
# =============================================================================


class TestCarbonFootprintEdgeCases:
    """Edge cases for carbon footprint calculation."""

    def test_zero_energy(self):
        """Zero energy should result in zero CO2."""
        assert calculate_carbon_footprint(0.0, 1.4, 475.0) == 0.0

    def test_zero_grid_intensity(self):
        """Zero grid intensity should result in zero CO2 (renewable energy)."""
        assert calculate_carbon_footprint(5.0, 1.4, 0.0) == 0.0

    def test_negative_energy(self):
        """Negative energy should return negative CO2 (physically invalid but mathematically correct)."""
        assert calculate_carbon_footprint(-5.0, 1.4, 475.0) < 0


# =============================================================================
# Test calculate_carbon_intensity — normal cases
# =============================================================================


class TestCarbonIntensityNormal:
    """Normal cases for carbon intensity calculation."""

    def test_basic(self):
        """3.325 kgCO2 / 5.0 kWh should equal 0.665 kgCO2/kWh."""
        assert calculate_carbon_intensity(3.325, 5.0) == pytest.approx(0.665, rel=1e-4)


# =============================================================================
# Test calculate_carbon_intensity — edge cases
# =============================================================================


class TestCarbonIntensityEdgeCases:
    """Edge cases for carbon intensity calculation."""

    def test_zero_energy(self):
        """Zero energy should return zero intensity (avoid division by zero)."""
        assert calculate_carbon_intensity(0.0, 0.0) == 0.0

    def test_zero_co2(self):
        """Zero CO2 should return zero intensity."""
        assert calculate_carbon_intensity(0.0, 5.0) == 0.0


# =============================================================================
# Test build_carbon_footprint — normal cases
# =============================================================================


class TestBuildCarbonFootprintNormal:
    """Normal cases for building CarbonFootprint domain object."""

    def test_produces_domain_object(self):
        """Should produce a valid CarbonFootprint dataclass."""
        fp = build_carbon_footprint(
            namespace="team-alpha",
            team="platform",
            cost_center="engineering",
            environment="prod",
            energy_joules=18_000_000,  # 5 kWh
            pue=1.4,
            grid_intensity_gco2_per_kwh=475.0,
            timestamp=NOW,
        )
        assert isinstance(fp, CarbonFootprint)
        assert fp.namespace == "team-alpha"
        assert fp.energy_kwh == pytest.approx(5.0, rel=1e-4)
        assert fp.co2_kg == pytest.approx(3.325, rel=1e-4)


# =============================================================================
# Test build_carbon_footprint — edge cases
# =============================================================================


class TestBuildCarbonFootprintEdgeCases:
    """Edge cases for building CarbonFootprint domain object."""

    def test_zero_energy(self):
        """Zero energy should produce a valid object with zero values."""
        fp = build_carbon_footprint(
            namespace="team-alpha",
            team="platform",
            cost_center="engineering",
            environment="prod",
            energy_joules=0,
            pue=1.4,
            grid_intensity_gco2_per_kwh=475.0,
            timestamp=NOW,
        )
        assert fp.energy_kwh == 0.0
        assert fp.co2_kg == 0.0


# =============================================================================
# Test build_energy_breakdown — normal cases
# =============================================================================


class TestBuildEnergyBreakdownNormal:
    """Normal cases for building EnergyBreakdown domain object."""

    def test_produces_domain_object(self):
        """Should produce a valid EnergyBreakdown dataclass."""
        eb = build_energy_breakdown(
            namespace="team-alpha",
            total_energy_kwh=10.0,
            idle_energy_kwh=4.0,
            timestamp=NOW,
        )
        assert isinstance(eb, EnergyBreakdown)
        assert eb.namespace == "team-alpha"
        assert eb.total_energy_kwh == 10.0
        assert eb.idle_waste_kwh == 4.0
        assert eb.active_energy_kwh == 6.0
        assert eb.waste_pct == pytest.approx(40.0, rel=1e-4)


# =============================================================================
# Test build_energy_breakdown — edge cases
# =============================================================================


class TestBuildEnergyBreakdownEdgeCases:
    """Edge cases for building EnergyBreakdown domain object."""

    def test_zero_idle_waste(self):
        """Zero idle waste should produce valid object with 0% waste."""
        eb = build_energy_breakdown(
            namespace="team-alpha",
            total_energy_kwh=10.0,
            idle_energy_kwh=0.0,
            timestamp=NOW,
        )
        assert eb.waste_pct == 0.0
        assert eb.active_energy_kwh == 10.0

    def test_zero_total_energy(self):
        """Zero total energy should produce valid object with 0% waste (avoid division by zero)."""
        eb = build_energy_breakdown(
            namespace="team-alpha",
            total_energy_kwh=0.0,
            idle_energy_kwh=0.0,
            timestamp=NOW,
        )
        assert eb.waste_pct == 0.0
        assert eb.active_energy_kwh == 0.0

    def test_100_percent_idle(self):
        """100% idle waste should produce 100% waste ratio."""
        eb = build_energy_breakdown(
            namespace="team-alpha",
            total_energy_kwh=10.0,
            idle_energy_kwh=10.0,
            timestamp=NOW,
        )
        assert eb.waste_pct == pytest.approx(100.0, rel=1e-4)
        assert eb.active_energy_kwh == 0.0