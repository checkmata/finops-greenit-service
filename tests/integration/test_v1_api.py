"""V1 API: clusters/failover, cluster selection, lifecycle 409, health degraded, history, /metrics."""

from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cluster_registry import ClusterRegistry
from app.composition import Container
from app.domain.models.cluster import ClusterRole
from app.domain.models.energy import EnergyAttribution
from tests.helpers_v1 import InMemoryMetricsPort, energy, pod
from tests.integration.test_api import FakeStoragePort, _make_recommendation, _make_snapshot

PRIMARY_DEPS = [("ecommerce", "frontend", 3, 3), ("trading", "order-matcher", 2, 2)]
STANDBY_DEPS = [("ecommerce", "frontend", 1, 1)]


def build(primary_reachable=True, storage=None):
    primary = InMemoryMetricsPort("primary", {
        "ecommerce": {"total": pod("ecommerce", cluster="primary"), "energy": energy("ecommerce", 2.0)},
    }, reachable=primary_reachable, deployments=PRIMARY_DEPS)
    secondary = InMemoryMetricsPort("secondary", {
        "ecommerce": {"total": pod("ecommerce", cluster="secondary", cpu_core_hours=0.1),
                      "energy": energy("ecommerce", 0.5, attribution=EnergyAttribution.NODE_CPU_SHARE)},
    }, deployments=STANDBY_DEPS)
    registry = ClusterRegistry(
        {"primary": ClusterRole.PRIMARY, "secondary": ClusterRole.SECONDARY},
        {"primary": primary, "secondary": secondary},
    )
    from app.adapters.inbound.api.main import create_app

    app: FastAPI = create_app()
    storage = storage or FakeStoragePort()
    app.state.container = Container(registry=registry, storage=storage)
    # No `with TestClient(...)`: the lifespan (real adapters + scheduler) is not started.
    return TestClient(app, raise_server_exceptions=False), registry, storage


class TestClusters:
    def test_nominal_plan(self):
        client, _, _ = build()
        body = client.get("/api/clusters").json()
        assert body["active_cluster"] == "primary" and body["failover_active"] is False
        assert [c["name"] for c in body["clusters"]] == ["primary", "secondary"]
        assert "does not move traffic" in body["note"]

    def test_primary_unreachable_triggers_failover_decision(self):
        client, registry, _ = build()
        client.get("/api/clusters")                     # primary observed once (healthy)
        registry.port("primary").reachable = False      # then it goes dark
        body = client.get("/api/clusters").json()
        pl = {p["workload"]: p for p in body["placements"]}
        assert body["active_cluster"] == "secondary"
        assert pl["frontend"]["status"] == "failover" and pl["frontend"]["serving_cluster"] == "secondary"
        assert pl["order-matcher"]["status"] == "outage"  # remembered, not silently dropped
        primary = [c for c in body["clusters"] if c["name"] == "primary"][0]
        assert primary["workloads_source"] == "last_seen"

    def test_ksm_down_with_prometheus_reachable_keeps_workloads(self):
        """Regression (failover demo): KSM series go stale → empty list, workloads must not vanish."""
        from app.domain.models.data_quality import DataQuality, TelemetrySource
        from tests.helpers_v1 import NOW

        client, registry, _ = build()
        client.get("/api/clusters")
        primary = registry.port("primary")
        primary.deployments = []                                   # stale series → nothing returned
        primary.quality = DataQuality(frozenset({TelemetrySource.KEPLER}), NOW)  # KSM down
        body = client.get("/api/clusters").json()
        pl = {p["workload"]: p["status"] for p in body["placements"]}
        assert pl == {"frontend": "failover", "order-matcher": "outage"}

    def test_never_observed_primary_is_reported_unknown(self):
        client, _, _ = build(primary_reachable=False)
        body = client.get("/api/clusters").json()
        primary = [c for c in body["clusters"] if c["name"] == "primary"][0]
        assert primary["workloads_source"] == "unknown" and body["active_cluster"] == "secondary"

    def test_operator_declaration_and_failback(self):
        client, _, _ = build()
        r = client.put("/api/clusters/primary/availability", json={"available": False, "reason": "maintenance"})
        assert r.status_code == 200 and r.json()["active_cluster"] == "secondary"
        r = client.put("/api/clusters/primary/availability", json={"available": True})
        assert r.json()["active_cluster"] == "primary" and r.json()["failover_active"] is False

    def test_unknown_cluster(self):
        client, _, _ = build()
        assert client.put("/api/clusters/nope/availability", json={"available": False}).status_code == 404
        assert client.get("/api/finops/namespace/ecommerce?cluster=nope").status_code == 404


