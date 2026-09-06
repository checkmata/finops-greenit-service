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
    Column,
    DateTime,
    Double,
    Index,
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
    namespace = Column(Text, nullable=False)
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
    namespace = Column(Text, nullable=False, primary_key=True)
    team = Column(Text, nullable=False, default="unknown")
    cost_center = Column(Text, nullable=False, default="unknown")
    environment = Column(Text, nullable=False, default="unknown")
    cpu_core_hours = Column(Double, nullable=False, default=0.0)
    memory_gb_hours = Column(Double, nullable=False, default=0.0)
    cost_usd = Column(Double, nullable=False, default=0.0)
    energy_kwh = Column(Double, nullable=False, default=0.0)
    co2e_kg = Column(Double, nullable=False, default=0.0)
    waste_pct = Column(Double, nullable=False, default=0.0)

    __table_args__ = (
        Index("idx_metric_snapshots_namespace_time", "namespace", "time"),
    )