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
from datetime import datetime
from typing import Sequence
from uuid import UUID

from app.domain.models.recommendation import Recommendation, StoredRecommendation
from app.domain.models.snapshot import AggregateRow, MetricSnapshot, WorkloadSnapshot


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
    # V1 — recommendation lifecycle with real ids + de-duplication
    # -------------------------------------------------------------------------

    @abstractmethod
    async def upsert_recommendation(self, rec: Recommendation) -> tuple[UUID | None, str]:
        """Insert, refresh or skip a generated recommendation (by fingerprint).

        Returns (id, action) where action is "insert" | "refresh" | "skip".
        Decision rules live in app/domain/recommendation_lifecycle.decide_upsert.
        """

    @abstractmethod
    async def get_recommendation_record(self, rec_id: UUID) -> StoredRecommendation | None:
        """Recommendation + id + status + timestamps. None if not found."""

    @abstractmethod
    async def list_recommendation_records(
        self,
        *,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[StoredRecommendation]:
        """List stored recommendations with their REAL ids, newest first."""

    @abstractmethod
    async def mark_obsolete(
        self, *, cluster: str, namespace: str, keep_fingerprints: set[str],
        categories: set[str] | None = None, exclude_categories: set[str] | None = None,
    ) -> int:
        """Set PENDING recommendations of (cluster, namespace) that are not in
        keep_fingerprints to `obsolete`. Human-handled states are untouched.
        V3: several producers (rules every cycle, ML hourly) — each one only closes
        ITS categories (`categories` = only these, `exclude_categories` = all but these).
        Returns the number of rows changed."""

    # -------------------------------------------------------------------------
    # V1 — richer history
    # -------------------------------------------------------------------------

    @abstractmethod
    async def save_workload_snapshots(self, snapshots: Sequence[WorkloadSnapshot]) -> None:
        """Persist per-workload cost rows of one scheduler cycle."""

    @abstractmethod
    async def query_snapshots(
        self,
        *,
        start: datetime,
        end: datetime,
        cluster: str | None = None,
        namespace: str | None = None,
    ) -> Sequence[MetricSnapshot]:
        """Namespace-level snapshots in [start, end], optional filters, oldest first."""

    @abstractmethod
    async def query_workload_snapshots(
        self,
        *,
        start: datetime,
        end: datetime,
        cluster: str | None = None,
        namespace: str | None = None,
        workload: str | None = None,
    ) -> Sequence[WorkloadSnapshot]:
        """Workload-level snapshots in [start, end], optional filters, oldest first."""

    @abstractmethod
    async def query_aggregates(
        self,
        *,
        scope: str,
        granularity: str,
        start: datetime,
        end: datetime,
        cluster: str | None = None,
        namespace: str | None = None,
        workload: str | None = None,
    ) -> Sequence[AggregateRow]:
        """V2 — long-term history from the continuous aggregates.

        scope: "namespace" | "workload"; granularity: "hour" | "day" | "week" | "month".
        """

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    @abstractmethod
    async def close(self) -> None:
        """Release underlying DB connection/pool resources."""


# -------------------------------------------------------------------------
# MetricSnapshot moved to app/domain/models/snapshot.py in V1 (history
# aggregation is domain logic now). Re-exported here for compatibility.
# -------------------------------------------------------------------------
