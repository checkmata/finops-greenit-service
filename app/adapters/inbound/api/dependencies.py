"""
Shared FastAPI dependencies (V1).

Routers depend on PORTS; these functions fetch the shared instances built by
app.composition at startup (request.app.state.container). Tests override them
with `app.dependency_overrides[get_metrics_port] = ...` exactly as before —
each router module re-exports these names for backward compatibility.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Query, Request

from app.cluster_registry import ClusterRegistry, UnknownClusterError
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

ClusterParam = Annotated[
    str | None,
    Query(description="Cluster name (default: the primary cluster). See GET /api/clusters"),
]


def _container(request: Request):
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="Service not initialised (no adapters configured)")
    return container


def get_cluster_registry(request: Request) -> ClusterRegistry:
    return _container(request).registry


def get_metrics_port(request: Request, cluster: ClusterParam = None) -> MetricsPort:
    """MetricsPort of `?cluster=` (primary by default). 404 on unknown cluster."""
    try:
        return _container(request).registry.port(cluster)
    except UnknownClusterError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown cluster '{cluster}'") from exc


def get_storage_port(request: Request) -> StoragePort:
    return _container(request).storage
