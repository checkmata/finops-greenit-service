"""
Green IT router — energy and carbon metrics.

Endpoints:
- GET /api/greenit/namespace/{namespace}?cluster=  → current state (live Prometheus/Kepler)
- GET /api/greenit/namespace/{namespace}/history    → stored snapshots (TimescaleDB)

V1 changes:
- Energy comes with its provenance (container / pod / node_cpu_share / unavailable).
  A node-level fallback is labelled "estimated"; nothing attributable → 404,
  never a fabricated 0 kWh.
- Idle energy is null when not measurable (the MVP returned 0.0).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.inbound.api.dependencies import get_metrics_port, get_storage_port
from app.adapters.inbound.api.schemas.greenit_schemas import (
    carbon_footprint_to_response,
    energy_breakdown_to_response,
)
from app.adapters.inbound.api.schemas.recommendation_schemas import snapshot_history_to_response
from app.adapters.outbound.prometheus_adapter import (
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.config import settings
from app.domain.greenit_engine import build_energy_breakdown, carbon_footprint_from_measurement
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

__all__ = ["router", "get_metrics_port", "get_storage_port"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/greenit", tags=["greenit"])


@router.get(
    "/namespace/{namespace}",
    summary="Get current Green IT metrics for a namespace",
    description="Live energy from Kepler (with provenance) and the derived carbon footprint.",
    response_model=None,
    responses={404: {"description": "No attributable energy"}, 503: {"description": "Prometheus unreachable"}},
)
async def get_namespace_carbon(
    namespace: str,
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
) -> dict:
    try:
        energy = await metrics.get_energy(namespace=namespace)
    except PrometheusUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Prometheus is unavailable: {exc}") from exc
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=404, detail=f"No energy metrics found for namespace '{namespace}': {exc}") from exc

    if not energy.is_available:
        raise HTTPException(
            status_code=404,
            detail=f"No energy series attributable to namespace '{namespace}' (Kepler missing or unsupported labels)",
        )

    try:
        pod_metrics = await metrics.get_pod_metrics(namespace=namespace)
    except Exception:
        pod_metrics = []
    first = pod_metrics[0] if pod_metrics else None

    carbon = carbon_footprint_from_measurement(
        energy,
        team=first.team if first else "unknown",
        cost_center=first.cost_center if first else "unknown",
        environment=first.environment if first else "unknown",
        pue=settings.PUE_CONSTANT,
        grid_intensity_gco2_per_kwh=settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH,
    )
    breakdown = build_energy_breakdown(namespace, energy.total_kwh or 0.0, energy.idle_kwh)

    return {
        "namespace": namespace,
        "cluster": first.cluster if first else None,
        "carbon_footprint": carbon_footprint_to_response(carbon).model_dump(),
        "energy_breakdown": energy_breakdown_to_response(breakdown).model_dump(),
        "energy_source": {
            "attribution": energy.attribution.value,
            # V2 fix: precision depends on attribution AND power source (a
            # container-level figure from Kepler's power MODEL is an estimate).
            "precision": energy.precision,
            "power_source": energy.power_source.value,
            "scope": energy.scope,
            "source_metric": energy.source_metric,
            "window_hours": energy.window_hours,
            "idle_energy_measured": energy.idle_kwh is not None,
            "pue": settings.PUE_CONSTANT,
            "grid_intensity_gco2_per_kwh": settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH,
        },
    }


@router.get(
    "/namespace/{namespace}/history",
    summary="Get historical Green IT metrics for a namespace",
    description="Stored snapshots from TimescaleDB (does NOT query Prometheus).",
    response_model=None,
    responses={404: {"description": "No snapshots found for this namespace"}},
)
async def get_namespace_history(
    namespace: str,
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    days: Annotated[int, Query(ge=1, le=90, description="Number of days of history")] = 7,
) -> dict:
    snapshots = await storage.get_snapshot_history(namespace=namespace, days=days)
    if not snapshots:
        raise HTTPException(
            status_code=404,
            detail=f"No historical snapshots found for namespace '{namespace}' (last {days} days)",
        )
    return snapshot_history_to_response(namespace=namespace, days=days, snapshots=snapshots).model_dump()


@router.get(
    "/namespace/{namespace}/workloads",
    summary="Energy and CO2 per workload and per pod (live)",
    response_model=None,
    responses={404: {"description": "No per-container energy for this namespace"}},
)
async def get_workload_carbon(
    namespace: str,
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
    window_seconds: Annotated[int, Query(ge=60, le=7 * 86400)] = 86400,
) -> dict:
    """V2 — cluster → namespace → workload → pod. Each figure says measured/estimated."""
    from app.domain.greenit_engine import calculate_carbon_footprint

    try:
        items = await metrics.get_workload_energy(namespace, window_seconds=window_seconds)
    except PrometheusUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Prometheus is unavailable: {exc}") from exc
    if not items:
        raise HTTPException(status_code=404, detail=f"No per-container energy for namespace '{namespace}'")

    def co2(kwh: float) -> float:
        return round(calculate_carbon_footprint(kwh, settings.PUE_CONSTANT,
                                                settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH), 9)

    return {
        "namespace": namespace,
        "window_seconds": window_seconds,
        "workloads": [
            {
                "workload": w.workload,
                "energy_kwh": round(w.kwh, 9) if w.kwh is not None else None,
                "co2_kg": co2(w.kwh) if w.kwh is not None else None,
                "energy_source": w.precision,           # measured | estimated | unavailable
                "power_source": w.power_source.value,
                "scope": w.scope,
                "pods": [{"pod": pod, "energy_kwh": round(k, 9), "co2_kg": co2(k)} for pod, k in w.pods],
            }
            for w in items
        ],
    }
