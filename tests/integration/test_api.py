"""
API integration tests — all router endpoints via FastAPI's TestClient.

WHY dependency_overrides instead of real ports:
- The router is the composition root — tests need to verify it correctly calls
  domain engines and maps results to Pydantic responses.
- Real Prometheus/DB would re-test the adapters (already covered in
  test_prometheus_adapter_integration.py and test_timescale_adapter_integration.py).
- dependency_overrides replaces the adapter with a fake that returns controlled data,
  isolating the router logic.

WHAT these tests cover:
- Every endpoint from finops_router, greenit_router, recommendations_router, health_router
- Correct HTTP status codes (200, 400, 404, 503)
- Correct response structure (keys present, types valid)
- Error handling (empty data, invalid status transitions, missing resources)
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient

from app.adapters.inbound.api.dependencies import get_cluster_registry
from app.cluster_registry import ClusterRegistry
from app.domain.models.cluster import ClusterRole
from app.domain.models.energy import EnergyAttribution, EnergyMeasurement
from app.domain.models.recommendation import StoredRecommendation
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

from app.adapters.inbound.api.finops_router import (
    get_metrics_port as finops_get_metrics_port,
    get_storage_port as finops_get_storage_port,
)
from app.adapters.inbound.api.greenit_router import (
    get_metrics_port as greenit_get_metrics_port,
    get_storage_port as greenit_get_storage_port,
)
from app.adapters.inbound.api.health_router import (
    get_metrics_port as health_get_metrics_port,
    get_storage_port as health_get_storage_port,
)
from app.adapters.inbound.api.recommendations_router import (
    get_storage_port as recs_get_storage_port,
)
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.pod_metric import (
    ClusterAllocatable,
    PodMetric,
    StorageMetric,
)
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.ports.storage_port import MetricSnapshot


# =============================================================================
# Fake implementations — return controlled data for every port method
# =============================================================================


class FakeMetricsPort(MetricsPort):
    """Fake MetricsPort for API tests.

    WHY a class (not a lambda):
    - MetricsPort is an ABC — the fake must implement all abstract methods.
    - Class lets us configure return values per test if needed.
    """

    def __init__(self) -> None:
        self._pod_metrics: list[PodMetric] = []
        self._energy_kwh: float = 0.0
        self._data_quality: DataQuality = DataQuality(
            healthy_sources=frozenset(TelemetrySource),
            timestamp=datetime.now(tz=timezone.utc),
        )
        self._cluster = ClusterAllocatable(
            cpu_allocatable=32.0,
            memory_allocatable=128 * 1024**3,
            timestamp=datetime.now(tz=timezone.utc),
        )
        self._storage_metrics: list[StorageMetric] = []

    async def get_pod_metrics(self, namespace: str) -> list[PodMetric]:
        return self._pod_metrics

    async def get_cluster_allocatable(self) -> ClusterAllocatable:
        return self._cluster

    async def get_storage_metrics(self, namespace: str) -> list[StorageMetric]:
        return self._storage_metrics

    async def check_data_quality(self) -> DataQuality:
        return self._data_quality

    async def get_energy_kwh(self, namespace: str) -> float:
        return self._energy_kwh

    # --- V1 port methods ---
    async def ping(self) -> bool:
        return True

    async def list_namespaces(self) -> list[str]:
        return sorted({p.namespace for p in self._pod_metrics})

    async def get_workload_metrics(self, namespace: str, namespace_total=None) -> list[PodMetric]:
        return []

    async def get_workload_energy(self, namespace: str, window_seconds: int = 86400) -> list:
        return []

    async def get_energy(self, namespace: str, window_seconds: int = 86400) -> EnergyMeasurement:
        # Delegates to get_energy_kwh so subclasses overriding it (error cases) still apply.
        from app.adapters.outbound.prometheus_adapter import PrometheusQueryError

        try:
            kwh = await self.get_energy_kwh(namespace)
        except PrometheusQueryError:
            return EnergyMeasurement(namespace, None, None, EnergyAttribution.UNAVAILABLE, "none")
        return EnergyMeasurement(namespace, kwh, None, EnergyAttribution.CONTAINER, "fake")

    async def get_deployment_availability(self) -> list:
        return []


class FakeStoragePort(StoragePort):
    """Fake StoragePort for API tests.

    Stores data in memory — enough for router logic verification.
    All methods are async to match the ABC, but execution is immediate.
    """

    def __init__(self) -> None:
        self._recommendations: dict[UUID, Recommendation] = {}
        self._statuses: dict[UUID, str] = {}
        self._snapshots: list[MetricSnapshot] = []
        self._next_id: int = 0

    async def save_recommendation(self, rec: Recommendation) -> UUID:
        self._next_id += 1
        rec_id = UUID(int=self._next_id)
        self._recommendations[rec_id] = rec
        self._statuses[rec_id] = "pending"
        return rec_id

    async def update_recommendation_status(
        self, rec_id: UUID, new_status: str
    ) -> bool:
        if rec_id not in self._recommendations:
            return False
        self._statuses[rec_id] = new_status
        return True

    async def get_recommendation(self, rec_id: UUID) -> Recommendation | None:
        return self._recommendations.get(rec_id)

    async def list_recommendations(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Recommendation]:
        results = list(self._recommendations.values())
        if namespace is not None:
            results = [r for r in results if r.namespace == namespace]
        if status is not None:
            results = [
                r
                for r, s in zip(self._recommendations.values(), self._statuses.values())
                if s == status
            ]
        return results[offset : offset + limit]

    async def save_snapshot(self, snapshot: MetricSnapshot) -> None:
        self._snapshots.append(snapshot)

    async def get_snapshot_history(
        self, namespace: str, days: int = 7
    ) -> list[MetricSnapshot]:
        return [s for s in self._snapshots if s.namespace == namespace]

    async def close(self) -> None:
        pass

    # --- V1 port methods ---
    def _stored(self, rec_id: UUID) -> StoredRecommendation:
        now = datetime.now(tz=timezone.utc)
        return StoredRecommendation(rec_id, self._statuses[rec_id], now, now, self._recommendations[rec_id])

    async def upsert_recommendation(self, rec: Recommendation):
        return await self.save_recommendation(rec), "insert"

    async def get_recommendation_record(self, rec_id: UUID):
        return self._stored(rec_id) if rec_id in self._recommendations else None

    async def list_recommendation_records(
        self, *, cluster=None, namespace=None, status=None, limit=50, offset=0
    ):
        out = [
            self._stored(i) for i in self._recommendations
            if (namespace is None or self._recommendations[i].namespace == namespace)
            and (cluster is None or self._recommendations[i].cluster == cluster)
            and (status is None or self._statuses[i] == status)
        ]
        return out[offset: offset + limit]

    async def save_workload_snapshots(self, snapshots) -> None:
        pass

    async def mark_obsolete(self, *, cluster, namespace, keep_fingerprints, categories=None,
                            exclude_categories=None) -> int:
        changed = 0
        for i, rec in self._recommendations.items():
            if (rec.cluster, rec.namespace) == (cluster, namespace) and self._statuses[i] == "pending" \
                    and rec.fingerprint not in keep_fingerprints:
                self._statuses[i] = "obsolete"
                changed += 1
        return changed

    async def query_snapshots(self, *, start, end, cluster=None, namespace=None):
        return [s for s in self._snapshots if namespace is None or s.namespace == namespace]

    async def query_workload_snapshots(self, *, start, end, cluster=None, namespace=None, workload=None):
        return []

    async def query_aggregates(self, **kwargs):
        return []

    def _seed_recommendation(self, rec: Recommendation) -> UUID:
        """Seed a recommendation directly (bypasses async, for test setup only)."""
        self._next_id += 1
        rec_id = UUID(int=self._next_id)
        self._recommendations[rec_id] = rec
        self._statuses[rec_id] = "pending"
        return rec_id


# =============================================================================
# Helpers — build fake domain objects for controlled test data
# =============================================================================


def _make_pod_metric(namespace: str = "team-alpha", **overrides) -> PodMetric:
    """Build a PodMetric with sensible defaults."""
    defaults = dict(
        namespace=namespace,
        team=f"{namespace}-team",
        cost_center="cc-001",
        environment="production",
        cpu_core_hours=1.5,
        memory_bytes=512 * 1024**2,
        cpu_requested=2.0,
        memory_requested=1024 * 1024**2,
        timestamp=datetime.now(tz=timezone.utc),
    )
    defaults.update(overrides)
    return PodMetric(**defaults)


def _make_recommendation(namespace: str = "team-alpha", **overrides) -> Recommendation:
    """Build a Recommendation with sensible defaults."""
    defaults = dict(
        category=Category.COST_RIGHTSIZING,
        severity=Severity.WARNING,
        namespace=namespace,
        resource="pod/alpha-web-0",
        message="CPU over-provisioned: using 30% of requested",
        current_value="2.0 cores",
        recommended_value="0.6 cores",
        estimated_savings_usd=12.50,
        estimated_savings_kg_co2=0.8,
        timestamp=datetime.now(tz=timezone.utc),
    )
    defaults.update(overrides)
    return Recommendation(**defaults)


def _make_snapshot(namespace: str = "team-alpha", **overrides) -> MetricSnapshot:
    """Build a MetricSnapshot with sensible defaults."""
    defaults = dict(
        time=datetime.now(tz=timezone.utc),
        namespace=namespace,
        team=f"{namespace}-team",
        cost_center="cc-001",
        environment="production",
        cpu_core_hours=1.5,
        memory_gb_hours=2.0,
        cost_usd=5.25,
        energy_kwh=0.5,
        co2e_kg=0.000237,
        waste_pct=30.0,
    )
    defaults.update(overrides)
    return MetricSnapshot(**defaults)


# =============================================================================
# Fixtures — set up TestClient with dependency overrides
# =============================================================================


def _make_test_client(
    metrics_port: FakeMetricsPort | None = None,
    storage_port: FakeStoragePort | None = None,
) -> tuple[TestClient, FakeMetricsPort, FakeStoragePort]:
    """Create a TestClient with all dependency overrides applied.

    WHY override ALL four routers' get_metrics_port/get_storage_port:
    - Each router defines its own dependency function (fastapi.Depends()).
    - FastAPI uses the function object identity as the override key.
    - We must override every function individually — sharing one override
      across routers would not work.

    WHY create app WITHOUT lifespan (not using create_app()):
    - The production lifespan starts a snapshot scheduler and real adapters.
    - On Windows, httpx.AsyncClient teardown hangs the event loop.
    - We build a minimal app here: just routers + DI overrides, no background tasks.
    """
    from fastapi import FastAPI

    from app.adapters.inbound.api.finops_router import router as finops_router
    from app.adapters.inbound.api.greenit_router import router as greenit_router
    from app.adapters.inbound.api.health_router import router as health_router
    from app.adapters.inbound.api.recommendations_router import router as recs_router

    metrics = metrics_port or FakeMetricsPort()
    storage = storage_port or FakeStoragePort()

    app = FastAPI()
    app.include_router(finops_router)
    app.include_router(greenit_router)
    app.include_router(recs_router)
    app.include_router(health_router)

    app.dependency_overrides[finops_get_metrics_port] = lambda: metrics
    app.dependency_overrides[finops_get_storage_port] = lambda: storage
    app.dependency_overrides[greenit_get_metrics_port] = lambda: metrics
    app.dependency_overrides[greenit_get_storage_port] = lambda: storage
    app.dependency_overrides[health_get_metrics_port] = lambda: metrics
    app.dependency_overrides[health_get_storage_port] = lambda: storage
    app.dependency_overrides[recs_get_storage_port] = lambda: storage
    # V1: routers share one registry of per-cluster MetricsPorts.
    registry = ClusterRegistry({"local": ClusterRole.PRIMARY}, {"local": metrics})
    app.dependency_overrides[get_cluster_registry] = lambda: registry

    client = TestClient(app, raise_server_exceptions=False)
    return client, metrics, storage


# =============================================================================
# Health endpoint tests
# =============================================================================


class TestHealthEndpoint:
    """GET /health — checks Prometheus + DB connectivity."""

    def test_health_returns_200_when_all_healthy(self) -> None:
        """Both Prometheus and DB are healthy → 200."""
        client, _, _ = _make_test_client()
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "components" in data
        assert data["components"]["prometheus"]["status"] == "healthy"
        assert data["components"]["database"]["status"] == "healthy"

    def test_health_returns_elapsed_ms(self) -> None:
        """Response includes timing information."""
        client, _, _ = _make_test_client()
        response = client.get("/health")
        data = response.json()
        assert "elapsed_ms" in data
        assert isinstance(data["elapsed_ms"], (int, float))
        assert data["elapsed_ms"] >= 0


# =============================================================================
# FinOps router tests
# =============================================================================


class TestFinOpsCurrentEndpoint:
    """GET /api/finops/namespace/{namespace} — current state from Prometheus."""

    def test_returns_200_with_valid_data(self) -> None:
        """Namespace with pod metrics → 200 with cost breakdown."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = [_make_pod_metric()]
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/finops/namespace/team-alpha")
        assert response.status_code == 200
        data = response.json()
        assert data["namespace"] == "team-alpha"
        assert data["pod_count"] == 1
        assert "requested_cost_usd" in data
        assert "actual_cost_usd" in data
        assert "cost_savings_usd" in data
        assert isinstance(data["pods"], list)
        assert len(data["pods"]) == 1

    def test_returns_404_when_no_pods(self) -> None:
        """Namespace with no pod metrics → 404."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = []
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/finops/namespace/empty-ns")
        assert response.status_code == 404
        assert "No pods found" in response.json()["detail"]

    def test_pod_response_has_all_fields(self) -> None:
        """Pod response contains all expected fields."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = [_make_pod_metric()]
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/finops/namespace/team-alpha")
        pod = response.json()["pods"][0]
        required_fields = [
            "namespace", "team", "cost_center", "environment",
            "cpu_core_hours", "memory_bytes", "cpu_requested", "memory_requested",
            "cpu_utilization_pct", "memory_utilization_pct",
            "is_overprovisioned_cpu", "is_overprovisioned_memory",
            "timestamp",
        ]
        for field in required_fields:
            assert field in pod, f"Missing field: {field}"


