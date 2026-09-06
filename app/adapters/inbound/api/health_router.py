"""
Health router — FastAPI endpoints for liveness/readiness probes.

Endpoints:
- GET /health → comprehensive health check (Prometheus + Database)

WHY a dedicated health router:
- Kubernetes/OpenShift probes need a simple endpoint to check service health
- Different components can fail independently (Prometheus down vs DB down)
- Operators need to know WHICH dependency failed, not just "something is wrong"
- The health endpoint must NOT depend on the domain layer (it's infrastructure)

WHY check both Prometheus and Database:
- If Prometheus is down: the service can't fetch live metrics (read-only failure)
- If Database is down: the service can't store/retrieve historical data
- Both are critical for the service to function correctly
- Returning which one failed helps operators diagnose quickly
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends

from app.adapters.outbound.prometheus_adapter import (
    PrometheusMetricsAdapter,
    PrometheusUnavailableError,
)
from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.config import settings
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


# =============================================================================
# Dependency injection
# =============================================================================


def get_metrics_port() -> MetricsPort:
    """FastAPI dependency: returns the Prometheus metrics adapter."""
    return PrometheusMetricsAdapter(
        prometheus_url=settings.PROMETHEUS_URL,
        timeout_seconds=settings.PROMETHEUS_QUERY_TIMEOUT_SECONDS,
    )


def get_storage_port() -> StoragePort:
    """FastAPI dependency: returns the TimescaleDB storage adapter."""
    return TimescaleAdapter(database_url=settings.DATABASE_URL)


# =============================================================================
# Health check logic
# =============================================================================


async def check_prometheus_health(metrics: MetricsPort) -> dict:
    """Check if Prometheus is reachable.

    Returns:
        Dict with status ("healthy" or "unhealthy") and details.
    """
    try:
        # Try a lightweight query to check connectivity
        # We query a simple metric that should always exist
        await metrics.check_data_quality()
        return {
            "status": "healthy",
            "component": "prometheus",
            "url": settings.PROMETHEUS_URL,
        }
    except PrometheusUnavailableError as exc:
        return {
            "status": "unhealthy",
            "component": "prometheus",
            "url": settings.PROMETHEUS_URL,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "component": "prometheus",
            "url": settings.PROMETHEUS_URL,
            "error": f"Unexpected error: {exc}",
        }


async def check_database_health(storage: StoragePort) -> dict:
    """Check if the database is reachable.

    Returns:
        Dict with status ("healthy" or "unhealthy") and details.
    """
    try:
        # Try a lightweight query to check connectivity
        # We list recommendations with limit=1 to verify DB is accessible
        await storage.list_recommendations(limit=1)
        return {
            "status": "healthy",
            "component": "database",
            "url": settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else "configured",
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "component": "database",
            "url": settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else "configured",
            "error": str(exc),
        }


# =============================================================================
# GET /health — comprehensive health check
# =============================================================================


@router.get(
    "/health",
    summary="Health check for liveness/readiness probes",
    description="Checks connectivity to Prometheus and TimescaleDB. Returns which dependencies are healthy/unhealthy.",
    response_model=None,
    responses={
        200: {"description": "All dependencies healthy"},
        503: {"description": "One or more dependencies unhealthy"},
    },
)
async def health_check(
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
    storage: Annotated[StoragePort, Depends(get_storage_port)],
) -> dict:
    """Health check endpoint for Kubernetes/OpenShift probes.

    Checks connectivity to:
    1. Prometheus (metrics source)
    2. TimescaleDB (storage backend)

    Returns detailed status for each dependency so operators can:
    - See which component failed (Prometheus vs Database)
    - Understand why it failed (error message)
    - Make informed decisions about service readiness

    Args:
        metrics: MetricsPort dependency (injected by FastAPI)
        storage: StoragePort dependency (injected by FastAPI)

    Returns:
        JSON response with overall status and per-component details.
    """
    start_time = time.time()

    # Check both dependencies concurrently
    import asyncio
    prometheus_check, database_check = await asyncio.gather(
        check_prometheus_health(metrics),
        check_database_health(storage),
    )

    elapsed_ms = (time.time() - start_time) * 1000

    # Determine overall status
    all_healthy = (
        prometheus_check["status"] == "healthy"
        and database_check["status"] == "healthy"
    )

    response = {
        "status": "healthy" if all_healthy else "unhealthy",
        "timestamp": time.time(),
        "elapsed_ms": round(elapsed_ms, 2),
        "components": {
            "prometheus": prometheus_check,
            "database": database_check,
        },
    }

    # Return 503 if any component is unhealthy
    from fastapi.responses import JSONResponse

    status_code = 200 if all_healthy else 503
    return JSONResponse(content=response, status_code=status_code)