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

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.outbound.db.models import (
    Base,
    MetricSnapshotRecord,
    RecommendationRecord,
    WorkloadSnapshotRecord,
)
from app.domain.models.recommendation import (
    Category,
    Recommendation,
    Severity,
    StoredRecommendation,
)
from app.domain.models.snapshot import AggregateRow, WorkloadSnapshot
from app.domain.recommendation_lifecycle import UpsertAction, decide_upsert
from app.ports.storage_port import MetricSnapshot, StoragePort


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize DB timestamps to aware UTC (SQLite returns naive datetimes)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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
        cluster=rec.cluster,
        namespace=rec.namespace,
        workload=rec.workload,
        category=rec.category.value,
        severity=rec.severity.value,
        resource=rec.resource,
        message=rec.message,
        current_value=rec.current_value,
        recommended_value=rec.recommended_value,
        estimated_savings_usd=rec.estimated_savings_usd,
        estimated_savings_co2_kg=rec.estimated_savings_kg_co2,
        estimation_method=rec.estimation_method,
        # V1: attribution is carried by the domain object (was hard-coded "unknown")
        team=rec.team,
        cost_center=rec.cost_center,
        environment=rec.environment,
        fingerprint=rec.fingerprint,
        # DB-only field (default for new records)
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
        timestamp=_utc(record.created_at),
        cluster=record.cluster or "default",
        workload=record.workload or "*",
        team=record.team,
        cost_center=record.cost_center,
        environment=record.environment,
        estimation_method=record.estimation_method,
    )


def record_to_stored(record: RecommendationRecord) -> StoredRecommendation:
    """ORM row → StoredRecommendation (advice + REAL id + lifecycle state)."""
    return StoredRecommendation(
        id=record.id,
        status=record.status,
        created_at=_utc(record.created_at),
        updated_at=_utc(record.updated_at),
        recommendation=record_to_recommendation(record),
    )


def workload_snapshot_to_record(snap: WorkloadSnapshot) -> WorkloadSnapshotRecord:
    return WorkloadSnapshotRecord(**{f: getattr(snap, f) for f in _WORKLOAD_FIELDS})


def record_to_workload_snapshot(record: WorkloadSnapshotRecord) -> WorkloadSnapshot:
    values = {f: getattr(record, f) for f in _WORKLOAD_FIELDS}
    values["time"] = _utc(values["time"])
    return WorkloadSnapshot(**values)


_WORKLOAD_FIELDS = tuple(WorkloadSnapshot.__dataclass_fields__)

_SNAPSHOT_V1_FIELDS = (
    "cluster", "requested_cost_usd", "waste_cost_usd", "potential_savings_usd",
    "storage_cost_usd", "storage_status", "idle_energy_kwh", "energy_attribution",
    "energy_power_source", "energy_scope", "energy_interval_kwh", "co2_interval_kg", "interval_seconds",
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
        **{f: getattr(snap, f) for f in _SNAPSHOT_V1_FIELDS},
    )


