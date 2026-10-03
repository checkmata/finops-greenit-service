"""
History router (V1) — cost / energy / CO2 / waste / savings over time.

Endpoints:
- GET /api/history/timeseries   aggregated series by cluster / namespace / workload
- GET /api/history/workloads    raw per-workload cost snapshots

Reads TimescaleDB only. Aggregation rules (average samples of a series inside
a bucket, sum across series) live in app/domain/history.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.adapters.inbound.api.dependencies import get_storage_port
from app.domain.history import GROUP_KEYS, NAMESPACE_METRICS, ROLLUP_KEYS, WORKLOAD_METRICS, aggregate, rollup
from app.ports.storage_port import StoragePort

router = APIRouter(prefix="/api/history", tags=["history"])


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


@router.get("/timeseries", summary="Aggregated history", response_model=None,
            responses={400: {"description": "Invalid metric / group_by"}})
async def timeseries(
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    metric: Annotated[str, Query(description=f"namespace scope: {sorted(NAMESPACE_METRICS)}; workload scope: {sorted(WORKLOAD_METRICS)}")] = "cost",
    scope: Annotated[Literal["namespace", "workload"], Query()] = "namespace",
    group_by: Annotated[str, Query(description=f"comma list of {list(GROUP_KEYS)}")] = "cluster",
    cluster: str | None = None,
    namespace: str | None = None,
    workload: str | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
) -> dict:
    start, end = _window(days)
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    if scope == "namespace":
        rows = await storage.query_snapshots(start=start, end=end, cluster=cluster, namespace=namespace)
    else:
        rows = await storage.query_workload_snapshots(
            start=start, end=end, cluster=cluster, namespace=namespace, workload=workload
        )
    try:
        series = aggregate(rows, metric=metric, group_by=groups, bucket_seconds=bucket_minutes * 60)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "metric": metric,
        "scope": scope,
        "group_by": groups,
        "bucket_minutes": bucket_minutes,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series": [
            {"group": s.group, "points": [{"time": p.time.isoformat(), "value": p.value} for p in s.points]}
            for s in series
        ],
    }


@router.get("/workloads", summary="Raw per-workload cost snapshots", response_model=None)
async def workload_history(
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    cluster: str | None = None,
    namespace: str | None = None,
    workload: str | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> dict:
    start, end = _window(days)
    rows = await storage.query_workload_snapshots(
        start=start, end=end, cluster=cluster, namespace=namespace, workload=workload
    )
    rows = list(rows)[-limit:]
    return {
        "count": len(rows),
        "snapshots": [
            {**{k: getattr(r, k) for k in r.__dataclass_fields__}, "time": r.time.isoformat()}
            for r in rows
        ],
    }


@router.get("/aggregate", summary="Long-term history (continuous aggregates)", response_model=None,
            responses={400: {"description": "Invalid scope / granularity / group_by"}})
async def aggregate_history(
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    granularity: Annotated[Literal["hour", "day", "week", "month"], Query()] = "day",
    scope: Annotated[Literal["namespace", "workload"], Query()] = "namespace",
    group_by: Annotated[str, Query(description=f"comma list of {list(ROLLUP_KEYS)}")] = "cluster",
    cluster: str | None = None,
    namespace: str | None = None,
    workload: str | None = None,
    days: Annotated[int, Query(ge=1, le=730)] = 30,
) -> dict:
    """
    V2 — cost / CPU / memory / storage / energy / CO2 per hour, day, week or month,
    compared by cluster, namespace or workload (e.g. primary vs secondary,
    banking vs trading vs ecommerce). Energy/CO2 are exact sums of per-cycle
    measurements; costs are averages of monthly-rate estimates.
    """
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    if "workload" in groups and scope != "workload":
        raise HTTPException(status_code=400, detail="group_by=workload needs scope=workload")
    start, end = _window(days)
    rows = await storage.query_aggregates(scope=scope, granularity=granularity, start=start, end=end,
                                          cluster=cluster, namespace=namespace, workload=workload)
    try:
        points = rollup(rows, groups)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "granularity": granularity, "scope": scope, "group_by": groups,
        "start": start.isoformat(), "end": end.isoformat(),
        "points": [{"bucket": p.bucket.isoformat(), **p.group, **p.values} for p in points],
    }
