"""
Pydantic schemas for recommendation responses and historical data.

WHY a separate Pydantic layer (not returning domain dataclasses directly):
- Same reasons as finops/greenit_schemas.py: JSON serialization, OpenAPI docs, validation
- Recommendation dataclass has no JSON schema — FastAPI can't generate docs from it
- Pydantic provides Field descriptions for Swagger UI documentation
- Request schemas validate incoming query parameters (namespace, days, status)

WHAT these schemas cover:
- RecommendationResponse: single recommendation (mirrors domain Recommendation)
- RecommendationSetResponse: list of recommendations with metadata (mirrors RecommendationSet)
- HistoricalSnapshotResponse: time-series data for Grafana/API consumption
- Request schemas: query parameters for filtering recommendations and snapshots
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models.recommendation import (
    Category,
    Recommendation,
    RecommendationSet,
    Severity,
)
from app.ports.storage_port import MetricSnapshot


# =============================================================================
# Request schemas — query parameters
# =============================================================================


class RecommendationQueryRequest(BaseModel):
    """Request schema for querying recommendations.

    Used by GET /api/v1/recommendations?namespace=team-alpha&status=pending
    """

    namespace: str | None = Field(
        None,
        description="Filter by Kubernetes namespace",
        examples=["team-alpha"],
    )
    status: str | None = Field(
        None,
        description="Filter by status: pending, approved, executed, dismissed",
        examples=["pending"],
    )
    limit: int = Field(
        50,
        description="Maximum number of results to return",
        ge=1,
        le=500,
    )
    offset: int = Field(
        0,
        description="Number of results to skip (for pagination)",
        ge=0,
    )


class SnapshotHistoryRequest(BaseModel):
    """Request schema for querying historical snapshots.

    Used by GET /api/v1/metrics/{namespace}/history?days=7
    """

    namespace: str = Field(
        ...,
        description="Kubernetes namespace to query",
        examples=["team-alpha"],
    )
    days: int = Field(
        7,
        description="Number of days of history to retrieve",
        ge=1,
        le=90,
    )


class UpdateRecommendationStatusRequest(BaseModel):
    """Request schema for updating a recommendation's status.

    Used by PATCH /api/v1/recommendations/{id}/status
    """

    status: str = Field(
        ...,
        description="New status: pending, approved, executed, dismissed",
        examples=["approved"],
    )


# =============================================================================
# Response schemas — Recommendation
# =============================================================================


class RecommendationResponse(BaseModel):
    """Pydantic response schema for a single recommendation.

    Mirrors domain Recommendation dataclass. Adds id field (from DB)
    and excludes timestamp (domain-level metadata).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="Recommendation UUID (assigned by database)")
    namespace: str = Field(..., description="Kubernetes namespace")
    category: Category = Field(..., description="Recommendation category")
    severity: Severity = Field(..., description="Urgency level")
    resource: str = Field(
        ...,
        description="Specific resource (pod name, PVC name, or 'cluster')",
    )
    message: str = Field(
        ...,
        description="Human-readable explanation of the recommendation",
    )
    current_value: str = Field(
        ...,
        description="Current value from Prometheus (e.g., '8' cores)",
    )
    recommended_value: str = Field(
        ...,
        description="Engine-suggested value (e.g., '4.8' cores)",
    )
    estimated_savings_usd: float | None = Field(
        None,
        description="Estimated monthly cost savings in USD (if applicable)",
        ge=0.0,
    )
    estimated_savings_kg_co2: float | None = Field(
        None,
        description="Estimated monthly CO2 savings in kg (if applicable)",
        ge=0.0,
    )
    created_at: datetime = Field(
        ...,
        description="When the recommendation was generated (UTC)",
    )
    updated_at: datetime = Field(
        ...,
        description="When the status was last updated (UTC)",
    )


# =============================================================================
# Response schemas — RecommendationSet
# =============================================================================


class RecommendationSetResponse(BaseModel):
    """Pydantic response schema for a set of recommendations.

    Mirrors domain RecommendationSet dataclass. Includes computed
    summary fields (total, critical_count, total savings) as explicit
    fields for API consumers.
    """

    model_config = ConfigDict(from_attributes=True)

    namespace: str = Field(..., description="Kubernetes namespace")
    generated_at: datetime = Field(
        ...,
        description="When the recommendations were generated (UTC)",
    )

    # Summary fields (computed from the list)
    total: int = Field(
        ...,
        description="Total number of recommendations",
        ge=0,
    )
    critical_count: int = Field(
        ...,
        description="Number of critical-severity recommendations",
        ge=0,
    )
    total_estimated_savings_usd: float = Field(
        ...,
        description="Sum of USD savings across all recommendations",
        ge=0.0,
    )
    total_estimated_savings_co2_kg: float = Field(
        ...,
        description="Sum of CO2 savings across all recommendations (kg)",
        ge=0.0,
    )

    # The actual recommendations
    recommendations: list[RecommendationResponse] = Field(
        ...,
        description="List of recommendations (may be empty)",
    )


# =============================================================================
# Response schemas — Historical snapshots
# =============================================================================