def record_to_snapshot(record: MetricSnapshotRecord) -> MetricSnapshot:
    """Convert an ORM MetricSnapshotRecord back to a domain MetricSnapshot dataclass.

    WHY this direction:
    - Reads: DB → domain (adapter reads from DB, returns domain to caller)
    """
    v1 = {f: getattr(record, f) for f in _SNAPSHOT_V1_FIELDS if getattr(record, f) is not None}
    # Nullable V1 columns: keep None (meaningful "unknown"), not dataclass defaults.
    for nullable in ("storage_cost_usd", "idle_energy_kwh", "energy_interval_kwh", "co2_interval_kg",
                     "interval_seconds"):
        v1[nullable] = getattr(record, nullable)
    return MetricSnapshot(
        time=_utc(record.time),
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
        **v1,
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

        # Pool sizing only applies to PostgreSQL; SQLite (tests / local demo)
        # uses its own pool class that rejects these arguments.
        pool_kwargs = {"pool_size": 5, "max_overflow": 10} if database_url.startswith("postgresql") else {}
        self._engine: AsyncEngine = create_async_engine(database_url, echo=False, **pool_kwargs)
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
    # V1 — lifecycle with real ids + de-duplication
    # -------------------------------------------------------------------------

    async def upsert_recommendation(self, rec: Recommendation) -> tuple[UUID | None, str]:
        """Insert / refresh / skip by fingerprint (rules: domain.decide_upsert).

        WHY: the scheduler runs every 5 minutes; inserting blindly created a new
        PENDING duplicate of the same advice at every cycle.
        Single writer (the scheduler) → select-then-write is sufficient here.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(RecommendationRecord)
                .where(RecommendationRecord.fingerprint == rec.fingerprint)
                .order_by(RecommendationRecord.updated_at.desc())
                .limit(1)
            )
            existing = result.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            action = decide_upsert(
                existing.status if existing else None,
                _utc(existing.updated_at) if existing else None,
                now,
            )
            if action is UpsertAction.SKIP:
                return existing.id, action.value
            if action is UpsertAction.REFRESH:
                existing.severity = rec.severity.value
                existing.message = rec.message
                existing.current_value = rec.current_value
                existing.recommended_value = rec.recommended_value
                existing.estimated_savings_usd = rec.estimated_savings_usd
                existing.estimated_savings_co2_kg = rec.estimated_savings_kg_co2
                existing.estimation_method = rec.estimation_method
                existing.team = rec.team
                existing.cost_center = rec.cost_center
                existing.environment = rec.environment
                existing.updated_at = now
                await session.commit()
                return existing.id, action.value
            record = recommendation_to_record(rec)
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.id, action.value

    async def get_recommendation_record(self, rec_id: UUID) -> StoredRecommendation | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(RecommendationRecord).where(RecommendationRecord.id == rec_id)
            )
            record = result.scalar_one_or_none()
            return record_to_stored(record) if record else None

    async def list_recommendation_records(
        self,
        *,
        cluster: str | None = None,
        namespace: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[StoredRecommendation]:
        async with self._session_factory() as session:
            stmt = select(RecommendationRecord)
            if cluster is not None:
                stmt = stmt.where(RecommendationRecord.cluster == cluster)
            if namespace is not None:
                stmt = stmt.where(RecommendationRecord.namespace == namespace)
            if status is not None:
                stmt = stmt.where(RecommendationRecord.status == status)
            stmt = stmt.order_by(RecommendationRecord.created_at.desc()).limit(limit).offset(offset)
            result = await session.execute(stmt)
            return [record_to_stored(r) for r in result.scalars().all()]

    async def mark_obsolete(
        self, *, cluster: str, namespace: str, keep_fingerprints: set[str],
        categories: set[str] | None = None, exclude_categories: set[str] | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = (
                update(RecommendationRecord)
                .where(RecommendationRecord.cluster == cluster)
                .where(RecommendationRecord.namespace == namespace)
                .where(RecommendationRecord.status == "pending")
                .values(status="obsolete", updated_at=datetime.now(timezone.utc))
            )
            if keep_fingerprints:
                stmt = stmt.where(RecommendationRecord.fingerprint.not_in(keep_fingerprints))
            if categories is not None:
                stmt = stmt.where(RecommendationRecord.category.in_(categories))
            if exclude_categories:
                stmt = stmt.where(RecommendationRecord.category.not_in(exclude_categories))
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    # -------------------------------------------------------------------------
    # V1 — richer history
    # -------------------------------------------------------------------------

    async def save_workload_snapshots(self, snapshots: Sequence[WorkloadSnapshot]) -> None:
        if not snapshots:
            return
        async with self._session_factory() as session:
            session.add_all([workload_snapshot_to_record(s) for s in snapshots])
            await session.commit()

    async def query_snapshots(
        self,
        *,
        start: datetime,
        end: datetime,
        cluster: str | None = None,
        namespace: str | None = None,
    ) -> Sequence[MetricSnapshot]:
        async with self._session_factory() as session:
            stmt = select(MetricSnapshotRecord).where(
                MetricSnapshotRecord.time >= start, MetricSnapshotRecord.time <= end
            )
            if cluster is not None:
                stmt = stmt.where(MetricSnapshotRecord.cluster == cluster)
            if namespace is not None:
                stmt = stmt.where(MetricSnapshotRecord.namespace == namespace)
            result = await session.execute(stmt.order_by(MetricSnapshotRecord.time.asc()))
            return [record_to_snapshot(r) for r in result.scalars().all()]

    async def query_workload_snapshots(
        self,
        *,
        start: datetime,
        end: datetime,
        cluster: str | None = None,
        namespace: str | None = None,
        workload: str | None = None,
    ) -> Sequence[WorkloadSnapshot]:
        async with self._session_factory() as session:
            stmt = select(WorkloadSnapshotRecord).where(
                WorkloadSnapshotRecord.time >= start, WorkloadSnapshotRecord.time <= end
            )
            if cluster is not None:
                stmt = stmt.where(WorkloadSnapshotRecord.cluster == cluster)
            if namespace is not None:
                stmt = stmt.where(WorkloadSnapshotRecord.namespace == namespace)
            if workload is not None:
                stmt = stmt.where(WorkloadSnapshotRecord.workload == workload)
            result = await session.execute(stmt.order_by(WorkloadSnapshotRecord.time.asc()))
            return [record_to_workload_snapshot(r) for r in result.scalars().all()]

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
        """
        Read the continuous aggregates (migration 004). hour → *_hourly, day →
        *_daily; week/month are re-bucketed from *_daily: costs averaged WEIGHTED
        by sample count, energy/CO2 summed. TimescaleDB only (not SQLite).
        """
        if scope not in ("namespace", "workload"):
            raise ValueError("scope must be namespace or workload")
        if granularity not in ("hour", "day", "week", "month"):
            raise ValueError("granularity must be hour, day, week or month")
        base = "metric_snapshots" if scope == "namespace" else "workload_snapshots"
        source = f"{base}_hourly" if granularity == "hour" else f"{base}_daily"
        wl_col = "workload" if scope == "workload" else "'*'"
        storage = "storage_cost_usd" if scope == "namespace" else "NULL::double precision"
        filters = ["bucket >= :start", "bucket <= :end"]
        params: dict = {"start": start, "end": end}
        for col, val in (("cluster", cluster), ("namespace", namespace)):
            if val is not None:
                filters.append(f"{col} = :{col}")
                params[col] = val
        if workload is not None and scope == "workload":
            filters.append("workload = :workload")
            params["workload"] = workload
        where = " AND ".join(filters)
        if granularity in ("hour", "day"):
            sql = f"""SELECT bucket, cluster, namespace, {wl_col} AS workload, cost_usd, requested_cost_usd,
                waste_cost_usd, savings_usd, cpu_cores, memory_gb, {storage} AS storage_cost_usd,
                energy_kwh, co2_kg, samples FROM {source} WHERE {where} ORDER BY bucket"""
        else:
            size = "1 week" if granularity == "week" else "1 month"
            w = lambda c: f"sum({c} * samples) / NULLIF(sum(CASE WHEN {c} IS NULL THEN 0 ELSE samples END), 0)"  # noqa: E731
            sql = f"""SELECT time_bucket(INTERVAL '{size}', bucket) AS b, cluster, namespace, {wl_col} AS workload,
                {w('cost_usd')}, {w('requested_cost_usd')}, {w('waste_cost_usd')}, {w('savings_usd')},
                {w('cpu_cores')}, {w('memory_gb')}, {w(storage) if scope == 'namespace' else 'NULL::double precision'},
                sum(energy_kwh), sum(co2_kg), sum(samples)::int
                FROM {source} WHERE {where} GROUP BY 1, 2, 3, 4 ORDER BY 1"""
        async with self._engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            return [AggregateRow(_utc(r[0]), r[1], r[2], r[3], *r[4:13], int(r[13] or 0)) for r in result]

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def close(self) -> None:
        """Release underlying DB connection/pool resources.

        Called during FastAPI shutdown to prevent connection leaks.
        """
        await self._engine.dispose()