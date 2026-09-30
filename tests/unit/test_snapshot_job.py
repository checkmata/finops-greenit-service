"""
Unit tests for snapshot_job — scheduler error-handling tests.

WHY we test error-handling separately:
- The scheduler is the ONLY place that writes to storage
- If one cycle fails, it should log and continue (not crash the loop)
- If the scheduler crashes, no more snapshots are written → data loss

WHAT we test:
- Prometheus failure: scheduler logs error and continues to next namespace
- Storage failure: scheduler logs error and continues to next namespace
- Partial failure: some namespaces succeed, others fail
- Empty metrics: scheduler handles gracefully (no crash)

WHAT we don't test (those are integration tests):
- Actual Prometheus connectivity (use mock)
- Actual database writes (use mock)
- Correctness of recommendation generation (tested in domain engine tests)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.energy import EnergyAttribution, EnergyMeasurement
from app.domain.models.pod_metric import PodMetric
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import StoragePort

NOW = datetime.now(timezone.utc)
BYTES_PER_GB = 1024**3


# =============================================================================
# Fixtures — mock adapters
# =============================================================================


@pytest.fixture
def mock_metrics():
    """Create a mock MetricsPort for testing."""
    mock = AsyncMock(spec=MetricsPort)
    mock.get_pod_metrics.return_value = [
        PodMetric(
            namespace="team-alpha",
            team="platform",
            cost_center="engineering",
            environment="prod",
            cpu_core_hours=4.0,
            memory_bytes=8 * BYTES_PER_GB,
            cpu_requested=8.0,
            memory_requested=16 * BYTES_PER_GB,
            timestamp=NOW,
        )
    ]
    mock.check_data_quality.return_value = DataQuality(
        healthy_sources=frozenset(TelemetrySource),
        timestamp=NOW,
    )
    mock.get_energy_kwh.return_value = 5.0
    # V1 port methods — realistic values so the normal path is exercised
    mock.ping.return_value = True
    mock.list_namespaces.return_value = ["team-alpha"]
    mock.get_workload_metrics.return_value = []
    mock.get_workload_energy.return_value = []
    mock.get_storage_metrics.return_value = []
    mock.get_energy.return_value = EnergyMeasurement(
        "team-alpha", 5.0, None, EnergyAttribution.CONTAINER, "kepler_container_cpu_joules_total"
    )
    return mock


@pytest.fixture
def mock_storage():
    """Create a mock StoragePort for testing."""
    mock = AsyncMock(spec=StoragePort)
    mock.save_recommendation.return_value="test-uuid"
    mock.upsert_recommendation.return_value = ("test-uuid", "insert")
    mock.save_snapshot.return_value = None
    return mock


@pytest.fixture
def mock_metrics_failing():
    """Create a mock MetricsPort that raises PrometheusUnavailableError."""
    from app.adapters.outbound.prometheus_adapter import PrometheusUnavailableError

    mock = AsyncMock(spec=MetricsPort)
    mock.get_pod_metrics.side_effect = PrometheusUnavailableError("Prometheus down")
    return mock


@pytest.fixture
def mock_storage_failing():
    """Create a mock StoragePort that raises exceptions."""
    mock = AsyncMock(spec=StoragePort)
    mock.save_recommendation.side_effect = Exception("Database connection failed")
    mock.save_snapshot.side_effect = Exception("Database connection failed")
    return mock


# =============================================================================
# Test run_snapshot_cycle — normal cases
# =============================================================================


class TestSnapshotCycleNormal:
    """Normal cases for snapshot cycle execution."""

    @pytest.mark.asyncio
    async def test_successful_cycle(self, mock_metrics, mock_storage):
        """Successful cycle should return recommendation count."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        rec_count = await run_snapshot_cycle(
            metrics_port=mock_metrics,
            storage_port=mock_storage,
            namespaces=["team-alpha"],
        )
        assert rec_count >= 0
        mock_metrics.get_pod_metrics.assert_called_once_with(namespace="team-alpha")
        mock_storage.save_snapshot.assert_called_once()
        saved = mock_storage.save_snapshot.call_args.args[0]
        assert saved.cost_usd > 0  # V1: cost is historized (MVP stored 0.0)
        assert saved.co2e_kg == pytest.approx(5.0 * 1.4 * 0.475)

    @pytest.mark.asyncio
    async def test_cycle_with_multiple_namespaces(self, mock_metrics, mock_storage):
        """Cycle with multiple namespaces should process each one."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        rec_count = await run_snapshot_cycle(
            metrics_port=mock_metrics,
            storage_port=mock_storage,
            namespaces=["team-alpha", "team-beta"],
        )
        assert rec_count >= 0
        assert mock_metrics.get_pod_metrics.call_count == 2


# =============================================================================
# Test run_snapshot_cycle — error handling
# =============================================================================


class TestSnapshotCycleErrorHandling:
    """Error handling cases for snapshot cycle execution."""

    @pytest.mark.asyncio
    async def test_prometheus_failure_continues(self, mock_metrics_failing, mock_storage, caplog):
        """Prometheus failure should log error and continue (not crash)."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        with caplog.at_level(logging.WARNING):
            rec_count = await run_snapshot_cycle(
                metrics_port=mock_metrics_failing,
                storage_port=mock_storage,
                namespaces=["team-alpha"],
            )
        # Should return 0 recommendations (no metrics fetched)
        assert rec_count == 0
        # Should log the warning
        assert "Failed to fetch pod metrics" in caplog.text or "Snapshot failed" in caplog.text

    @pytest.mark.asyncio
    async def test_storage_failure_continues(self, mock_metrics, mock_storage_failing, caplog):
        """Storage failure should log error and continue (not crash)."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        with caplog.at_level(logging.ERROR):
            await run_snapshot_cycle(
                metrics_port=mock_metrics,
                storage_port=mock_storage_failing,
                namespaces=["team-alpha"],
            )
        # Should still attempt to save (even if it fails)
        # The scheduler logs errors but doesn't crash
        assert "Failed to save" in caplog.text or "Snapshot failed" in caplog.text

    @pytest.mark.asyncio
    async def test_partial_failure_continues(self, mock_metrics, mock_storage, caplog):
        """Partial failure should process remaining namespaces."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        # Make the first call to get_pod_metrics fail for team-alpha
        from app.adapters.outbound.prometheus_adapter import PrometheusUnavailableError

        original_get_pod_metrics = mock_metrics.get_pod_metrics

        async def failing_get_pod_metrics(namespace: str):
            if namespace == "team-alpha":
                raise PrometheusUnavailableError("Prometheus down")
            return await original_get_pod_metrics(namespace)

        mock_metrics.get_pod_metrics.side_effect = failing_get_pod_metrics

        with caplog.at_level(logging.WARNING):
            await run_snapshot_cycle(
                metrics_port=mock_metrics,
                storage_port=mock_storage,
                namespaces=["team-alpha", "team-beta"],
            )
        # Should process team-beta even though team-alpha failed
        # The scheduler logs errors but doesn't crash
        assert "Failed to fetch pod metrics" in caplog.text or "Snapshot failed" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_pod_metrics_skips_generation(self, mock_metrics, mock_storage):
        """Empty pod metrics should skip recommendation generation."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        mock_metrics.get_pod_metrics.return_value = []

        rec_count = await run_snapshot_cycle(
            metrics_port=mock_metrics,
            storage_port=mock_storage,
            namespaces=["team-alpha"],
        )
        # Should return 0 recommendations (no pods to analyze)
        assert rec_count == 0


# =============================================================================
# Test start_snapshot_scheduler — lifecycle
# =============================================================================


class TestStartSnapshotScheduler:
    """Tests for the scheduler lifecycle."""

    @pytest.mark.asyncio
    async def test_scheduler_can_be_cancelled(self, mock_metrics, mock_storage):
        """Scheduler should be cancellable without hanging."""
        from app.scheduler.snapshot_job import start_snapshot_scheduler

        # Create a task that we'll cancel after a short delay
        task = asyncio.create_task(
            start_snapshot_scheduler(
                metrics_port=mock_metrics,
                storage_port=mock_storage,
                interval_seconds=1,  # Very short interval for testing
            )
        )

        # Let it run for a moment
        await asyncio.sleep(0.1)

        # Cancel it
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

        # If we get here, the scheduler was cancelled successfully
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_run_once_function(self, mock_metrics, mock_storage):
        """run_once should execute a single cycle and return."""
        from app.scheduler.snapshot_job import run_once

        rec_count = await run_once(
            metrics_port=mock_metrics,
            storage_port=mock_storage,
            namespaces=["team-alpha"],
        )
        assert rec_count >= 0

class TestAbsentNamespace:
    @pytest.mark.asyncio
    async def test_namespace_without_requests_or_usage_is_not_snapshotted(self, mock_metrics, mock_storage):
        """Regression from the REAL E2E test: no zero-valued snapshot for a namespace absent from a cluster."""
        from app.scheduler.snapshot_job import run_snapshot_cycle

        mock_metrics.get_pod_metrics.return_value = [PodMetric(
            namespace="trading", team="unknown", cost_center="unknown", environment="unknown",
            cpu_core_hours=0.0, memory_bytes=0.0, cpu_requested=0.0, memory_requested=0.0, timestamp=NOW,
        )]
        assert await run_snapshot_cycle(mock_metrics, mock_storage, namespaces=["trading"]) == 0
        mock_storage.save_snapshot.assert_not_called()
        mock_storage.upsert_recommendation.assert_not_called()
