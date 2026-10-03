"""
Recommendations router — human-in-the-loop lifecycle.

Endpoints:
- GET   /api/recommendations/history        → stored recommendations (REAL ids + status)
- PATCH /api/recommendations/{id}/status     → validated status transition

V1 fixes:
- The MVP returned fake ids (UUID(int=i)), so PATCH could not be chained.
- The MVP declared the allowed transitions but never checked them.
  Transitions are now domain rules (app/domain/recommendation_lifecycle.py);
  an illegal transition returns 409 Conflict and changes nothing.
- Status vocabulary: pending / approved / rejected / executed / failed.

Nothing here executes an action on a cluster: `executed` records that a HUMAN
applied the change (automation is out of scope until V4, and gated).
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.adapters.inbound.api.dependencies import get_storage_port
from app.adapters.inbound.api.schemas.recommendation_schemas import stored_recommendation_to_response
from app.domain.recommendation_lifecycle import (
    ALLOWED_TRANSITIONS,
    VALID_STATUSES,
    RecommendationStatus,
    can_transition,
)
from app.ports.storage_port import StoragePort

__all__ = ["router", "get_storage_port", "validate_status_transition", "VALID_STATUSES", "ALLOWED_TRANSITIONS"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])

# Kept for backward compatibility with MVP imports; the rule lives in the domain.
validate_status_transition = can_transition


class UpdateStatusRequest(BaseModel):
    """Request body for PATCH /api/recommendations/{id}/status"""

    status: str = Field(
        ...,
        description="New status: pending, approved, rejected, executed, failed",
        examples=["approved"],
    )
    comment: str | None = Field(None, description="Optional reviewer comment (logged)")


def _invalid_status(value: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=f"Invalid status '{value}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
    )


@router.get(
    "/history",
    summary="Get stored recommendations",
    description="Recommendations from TimescaleDB with their real id and lifecycle status.",
    response_model=None,
    responses={400: {"description": "Invalid status filter"}, 404: {"description": "Nothing matches"}},
)
async def get_recommendations_history(
    storage: Annotated[StoragePort, Depends(get_storage_port)],
    cluster: Annotated[str | None, Query(description="Filter by cluster")] = None,
    namespace: Annotated[str | None, Query(description="Filter by namespace")] = None,
    status: Annotated[str | None, Query(description="Filter by status")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Max results")] = 50,
    offset: Annotated[int, Query(ge=0, description="Skip for pagination")] = 0,
) -> list[dict]:
    if status is not None and status not in VALID_STATUSES:
        raise _invalid_status(status)

    records = await storage.list_recommendation_records(
        cluster=cluster, namespace=namespace, status=status, limit=limit, offset=offset
    )
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"No recommendations found (cluster={cluster}, namespace={namespace}, status={status})",
        )
    return [stored_recommendation_to_response(r).model_dump() for r in records]


@router.patch(
    "/{rec_id}/status",
    summary="Update recommendation status",
    description="Human decision on a recommendation, with transition validation.",
    response_model=None,
    responses={
        400: {"description": "Unknown status value"},
        404: {"description": "Recommendation not found"},
        409: {"description": "Transition not allowed from the current status"},
    },
)
async def update_recommendation_status(
    rec_id: UUID,
    request: UpdateStatusRequest,
    storage: Annotated[StoragePort, Depends(get_storage_port)],
) -> dict:
    new_status = request.status
    if new_status not in VALID_STATUSES:
        raise _invalid_status(new_status)

    current = await storage.get_recommendation_record(rec_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Recommendation '{rec_id}' not found")

    if not can_transition(current.status, new_status):
        allowed = sorted(
            s.value for s in ALLOWED_TRANSITIONS.get(RecommendationStatus(current.status), frozenset())
        ) if current.status in VALID_STATUSES else []
        raise HTTPException(
            status_code=409,
            detail=f"Transition '{current.status}' → '{new_status}' not allowed. Allowed: {allowed or 'none (terminal)'}",
        )

    if not await storage.update_recommendation_status(rec_id, new_status):
        raise HTTPException(status_code=404, detail=f"Recommendation '{rec_id}' not found")
    logger.info("recommendation %s: %s → %s (%s)", rec_id, current.status, new_status, request.comment or "no comment")

    updated = await storage.get_recommendation_record(rec_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to retrieve updated recommendation")
    return stored_recommendation_to_response(updated).model_dump()