class TestFinOpsHistoryEndpoint:
    """GET /api/finops/namespace/{namespace}/history — historical from TimescaleDB."""

    def test_returns_200_with_snapshots(self) -> None:
        """Namespace with snapshots → 200 with snapshot list."""
        storage = FakeStoragePort()
        storage._snapshots = [_make_snapshot(), _make_snapshot()]
        client, _, _ = _make_test_client(storage_port=storage)
        response = client.get("/api/finops/namespace/team-alpha/history?days=7")
        assert response.status_code == 200
        data = response.json()
        assert data["namespace"] == "team-alpha"
        assert data["snapshot_count"] == 2
        assert len(data["snapshots"]) == 2

    def test_returns_404_when_no_snapshots(self) -> None:
        """Namespace with no snapshots → 404."""
        client, _, _ = _make_test_client()
        response = client.get("/api/finops/namespace/empty-ns/history")
        assert response.status_code == 404
        assert "No historical snapshots" in response.json()["detail"]

    def test_days_parameter_validation(self) -> None:
        """days parameter must be 1-90."""
        client, _, _ = _make_test_client()
        response = client.get("/api/finops/namespace/team-alpha/history?days=0")
        assert response.status_code == 422  # validation error
        response = client.get("/api/finops/namespace/team-alpha/history?days=91")
        assert response.status_code == 422


