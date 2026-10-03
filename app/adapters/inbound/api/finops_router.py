"""
FinOps router — cost and resource metrics.

Endpoints:
- GET /api/finops/namespace/{namespace}?cluster=  → current state (live Prometheus)
- GET /api/finops/namespace/{namespace}/history    → stored snapshots (TimescaleDB)

V1 changes:
- `?cluster=` selects which cluster's Prometheus is queried (primary by default).
- Storage (PVC) cost is included when kubelet stats exist; `storage_status`
  says when it is not ("unavailable" ≠ "free").
- Waste is decomposed per resource (cpu/memory/storage) and per workload.
- Adapters are shared instances (app.composition), not created per request.

Reads only — never writes (writes belong to the scheduler).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.inbound.api.dependencies import get_metrics_port, get_storage_port
from app.adapters.inbound.api.schemas.finops_schemas import pod_metric_to_response
from app.adapters.inbound.api.schemas.recommendation_schemas import snapshot_history_to_response
from app.adapters.outbound.prometheus_adapter import (
    PrometheusQueryError,
    PrometheusUnavailableError,
)
from app.config import settings
from app.domain.finops_engine import (
    CostBreakdown,
    calculate_monthly_cost,
    calculate_requested_cost,
    summarize_waste,
)
from app.domain.models.pod_metric import PodMetric, StorageMetric
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

__all__ = ["router", "get_metrics_port", "get_storage_port"]  # re-exported for test overrides

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/finops", tags=["finops"])


def _prices() -> dict[str, float]:
    return dict(
        cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD,
        cost_per_gb_hour=settings.COST_PER_GB_HOUR_USD,
        cost_per_gb_month=settings.COST_PER_GB_MONTH_USD,
    )


def _components(c: CostBreakdown) -> dict[str, float]:
    return {"cpu": c.cpu_cost_usd, "memory": c.memory_cost_usd,
            "storage": c.storage_cost_usd, "total": c.total_cost_usd}


def _workload_view(w: PodMetric) -> dict:
    req = calculate_requested_cost(w, **_prices())
    act = calculate_monthly_cost(w, **_prices())
    waste = summarize_waste(w, req, act)
    return {
        "workload": w.workload,
        "team": w.team,
        "cpu_cores_used": round(w.cpu_core_hours, 4),
        "cpu_cores_requested": w.cpu_requested,
        "cpu_utilization_pct": round(w.cpu_utilization_pct, 1),
        "memory_utilization_pct": round(w.memory_utilization_pct, 1),
        "requested_cost_usd": req.total_cost_usd,
        "actual_cost_usd": act.total_cost_usd,
        "waste_cost_usd": waste.wasted_cost_usd,
    }


@router.get(
    "/namespace/{namespace}",
    summary="Get current FinOps metrics for a namespace",
    description="Live metrics from the selected cluster's Prometheus + cost estimates.",
    response_model=None,
    responses={404: {"description": "No metrics / unknown cluster"}, 503: {"description": "Prometheus unreachable"}},
)
async def get_namespace_metrics(
    namespace: str,
    metrics: Annotated[MetricsPort, Depends(get_metrics_port)],
) -> dict:
    try:
        pod_metrics = await metrics.get_pod_metrics(namespace=namespace)
    except PrometheusUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Prometheus is unavailable: {exc}") from exc
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=404, detail=f"No metrics found for namespace '{namespace}': {exc}") from exc

    if not pod_metrics:
        raise HTTPException(status_code=404, detail=f"No pods found with metrics for namespace '{namespace}'")
    pod = pod_metrics[0]

    # Storage: None = unavailable → not priced (never an invented 0).
    storage: list[StorageMetric] | None
    try:
        raw = await metrics.get_storage_metrics(namespace=namespace)
        storage = list(raw) if isinstance(raw, list) else None
    except Exception as exc:
        logger.info("storage metrics unavailable for %s: %s", namespace, exc)
        storage = None

    try:
        workloads = await metrics.get_workload_metrics(namespace, namespace_total=pod)
    except Exception as exc:
        logger.info("workload breakdown unavailable for %s: %s", namespace, exc)
        workloads = []

    requested = calculate_requested_cost(pod, storage_metrics=storage, **_prices())
    actual = calculate_monthly_cost(pod, storage_metrics=storage, **_prices())
    waste = summarize_waste(pod, requested, actual)

    return {
        "namespace": namespace,
        "cluster": pod.cluster,
        "pod_count": len(pod_metrics),
        "pods": [pod_metric_to_response(pod).model_dump()],
        "requested_cost_usd": requested.total_cost_usd,
        "actual_cost_usd": actual.total_cost_usd,
        # MVP field kept; V1 value = gross waste (Σ per-resource reserved-but-unused).
        "cost_savings_usd": waste.wasted_cost_usd,
        "cost_breakdown": {
            "requested": _components(requested),
            "actual": _components(actual),
            "waste": {
                "cpu": waste.cpu_waste_usd,
                "memory": waste.memory_waste_usd,
                "storage": waste.storage_waste_usd,
                "total": waste.wasted_cost_usd,
                "ratio": waste.waste_ratio,
            },
        },
        "storage_status": requested.storage_status.value,
        "workloads": [_workload_view(w) for w in workloads],
    }


@router.get(
    "/namespace/{namespace}/history",
    summary="Get historical FinOps metrics for a namespace",
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
