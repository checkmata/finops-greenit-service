"""
TimescaleDB adapter — concrete implementation of StoragePort.

Uses SQLAlchemy async engine with asyncpg driver to talk to TimescaleDB.
Handles all domain ↔ ORM mapping via explicit mapper functions.

WHY async:
- FastAPI is async; blocking DB calls would block the event loop
- asyncpg is the recommended async driver for PostgreSQL

WHY explicit mappers (not SQLAlchemy's type_coerce or hybrid properties):
- Keeps ORM models schema-only (no business logic leakage)
- Makes conversion logic testable in isolation
- Follows the same pattern as our Pydantic DTO mappers in adapters/inbound/api/
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.outbound.db.models import Base, MetricSnapshotRecord, RecommendationRecord
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.ports.storage_port import MetricSnapshot, StoragePort


# =============================================================================
# Mapper functions — domain ↔ ORM conversion
# =============================================================================
# WHY standalone functions (not methods on the adapter):
# - They're pure functions (no self, no I/O) — easily testable
# - They make the conversion boundary explicit and auditable
# - If we add a new domain field, we update exactly one mapper function
# =============================================================================


def recommendation_to_record(rec: Recommendation) -> RecommendationRecord:
    """Convert a domain Recommendation dataclass to an ORM RecommendationRecord.

    WHY this direction:
    - Writes: domain → DB (engine generates recs, adapter persists them)
    - The mapper fills DB-only fields (id, status, timestamps) with defaults
    """
    return RecommendationRecord(
        # Domain fields (direct mapping)
        namespace=rec.namespace,
        category=rec.category.value,
        severity=rec.severity.value,
        resource=rec.resource,
        message=rec.message,
        current_value=rec.current_value,
        recommended_value=rec.recommended_value,
        estimated_savings_usd=rec.estimated_savings_usd,
        estimated_savings_co2_kg=rec.estimated_savings_kg_co2,
        # DB-only fields (defaults for new records)
        team="unknown",
        cost_center="unknown",
        environment="unknown",
        status="pending",
    )


def record_to_recommendation(record: RecommendationRecord) -> Recommendation:
    """Convert an ORM RecommendationRecord back to a domain Recommendation dataclass.

    WHY this direction:
    - Reads: DB → domain (adapter reads from DB, returns domain to caller)
    - Strips DB-only fields (id, status, timestamps) — caller doesn't need them
    - Reconstructs enums from string values (Category, Severity)
    """
    return Recommendation(
        namespace=record.namespace,
        category=Category(record.category),
        severity=Severity(record.severity),
        resource=record.resource,
        message=record.message,
        current_value=record.current_value,
        recommended_value=record.recommended_value,
        estimated_savings_usd=record.estimated_savings_usd,
        estimated_savings_kg_co2=record.estimated_savings_co2_kg,
    )


def snapshot_to_record(snap: MetricSnapshot) -> MetricSnapshotRecord:
    """Convert a domain MetricSnapshot dataclass to an ORM MetricSnapshotRecord.

    WHY this direction:
    - Writes: domain → DB (scheduler generates snapshots, adapter persists them)
    """
    return MetricSnapshotRecord(
        time=snap.time,
        namespace=snap.namespace,
        team=snap.team,
        cost_center=snap.cost_center,
        environment=snap.environment,
        cpu_core_hours=snap.cpu_core_hours,
        memory_gb_hours=snap.memory_gb_hours,
        cost_usd=snap.cost_usd,
        energy_kwh=snap.energy_kwh,
        co2e_kg=snap.co2e_kg,
        waste_pct=snap.waste_pct,
    )


def record_to_snapshot(record: MetricSnapshotRecord) -> MetricSnapshot:
    """Convert an ORM MetricSnapshotRecord back to a domain MetricSnapshot dataclass.

    WHY this direction:
    - Reads: DB → domain (adapter reads from DB, returns domain to caller)
    """
    return MetricSnapshot(
        time=record.time,
        namespace=record.namespace,
        team=record.team,
        cost_center=record.cost_center,
        environment=record.environment,
        cpu_core_hours=record.cpu_core_hours,
        memory_gb_hours=record.memory_gb_hours,
        cost_usd=record.cost_usd,
        energy_kwh=record.energy_kwh,
        co2e_kg=record.co2e_kg,
        waste_pct=record.waste_pct,
    )


# =============================================================================
# Adapter implementation
# =============================================================================


class TimescaleAdapter(StoragePort):
    """Implements StoragePort against a PostgreSQL/TimescaleDB database.

    Responsibilities:
    - Connection pool management (async engine + session factory)
    - SQL query execution (select, insert, update)
    - Domain ↔ ORM mapping (via standalone mapper functions)

    What it does NOT do:
    - Business logic (that lives in domain engines)
    - Schema creation (that lives in 001_init.sql migration)
    - Health checks (that lives in a separate adapter or infrastructure layer)
    """

    def __init__(self, database_url: str) -> None:
        """Initialize the adapter with an async engine.

        Args:
            database_url: PostgreSQL connection string.
                          Accepts both formats:
                          - postgresql://user:pass@host:5432/dbname
                          - postgresql+asyncpg://user:pass@host:5432/dbname
                          The adapter auto-converts to asyncpg if needed.
        """
        if database_url.startswith("postgresql://") and "+asyncpg" not in database_url:
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def ensure_tables_exist(self) -> None:
        """Create tables if they don't exist (for local dev / tests).

        In production, tables are created via 001_init.sql migration.
        This method is idempotent — safe to call on every startup.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # -------------------------------------------------------------------------
    # Recommendations (relational)
    # -------------------------------------------------------------------------

    async def save_recommendation(self, rec: Recommendation) -> UUID:
        """Persist a recommendation. Returns the assigned UUID.

        Uses the recommendation_to_record mapper to convert domain → ORM,
        then commits. After commit, refreshes the record to get the
        server-generated UUID and timestamps.
        """
        async with self._session_factory() as session:
            record = recommendation_to_record(rec)
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.id

    async def update_recommendation_status(
        self, rec_id: UUID, new_status: str
    ) -> bool:
        """Update the lifecycle status of a recommendation.

        Returns True if the row was updated, False if rec_id not found.
        Also updates the `updated_at` timestamp to track when the status changed.
        """
        async with self._session_factory() as session:
            now = datetime.now(timezone.utc)
            result = await session.execute(
                update(RecommendationRecord)
                .where(RecommendationRecord.id == rec_id)
                .values(status=new_status, updated_at=now)
            )
            await session.commit()
            return result.rowcount > 0

    async def get_recommendation(self, rec_id: UUID) -> Recommendation | None:
        """Retrieve a single recommendation by ID.

        Uses the record_to_recommendation mapper to convert ORM → domain.
        Returns None if the ID doesn't exist.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(RecommendationRecord).where(RecommendationRecord.id == rec_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            return record_to_recommendation(record)

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
        Uses the record_to_recommendation mapper for each result.
        """
        async with self._session_factory() as session:
            stmt = select(RecommendationRecord)
            if namespace is not None:
                stmt = stmt.where(RecommendationRecord.namespace == namespace)
            if status is not None:
                stmt = stmt.where(RecommendationRecord.status == status)
            stmt = stmt.order_by(RecommendationRecord.created_at.desc())
            stmt = stmt.limit(limit).offset(offset)
            result = await session.execute(stmt)
            records = result.scalars().all()
            return [record_to_recommendation(r) for r in records]

    # -------------------------------------------------------------------------
    # Metric snapshots (time-series)
    # -------------------------------------------------------------------------

    async def save_snapshot(self, snapshot: MetricSnapshot) -> None:
        """Persist a single metric snapshot row.

        Uses the snapshot_to_record mapper to convert domain → ORM.
        One row per namespace per scheduler run.
        """
        async with self._session_factory() as session:
            record = snapshot_to_record(snapshot)
            session.add(record)
            await session.commit()

    async def get_snapshot_history(
        self,
        namespace: str,
        days: int = 7,
    ) -> Sequence[MetricSnapshot]:
        """Retrieve metric snapshots for a namespace over the last N days.

        Computes start_time = now() - timedelta(days=days).
        Uses the record_to_snapshot mapper for each result.
        Ordered by time DESC (most recent first).
        """
        async with self._session_factory() as session:
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(days=days)
            stmt = (
                select(MetricSnapshotRecord)
                .where(MetricSnapshotRecord.namespace == namespace)
                .where(MetricSnapshotRecord.time >= start_time)
                .order_by(MetricSnapshotRecord.time.desc())
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            return [record_to_snapshot(r) for r in records]

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def close(self) -> None:
        """Release underlying DB connection/pool resources.

        Called during FastAPI shutdown to prevent connection leaks.
        """
        await self._engine.dispose()