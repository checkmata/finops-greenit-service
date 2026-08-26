"""
Domain model for Green IT carbon footprint calculations.

Why plain dataclass (not Pydantic):
- Same reason as PodMetric: domain purity. The domain never imports Pydantic.
- These objects represent *calculated outputs* from the green IT engine.
- Immutability ensures audit trail integrity — carbon footprint is a snapshot.

PUE and grid carbon intensity are NOT fields here — they are inputs from
config.py, passed into the engine as parameters. The engine multiplies
raw energy (kWh from Kepler) × PUE × grid_intensity to produce co2_kg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CarbonFootprint:
    """
    Green IT carbon footprint for a namespace (or workload).

    Field mapping (from docs/metrics-reference.md):
    - energy_kwh ← kepler_node_cpu_joules_total (increase[24h], converted to kWh)
                ← or kepler_container_cpu_joules_total (per-container)
    - co2_kg    = energy_kwh × PUE × (grid_intensity / 1000)

    WHY energy_kwh is the raw Kepler value:
    - Kepler reports joules. Conversion: joules / 3,600,000 = kWh.
    - PUE and grid_intensity are applied by greenit_engine.py, not stored here,
      because they are environment-specific constants, not per-pod measurements.

    WHY co2_kg is computed externally, not in the dataclass:
    - Frozen dataclasses have no __post_init__ side effects.
    - Computation belongs in the engine (business logic), not the model.
    - Enables unit testing the engine with mock inputs independently.
    """

    # --- identity ---
    namespace: str
    team: str
    cost_center: str
    environment: str

    # --- raw energy (Kepler P0) ---
    energy_kwh: float

    # --- calculated carbon (engine output) ---
    co2_kg: float

    # --- metadata ---
    timestamp: datetime

    @property
    def co2_grams(self) -> float:
        """Convenience: CO2 in grams for human-readable reports."""
        return self.co2_kg * 1000.0

    @property
    def cost_estimate_usd(self) -> float:
        """
        Placeholder for carbon-cost correlation (Phase 2).

        WHY: Some orgs assign a dollar value to carbon (carbon tax, credits).
        This field is reserved for future use; currently always 0.0.
        """
        return 0.0


@dataclass(frozen=True)
class EnergyBreakdown:
    """
    Energy consumption breakdown by component (FinOps + Green IT correlation).

    WHY: Helps attribute energy waste to specific cost drivers.
    - idle_waste_kwh: energy consumed by idle CPUs (P1 metric)
    - active_energy_kwh: energy consumed by actual workload

    Field mapping (Green IT P1):
    - idle_waste_kwh ← node_cpu_seconds_total{idle} × Kepler power
    """

    namespace: str
    total_energy_kwh: float
    idle_waste_kwh: float
    timestamp: datetime

    @property
    def active_energy_kwh(self) -> float:
        """Energy consumed by actual workload (total minus idle waste)."""
        return max(0.0, self.total_energy_kwh - self.idle_waste_kwh)

    @property
    def waste_pct(self) -> float:
        """Percentage of energy wasted on idle resources."""
        if self.total_energy_kwh == 0:
            return 0.0
        return (self.idle_waste_kwh / self.total_energy_kwh) * 100.0