"""
Domain model for an energy measurement WITH its provenance.

WHY provenance matters (V1 fix):
- The MVP fell back to cluster-wide node energy and attributed ALL of it to the
  requested namespace. That silently over-estimated CO2 for every namespace.
- A number without its source cannot be defended. Every energy value now carries
  HOW it was obtained, so recommendations can say "estimated" when they must.

Attribution hierarchy (most precise first):
    CONTAINER       Kepler per-container series, summed per namespace (measured)
    POD             Kepler per-pod series, summed per namespace (measured)
    NODE_CPU_SHARE  Kepler node energy × (namespace CPU usage / cluster CPU usage)
                    → an ESTIMATE, lower precision, flagged downstream
    UNAVAILABLE     nothing trustworthy → no value (never 0.0)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EnergyAttribution(str, Enum):
    CONTAINER = "container"
    POD = "pod"
    NODE_CPU_SHARE = "node_cpu_share"
    UNAVAILABLE = "unavailable"

    @property
    def is_measured(self) -> bool:
        """True when the value comes from a per-workload Kepler series."""
        return self in (EnergyAttribution.CONTAINER, EnergyAttribution.POD)

    @property
    def precision(self) -> str:
        """Human label used in API responses and recommendation messages."""
        if self.is_measured:
            return "measured"
        if self is EnergyAttribution.NODE_CPU_SHARE:
            return "estimated"
        return "unavailable"


class PowerSource(str, Enum):
    """
    How the power figure itself was obtained (independent of attribution).

    Read from Kepler's own `kepler_node_info{components_power_source=...}`:
    - HARDWARE: RAPL / ACPI power meter counters
    - MODEL:    Kepler's trained power model ("estimator") — e.g. in VMs without
                RAPL. Real activity counters (eBPF CPU time) in, estimated watts out.
    - UNKNOWN:  the source did not say
    Observed in V2 on the real minikube/KVM clusters: MODEL (no RAPL in guests).
    """

    HARDWARE = "hardware"
    MODEL = "model"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkloadEnergy:
    """Energy of one workload (and its pods) over a window — V2 CO2 per workload."""

    namespace: str
    workload: str
    kwh: float | None
    power_source: PowerSource
    scope: str
    pods: tuple[tuple[str, float], ...] = ()   # (pod name, kWh)

    @property
    def precision(self) -> str:
        if self.kwh is None:
            return "unavailable"
        return "measured" if self.power_source is PowerSource.HARDWARE else "estimated"


@dataclass(frozen=True)
class EnergyMeasurement:
    """
    Energy of one namespace over a window, as returned by MetricsPort.get_energy().

    - total_kwh: None when attribution is UNAVAILABLE (never a fake 0.0).
    - idle_kwh:  None when the Kepler version/environment does not expose an
                 idle split for this scope. The engine then refuses to emit an
                 ENERGY_WASTE recommendation.
    - source_metric: the exact Prometheus metric used (auditability).
    """

    namespace: str
    total_kwh: float | None
    idle_kwh: float | None
    attribution: EnergyAttribution
    source_metric: str
    window_hours: int = 24
    power_source: PowerSource = PowerSource.UNKNOWN
    # What the figure covers. Observed on real Kepler 0.8 with RAPL (kind, V2):
    # kepler_container_joules_total = package + dram + "other", where "other"
    # derives from the PLATFORM power, which Kepler still estimates (no ACPI
    # power meter). A measured figure therefore only covers "cpu_package+dram";
    # "platform" means the full Kepler total (partly or fully modelled).
    scope: str = "unknown"

    @property
    def precision(self) -> str:
        """measured | estimated | unavailable — combines attribution AND power source."""
        if not self.is_available:
            return "unavailable"
        if self.attribution.is_measured and self.power_source is not PowerSource.MODEL:
            return "measured"
        return "estimated"

    @property
    def is_available(self) -> bool:
        return self.total_kwh is not None and self.attribution is not EnergyAttribution.UNAVAILABLE