# =============================================================================
# GreenIT router tests
# =============================================================================


class TestGreenITCurrentEndpoint:
    """GET /api/greenit/namespace/{namespace} — current state from Prometheus."""

    def test_returns_200_with_energy_data(self) -> None:
        """Namespace with energy data → 200 with carbon footprint."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = [_make_pod_metric()]
        metrics._energy_kwh = 0.5
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/greenit/namespace/team-alpha")
        assert response.status_code == 200
        data = response.json()
        assert data["namespace"] == "team-alpha"
        assert "carbon_footprint" in data
        assert "energy_breakdown" in data
        cf = data["carbon_footprint"]
        assert "energy_kwh" in cf
        assert "co2_kg" in cf
        assert "co2_grams" in cf

    def test_returns_404_when_no_energy(self) -> None:
        """Namespace with no energy data → 404 (adapter raises PrometheusQueryError)."""
        from app.adapters.outbound.prometheus_adapter import PrometheusQueryError

        class ErrorMetricsPort(FakeMetricsPort):
            async def get_energy_kwh(self, namespace: str) -> float:
                raise PrometheusQueryError(f"No energy metrics for '{namespace}'")

        client, _, _ = _make_test_client(metrics_port=ErrorMetricsPort())
        response = client.get("/api/greenit/namespace/empty-ns")
        assert response.status_code == 404


class TestGreenITHistoryEndpoint:
    """GET /api/greenit/namespace/{namespace}/history — historical from TimescaleDB."""

    def test_returns_200_with_snapshots(self) -> None:
        """Namespace with snapshots → 200."""
        storage = FakeStoragePort()
        storage._snapshots = [_make_snapshot()]
        client, _, _ = _make_test_client(storage_port=storage)
        response = client.get("/api/greenit/namespace/team-alpha/history?days=7")
        assert response.status_code == 200
        assert response.json()["snapshot_count"] == 1

    def test_returns_404_when_empty(self) -> None:
        """No snapshots → 404."""
        client, _, _ = _make_test_client()
        response = client.get("/api/greenit/namespace/empty-ns/history")
        assert response.status_code == 404


# =============================================================================
# Recommendations router tests
# =============================================================================


class TestRecommendationsHistoryEndpoint:
    """GET /api/recommendations/history — stored recommendations from TimescaleDB."""

    def test_returns_200_with_recommendations(self) -> None:
        """Has recommendations → 200 with list."""
        storage = FakeStoragePort()
        rec = _make_recommendation()
        storage._seed_recommendation(rec)
        client, _, _ = _make_test_client(storage_port=storage)
        response = client.get("/api/recommendations/history")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_returns_404_when_empty(self) -> None:
        """No recommendations → 404."""
        client, _, _ = _make_test_client()
        response = client.get("/api/recommendations/history")
        assert response.status_code == 404

    def test_invalid_status_returns_400(self) -> None:
        """Invalid status value → 400."""
        client, _, _ = _make_test_client()
        response = client.get("/api/recommendations/history?status=invalid")
        assert response.status_code == 400
        assert "Invalid status" in response.json()["detail"]

    def test_namespace_filter(self) -> None:
        """Filter by namespace."""
        storage = FakeStoragePort()
        storage._seed_recommendation(_make_recommendation(namespace="team-alpha"))
        storage._seed_recommendation(_make_recommendation(namespace="team-beta"))
        client, _, _ = _make_test_client(storage_port=storage)
        response = client.get("/api/recommendations/history?namespace=team-alpha")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["namespace"] == "team-alpha"


class TestRecommendationsStatusUpdate:
    """PATCH /api/recommendations/{id}/status — update lifecycle status."""

    def test_returns_200_on_valid_transition(self) -> None:
        """pending → approved is a valid transition."""
        storage = FakeStoragePort()
        rec = _make_recommendation()
        rec_id = storage._seed_recommendation(rec)
        client, _, _ = _make_test_client(storage_port=storage)
        response = client.patch(
            f"/api/recommendations/{rec_id}/status",
            json={"status": "approved"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "namespace" in data

    def test_returns_404_for_missing_rec(self) -> None:
        """Recommendation not found → 404."""
        client, _, _ = _make_test_client()
        fake_id = UUID(int=999)
        response = client.patch(
            f"/api/recommendations/{fake_id}/status",
            json={"status": "approved"},
        )
        assert response.status_code == 404

    def test_returns_400_for_invalid_status(self) -> None:
        """Invalid status value → 400."""
        client, _, _ = _make_test_client()
        fake_id = UUID(int=1)
        response = client.patch(
            f"/api/recommendations/{fake_id}/status",
            json={"status": "not_a_real_status"},
        )
        assert response.status_code == 400


# =============================================================================
# Response structure tests — validate OpenAPI contract
# =============================================================================


class TestResponseStructure:
    """Verify response bodies match the Pydantic schemas."""

    def test_finops_response_keys(self) -> None:
        """FinOps endpoint returns all expected top-level keys."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = [_make_pod_metric()]
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/finops/namespace/team-alpha")
        data = response.json()
        expected_keys = {
            "namespace", "pod_count", "pods",
            "requested_cost_usd", "actual_cost_usd", "cost_savings_usd",
            # V1 additions (additive contract change, documented in docs/FINOPS.md)
            "cluster", "cost_breakdown", "storage_status", "workloads",
        }
        assert expected_keys == set(data.keys())

    def test_greenit_response_keys(self) -> None:
        """GreenIT endpoint returns all expected top-level keys."""
        metrics = FakeMetricsPort()
        metrics._pod_metrics = [_make_pod_metric()]
        metrics._energy_kwh = 0.5
        client, _, _ = _make_test_client(metrics_port=metrics)
        response = client.get("/api/greenit/namespace/team-alpha")
        data = response.json()
        expected_keys = {
            "namespace", "carbon_footprint", "energy_breakdown",
            # V1 additions: provenance of the energy figure
            "cluster", "energy_source",
        }
        assert expected_keys == set(data.keys())

    def test_health_response_keys(self) -> None:
        """Health endpoint returns all expected top-level keys."""
        client, _, _ = _make_test_client()
        response = client.get("/health")
        data = response.json()
        expected_keys = {"status", "timestamp", "elapsed_ms", "components"}
        assert expected_keys == set(data.keys())
        assert "prometheus" in data["components"]
        assert "database" in data["components"]
