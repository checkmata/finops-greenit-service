"""
Ports (interfaces) — define contracts for inbound/outbound adapters.

A port declares WHAT the domain needs from the outside world.
An adapter implements HOW that need is fulfilled.

Current ports:
- MetricsPort: contract for fetching telemetry (Prometheus/Kepler).
"""

from app.ports.metrics_port import MetricsPort

__all__ = [
    "MetricsPort",
]