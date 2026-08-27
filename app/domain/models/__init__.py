"""
Domain models for finops-greenit-service.

These are plain Python dataclasses — zero framework dependency (no Pydantic,
no FastAPI, no httpx). This is non-negotiable under Hexagonal Architecture:
the domain layer must be independently testable and portable.

Adapters (inbound/outbound) are responsible for mapping to/from these objects.
"""

from app.domain.models.pod_metric import (
    ClusterAllocatable,
    PodMetric,
    StorageMetric,
)
from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown
from app.domain.models.recommendation import (
    Category,
    Recommendation,
    Severity,
)

__all__ = [
    "PodMetric",
    "ClusterAllocatable",
    "StorageMetric",
    "CarbonFootprint",
    "EnergyBreakdown",
    "Recommendation",
    "Category",
    "Severity",
]