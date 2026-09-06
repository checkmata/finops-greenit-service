"""
Integration tests for PrometheusMetricsAdapter — HTTP mocking.

WHY HTTP mocking is needed here (and only here):
- The adapter is the ONLY code that speaks HTTP to Prometheus
- Domain engine tests don't need HTTP (pure functions)
- Router tests mock the adapter (not HTTP)
- This is the ONE place where we verify JSON → domain mapping

HOW mocking works:
- We replace httpx.AsyncClient with a fake that returns canned Prometheus JSON
- The adapter doesn't know the difference — it calls _client.get() as usual
- This tests the CONTRACT between Prometheus's API and our domain objects

WHAT "contract testing a port" means:
- MetricsPort defines the contract: "give me namespace, I return PodMetric"
- This test verifies: "given real Prometheus JSON, does the adapter produce correct PodMetric?"
- If Prometheus changes its JSON format, this test breaks → we know to update the adapter
"""

from __future__ import annotations

import pytest

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.domain.models.data_quality import TelemetrySource


# =============================================================================
# Fake HTTP transport — simulates Prometheus API responses
# =============================================================================


class FakeResponse:
    """Simulates httpx.Response for testing."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise HTTPStatusError if status code indicates failure."""
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=self,
            )

    def json(self):
        """Return the JSON payload."""
        return self._payload


class FakeClient:
    """Simulates httpx.AsyncClient for testing.

    Routes on the PromQL query string to return appropriate responses.
    This is a simplified mock — real httpx.AsyncClient would make HTTP calls.
    """

    base_url = "http://fake-prometheus:9090"

    def __init__(self, query_router: dict[str, dict] | None = None):
        self._router = query_router or {}

    async def get(self, path: str, params: dict | None = None) -> FakeResponse:
        """Simulate HTTP GET to Prometheus API."""
        query = params.get("query", "") if params else ""

        # Check if we have a canned response for this query
        for pattern, response in self._router.items():
            if pattern in query:
                return FakeResponse(response)

        # Default: return empty success
        return FakeResponse({"status": "success", "data": {"result": []}})

    async def aclose(self) -> None:
        """No-op close method (no real connections to close)."""
        pass


def make_adapter(query_responses: dict[str, dict]) -> PrometheusMetricsAdapter:
    """Create a PrometheusMetricsAdapter with a fake HTTP client.

    Args:
        query_responses: Dict mapping PromQL patterns to canned JSON responses.

    WHY bypass the constructor:
    - PrometheusMetricsAdapter.__init__ creates a real httpx.AsyncClient
    - That client keeps connection pools alive, which hang event loop teardown on Windows
    - We skip __init__ and inject FakeClient directly → no real HTTP resources created
    """
    adapter = object.__new__(PrometheusMetricsAdapter)
    adapter._client = FakeClient(query_responses)
    return adapter


# =============================================================================
# Real Prometheus JSON shapes (from docs/metrics-reference.md)
# =============================================================================

# Container CPU usage (rate over 30d)
PROMETHEUS_CPU_USAGE_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"namespace": "team-alpha"},
                "value": [1725523200, "4.5"],
            }
        ]
    },
}

# Container memory working set
PROMETHEUS_MEMORY_USAGE_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"namespace": "team-alpha"},
                "value": [1725523200, "8589934592"],
            }
        ]
    },
}

# CPU requests (kube-state-metrics)
PROMETHEUS_CPU_REQUESTS_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"namespace": "team-alpha", "resource": "cpu"},
                "value": [1725523200, "8.0"],
            }
        ]
    },
}

# Memory requests (kube-state-metrics)
PROMETHEUS_MEMORY_REQUESTS_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"namespace": "team-alpha", "resource": "memory"},
                "value": [1725523200, "17179869184"],
            }
        ]
    },
}

# Pod labels (kube-state-metrics)
PROMETHEUS_POD_LABELS_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {
                    "namespace": "team-alpha",
                    "label_team": "platform",
                    "label_cost_center": "engineering",
                    "label_environment": "prod",
                },
                "value": [1725523200, "1"],
            }
        ]
    },
}

# Kepler energy (joules)
PROMETHEUS_KEPLER_ENERGY_JSON = {
    "status": "success",
    "data": {
        "result": [
            {
                "metric": {"namespace": "team-alpha"},
                "value": [1725523200, "18000000"],
            }
        ]
    },
}

# Data quality (up metric)
PROMETHEUS_DATA_QUALITY_JSON = {
    "status": "success",
    "data": {
        "result": [
            {"metric": {"job": "kube-state-metrics"}, "value": [1, "1"]},
            {"metric": {"job": "node-exporter"}, "value": [1, "1"]},
            {"metric": {"job": "kepler"}, "value": [1, "0"]},
        ]
    },
}


# =============================================================================
# Test get_pod_metrics — real Prometheus JSON mapping
# =============================================================================


