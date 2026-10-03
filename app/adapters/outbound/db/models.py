"""
SQLAlchemy ORM models for TimescaleDB tables.

WHY separate from domain dataclasses:
- Domain models are pure dataclasses (zero framework imports) — they represent
  business concepts (Recommendation, PodMetric, CarbonFootprint).
- SQLAlchemy models represent DATABASE schema — they carry ORM machinery
  (session tracking, lazy loading, relationship mapping) that domain must never see.
- The adapter layer (TimescaleAdapter) converts between the two:
  domain → ORM for writes, ORM → domain for reads.
- This mirrors the DTO pattern we already use for Pydantic schemas:
  Pydantic DTOs = HTTP boundary, SQLAlchemy models = DB boundary, domain = business logic.

CRITICAL RULE: Nothing in app/domain/ must ever import from this file.
This is enforced by architecture, not by code. If you see a domain file
importing from db/models.py, that's a bug — fix it immediately.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Double,
    Index,
    Integer,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base — all models inherit from this.

    Provides: metadata, __tablename__, Column mapping.
    Does NOT provide: session management, query building (those live in the adapter).
    """

    pass


class RecommendationRecord(Base):
    """ORM model for the `recommendations` relational table.

    Maps 1:1 to the SQL schema in 001_init.sql.
    Status follows lifecycle: pending → approved → executed → dismissed.

    WHY this is NOT a domain dataclass:
    - It has `id` (UUID primary key) — domain Recommendation doesn't need an ID
    - It has `created_at`/`updated_at` timestamps — domain uses `timestamp` (optional)
    - It has `team`/`cost_center`/`environment` — domain Recommendation doesn't carry these
      (they're stored for chargeback but the engine doesn't use them)
    - It has `status` — domain Recommendation is immutable, status lives only in DB
    """

    __tablename__ = "recommendations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    cluster = Column(Text, nullable=False, default="default")
    namespace = Column(Text, nullable=False)
    workload = Column(Text, nullable=False, default="*")
    team = Column(Text, nullable=False, default="unknown")
    cost_center = Column(Text, nullable=False, default="unknown")
    environment = Column(Text, nullable=False, default="unknown")
    category = Column(Text, nullable=False)
    severity = Column(Text, nullable=False)
    resource = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    current_value = Column(Text, nullable=False)
    recommended_value = Column(Text, nullable=False)
    estimated_savings_usd = Column(Double, nullable=True)
    estimated_savings_co2_kg = Column(Double, nullable=True)
    estimation_method = Column(Text, nullable=True)
    # cluster|namespace|workload|category|resource — see Recommendation.fingerprint
    fingerprint = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="pending")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_recommendations_namespace_status", "namespace", "status"),
        Index("idx_recommendations_team", "team"),
        Index("idx_recommendations_fingerprint", "fingerprint"),
    )


class MetricSnapshotRecord(Base):
    """ORM model for the `metric_snapshots` TimescaleDB hypertable.

    One row per namespace per scheduler run. TimescaleDB hypertable
    auto-partitions by `time` column and applies 7-day retention.

    Uses composite primary key (time + namespace) for hypertable compatibility.
    This is NOT a domain dataclass — it's a database schema representation.

    WHY this is NOT a domain dataclass:
    - It has no business logic (no properties, no computed fields)
    - It exists solely for ORM mapping (session tracking, SQL generation)
    - The domain engine works with PodMetric/CarbonFootprint, not raw snapshot rows
    """

    __tablename__ = "metric_snapshots"

    time = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        primary_key=True,
    )
    cluster = Column(Text, nullable=False, default="default", primary_key=True)
    namespace = Column(Text, nullable=False, primary_key=True)
    team = Column(Text, nullable=False, default="unknown")
    cost_center = Column(Text, nullable=False, default="unknown")
    environment = Column(Text, nullable=False, default="unknown")
    cpu_core_hours = Column(Double, nullable=False, default=0.0)
    memory_gb_hours = Column(Double, nullable=False, default=0.0)
    cost_usd = Column(Double, nullable=False, default=0.0)
    # V1: nullable — energy may be unmeasurable; None is stored, never a fake 0.
    energy_kwh = Column(Double, nullable=True)
    co2e_kg = Column(Double, nullable=True)
    waste_pct = Column(Double, nullable=False, default=0.0)
    requested_cost_usd = Column(Double, nullable=False, default=0.0)
    waste_cost_usd = Column(Double, nullable=False, default=0.0)
    potential_savings_usd = Column(Double, nullable=False, default=0.0)
    storage_cost_usd = Column(Double, nullable=True)
    storage_status = Column(Text, nullable=False, default="unavailable")
    idle_energy_kwh = Column(Double, nullable=True)
    energy_attribution = Column(Text, nullable=False, default="unavailable")
    energy_power_source = Column(Text, nullable=False, default="unknown")
    energy_scope = Column(Text, nullable=False, default="unknown")
    energy_interval_kwh = Column(Double, nullable=True)
    co2_interval_kg = Column(Double, nullable=True)
    interval_seconds = Column(Double, nullable=True)

    __table_args__ = (
        Index("idx_metric_snapshots_namespace_time", "namespace", "time"),
        Index("idx_metric_snapshots_cluster_ns_time", "cluster", "namespace", "time"),
    )


