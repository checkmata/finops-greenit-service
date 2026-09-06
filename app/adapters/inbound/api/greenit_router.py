"""
Green IT router — FastAPI endpoints for energy and carbon metrics.

Endpoints:
- GET /api/greenit/namespace/{namespace}  → current state (Prometheus)
- GET /api/greenit/namespace/{namespace}/history → historical snapshots (TimescaleDB)

WHY dependency injection:
- Same reasons as finops_router.py: swappable adapters, testable code.
- The router declares what it needs (MetricsPort, StoragePort) — not which implementation.
- FastAPI's Depends() injects the concrete adapter at runtime.

WHY this is the ONLY place that "wires" ports to adapters:
- The domain engine calls MetricsPort (interface). It never sees PrometheusAdapter.
- The router is the composition root: it imports the concrete adapter and connects it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.config import settings
from app.domain.greenit_engine import build_carbon_footprint, build_energy_breakdown
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/greenit", tags=["greenit"])


# =============================================================================
# Dependency injection — FastAPI's DI system
# =============================================================================


def get_metrics_port() -> MetricsPort:
    """FastAPI dependency: returns the Prometheus metrics adapter.

    WHY a function (not a class):
    - FastAPI's Depends() needs a callable
    - This keeps adapter instantiation lazy (only when endpoint is called)
    - Easy to override in tests: dependency_overrides[get_metrics_port] = mock
    """
    return PrometheusMetricsAdapter(
        prometheus_url=settings.PROMETHEUS_URL,
        timeout_seconds=settings.PROMETHEUS_QUERY_TIMEOUT_SECONDS,
    )


def get_storage_port() -> StoragePort:
    """FastAPI dependency: returns the TimescaleDB storage adapter.

    WHY a function (not a class):
    - Same reasons as get_metrics_port()
    - Keeps DB connection lazy (only when history endpoint is called)
    """
    return TimescaleAdapter(database_url=settings.DATABASE_URL)


# =============================================================================
# GET /api/greenit/namespace/{namespace} — current state from Prometheus
# =============================================================================


@router.get(
    "/namespace/{namespace}",
    summary="Get current Green IT metrics for a namespace",
    description="Fetches live energy consumption from Kepler and computes carbon footprint.",
    response_model=None,
    responses={
        200: {"description": "Current carbon/energy metrics for the namespace"},
        404: {"description": "Namespace not found (no pods with metrics)"},
        503: {"description": "Prometheus is unreachable"},
    },
)
async def get_namespace_carbon(
    namespace: str,
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
) -> dict:
    """Get current Green IT metrics for a Kubernetes namespace.

    This endpoint queries Prometheus LIVE — it does NOT read from TimescaleDB.
    The response includes carbon footprint computed by greenit_engine.

    Args:
        namespace: Kubernetes namespace (e.g., "team-alpha")
        metrics: MetricsPort dependency (injected by FastAPI)

    Returns:
        JSON response with energy consumption and carbon footprint.

    Raises:
        404: No pods found with metrics for this namespace
        503: Prometheus is unreachable
    """
    try:
        energy_kwh = await metrics.get_energy_kwh(namespace=namespace)
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Prometheus is unavailable: {exc}",
        ) from exc
    except PrometheusQueryError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No energy metrics found for namespace '{namespace}': {exc}",
        ) from exc

    # Get pod metrics for attribution (team, cost_center, environment)
    try:
        pod_metrics = await metrics.get_pod_metrics(namespace=namespace)
    except Exception:
        pod_metrics = []

    first_pod = pod_metrics[0] if pod_metrics else None
    team = first_pod.team if first_pod else "unknown"
    cost_center = first_pod.cost_center if first_pod else "unknown"
    environment = first_pod.environment if first_pod else "unknown"

    # Build domain objects using greenit_engine (pure functions)
    carbon_footprint = build_carbon_footprint(
        namespace=namespace,
        team=team,
        cost_center=cost_center,
        environment=environment,
        energy_joules=energy_kwh * 3_600_000,  # Convert kWh back to joules for builder
        pue=settings.PUE_CONSTANT,
        grid_intensity_gco2_per_kwh=settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH,
    )

    energy_breakdown = build_energy_breakdown(
        namespace=namespace,
        total_energy_kwh=energy_kwh,
        idle_energy_kwh=0.0,  # P1 metric — not queried yet
    )

    # Map domain → Pydantic response
    from app.adapters.inbound.api.schemas.greenit_schemas import (
        carbon_footprint_to_response,
        energy_breakdown_to_response,
    )

    cf_response = carbon_footprint_to_response(carbon_footprint)
    eb_response = energy_breakdown_to_response(energy_breakdown)

    return {
        "namespace": namespace,
        "carbon_footprint": cf_response.model_dump(),
        "energy_breakdown": eb_response.model_dump(),
    }


# =============================================================================
# GET /api/greenit/namespace/{namespace}/history — historical from TimescaleDB
# =============================================================================


@router.get(
    "/namespace/{namespace}/history",
    summary="Get historical Green IT metrics for a namespace",
    description="Retrieves stored metric snapshots from TimescaleDB (does NOT query Prometheus).",
    response_model=None,
    responses={
        200: {"description": "Historical snapshots for the namespace"},
        404: {"description": "No snapshots found for this namespace"},
    },
)
async def get_namespace_history(
    namespace: str,
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    days: Annotated[int, Query(ge=1, le=90, description="Number of days of history")] = 7,
) -> dict:
    """Get historical metric snapshots for a namespace.

    This endpoint reads from TimescaleDB ONLY — it does NOT query Prometheus.
    Data is written by the snapshot_job.py scheduler on a timer (every 5 minutes).

    WHY this is a different data path than the "current" endpoint:
    - "Current" endpoint → Prometheus (live metrics, no storage)
    - "History" endpoint → TimescaleDB (stored snapshots, no Prometheus)
    - They share the same router file but use completely different backends.
    - This is intentional: the router is the composition root, not a data source.

    Args:
        namespace: Kubernetes namespace (e.g., "team-alpha")
        storage: StoragePort dependency (injected by FastAPI)
        days: Number of days of history (1-90, default 7)

    Returns:
        JSON response with historical snapshot data points.

    Raises:
        404: No snapshots found for this namespace in the requested time range
    """
    snapshots = await storage.get_snapshot_history(namespace=namespace, days=days)

    if not snapshots:
        raise HTTPException(
            status_code=404,
            detail=f"No historical snapshots found for namespace '{namespace}' (last {days} days)",
        )

    # Map domain → Pydantic response
    from app.adapters.inbound.api.schemas.recommendation_schemas import snapshot_history_to_response

    response = snapshot_history_to_response(namespace=namespace, days=days, snapshots=snapshots)
    return response.model_dump()