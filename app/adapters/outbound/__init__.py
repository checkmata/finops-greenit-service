"""
Outbound adapters (concrete integrations to the outside world).

Two adapters:
- PrometheusMetricsAdapter: implements MetricsPort, talks to OpenShift Prometheus + Kepler.
- TimescaleAdapter:         implements StoragePort, talks to PostgreSQL/TimescaleDB.
"""

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.adapters.outbound.timescale_adapter import TimescaleAdapter

__all__ = [
    "PrometheusMetricsAdapter",
    "PrometheusUnavailableError",
    "PrometheusQueryError",
    "TimescaleAdapter",
]