class SnapshotPointResponse(BaseModel):
    """Pydantic response schema for a single snapshot data point.

    Used in time-series charts (Grafana, custom dashboards).
    Each point represents one namespace at one point in time.
    """

    model_config = ConfigDict(from_attributes=True)

    time: datetime = Field(..., description="Snapshot timestamp (UTC)")
    namespace: str = Field(..., description="Kubernetes namespace")
    team: str = Field(..., description="Team attribution label")
    cost_center: str = Field(..., description="Cost center attribution label")
    environment: str = Field(..., description="Environment (production, staging, etc.)")

    cpu_core_hours: float = Field(..., description="CPU usage in core-hours", ge=0.0)
    memory_gb_hours: float = Field(..., description="Memory usage in GB-hours", ge=0.0)
    cost_usd: float = Field(..., description="Estimated cost in USD", ge=0.0)
    energy_kwh: float = Field(..., description="Energy consumption in kWh", ge=0.0)
    co2e_kg: float = Field(..., description="Carbon emissions in kgCO2e", ge=0.0)
    waste_pct: float = Field(
        ...,
        description="Percentage of resources wasted (idle/over-provisioned)",
        ge=0.0,
        le=100.0,
    )


class HistoricalSnapshotResponse(BaseModel):
    """Pydantic response schema for historical snapshot data.

    Returned by GET /api/v1/metrics/{namespace}/history?days=7

    Contains a list of SnapshotPointResponse objects, ordered by time DESC.
    Designed for time-series chart consumption (Grafana, custom dashboards).
    """

    namespace: str = Field(..., description="Kubernetes namespace")
    days: int = Field(
        ...,
        description="Number of days of history returned",
        ge=1,
    )
    snapshot_count: int = Field(
        ...,
        description="Total number of snapshot data points",
        ge=0,
    )
    snapshots: list[SnapshotPointResponse] = Field(
        ...,
        description="List of snapshots ordered by time DESC (most recent first)",
    )


# =============================================================================
# Mapper functions — domain → Pydantic
# =============================================================================


def recommendation_to_response(rec: Recommendation, rec_id: UUID) -> RecommendationResponse:
    """Convert a domain Recommendation dataclass to a Pydantic response DTO.

    WHY rec_id is a separate parameter:
    - Domain Recommendation has no id field (it's a pure business object)
    - The database assigns the id when persisting
    - The adapter (router) provides the id from the storage port
    """
    return RecommendationResponse(
        id=rec_id,
        namespace=rec.namespace,
        category=rec.category,
        severity=rec.severity,
        resource=rec.resource,
        message=rec.message,
        current_value=rec.current_value,
        recommended_value=rec.recommended_value,
        estimated_savings_usd=rec.estimated_savings_usd,
        estimated_savings_kg_co2=rec.estimated_savings_kg_co2,
        created_at=rec.timestamp or datetime.now(),
        updated_at=datetime.now(),
    )


def recommendation_set_to_response(
    rec_set: RecommendationSet,
    rec_ids: list[UUID],
) -> RecommendationSetResponse:
    """Convert a domain RecommendationSet dataclass to a Pydantic response DTO.

    WHY rec_ids is a separate parameter:
    - Domain RecommendationSet has no id list (pure business object)
    - The database assigns ids when persisting
    - The adapter (router) provides the ids from the storage port
    """
    rec_responses = [
        recommendation_to_response(rec, rec_id)
        for rec, rec_id in zip(rec_set.recommendations, rec_ids)
    ]

    return RecommendationSetResponse(
        namespace=rec_set.namespace,
        generated_at=rec_set.generated_at,
        total=rec_set.total,
        critical_count=rec_set.critical_count,
        total_estimated_savings_usd=rec_set.total_estimated_savings_usd,
        total_estimated_savings_co2_kg=rec_set.total_estimated_savings_co2_kg,
        recommendations=rec_responses,
    )


def snapshot_to_response(snapshot: MetricSnapshot) -> SnapshotPointResponse:
    """Convert a domain MetricSnapshot dataclass to a Pydantic response DTO."""
    return SnapshotPointResponse(
        time=snapshot.time,
        namespace=snapshot.namespace,
        team=snapshot.team,
        cost_center=snapshot.cost_center,
        environment=snapshot.environment,
        cpu_core_hours=snapshot.cpu_core_hours,
        memory_gb_hours=snapshot.memory_gb_hours,
        cost_usd=snapshot.cost_usd,
        energy_kwh=snapshot.energy_kwh,
        co2e_kg=snapshot.co2e_kg,
        waste_pct=snapshot.waste_pct,
    )


def snapshot_history_to_response(
    namespace: str,
    days: int,
    snapshots: Sequence[MetricSnapshot],
) -> HistoricalSnapshotResponse:
    """Convert a list of domain MetricSnapshot objects to a Pydantic response DTO.

    Wraps the snapshots in a response envelope with metadata (namespace, days, count).
    """
    return HistoricalSnapshotResponse(
        namespace=namespace,
        days=days,
        snapshot_count=len(snapshots),
        snapshots=[snapshot_to_response(s) for s in snapshots],
    )