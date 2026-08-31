"""
Integration tests for PrometheusMetricsAdapter (JSON → domain mapping).

Per AGENTS.md: only prometheus_adapter.py tests use HTTP mocking.
These tests stub the httpx client with fake Prometheus responses and verify
that raw Prometheus JSON is mapped correctly into domain dataclasses.
"""

from __future__ import annotations

import pytest

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.domain.models.data_quality import TelemetrySource


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient; routes on the PromQL passed in."""

    base_url = "http://fake-prometheus:9090"

    def __init__(self, query_router):
        self._router = query_router
        self.options = []  # not used, present for parity

    async def get(self, path: str, params: dict | None = None) -> FakeResponse:
        return FakeResponse(self._router(params["query"]))


def make_adapter(query_router) -> PrometheusMetricsAdapter:
    adapter = PrometheusMetricsAdapter("http://fake-prometheus:9090", 1)
    adapter._client = FakeClient(query_router)  # inject the fake transport
    return adapter


def default_router(query):
    """Echo simple success results based on which metric is asked for."""
    q = query.lower()
    if "container_cpu_usage" in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "4.5"]}]}}
    if "memory_working" in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "8589934592"]}]}}
    if 'resource="cpu"' in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "8.0"]}]}}
    if 'resource="memory"' in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "17179869184"]}]}}
    if "kube_pod_labels" in q and "team-alpha" in q:
        return {"status": "success", "data": {"result": [{"metric": {"label_team": "platform", "label_environment": "prod"}, "value": [1, "1"]}]}}
    if "kepler_container" in q:
        return {"status": "success", "data": {"result": [{"metric": {}, "value": [1, "18000000"]}]}}
    return {"status": "success", "data": {"result": []}}


@pytest.mark.asyncio
async def test_get_pod_metrics_maps_prometheus_json_to_domain():
    adapter = make_adapter(default_router)

    pods = await adapter.get_pod_metrics("team-alpha")

    assert len(pods) == 1
    pod = pods[0]
    assert pod.namespace == "team-alpha"
    assert pod.cpu_core_hours == 4.5
    assert pod.memory_bytes == 8589934592
    assert pod.cpu_requested == 8.0
    assert pod.memory_requested == 17179869184
    # Attribution labels come from kube_pod_labels.
    assert pod.team == "platform"
    assert pod.environment == "prod"
    assert pod.cost_center == "unknown"


@pytest.mark.asyncio
async def test_check_data_quality_reports_only_healthy_sources():
    def router(query):
        return {
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"job": "kube-state-metrics"}, "value": [1, "1"]},
                    {"metric": {"job": "node-exporter"}, "value": [1, "1"]},
                    {"metric": {"job": "kepler"}, "value": [1, "0"]},
                ]
            },
        }

    adapter = make_adapter(router)
    quality = await adapter.check_data_quality()

    assert quality.is_healthy(TelemetrySource.KUBE_STATE_METRICS)
    assert quality.is_healthy(TelemetrySource.NODE_EXPORTER)
    assert not quality.is_healthy(TelemetrySource.KEPLER)
    assert TelemetrySource.KEPLER in quality.degraded_sources


@pytest.mark.asyncio
async def test_get_energy_kwh_converts_joules_to_kwh():
    # 18,000,000 joules = 5 kWh exactly.
    adapter = make_adapter(default_router)

    kwh = await adapter.get_energy_kwh("team-alpha")

    assert kwh == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_get_energy_kwh_raises_when_no_kepler_data():
    def router(query):
        return {"status": "success", "data": {"result": []}}  # nothing at all

    adapter = make_adapter(router)

    with pytest.raises(PrometheusQueryError):
        await adapter.get_energy_kwh("team-alpha")


@pytest.mark.asyncio
async def test_reachable_prometheus_that_is_down_returns_empty_health():
    def router(query):
        raise PrometheusUnavailableError("connection refused")

    adapter = make_adapter(router)
    quality = await adapter.check_data_quality()

    assert len(quality.healthy_sources) == 0
    assert not quality.all_healthy


@pytest.mark.asyncio
async def test_transport_error_raises_unavailable():
    class ExplodingClient(FakeClient):
        async def get(self, path, params=None):
            raise PrometheusUnavailableError("timeout")

    adapter = make_adapter(default_router)
    adapter._client = ExplodingClient(default_router)

    with pytest.raises(PrometheusUnavailableError):
        await adapter.get_pod_metrics("team-alpha")