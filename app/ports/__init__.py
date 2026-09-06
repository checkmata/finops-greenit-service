"""
Ports (interfaces) — define contracts for inbound/outbound adapters.

A port declares WHAT the domain needs from the outside world.
An adapter implements HOW that need is fulfilled.

Current ports:
- MetricsPort: contract for fetching telemetry (Prometheus/Kepler).
- StoragePort:  contract for persisting recommendations + metric snapshots.
"""

from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import MetricSnapshot, StoragePort

__all__ = [
    "MetricSnapshot",
    "MetricsPort",
    "StoragePort",
]