"""V2 outbound adapters: Kubernetes actuator (HTTP transport), failover store (SQL), failover API."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.outbound.failover_store import SqlFailoverStore
from app.adapters.outbound.kubernetes_actuator import KubernetesActuator
from app.adapters.outbound.postgres_leader import AlwaysLeader, _asyncpg_dsn
from app.cluster_config import Credentials, KubernetesAccess, TlsConfig
from app.domain.failover_controller import ControllerState, FailoverMode
from app.ports.failover_ports import FailoverEvent
from tests.helpers_v1 import storage_ctx

T = datetime(2026, 9, 29, tzinfo=timezone.utc)


def actuator(handler):
    a = KubernetesActuator(KubernetesAccess("https://k8s:6443", Credentials("bearer", None, "tok"), TlsConfig()))
    a._client._transport = httpx.MockTransport(handler)
    return a


@pytest.mark.asyncio
async def test_actuator_reads_and_patches_the_scale_subresource():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers.get("authorization"), req.headers.get("content-type"),
                     req.content))
        if req.url.path.endswith("/frontend/scale"):
            return httpx.Response(200, json={"spec": {"replicas": 1}})
        if "/trading/" in req.url.path:
            return httpx.Response(403)
        return httpx.Response(404)

    a = actuator(handler)
    assert await a.get_replicas("ecommerce", "frontend") == 1
    assert await a.get_replicas("trading", "order-matcher") is None      # 403: outside RBAC / absent namespace
    assert await a.get_replicas("ecommerce", "nope") is None             # 404
    await a.scale("ecommerce", "frontend", 3)
    method, path, auth, ctype, body = seen[-1]
    assert (method, path) == ("PATCH", "/apis/apps/v1/namespaces/ecommerce/deployments/frontend/scale")
    assert auth == "Bearer tok" and ctype == "application/merge-patch+json"
    assert json.loads(body) == {"spec": {"replicas": 3}}
    await a.close()


@pytest.mark.asyncio
async def test_actuator_raises_on_server_error():
    a = actuator(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await a.scale("ecommerce", "frontend", 2)
    await a.close()


@pytest.mark.asyncio
async def test_failover_store_state_and_events_roundtrip():
    async with storage_ctx() as st:
        store = SqlFailoverStore(st._session_factory)
        assert await store.load_state() is None
        state = ControllerState(FailoverMode.FAILED_OVER, "secondary", None, T, T)
        await store.save_state(state)
        await store.save_state(state)                                        # upsert of the singleton
        assert (await store.load_state()) == state
        eid = await store.record_event(FailoverEvent(None, T, "failover", "primary", "secondary", "r",
                                                     "awaiting_approval", {}))
        assert await store.update_event(eid, action_status="executed", action_detail={"scaled": [1]},
                                        decided_by="approved by ops")
        ev = await store.get_event(eid)
        assert (ev.action_status, ev.action_detail, ev.decided_by) == ("executed", {"scaled": [1]}, "approved by ops")
        assert [e.id for e in await store.list_events()] == [eid]
        assert await store.get_event(__import__("uuid").uuid4()) is None


def test_failover_api_status_approve_reject():
    from app.cluster_registry import ClusterRegistry
    from app.composition import Container
    from app.domain.models.cluster import ClusterRole
    from tests.integration.test_api import FakeStoragePort
    from tests.unit.test_v2_failover_service import MemStore, RecordingActuator

    from app.domain.failover_controller import FailoverPolicy
    from app.failover_service import FailoverService, FailoverSettings
    from tests.helpers_v1 import InMemoryMetricsPort

    reg = ClusterRegistry({"primary": ClusterRole.PRIMARY, "secondary": ClusterRole.SECONDARY},
                          {"primary": InMemoryMetricsPort("primary", {}), "secondary": InMemoryMetricsPort("secondary", {})})
    store = MemStore()
    svc = FailoverService(reg, store, {"secondary": RecordingActuator({})},
                          FailoverSettings("manual", FailoverPolicy(60, 120), frozenset({"x"})))
    from app.adapters.inbound.api.main import create_app

    app = create_app()
    app.state.container = Container(registry=reg, storage=FakeStoragePort(), leader=AlwaysLeader(), failover=svc)
    client = TestClient(app, raise_server_exceptions=False)

    body = client.get("/api/failover").json()
    assert body["mode"] == "manual" and body["policy"]["failure_threshold_seconds"] == 60

    import asyncio
    eid = asyncio.run(store.record_event(FailoverEvent(None, T, "failover", "primary", "secondary", "r",
                                                       "awaiting_approval", {})))
    r = client.post(f"/api/failover/events/{eid}/approve", json={"approver": "ops"})
    assert r.status_code == 200 and r.json()["action_status"] == "executed"
    assert client.post(f"/api/failover/events/{eid}/reject", json={"approver": "ops"}).status_code == 409
    assert client.post(f"/api/failover/events/{__import__('uuid').uuid4()}/approve",
                       json={"approver": "ops"}).status_code == 404

    app2 = create_app()
    app2.state.container = Container(registry=reg, storage=FakeStoragePort())
    assert TestClient(app2).get("/api/failover").status_code == 404      # single cluster / not configured


@pytest.mark.asyncio
async def test_always_leader_and_dsn():
    leader = AlwaysLeader()
    assert await leader.try_acquire() and leader.is_leader
    await leader.release()
    assert _asyncpg_dsn("postgresql+asyncpg://u@h/db") == "postgresql://u@h/db"


@pytest.mark.asyncio
async def test_leader_try_acquire_is_serialized_across_loops(monkeypatch):
    """V3: concurrent callers must not run two operations on the same asyncpg connection."""
    import asyncio

    from app.adapters.outbound import postgres_leader

    class Conn:
        def __init__(self):
            self.busy, self.overlaps, self.connects = False, 0, 0

        def is_closed(self):
            return False

        async def fetchval(self, *a):
            if self.busy:
                self.overlaps += 1
            self.busy = True
            await asyncio.sleep(0.01)
            self.busy = False
            return True

        async def execute(self, *a):
            return await self.fetchval()

    conn = Conn()

    async def connect(*a, **k):
        conn.connects += 1
        return conn
    monkeypatch.setattr(postgres_leader.asyncpg, "connect", connect)
    leader = postgres_leader.PostgresAdvisoryLeader("postgresql://x")
    results = await asyncio.gather(*(leader.try_acquire() for _ in range(5)))
    assert all(results) and conn.overlaps == 0 and conn.connects == 1
