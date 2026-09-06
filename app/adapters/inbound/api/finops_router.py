"""
FinOps router — FastAPI endpoints for cost and resource metrics.

Endpoints:
- GET /api/finops/namespace/{namespace}  → current state (Prometheus)
- GET /api/finops/namespace/{namespace}/history → historical snapshots (TimescaleDB)

WHY dependency injection:
- The router declares what it needs (MetricsPort, StoragePort) — not which implementation.
- FastAPI's Depends() injects the concrete adapter at runtime.
- Tests can swap the adapter for a mock without changing router code.

WHY this is the ONLY place that "wires" ports to adapters:
- The domain engine calls MetricsPort (interface). It never sees PrometheusAdapter.
- The router is the composition root: it imports the concrete adapter and connects it.
- This is the hexagonal "adapter" boundary: inbound (router) → domain → outbound (adapter).
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
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/finops", tags=["finops"])


# =============================================================================
# Dependency injection — FastAPI's DI system
# =============================================================================
# WHY Depends():
# - FastAPI creates a new adapter instance per request (or per-scope if cached).
# - This keeps the adapter stateless and testable.
# - Tests can override the dependency with a mock/fake adapter.
#
# HOW it works:
# 1. Router declares: metrics: Annotated[MetricsPort, Depends(get_metrics_port)]
# 2. FastAPI calls get_metrics_port() → returns PrometheusMetricsAdapter
# 3. Router receives the adapter as a function parameter
# 4. Tests can override: app.dependency_overrides[get_metrics_port] = MockAdapter()
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
# GET /api/finops/namespace/{namespace} — current state from Prometheus
# =============================================================================


@router.get(
    "/namespace/{namespace}",
    summary="Get current FinOps metrics for a namespace",
    description="Fetches live resource metrics from Prometheus and computes cost estimates.",
    response_model=None,
    responses={
        200: {"description": "Current metrics for the namespace"},
        404: {"description": "Namespace not found (no pods with metrics)"},
        503: {"description": "Prometheus is unreachable"},
    },
)
async def get_namespace_metrics(
    namespace: str,
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
) -> dict:
    """Get current FinOps metrics for a Kubernetes namespace.

    This endpoint queries Prometheus LIVE — it does NOT read from TimescaleDB.
    The response includes cost estimates computed by finops_engine.

    Args:
        namespace: Kubernetes namespace (e.g., "team-alpha")
        metrics: MetricsPort dependency (injected by FastAPI)

    Returns:
        JSON response with pod metrics, cost breakdown, and utilization stats.

    Raises:
        404: No pods found with metrics for this namespace
        503: Prometheus is unreachable
    """
    try:
        pod_metrics = await metrics.get_pod_metrics(namespace=namespace)
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Prometheus is unavailable: {exc}",
        ) from exc
    except PrometheusQueryError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No metrics found for namespace '{namespace}': {exc}",
        ) from exc

    if not pod_metrics:
        raise HTTPException(
            status_code=404,
            detail=f"No pods found with metrics for namespace '{namespace}'",
        )

    # Compute cost estimates using finops_engine (pure function)
    from app.domain.finops_engine import calculate_requested_cost, calculate_monthly_cost

    requested_cost = calculate_requested_cost(
        pod_metrics[0],
        cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD,
        cost_per_gb_hour=settings.COST_PER_GB_HOUR_USD,
        cost_per_gb_month=settings.COST_PER_GB_MONTH_USD,
    )
    actual_cost = calculate_monthly_cost(
        pod_metrics[0],
        cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD,
        cost_per_gb_hour=settings.COST_PER_GB_HOUR_USD,
        cost_per_gb_month=settings.COST_PER_GB_MONTH_USD,
    )

    # Map domain → Pydantic response
    from app.adapters.inbound.api.schemas.finops_schemas import pod_metric_to_response

    pod_response = pod_metric_to_response(pod_metrics[0])

    return {
        "namespace": namespace,
        "pod_count": len(pod_metrics),
        "pods": [pod_response.model_dump()],
        "requested_cost_usd": requested_cost.total_cost_usd,
        "actual_cost_usd": actual_cost.total_cost_usd,
        "cost_savings_usd": requested_cost.total_cost_usd - actual_cost.total_cost_usd,
    }


# =============================================================================
# GET /api/finops/namespace/{namespace}/history — historical from TimescaleDB
# =============================================================================


@router.get(
    "/namespace/{namespace}/history",
    summary="Get historical FinOps metrics for a namespace",
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