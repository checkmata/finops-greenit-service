"""
Health endpoints.

- GET /health/live → liveness: the process answers. Never depends on external
  systems (restarting our pod does not fix a down Prometheus).
- GET /health      → readiness with per-dependency detail.

V1 changes:
- Prometheus is checked with ping() (transport truth). The MVP used
  check_data_quality(), which swallows connection errors → always "healthy".
- Multicluster: every cluster's Prometheus is checked.
    healthy   → DB ok and every cluster reachable                    (200)
    degraded  → DB ok, at least one cluster reachable, some are not   (200)
    unhealthy → DB down, or no cluster reachable                     (503)
  WHY "degraded" is 200: losing ONE observed cluster is exactly the failover
  scenario the service must keep reporting on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.adapters.inbound.api.dependencies import get_cluster_registry, get_metrics_port, get_storage_port
from app.cluster_registry import ClusterRegistry
from app.config import settings
from app.ports.storage_port import StoragePort

__all__ = ["router", "get_metrics_port", "get_storage_port", "get_cluster_registry"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _ping(name: str, port) -> dict:
    try:
        ok = bool(await port.ping())
        return {"cluster": name, "status": "healthy" if ok else "unhealthy",
                **({} if ok else {"error": "Prometheus did not answer"})}
    except Exception as exc:
        return {"cluster": name, "status": "unhealthy", "error": f"Unexpected error: {exc}"}


async def check_prometheus_health(registry: ClusterRegistry) -> dict:
    results = await asyncio.gather(*(_ping(n, p) for n, p in registry.ports().items()))
    healthy = [r for r in results if r["status"] == "healthy"]
    status = "healthy" if len(healthy) == len(results) else ("degraded" if healthy else "unhealthy")
    return {"status": status, "component": "prometheus", "clusters": list(results)}


async def check_database_health(storage: StoragePort) -> dict:
    target = settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else "configured"
    try:
        await storage.list_recommendations(limit=1)
        return {"status": "healthy", "component": "database", "url": target}
    except Exception as exc:
        return {"status": "unhealthy", "component": "database", "url": target, "error": str(exc)}


@router.get("/health/live", summary="Liveness probe", response_model=None)
async def liveness() -> dict:
    return {"status": "alive"}


@router.get(
    "/health/ready",
    summary="Readiness: can the service do its job right now?",
    response_model=None,
    responses={200: {"description": "healthy or degraded"}, 503: {"description": "unhealthy"}},
)
@router.get(
    "/health",
    summary="Readiness (alias of /health/ready, kept for MVP clients)",
    response_model=None,
    responses={200: {"description": "healthy or degraded"}, 503: {"description": "unhealthy"}},
)
async def health_check(
    request: Request,
    registry: Annotated[ClusterRegistry, Depends(get_cluster_registry)],
    storage: Annotated[StoragePort, Depends(get_storage_port)],
) -> JSONResponse:
    start = time.time()
    prometheus, database = await asyncio.gather(
        check_prometheus_health(registry), check_database_health(storage)
    )
    container = getattr(request.app.state, "container", None)
    creds = {r.spec.cluster: r.status()["actuator"] for r in (getattr(container, "credentials", None) or [])}
    creds_ok = all(c["state"] == "valid" for c in creds.values())
    if database["status"] != "healthy" or prometheus["status"] == "unhealthy":
        overall = "unhealthy"
    elif prometheus["status"] == "degraded" or not creds_ok:
        # an actuator without a valid credential cannot fail over: degraded, not unready
        overall = "degraded"
    else:
        overall = "healthy"
    body = {
        "status": overall,
        "timestamp": time.time(),
        "elapsed_ms": round((time.time() - start) * 1000, 2),
        "components": {"prometheus": prometheus, "database": database,
                       **({"actuator_credentials": creds} if creds else {})},
    }
    # V2 HA: which replica writes. A follower is fully READY (it serves the API);
    # it just does not run the scheduler/failover loop.
    container = getattr(request.app.state, "container", None)
    leader = getattr(container, "leader", None)
    if leader is not None:
        from app import observability as obs

        obs.IS_LEADER.set(1 if leader.is_leader else 0)
        body["replica"] = {"is_leader": leader.is_leader}
    return JSONResponse(content=body, status_code=503 if overall == "unhealthy" else 200)
