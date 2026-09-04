"""
Green IT engine — pure calculation logic for energy → carbon conversion.

WHY this file cannot import from adapters/ or ports/:
- Same reason as finops_engine.py: the domain must be testable in isolation.
- If it imported PrometheusAdapter, you'd need a running Prometheus to test
  a simple joules→kWh→kgCO2 conversion. That's absurd.
- By importing ONLY domain models, every function here is a pure math formula.

WHY PUE and grid_intensity are function arguments (not imported from config):
- The domain engine must not import app.config (breaks purity).
- The adapter/layer that calls these functions injects the config values:
    greenit_engine.build_carbon_footprint(..., pue=settings.PUE_CONSTANT, ...)
- This enables testing with any PUE/grid values without touching config.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown

# ---------------------------------------------------------------------------
# Constants (physics, not configuration)
# ---------------------------------------------------------------------------

# 1 kWh = 3,600,000 joules (1 kilowatt × 3600 seconds).
JOULES_PER_KWH = 3_600_000

# Grid carbon intensity is in gCO2/kWh. Convert to kgCO2 by dividing by 1000.
GRAMS_PER_KG = 1_000


# ---------------------------------------------------------------------------
# Core conversion functions — pure math, no I/O
# ---------------------------------------------------------------------------

def joules_to_kwh(joules: float) -> float:
    """
    Convert energy from joules to kilowatt-hours.

    Physics:
        1 joule  = 1 watt-second
        1 kWh    = 1000 watts × 3600 seconds = 3,600,000 joules

    Therefore:
        kWh = joules / 3,600,000

    Example:
        18,000,000 joules / 3,600,000 = 5.0 kWh
    """
    return joules / JOULES_PER_KWH


def kwh_to_joules(kwh: float) -> float:
    """
    Convert energy from kilowatt-hours to joules.

    Inverse of joules_to_kwh: joules = kWh × 3,600,000
    """
    return kwh * JOULES_PER_KWH


def calculate_facility_energy(it_energy_kwh: float, pue: float) -> float:
    """
    Calculate total facility energy from IT equipment energy.

    WHY PUE multiplies IT energy:
    - PUE (Power Usage Effectiveness) = Total Facility Power / IT Equipment Power
    - IT equipment (servers, switches) is NOT the only thing consuming power.
    - The facility also consumes power for:
      • Cooling (CRAC/CRAH units, chillers, cooling towers)
      • Lighting
      • Power distribution (UPS losses, PDU losses, voltage conversion)
    - If PUE = 1.4, then for every 1 kWh of IT power, the facility uses 1.4 kWh total.
    - The extra 0.4 kWh is overhead (cooling, lighting, losses).

    Formula:
        facility_energy = it_energy × PUE

    Example:
        IT energy = 5 kWh, PUE = 1.4
        facility_energy = 5 × 1.4 = 7.0 kWh
    """
    return it_energy_kwh * pue


def calculate_carbon_footprint(
    energy_kwh: float,
    pue: float,
    grid_intensity_gco2_per_kwh: float,
) -> float:
    """
    Convert energy consumption to carbon emissions (kgCO2e).

    Formula:
        co2_kg = energy_kwh × PUE × (grid_intensity / 1000)

    Breaking it down:
        1. energy_kwh × PUE = total facility energy (includes cooling overhead)
        2. grid_intensity_gco2_per_kwh = grams of CO2 per kWh (location-specific)
        3. Divide by 1000 to convert grams → kilograms

    Physics:
        - Different electricity sources produce different CO2 per kWh:
          • France (nuclear): ~50 gCO2/kWh
          • Germany (mixed):  ~380 gCO2/kWh
          • Poland (coal):    ~700 gCO2/kWh
        - The same workload in France produces 14× less CO2 than in Poland.

    Example:
        energy = 5 kWh, PUE = 1.4, grid = 475 gCO2/kWh
        co2_kg = 5 × 1.4 × (475 / 1000) = 3.325 kgCO2
    """
    facility_energy = calculate_facility_energy(energy_kwh, pue)
    return facility_energy * (grid_intensity_gco2_per_kwh / GRAMS_PER_KG)


def calculate_carbon_intensity(
    co2_kg: float,
    energy_kwh: float,
) -> float:
    """
    Calculate carbon intensity of a workload (kgCO2 per kWh).

    Formula:
        intensity = co2_kg / energy_kwh

    WHY: Allows comparison across workloads regardless of size.
    A workload using 100 kWh with intensity 0.5 kgCO2/kWh is "cleaner"
    than one using 10 kWh with intensity 2.0 kgCO2/kWh.
    """
    if energy_kwh == 0:
        return 0.0
    return co2_kg / energy_kwh


# ---------------------------------------------------------------------------
# Domain object builders — pure functions that produce CarbonFootprint/EnergyBreakdown
# ---------------------------------------------------------------------------

def build_carbon_footprint(
    namespace: str,
    team: str,
    cost_center: str,
    environment: str,
    energy_joules: float,
    pue: float,
    grid_intensity_gco2_per_kwh: float,
    timestamp: datetime | None = None,
) -> CarbonFootprint:
    """
    Build a CarbonFootprint domain object from raw Kepler joule readings.

    Pipeline:
        1. joules → kWh (joules_to_kwh)
        2. kWh → kgCO2 (calculate_carbon_footprint)
        3. Assemble CarbonFootprint dataclass

    The caller (adapter) provides raw joules from Kepler.
    This function does the conversion and attribution.
    """
    energy_kwh = joules_to_kwh(energy_joules)
    co2_kg = calculate_carbon_footprint(energy_kwh, pue, grid_intensity_gco2_per_kwh)

    return CarbonFootprint(
        namespace=namespace,
        team=team,
        cost_center=cost_center,
        environment=environment,
        energy_kwh=round(energy_kwh, 6),
        co2_kg=round(co2_kg, 6),
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def build_energy_breakdown(
    namespace: str,
    total_energy_kwh: float,
    idle_energy_kwh: float,
    timestamp: datetime | None = None,
) -> EnergyBreakdown:
    """
    Build an EnergyBreakdown domain object.

    WHY idle waste matters:
    - Idle CPUs still consume power (leakage current, background processes).
    - If a node is 40% idle, that 40% of energy is pure waste.
    - The engine attributes this waste to the namespace for chargeback.

    idle_energy_kwh comes from:
        node_cpu_seconds_total{mode="idle"} × Kepler power model

    This is a P1 metric (deferred to Phase 2 in metrics-reference.md).
    For MVP, this function exists and is ready — but the adapter won't
    query the idle metric until P1 is explicitly requested.
    """
    return EnergyBreakdown(
        namespace=namespace,
        total_energy_kwh=round(total_energy_kwh, 6),
        idle_waste_kwh=round(idle_energy_kwh, 6),
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def calculate_carbon_overhead_from_overprovisioning(
    requested_kwh: float,
    actual_kwh: float,
    pue: float,
    grid_intensity_gco2_per_kwh: float,
) -> float:
    """
    Calculate CO2 wasted due to over-provisioning.

    Formula:
        wasted_kwh = requested_kwh - actual_kwh
        wasted_co2 = wasted_kwh × PUE × (grid_intensity / 1000)

    WHY: Over-provisioning doesn't just waste cost — it wastes energy.
    If you request 8 cores but use 4, the extra 4 cores still consume power
    (idle, but powered on). This function quantifies the carbon cost of waste.

    This reuses the FinOps oversizing calculation with a carbon multiplier
    (metrics-reference.md line 44: "reuses FinOps oversizing calc, different multiplier").
    """
    wasted_kwh = max(0.0, requested_kwh - actual_kwh)
    return calculate_carbon_footprint(wasted_kwh, pue, grid_intensity_gco2_per_kwh)