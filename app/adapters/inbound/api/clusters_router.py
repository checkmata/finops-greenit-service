"""
Clusters router (V1) — multicluster view and failover DECISION.

Endpoints:
- GET /api/clusters                         health of every observed cluster + failover plan
- PUT /api/clusters/{name}/availability     operator declares a cluster (un)available

What this is NOT: it does not move traffic, scale, or touch any cluster.
It observes (each cluster's own Prometheus) and computes which cluster SHOULD
serve each workload (app/domain/failover.py). Executing a failover belongs to
fleet tooling (ACM / GitOps / global load balancer) — see docs/FAILOVER.md.
The operator declaration is in-memory in V1 (lost on restart).
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.adapters.inbound.api.dependencies import get_cluster_registry
from app.cluster_registry import ClusterRegistry, UnknownClusterError
from app.domain.failover import plan_failover
from app.domain.models.cluster import FailoverPlan
from app.cluster_observer import observe_cluster

router = APIRouter(prefix="/api/clusters", tags=["clusters"])


class AvailabilityRequest(BaseModel):
    available: bool = Field(..., description="false = declare the cluster unavailable")
    reason: str | None = Field(None, description="e.g. 'maintenance', 'incident INC-42'")


def _plan_to_dict(plan: FailoverPlan, details: dict[str, dict]) -> dict:
    return {
        "active_cluster": plan.active_cluster,
        "failover_active": plan.failover_active,
        "clusters": [
            {
                "name": c.name,
                "role": c.role.value,
                "healthy": c.is_healthy,
                "reachable": c.reachable,
                "kube_state_metrics_up": c.control_plane_metrics_ok,
                "declared_unavailable": c.declared_unavailable,
                "declared_reason": c.declared_reason,
                "workloads": [
                    {"namespace": d.namespace, "workload": d.workload, "desired": d.desired, "available": d.available}
                    for d in c.deployments
                ],
                **details.get(c.name, {}),
            }
            for c in plan.clusters
        ],
        "placements": [
            {"namespace": p.namespace, "workload": p.workload, "status": p.status.value,
             "serving_cluster": p.serving_cluster, "reason": p.reason}
            for p in plan.placements
        ],
        "note": "Decision only — this service does not move traffic. See docs/FAILOVER.md.",
    }


async def _current_plan(registry: ClusterRegistry) -> dict:
    observed = await asyncio.gather(*(observe_cluster(registry, n) for n in registry.names))
    plan = plan_failover([s for s, _ in observed])
    return _plan_to_dict(plan, {s.name: d for s, d in observed})


@router.get("", summary="Clusters health and failover plan", response_model=None)
async def list_clusters(registry: Annotated[ClusterRegistry, Depends(get_cluster_registry)]) -> dict:
    return await _current_plan(registry)


@router.put(
    "/{name}/availability",
    summary="Declare a cluster available/unavailable (operator decision)",
    response_model=None,
    responses={404: {"description": "Unknown cluster"}},
)
async def declare_availability(
    name: str,
    body: AvailabilityRequest,
    registry: Annotated[ClusterRegistry, Depends(get_cluster_registry)],
) -> dict:
    try:
        registry.declare(name, body.available, body.reason)
    except UnknownClusterError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown cluster '{name}'") from exc
    return await _current_plan(registry)
