"""
Historical snapshot models (what the scheduler records every cycle).

WHY moved to the domain in V1 (was defined in ports/storage_port.py):
- History aggregation (cost/energy/CO2 over time, per cluster/namespace/workload)
  is now pure domain logic (app/domain/history.py) and needs these types.
- storage_port.py re-exports MetricSnapshot so existing imports keep working.

Two granularities:
- MetricSnapshot:   one row per (cluster, namespace) per cycle — cost AND energy/CO2.
- WorkloadSnapshot: one row per (cluster, namespace, workload) per cycle — cost only.
  Energy stays namespace-level in V1 because per-workload Kepler attribution
  depends on label names that vary across Kepler versions (see docs/GREENIT.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MetricSnapshot:
    """
    Aggregated metrics for one namespace of one cluster at one point in time.

    Units:
    - cpu_core_hours: average CPU cores used (historical name kept; see FINOPS.md)
    - memory_gb_hours: GB of working-set memory (historical name kept)
    - *_usd: MONTHLY cost estimates (quantity × hourly price × 720 h)
    - energy_kwh / co2e_kg: last 24 h, None when not measurable (V1)
    - waste_pct: COST waste ratio × 100 (V1; the MVP stored idle-energy % here,
      which was always 0)
    """

    time: datetime
    namespace: str
    team: str
    cost_center: str
    environment: str
    cpu_core_hours: float
    memory_gb_hours: float
    cost_usd: float  # actual (used) monthly cost — was always 0.0 in the MVP
    energy_kwh: float | None
    co2e_kg: float | None
    waste_pct: float

    # --- V1 additions (defaults keep MVP call sites valid) ---
    cluster: str = "default"
    requested_cost_usd: float = 0.0
    waste_cost_usd: float = 0.0
    potential_savings_usd: float = 0.0
    storage_cost_usd: float | None = None
    storage_status: str = "unavailable"
    idle_energy_kwh: float | None = None
    energy_attribution: str = "unavailable"
    # V2 provenance: hardware | model | unknown, and what the kWh covers
    energy_power_source: str = "unknown"
    energy_scope: str = "unknown"
    # V2: energy/CO2 over THIS cycle's interval (summable over any period)
    energy_interval_kwh: float | None = None
    co2_interval_kg: float | None = None
    interval_seconds: float | None = None


@dataclass(frozen=True)
class WorkloadSnapshot:
    """Cost view of one workload (e.g. a Deployment's `app` label) at one point in time."""

    time: datetime
    cluster: str
    namespace: str
    workload: str
    team: str
    cost_center: str
    environment: str
    cpu_cores_used: float
    cpu_cores_requested: float
    memory_gb_used: float
    memory_gb_requested: float
    requested_cost_usd: float
    actual_cost_usd: float
    waste_cost_usd: float
    potential_savings_usd: float
    # V2: energy/CO2 of the workload over the cycle interval (None = not attributable)
    energy_interval_kwh: float | None = None
    co2_interval_kg: float | None = None
    energy_precision: str = "unavailable"   # measured | estimated | unavailable
    energy_power_source: str = "unknown"
    # V3: point-in-time CPU (5-min rate). cpu_cores_used stays the long-window average (V1).
    cpu_cores_used_now: float | None = None


@dataclass(frozen=True)
class AggregateRow:
    """
    One bucket of long-term history for one series (cluster × namespace [× workload]).

    Costs are AVERAGES of the monthly-rate samples in the bucket; energy/CO2 are
    SUMS of per-cycle amounts (exact totals). None = never measured in the bucket.
    """

    bucket: datetime
    cluster: str
    namespace: str
    workload: str
    cost_usd: float | None
    requested_cost_usd: float | None
    waste_cost_usd: float | None
    savings_usd: float | None
    cpu_cores: float | None
    memory_gb: float | None
    storage_cost_usd: float | None
    energy_kwh: float | None
    co2_kg: float | None
    samples: int
