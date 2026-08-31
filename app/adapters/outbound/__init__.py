"""
Outbound adapters (concrete integrations to the outside world).

PrometheusMetricsAdapter is the ONLY adapter: it implements MetricsPort and
talks to OpenShift's built-in Prometheus (which scrapes cAdvisor,
kube-state-metrics, node-exporter, kubelet, and Kepler by default).

WHY a single outbound adapter:
- All telemetry goes through one HTTP client, one error path, one PromQL layer.
- AGENTS.md scopes exactly two data sources (Prometheus + Kepler) and routes
  BOTH through this same adapter.
"""

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusQueryError,
    PrometheusUnavailableError,
)

__all__ = [
    "PrometheusMetricsAdapter",
    "PrometheusUnavailableError",
    "PrometheusQueryError",
]