"""
Pydantic schemas for FinOps metrics responses.

WHY a separate Pydantic layer (not returning domain dataclasses directly):
- Pydantic provides automatic JSON serialization for FastAPI responses
- Pydantic generates OpenAPI docs (Swagger UI) with field descriptions
- Pydantic validates incoming request bodies (for future POST endpoints)
- Domain dataclasses have no JSON schema — FastAPI can't generate docs from them
- Pydantic aliases allow renaming fields for API consumers (e.g., camelCase)

WHAT these schemas mirror:
- PodMetricResponse mirrors PodMetric (domain dataclass)
- ClusterAllocatableResponse mirrors ClusterAllocatable
- StorageMetricResponse mirrors StorageMetric

WHY mapper functions (not inheritance):
- Domain dataclasses are frozen, Pydantic models are mutable
- Domain has computed properties (cpu_utilization_pct) — API shouldn't expose internals
- Keeps the boundary explicit: domain → adapter → API (never API → domain)
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models.pod_metric import PodMetric, StorageMetric


# =============================================================================
# Request schemas (for future POST endpoints)
# =============================================================================


class MetricsQueryRequest(BaseModel):
    """Request schema for querying metrics by namespace.

    Used by GET /api/v1/metrics?namespace=team-alpha
    """

    namespace: str = Field(
        ...,
        description="Kubernetes namespace to query",
        examples=["team-alpha"],
    )


# =============================================================================
# Response schemas — PodMetric
# =============================================================================


class PodMetricResponse(BaseModel):
    """Pydantic response schema for pod-level resource metrics.

    Mirrors domain PodMetric dataclass. Computed properties (cpu_utilization_pct,
    is_overprovisioned) are included as explicit fields — the API decides what to
    expose, not the domain engine.
    """

    model_config = ConfigDict(from_attributes=True)

    namespace: str = Field(..., description="Kubernetes namespace")
    team: str = Field(..., description="Team attribution label")
    cost_center: str = Field(..., description="Cost center attribution label")
    environment: str = Field(..., description="Environment (production, staging, etc.)")

    cpu_core_hours: float = Field(
        ...,
        description="CPU usage in core-hours (rate over 30 days)",
        ge=0.0,
    )
    memory_bytes: float = Field(
        ...,
        description="Memory usage in bytes (instant)",
        ge=0.0,
    )
    cpu_requested: float = Field(
        ...,
        description="Requested CPU in cores",
        ge=0.0,
    )
    memory_requested: float = Field(
        ...,
        description="Requested memory in bytes",
        ge=0.0,
    )

    # Computed fields (exposed explicitly, not as domain properties)
    cpu_utilization_pct: float = Field(
        ...,
        description="CPU utilization as % of requested (100% = fully utilized)",
        ge=0.0,
    )
    memory_utilization_pct: float = Field(
        ...,
        description="Memory utilization as % of requested",
        ge=0.0,
    )
    is_overprovisioned_cpu: bool = Field(
        ...,
        description="True if CPU usage < 50% of requested (waste)",
    )
    is_overprovisioned_memory: bool = Field(
        ...,
        description="True if memory usage < 50% of requested (waste)",
    )

    timestamp: datetime = Field(..., description="Prometheus query timestamp (UTC)")


# =============================================================================
# Response schemas — StorageMetric
# =============================================================================


class StorageMetricResponse(BaseModel):
    """Pydantic response schema for PVC storage metrics."""

    model_config = ConfigDict(from_attributes=True)

    namespace: str = Field(..., description="Kubernetes namespace")
    pvc_name: str = Field(..., description="PersistentVolumeClaim name")
    used_bytes: float = Field(..., description="Used storage in bytes", ge=0.0)
    capacity_bytes: float = Field(..., description="Total capacity in bytes", ge=0.0)
    is_bound: bool = Field(..., description="True if PVC is Bound to a PV")
    usage_pct: float = Field(
        ...,
        description="Usage as % of capacity",
        ge=0.0,
        le=100.0,
    )
    timestamp: datetime = Field(..., description="Prometheus query timestamp (UTC)")


# =============================================================================
# Response schemas — namespace summary
# =============================================================================


class NamespaceMetricsSummary(BaseModel):
    """Aggregated metrics summary for a namespace.

    Returned by GET /api/v1/metrics/{namespace}
    """

    namespace: str = Field(..., description="Kubernetes namespace")
    pod_count: int = Field(..., description="Number of pods in namespace", ge=0)
    total_cpu_core_hours: float = Field(..., description="Total CPU usage in core-hours", ge=0.0)
    total_memory_bytes: float = Field(..., description="Total memory usage in bytes", ge=0.0)
    total_cost_usd: float = Field(..., description="Estimated monthly cost in USD", ge=0.0)
    pvc_count: int = Field(..., description="Number of PVCs in namespace", ge=0)
    timestamp: datetime = Field(..., description="Data timestamp (UTC)")


# =============================================================================
# Mapper functions — domain → Pydantic
# =============================================================================


def pod_metric_to_response(pod: PodMetric) -> PodMetricResponse:
    """Convert a domain PodMetric dataclass to a Pydantic response DTO.

    WHY explicit mapper (not Pydantic's from_attributes):
    - Domain has no Pydantic integration (intentional)
    - Mapper makes the conversion boundary visible and testable
    - We compute utilization % here (API layer decides what to expose)
    """
    return PodMetricResponse(
        namespace=pod.namespace,
        team=pod.team,
        cost_center=pod.cost_center,
        environment=pod.environment,
        cpu_core_hours=pod.cpu_core_hours,
        memory_bytes=pod.memory_bytes,
        cpu_requested=pod.cpu_requested,
        memory_requested=pod.memory_requested,
        cpu_utilization_pct=pod.cpu_utilization_pct,
        memory_utilization_pct=pod.memory_utilization_pct,
        is_overprovisioned_cpu=pod.is_overprovisioned_cpu,
        is_overprovisioned_memory=pod.is_overprovisioned_memory,
        timestamp=pod.timestamp,
    )


def storage_metric_to_response(storage: StorageMetric) -> StorageMetricResponse:
    """Convert a domain StorageMetric dataclass to a Pydantic response DTO."""
    return StorageMetricResponse(
        namespace=storage.namespace,
        pvc_name=storage.pvc_name,
        used_bytes=storage.used_bytes,
        capacity_bytes=storage.capacity_bytes,
        is_bound=storage.is_bound,
        usage_pct=storage.usage_pct,
        timestamp=storage.timestamp,
    )