class TestGetPodMetrics:
    """Test that real Prometheus JSON is correctly mapped to PodMetric domain objects."""

    @pytest.mark.asyncio
    async def test_maps_real_prometheus_json_to_domain(self):
        """Given real Prometheus JSON, should produce correct PodMetric."""
        responses = {
            "container_cpu_usage_seconds_total": PROMETHEUS_CPU_USAGE_JSON,
            "container_memory_working_set_bytes": PROMETHEUS_MEMORY_USAGE_JSON,
            'resource="cpu"': PROMETHEUS_CPU_REQUESTS_JSON,
            'resource="memory"': PROMETHEUS_MEMORY_REQUESTS_JSON,
            "kube_pod_labels": PROMETHEUS_POD_LABELS_JSON,
        }
        adapter = make_adapter(responses)

        pods = await adapter.get_pod_metrics("team-alpha")

        assert len(pods) == 1
        pod = pods[0]
        assert pod.namespace == "team-alpha"
        assert pod.cpu_core_hours == 4.5
        assert pod.memory_bytes == 8589934592
        assert pod.cpu_requested == 8.0
        assert pod.memory_requested == 17179869184
        assert pod.team == "platform"
        assert pod.cost_center == "engineering"
        assert pod.environment == "prod"

    @pytest.mark.asyncio
    async def test_handles_missing_labels_gracefully(self):
        """Missing labels should default to 'unknown'."""
        responses = {
            "container_cpu_usage_seconds_total": PROMETHEUS_CPU_USAGE_JSON,
            "container_memory_working_set_bytes": PROMETHEUS_MEMORY_USAGE_JSON,
            'resource="cpu"': PROMETHEUS_CPU_REQUESTS_JSON,
            'resource="memory"': PROMETHEUS_MEMORY_REQUESTS_JSON,
            "kube_pod_labels": {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [1, "1"]}]},
            },
        }
        adapter = make_adapter(responses)

        pods = await adapter.get_pod_metrics("team-alpha")

        assert len(pods) == 1
        pod = pods[0]
        assert pod.team == "unknown"
        assert pod.cost_center == "unknown"
        assert pod.environment == "unknown"

    @pytest.mark.asyncio
    async def test_empty_namespace_returns_zero_values(self):
        """Namespace with no data should return PodMetric with zero values."""
        adapter = make_adapter({})

        pods = await adapter.get_pod_metrics("empty-namespace")

        assert len(pods) == 1
        pod = pods[0]
        assert pod.namespace == "empty-namespace"
        assert pod.cpu_core_hours == 0.0
        assert pod.memory_bytes == 0.0
        assert pod.cpu_requested == 0.0
        assert pod.memory_requested == 0.0


# =============================================================================
# Test get_energy_kwh — real Prometheus JSON mapping
# =============================================================================


class TestGetEnergyKwh:
    """Test that real Kepler JSON is correctly mapped to energy values."""

    @pytest.mark.asyncio
    async def test_maps_joules_to_kwh(self):
        """Given real Kepler JSON (joules), should convert to kWh."""
        adapter = make_adapter({"kepler_container": PROMETHEUS_KEPLER_ENERGY_JSON})

        kwh = await adapter.get_energy_kwh("team-alpha")

        # 18,000,000 joules / 3,600,000 = 5.0 kWh
        assert kwh == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_no_kepler_data_raises_error(self):
        """No Kepler data should raise PrometheusQueryError."""
        adapter = make_adapter({})

        with pytest.raises(PrometheusQueryError):
            await adapter.get_energy_kwh("team-alpha")


# =============================================================================
# Test check_data_quality — real Prometheus JSON mapping
# =============================================================================


class TestCheckDataQuality:
    """Test that real Prometheus 'up' metric is correctly mapped to DataQuality."""

    @pytest.mark.asyncio
    async def test_maps_up_metric_to_health_status(self):
        """Given real 'up' metric JSON, should produce correct DataQuality."""
        adapter = make_adapter({"up": PROMETHEUS_DATA_QUALITY_JSON})

        quality = await adapter.check_data_quality()

        assert quality.is_healthy(TelemetrySource.KUBE_STATE_METRICS)
        assert quality.is_healthy(TelemetrySource.NODE_EXPORTER)
        assert not quality.is_healthy(TelemetrySource.KEPLER)
        assert TelemetrySource.KEPLER in quality.degraded_sources


# =============================================================================
# Test error handling — transport failures
# =============================================================================


class TestErrorHandling:
    """Test that transport errors are correctly mapped to domain exceptions."""

    @pytest.mark.asyncio
    async def test_connection_error_raises_unavailable(self):
        """Connection error should raise PrometheusUnavailableError."""
        import httpx

        class ConnectionErrorClient(FakeClient):
            async def get(self, path, params=None):
                raise httpx.ConnectError("Connection refused")

        adapter = make_adapter({})
        adapter._client = ConnectionErrorClient()

        with pytest.raises(PrometheusUnavailableError):
            await adapter.get_pod_metrics("team-alpha")

    @pytest.mark.asyncio
    async def test_timeout_error_raises_unavailable(self):
        """Timeout error should raise PrometheusUnavailableError."""
        import httpx

        class TimeoutClient(FakeClient):
            async def get(self, path, params=None):
                raise httpx.TimeoutException("Request timed out")

        adapter = make_adapter({})
        adapter._client = TimeoutClient()

        with pytest.raises(PrometheusUnavailableError):
            await adapter.get_pod_metrics("team-alpha")

    @pytest.mark.asyncio
    async def test_http_500_raises_query_error(self):
        """HTTP 500 should raise PrometheusQueryError."""
        adapter = make_adapter({})
        adapter._client = FakeClient()

        async def get_500(path, params=None):
            return FakeResponse({"status": "error", "error": "internal error"}, status_code=500)

        adapter._client.get = get_500

        with pytest.raises(PrometheusQueryError):
            await adapter.get_pod_metrics("team-alpha")

    @pytest.mark.asyncio
    async def test_bad_json_raises_query_error(self):
        """Invalid JSON response should raise PrometheusQueryError."""
        adapter = make_adapter({})

        # Override _query to simulate bad JSON
        async def bad_query(promql):
            raise PrometheusQueryError("Invalid JSON")

        adapter._query = bad_query

        with pytest.raises(PrometheusQueryError):
            await adapter.get_pod_metrics("team-alpha")