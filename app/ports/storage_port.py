"""
Storage port — abstract interface for persisting recommendations and metric snapshots.

WHY a storage port:
- Keeps domain layer completely unaware of SQLAlchemy/TimescaleDB (hexagonal rule)
- Enables swapping TimescaleDB for SQLite in tests (port mock or in-memory)
- Routers (HTTP) read historical data through this port without knowing which DB backend

The port uses domain dataclasses as its contract — never SQLAlchemy models or raw dicts.
This ensures the port boundary is clean: domain objects go in, domain objects come out.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
from uuid import UUID

from app.domain.models.recommendation import Recommendation


class StoragePort(ABC):
    """Abstract contract for persisting FinOps/GreenIT data.

    All methods accept and return domain objects (Recommendation, MetricSnapshot).
    The adapter layer handles conversion to/from ORM models.
    """

    # -------------------------------------------------------------------------
    # Recommendations (relational)
    # -------------------------------------------------------------------------

    @abstractmethod
    async def save_recommendation(self, rec: Recommendation) -> UUID:
        """Persist a recommendation. Returns the assigned UUID.

        WHY UUID: generated client-side, no sequence contention, safe for
        distributed schedulers writing concurrently.
        """

    @abstractmethod
    async def update_recommendation_status(
        self, rec_id: UUID, new_status: str
    ) -> bool:
        """Update the lifecycle status of a recommendation.

        Valid transitions: pending → approved → executed → dismissed.
        Returns True if the row was updated, False if rec_id not found.
        """

    @abstractmethod
    async def get_recommendation(self, rec_id: UUID) -> Recommendation | None:
        """Retrieve a single recommendation by ID. Returns None if not found."""

    @abstractmethod
    async def list_recommendations(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Recommendation]:
        """List recommendations with optional filters.

        Ordered by created_at DESC (newest first).
        """

    # -------------------------------------------------------------------------
    # Metric snapshots (time-series)
    # -------------------------------------------------------------------------

    @abstractmethod
    async def save_snapshot(self, snapshot: "MetricSnapshot") -> None:
        """Persist a single metric snapshot row.

        One row per namespace per scheduler run.
        The adapter maps the domain MetricSnapshot to the ORM MetricSnapshotRecord.
        """

    @abstractmethod
    async def get_snapshot_history(
        self,
        namespace: str,
        days: int = 7,
    ) -> Sequence["MetricSnapshot"]:
        """Retrieve metric snapshots for a namespace over the last N days.

        WHY days (not start/end datetime): simpler API for callers.
        The adapter computes start_time = now() - timedelta(days=days).
        Ordered by time DESC (most recent first).
        """

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    @abstractmethod
    async def close(self) -> None:
        """Release underlying DB connection/pool resources."""


# -------------------------------------------------------------------------
# Domain model for metric snapshots (defined here to avoid circular import)
# -------------------------------------------------------------------------
# WHY here (not in app/domain/models/):
# - MetricSnapshot is tightly coupled to the storage port (it's a persistence concept)
# - Keeping it here avoids bloating domain/models/ with storage-specific objects
# - The domain engine doesn't need MetricSnapshot — it works with PodMetric/CarbonFootprint
# - If the domain engine ever needs historical data, we can move this to domain/models/
# -------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSnapshot:
    """A single row of aggregated metrics for a namespace at a point in time.

    This is the storage-port's domain object — not a PodMetric (which is per-pod)
    and not a Recommendation (which is advice). This is raw telemetry for history.

    Fields mirror the metric_snapshots hypertable exactly:
    - time, namespace, team, cost_center, environment
    - cpu_core_hours, memory_gb_hours, cost_usd
    - energy_kwh, co2e_kg, waste_pct
    """

    time: datetime
    namespace: str
    team: str
    cost_center: str
    environment: str
    cpu_core_hours: float
    memory_gb_hours: float
    cost_usd: float
    energy_kwh: float
    co2e_kg: float
    waste_pct: float