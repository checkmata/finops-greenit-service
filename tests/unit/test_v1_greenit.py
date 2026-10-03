"""V1 Green IT: unknown idle energy, energy provenance, no invented CO2."""

import pytest

from app.domain.greenit_engine import build_energy_breakdown, carbon_footprint_from_measurement
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.energy import EnergyAttribution
from app.domain.models.recommendation import Category
from app.domain.recommendation_engine import generate_all_recommendations
from tests.helpers_v1 import NOW, all_healthy, energy, pod

P = dict(cost_per_core_hour=0.03, cost_per_gb_hour=0.004, cost_per_gb_month=0.10)
C = dict(team="t", cost_center="c", environment="e", pue=1.4, grid_intensity_gco2_per_kwh=475)


def cats(recs):
    return [r.category for r in recs]


class TestIdleEnergy:
    def test_unknown_idle_propagates_none(self):
        eb = build_energy_breakdown("ns", 10.0, None, NOW)
        assert eb.idle_waste_kwh is None and eb.waste_pct is None and eb.active_energy_kwh is None

    def test_no_energy_waste_recommendation_when_idle_unknown(self):
        m = energy(kwh=10.0, idle=None)
        cf = carbon_footprint_from_measurement(m, **C)
        eb = build_energy_breakdown("banking", 10.0, None, NOW)
        recs = generate_all_recommendations(pod(), cf, eb, all_healthy(), **P).recommendations
        assert Category.ENERGY_WASTE not in cats(recs)
        assert Category.CARBON_INTENSITY in cats(recs)

    def test_energy_waste_when_idle_measured(self):
        cf = carbon_footprint_from_measurement(energy(kwh=10.0, idle=6.0), **C)
        eb = build_energy_breakdown("banking", 10.0, 6.0, NOW)
        recs = generate_all_recommendations(pod(), cf, eb, all_healthy(), **P).recommendations
        ew = [r for r in recs if r.category is Category.ENERGY_WASTE]
        assert len(ew) == 1 and ew[0].estimated_savings_kg_co2 == pytest.approx(6.0 * 1.4 * 0.475)


class TestProvenance:
    def test_unavailable_energy_gives_no_footprint(self):
        m = energy(kwh=None, attribution=EnergyAttribution.UNAVAILABLE)
        assert carbon_footprint_from_measurement(m, **C) is None

    def test_missing_footprint_with_kepler_up_is_flagged_not_zero(self):
        recs = generate_all_recommendations(pod(), None, None, all_healthy(), **P).recommendations
        dq = [r for r in recs if r.category is Category.DATA_QUALITY]
        assert len(dq) == 1 and dq[0].resource == "energy_attribution"
        assert Category.CARBON_INTENSITY not in cats(recs)

    def test_node_share_estimate_is_flagged(self):
        cf = carbon_footprint_from_measurement(energy(kwh=4.0, attribution=EnergyAttribution.NODE_CPU_SHARE), **C)
        assert cf.energy_attribution is EnergyAttribution.NODE_CPU_SHARE
        recs = generate_all_recommendations(pod(), cf, None, all_healthy(), **P).recommendations
        carbon = [r for r in recs if r.category is Category.CARBON_INTENSITY][0]
        assert "estimated" in carbon.message
        assert carbon.estimation_method and "node-level" in carbon.estimation_method

    def test_measured_energy_not_flagged(self):
        cf = carbon_footprint_from_measurement(energy(kwh=4.0), **C)
        recs = generate_all_recommendations(pod(), cf, None, all_healthy(), **P).recommendations
        carbon = [r for r in recs if r.category is Category.CARBON_INTENSITY][0]
        assert carbon.estimation_method is None

    def test_kepler_down_still_reports(self):
        q = DataQuality(frozenset({TelemetrySource.KUBE_STATE_METRICS, TelemetrySource.NODE_EXPORTER}), NOW)
        recs = generate_all_recommendations(pod(), None, None, q, **P).recommendations
        assert any(r.category is Category.DATA_QUALITY and "Kepler is down" in r.message for r in recs)
        assert any(r.category is Category.COST_RIGHTSIZING for r in recs)

    def test_precision_labels(self):
        assert EnergyAttribution.POD.precision == "measured"
        assert EnergyAttribution.NODE_CPU_SHARE.precision == "estimated"
        assert EnergyAttribution.UNAVAILABLE.precision == "unavailable"


class TestPrecisionCombinesAttributionAndPowerSource:
    def test_container_attribution_from_model_is_estimated(self):
        from app.domain.models.energy import PowerSource
        m = energy(kwh=1.0)
        model = type(m)(**{**m.__dict__, "power_source": PowerSource.MODEL})
        hw = type(m)(**{**m.__dict__, "power_source": PowerSource.HARDWARE})
        assert model.precision == "estimated" and hw.precision == "measured"
        cf = carbon_footprint_from_measurement(model, **C)
        assert cf.is_estimated
        recs = generate_all_recommendations(pod(), cf, None, all_healthy(), **P).recommendations
        carbon = [r for r in recs if r.category is Category.CARBON_INTENSITY][0]
        assert "Kepler power model" in carbon.message and "Kepler power model" in carbon.estimation_method
