"""
V3 ML endpoints — READ-ONLY (computed from the stored history on request; the
persisted ML recommendations are written only by the leader's hourly ML loop).

    GET /api/ml/anomalies?cluster=primary&hours=6       anomaly episodes, all workloads
    GET /api/ml/workloads/{cluster}/{namespace}/{workload}
        anomalies + validated forecasts (per metric) + right-sizing proposals
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/ml", tags=["ml"])


def _ml(request: Request):
    container = getattr(request.app.state, "container", None)
    if container is None or container.ml is None:
        raise HTTPException(status_code=404, detail="ML not configured")
    return container.ml


@router.get("/anomalies", summary="Anomaly episodes (robust rolling z-score) over the recent history")
async def anomalies(request: Request, cluster: str = Query(...), hours: float = Query(6, gt=0, le=168)) -> dict:
    eps = await _ml(request).anomalies(cluster, since_hours=hours)
    return {"cluster": cluster, "since_hours": hours, "count": len(eps),
            "episodes": [asdict(e) | {"start": e.start.isoformat(), "end": e.end.isoformat()} for e in eps]}


@router.get("/workloads/{cluster}/{namespace}/{workload}",
            summary="Anomalies, validated forecasts and right-sizing for one workload")
async def workload_analysis(request: Request, cluster: str, namespace: str, workload: str) -> dict:
    a = await _ml(request).analyze_workload(cluster, namespace, workload)
    if a is None:
        raise HTTPException(status_code=404, detail=f"no history for {cluster}/{namespace}/{workload}")
    return a.to_dict()