class TestClusterSelection:
    def test_finops_and_greenit_per_cluster(self):
        client, _, _ = build()
        a = client.get("/api/finops/namespace/ecommerce").json()
        b = client.get("/api/finops/namespace/ecommerce?cluster=secondary").json()
        assert a["cluster"] == "primary" and b["cluster"] == "secondary"
        assert a["actual_cost_usd"] > b["actual_cost_usd"]
        assert a["storage_status"] == "unavailable"  # in-memory port returns no PVC data
        g = client.get("/api/greenit/namespace/ecommerce?cluster=secondary").json()
        assert g["energy_source"]["precision"] == "estimated"
        assert g["carbon_footprint"]["energy_attribution"] == "node_cpu_share"
        assert g["energy_breakdown"]["idle_waste_kwh"] is None

    def test_greenit_404_when_energy_unattributable(self):
        client, _, _ = build()
        assert client.get("/api/greenit/namespace/unknown-ns").status_code == 404


class TestLifecycle:
    def test_real_ids_and_409_on_illegal_transition(self):
        storage = FakeStoragePort()
        rec_id = storage._seed_recommendation(_make_recommendation())
        client, _, _ = build(storage=storage)
        listed = client.get("/api/recommendations/history").json()
        assert listed[0]["id"] == str(rec_id) and listed[0]["status"] == "pending"

        assert client.patch(f"/api/recommendations/{rec_id}/status", json={"status": "executed"}).status_code == 409
        r = client.patch(f"/api/recommendations/{rec_id}/status", json={"status": "approved"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert client.patch(f"/api/recommendations/{rec_id}/status", json={"status": "executed"}).json()["status"] == "executed"
        r = client.patch(f"/api/recommendations/{rec_id}/status", json={"status": "pending"})
        assert r.status_code == 409 and "terminal" in r.json()["detail"]

    def test_legacy_dismissed_is_rejected_value(self):
        client, _, _ = build()
        r = client.patch(f"/api/recommendations/{UUID(int=5)}/status", json={"status": "dismissed"})
        assert r.status_code == 400


class TestHealth:
    def test_degraded_when_one_cluster_down(self):
        client, _, _ = build(primary_reachable=False)
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "degraded"

    def test_liveness_never_depends_on_dependencies(self):
        client, _, _ = build(primary_reachable=False)
        assert client.get("/health/live").json() == {"status": "alive"}

    def test_mvp_bug_fixed_prometheus_down_is_detected(self):
        client, registry, _ = build()
        for p in registry.ports().values():
            p.reachable = False
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["components"]["prometheus"]["status"] == "unhealthy"


class TestHistoryAndMetrics:
    def test_timeseries(self):
        storage = FakeStoragePort()
        storage._snapshots = [_make_snapshot(cost_usd=10.0), _make_snapshot(namespace="team-beta", cost_usd=5.0)]
        client, _, _ = build(storage=storage)
        body = client.get("/api/history/timeseries?metric=cost&group_by=cluster").json()
        assert body["series"][0]["points"][0]["value"] == pytest.approx(15.0)
        assert client.get("/api/history/timeseries?metric=bogus").status_code == 400

    def test_metrics_endpoint_after_a_cycle(self):
        import asyncio

        from app.scheduler.snapshot_job import run_multicluster_cycle

        client, registry, storage = build()
        asyncio.run(run_multicluster_cycle(registry.ports(), storage, namespaces=["ecommerce"]))
        r = client.get("/metrics/")
        assert r.status_code == 200 and "finops_scheduler_cycles_total" in r.text
