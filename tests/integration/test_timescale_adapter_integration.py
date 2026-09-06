"""
Integration tests for TimescaleAdapter — database persistence.

WHY database mocking is needed here (and only here):
- The adapter is the ONLY code that speaks SQL to TimescaleDB
- Domain engine tests don't need databases (pure functions)
- Router tests mock the adapter (not the database)
- This is the ONE place where we verify domain ↔ ORM mapping works

HOW mocking works:
- We use SQLite in-memory via aiosqlite for fast, isolated tests
- The adapter doesn't know the difference — it calls SQLAlchemy as usual
- This tests the CONTRACT between our domain objects and the database schema

WHAT "contract testing a port" means:
- StoragePort defines the contract: "give me a Recommendation, I persist it"
- This test verifies: "given a domain Recommendation, does it survive a round-trip?"
- If the schema changes, this test breaks → we know to update the adapter
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.outbound.timescale_adapter import (
    TimescaleAdapter,
    record_to_recommendation,
    record_to_snapshot,
    recommendation_to_record,
    snapshot_to_record,
)
from app.adapters.outbound.db.models import Base
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.ports.storage_port import MetricSnapshot


# =============================================================================
# Helper: in-memory SQLite adapter (no fixture — avoids pytest-asyncio compat)
# =============================================================================


async def _make_adapter() -> TimescaleAdapter:
    """Create a fresh in-memory SQLite adapter for each test.

    WHY not a fixture:
    - pytest-asyncio 0.23.x has a bug with async generator fixtures
    - inline setup is clearer about test isolation
    - each test gets a completely fresh database

    WHY bypass __init__:
    - TimescaleAdapter.__init__ passes pool_size/max_overflow to create_async_engine
    - SQLite uses StaticPool which doesn't accept those parameters
    - We create the engine with SQLite-compatible settings
    """
    adapter = object.__new__(TimescaleAdapter)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    adapter._engine = engine
    adapter._session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return adapter


# =============================================================================
# Sample domain objects
# =============================================================================


def make_recommendation(**overrides) -> Recommendation:
    """Create a sample Recommendation for testing."""
    defaults = {
        "namespace": "team-alpha",
        "category": Category.COST_RIGHTSIZING,
        "severity": Severity.CRITICAL,
        "resource": "cpu",
        "message": "CPU over-provisioned by 40%",
        "current_value": 8.0,
        "recommended_value": 4.0,
        "estimated_savings_usd": 87.60,
        "estimated_savings_kg_co2": 12.5,
    }
    defaults.update(overrides)
    return Recommendation(**defaults)


def make_snapshot(**overrides) -> MetricSnapshot:
    """Create a sample MetricSnapshot for testing."""
    defaults = {
        "time": datetime.now(timezone.utc),
        "namespace": "team-alpha",
        "team": "platform",
        "cost_center": "engineering",
        "environment": "prod",
        "cpu_core_hours": 720.0,
        "memory_gb_hours": 1440.0,
        "cost_usd": 150.0,
        "energy_kwh": 450.0,
        "co2e_kg": 65.0,
        "waste_pct": 25.0,
    }
    defaults.update(overrides)
    return MetricSnapshot(**defaults)


# =============================================================================
# Test recommendation persistence
# =============================================================================


class TestRecommendationPersistence:
    """Test that recommendations survive a full round-trip (save -> get)."""

    @pytest.mark.asyncio
    async def test_save_and_get_recommendation(self):
        """Saved recommendation should be retrievable with same values."""
        adapter = await _make_adapter()
        rec = make_recommendation()
        rec_id = await adapter.save_recommendation(rec)

        fetched = await adapter.get_recommendation(rec_id)

        assert fetched is not None
        assert fetched.namespace == "team-alpha"
        assert fetched.category == Category.COST_RIGHTSIZING
        assert fetched.severity == Severity.CRITICAL
        assert fetched.resource == "cpu"
        assert fetched.message == "CPU over-provisioned by 40%"
        assert float(fetched.current_value) == 8.0
        assert float(fetched.recommended_value) == 4.0
        assert fetched.estimated_savings_usd == pytest.approx(87.60)
        assert fetched.estimated_savings_kg_co2 == pytest.approx(12.5)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_save_returns_uuid(self):
        """save_recommendation should return a valid UUID."""
        adapter = await _make_adapter()
        rec = make_recommendation()
        rec_id = await adapter.save_recommendation(rec)

        assert isinstance(rec_id, uuid4().__class__)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self):
        """get_recommendation should return None for unknown ID."""
        adapter = await _make_adapter()
        fake_id = uuid4()
        fetched = await adapter.get_recommendation(fake_id)

        assert fetched is None
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_default(self):
        """list_recommendations should return all recommendations."""
        adapter = await _make_adapter()
        await adapter.save_recommendation(make_recommendation(namespace="team-alpha"))
        await adapter.save_recommendation(make_recommendation(namespace="team-beta"))

        recs = await adapter.list_recommendations()

        assert len(recs) == 2
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_filter_by_namespace(self):
        """list_recommendations with namespace filter should return subset."""
        adapter = await _make_adapter()
        await adapter.save_recommendation(make_recommendation(namespace="team-alpha"))
        await adapter.save_recommendation(make_recommendation(namespace="team-beta"))
        await adapter.save_recommendation(make_recommendation(namespace="team-alpha"))

        recs = await adapter.list_recommendations(namespace="team-alpha")

        assert len(recs) == 2
        assert all(r.namespace == "team-alpha" for r in recs)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_filter_by_status(self):
        """list_recommendations with status filter should return subset."""
        adapter = await _make_adapter()
        rec1 = make_recommendation()
        rec2 = make_recommendation()
        id1 = await adapter.save_recommendation(rec1)
        await adapter.save_recommendation(rec2)

        # Update one to "approved"
        await adapter.update_recommendation_status(id1, "approved")

        recs = await adapter.list_recommendations(status="approved")

        assert len(recs) == 1
        assert recs[0].namespace == "team-alpha"
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_ordered_by_created_at(self):
        """list_recommendations should return newest first."""
        adapter = await _make_adapter()
        await adapter.save_recommendation(make_recommendation(namespace="oldest"))
        # Small delay to ensure different created_at timestamps
        import asyncio
        await asyncio.sleep(0.01)
        await adapter.save_recommendation(make_recommendation(namespace="newest"))

        recs = await adapter.list_recommendations()

        # Newest should be first (ordered by created_at DESC)
        namespaces = [r.namespace for r in recs]
        assert namespaces.index("newest") < namespaces.index("oldest")
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_with_limit(self):
        """list_recommendations should respect limit parameter."""
        adapter = await _make_adapter()
        for i in range(5):
            await adapter.save_recommendation(make_recommendation(namespace=f"ns-{i}"))

        recs = await adapter.list_recommendations(limit=3)

        assert len(recs) == 3
        await adapter.close()

    @pytest.mark.asyncio
    async def test_list_recommendations_with_offset(self):
        """list_recommendations should respect offset parameter."""
        adapter = await _make_adapter()
        for i in range(5):
            await adapter.save_recommendation(make_recommendation(namespace=f"ns-{i}"))

        recs = await adapter.list_recommendations(offset=2, limit=2)

        assert len(recs) == 2
        await adapter.close()


# =============================================================================
# Test status updates
# =============================================================================


class TestStatusUpdates:
    """Test that recommendation status can be updated."""

    @pytest.mark.asyncio
    async def test_update_status_success(self):
        """Updating status of existing recommendation should return True."""
        adapter = await _make_adapter()
        rec_id = await adapter.save_recommendation(make_recommendation())

        result = await adapter.update_recommendation_status(rec_id, "approved")

        assert result is True
        await adapter.close()

    @pytest.mark.asyncio
    async def test_update_status_nonexistent_returns_false(self):
        """Updating status of nonexistent recommendation should return False."""
        adapter = await _make_adapter()
        fake_id = uuid4()

        result = await adapter.update_recommendation_status(fake_id, "approved")

        assert result is False
        await adapter.close()

    @pytest.mark.asyncio
    async def test_status_transition_pending_to_approved(self):
        """Should be able to transition from pending to approved."""
        adapter = await _make_adapter()
        rec_id = await adapter.save_recommendation(make_recommendation())
        await adapter.update_recommendation_status(rec_id, "approved")

        approved_recs = await adapter.list_recommendations(status="approved")
        assert len(approved_recs) == 1
        await adapter.close()

    @pytest.mark.asyncio
    async def test_status_transition_approved_to_executed(self):
        """Should be able to transition from approved to executed."""
        adapter = await _make_adapter()
        rec_id = await adapter.save_recommendation(make_recommendation())
        await adapter.update_recommendation_status(rec_id, "approved")
        await adapter.update_recommendation_status(rec_id, "executed")

        executed_recs = await adapter.list_recommendations(status="executed")
        assert len(executed_recs) == 1
        await adapter.close()

    @pytest.mark.asyncio
    async def test_status_transition_approved_to_dismissed(self):
        """Should be able to transition from approved to dismissed."""
        adapter = await _make_adapter()
        rec_id = await adapter.save_recommendation(make_recommendation())
        await adapter.update_recommendation_status(rec_id, "approved")
        await adapter.update_recommendation_status(rec_id, "dismissed")

        dismissed_recs = await adapter.list_recommendations(status="dismissed")
        assert len(dismissed_recs) == 1
        await adapter.close()


# =============================================================================
# Test snapshot persistence
# =============================================================================


class TestSnapshotPersistence:
    """Test that metric snapshots survive a full round-trip (save -> get)."""

    @pytest.mark.asyncio
    async def test_save_snapshot(self):
        """Saving a snapshot should not raise."""
        adapter = await _make_adapter()
        snap = make_snapshot()
        await adapter.save_snapshot(snap)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_save_and_get_snapshot_history(self):
        """Saved snapshots should be retrievable by namespace."""
        adapter = await _make_adapter()
        now = datetime.now(timezone.utc)
        snap1 = make_snapshot(time=now - timedelta(hours=2))
        snap2 = make_snapshot(time=now - timedelta(hours=1))

        await adapter.save_snapshot(snap1)
        await adapter.save_snapshot(snap2)

        history = await adapter.get_snapshot_history("team-alpha")

        assert len(history) == 2
        # Most recent first
        assert history[0].time > history[1].time
        await adapter.close()

    @pytest.mark.asyncio
    async def test_snapshot_history_filter_by_days(self):
        """get_snapshot_history should only return recent snapshots."""
        adapter = await _make_adapter()
        now = datetime.now(timezone.utc)
        snap_recent = make_snapshot(time=now - timedelta(days=1))
        snap_old = make_snapshot(time=now - timedelta(days=10))

        await adapter.save_snapshot(snap_recent)
        await adapter.save_snapshot(snap_old)

        # Get last 7 days
        history = await adapter.get_snapshot_history("team-alpha", days=7)

        assert len(history) == 1
        # SQLite strips timezone info, so compare without tzinfo
        assert history[0].time.replace(tzinfo=None) == snap_recent.time.replace(tzinfo=None)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_snapshot_history_filter_by_namespace(self):
        """get_snapshot_history should only return snapshots for the given namespace."""
        adapter = await _make_adapter()
        now = datetime.now(timezone.utc)
        snap_alpha = make_snapshot(namespace="team-alpha", time=now)
        snap_beta = make_snapshot(namespace="team-beta", time=now)

        await adapter.save_snapshot(snap_alpha)
        await adapter.save_snapshot(snap_beta)

        history = await adapter.get_snapshot_history("team-alpha")

        assert len(history) == 1
        assert history[0].namespace == "team-alpha"
        await adapter.close()

    @pytest.mark.asyncio
    async def test_snapshot_round_trip_preserves_values(self):
        """Snapshot values should survive a round-trip exactly."""
        adapter = await _make_adapter()
        snap = make_snapshot(
            cpu_core_hours=720.0,
            memory_gb_hours=1440.0,
            cost_usd=150.0,
            energy_kwh=450.0,
            co2e_kg=65.0,
            waste_pct=25.0,
        )

        await adapter.save_snapshot(snap)
        history = await adapter.get_snapshot_history("team-alpha")
        fetched = history[0]

        assert fetched.cpu_core_hours == pytest.approx(720.0)
        assert fetched.memory_gb_hours == pytest.approx(1440.0)
        assert fetched.cost_usd == pytest.approx(150.0)
        assert fetched.energy_kwh == pytest.approx(450.0)
        assert fetched.co2e_kg == pytest.approx(65.0)
        assert fetched.waste_pct == pytest.approx(25.0)
        await adapter.close()

    @pytest.mark.asyncio
    async def test_snapshot_empty_history(self):
        """No snapshots should return empty list."""
        adapter = await _make_adapter()
        history = await adapter.get_snapshot_history("nonexistent-namespace")

        assert history == []
        await adapter.close()


# =============================================================================
# Test mapper functions (pure functions, no DB)
# =============================================================================


class TestMappers:
    """Test that mapper functions correctly convert between domain and ORM."""

    def test_recommendation_round_trip(self):
        """Recommendation should survive domain -> ORM -> domain round-trip."""
        original = make_recommendation()
        record = recommendation_to_record(original)
        restored = record_to_recommendation(record)

        assert restored.namespace == original.namespace
        assert restored.category == original.category
        assert restored.severity == original.severity
        assert restored.resource == original.resource
        assert restored.message == original.message
        assert restored.current_value == original.current_value
        assert restored.recommended_value == original.recommended_value
        assert restored.estimated_savings_usd == original.estimated_savings_usd
        assert restored.estimated_savings_kg_co2 == original.estimated_savings_kg_co2

    def test_snapshot_round_trip(self):
        """Snapshot should survive domain -> ORM -> domain round-trip."""
        original = make_snapshot()
        record = snapshot_to_record(original)
        restored = record_to_snapshot(record)

        assert restored.time == original.time
        assert restored.namespace == original.namespace
        assert restored.team == original.team
        assert restored.cost_center == original.cost_center
        assert restored.environment == original.environment
        assert restored.cpu_core_hours == original.cpu_core_hours
        assert restored.memory_gb_hours == original.memory_gb_hours
        assert restored.cost_usd == original.cost_usd
        assert restored.energy_kwh == original.energy_kwh
        assert restored.co2e_kg == original.co2e_kg
        assert restored.waste_pct == original.waste_pct


# =============================================================================
# Test edge cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_multiple_save_and_get(self):
        """Multiple saves should all be retrievable."""
        adapter = await _make_adapter()
        ids = []
        for i in range(10):
            rec = make_recommendation(namespace=f"team-{i}")
            rec_id = await adapter.save_recommendation(rec)
            ids.append(rec_id)

        for rec_id in ids:
            fetched = await adapter.get_recommendation(rec_id)
            assert fetched is not None
        await adapter.close()

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """Closing adapter multiple times should not raise."""
        adapter = await _make_adapter()
        await adapter.close()
        await adapter.close()