"""
Domain model for telemetry data quality (per-source health tracking).

WHY this belongs in domain/models (not ports):
- Data quality is a domain concept — the engine decides what to show based on it.
- Ports import domain types, not the other way around.
- Clean layering: domain defines types, ports declare contracts using those types.

WHY per-source (not a single coarse status):
- A single "degraded" value hides WHICH source is down.
- If Kepler is down but kube-state-metrics is up, the engine should still show
  FinOps (cost) data while flagging Green-IT (carbon) data as unavailable.
- Representing health as "set of healthy sources" enables selective rendering:
  show what's healthy, flag what's missing.

Mapping from metrics-reference.md:
- up{job=~"kube-state-metrics|node-exporter|kepler"} == 0 → that source is DOWN
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TelemetrySource(str, Enum):
    """
    The telemetry sources this service depends on.

    WHY an enum: prevents free-text source names in domain logic.
    Values match the Prometheus `job` label names (metrics-reference.md line 15).

    - KUBE_STATE_METRICS: cluster state (resources, requests, PVCs, labels)
    - NODE_EXPORTER: node-level OS metrics (CPU, memory — used in P1)
    - KEPLER: eBPF energy metrics (Green-IT carbon calculations)
    """

    KUBE_STATE_METRICS = "kube-state-metrics"
    NODE_EXPORTER = "node-exporter"
    KEPLER = "kepler"


@dataclass(frozen=True)
class DataQuality:
    """
    Snapshot of which telemetry sources are currently healthy.

    Instead of a single coarse status (degraded/unreachable), we store the set
    of sources that ARE responding. Anything not in the set is treated as down.

    This lets the engine decide per recommendation category:
    - kube-state-metrics healthy → still produce FinOps right-sizing advice
    - kepler down → skip carbon recommendations, flag them as "data unavailable"

    Fields:
    - healthy_sources: sources with up == 1
    - timestamp: when the check was performed
    """

    healthy_sources: frozenset[TelemetrySource]
    timestamp: datetime

    @property
    def degraded_sources(self) -> frozenset[TelemetrySource]:
        """
        Sources currently DOWN (all known sources minus the healthy ones).

        WHY: The engine uses this to flag missing data on the dashboard.
        """
        all_sources = frozenset(TelemetrySource)
        return all_sources - self.healthy_sources

    @property
    def all_healthy(self) -> bool:
        """True if every telemetry source is responding."""
        return len(self.degraded_sources) == 0

    def is_healthy(self, source: TelemetrySource) -> bool:
        """
        Check a single source's health.

        WHY: The engine asks "can I run carbon recommendations?" by calling
        quality.is_healthy(TelemetrySource.KEPLER).
        """
        return source in self.healthy_sources