class WorkloadSnapshotRecord(Base):
    """ORM model for the `workload_snapshots` hypertable (V1).

    One row per (cluster, namespace, workload) per scheduler run — cost view only.
    """

    __tablename__ = "workload_snapshots"

    time = Column(DateTime(timezone=True), nullable=False, primary_key=True)
    cluster = Column(Text, nullable=False, primary_key=True)
    namespace = Column(Text, nullable=False, primary_key=True)
    workload = Column(Text, nullable=False, primary_key=True)
    team = Column(Text, nullable=False, default="unknown")
    cost_center = Column(Text, nullable=False, default="unknown")
    environment = Column(Text, nullable=False, default="unknown")
    cpu_cores_used = Column(Double, nullable=False, default=0.0)
    cpu_cores_requested = Column(Double, nullable=False, default=0.0)
    memory_gb_used = Column(Double, nullable=False, default=0.0)
    memory_gb_requested = Column(Double, nullable=False, default=0.0)
    requested_cost_usd = Column(Double, nullable=False, default=0.0)
    actual_cost_usd = Column(Double, nullable=False, default=0.0)
    waste_cost_usd = Column(Double, nullable=False, default=0.0)
    potential_savings_usd = Column(Double, nullable=False, default=0.0)
    energy_interval_kwh = Column(Double, nullable=True)
    co2_interval_kg = Column(Double, nullable=True)
    energy_precision = Column(Text, nullable=False, default="unavailable")
    energy_power_source = Column(Text, nullable=False, default="unknown")
    cpu_cores_used_now = Column(Double, nullable=True)   # V3 (migration 006)

    __table_args__ = (
        Index("idx_workload_snapshots_scope_time", "cluster", "namespace", "workload", "time"),
    )

class FailoverStateRecord(Base):
    """Singleton row of the failover controller state (migration 004)."""

    __tablename__ = "failover_state"

    id = Column(SmallInteger, primary_key=True, default=1)
    mode = Column(Text, nullable=False)
    active_cluster = Column(Text, nullable=False)
    primary_unhealthy_since = Column(DateTime(timezone=True), nullable=True)
    primary_healthy_since = Column(DateTime(timezone=True), nullable=True)
    last_transition_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class FailoverEventRecord(Base):
    """Failover / failback transitions and what was done about them (migration 004)."""

    __tablename__ = "failover_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    kind = Column(Text, nullable=False)
    from_cluster = Column(Text, nullable=False)
    to_cluster = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    action_status = Column(Text, nullable=False)
    action_detail = Column(JSON, nullable=False, default=dict)
    decided_by = Column(Text, nullable=False, default="controller")


class AgentActionRecord(Base):
    """V4 controlled action (migration 007)."""

    __tablename__ = "agent_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    idempotency_key = Column(Text, nullable=False)
    recommendation_id = Column(UUID(as_uuid=True), nullable=True)
    status = Column(Text, nullable=False)
    proposal = Column(JSON, nullable=False, default=dict)
    policy = Column(JSON, nullable=False, default=dict)
    dry_run = Column(JSON, nullable=False, default=dict)
    execution = Column(JSON, nullable=False, default=dict)
    approver = Column(Text, nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=300)


class AgentAuditRecord(Base):
    """V4 append-only audit log of every step of every action (migration 007)."""

    __tablename__ = "agent_audit"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    action_id = Column(UUID(as_uuid=True), nullable=False)
    actor = Column(Text, nullable=False)
    event = Column(Text, nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
