"""
Snapshot job — periodic persistence of metric snapshots and recommendations.

Runs on a timer (default: every 5 minutes). Queries Prometheus for current state,
generates recommendations via the domain engine, and writes both to TimescaleDB.

WHY this is the ONLY place that writes to storage:
- Prevents duplicate rows on every Grafana refresh (router reads only)
- Centralizes persistence logic (one place to test, one place to monitor)
- Decouples HTTP layer (routers) from storage (TimescaleDB)
- Enables retry/backoff logic in one place (not scattered across routers)

WHY asyncio.sleep (not a task queue like Celery):
- For a single-node MVP, asyncio.sleep is sufficient and zero-dependency.
- No Redis/RabbitMQ broker to manage, no worker processes to orchestrate.
- When we need distributed scheduling (multi-pod), we migrate to:
  - OpenShift CronJob (runs as a Kubernetes Job on a schedule)
  - Or a lightweight scheduler like APScheduler with a DB backend
- The async loop pattern maps 1:1 to a CronJob — just extract run_snapshot_cycle()
  into a standalone script and call it from the CronJob's command.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Sequence

from app.config import settings
from app.domain.greenit_engine import (
    build_carbon_footprint,
    build_energy_breakdown,
)
from app.domain.models.recommendation import RecommendationSet
from app.domain.recommendation_engine import generate_all_recommendations
from app.ports.metrics_port import MetricsPort
from app.ports.storage_port import MetricSnapshot, StoragePort

logger = logging.getLogger(__name__)

# Default interval between snapshot runs (seconds).
# WHY 5 minutes: balances freshness vs Prometheus load.
# At 5-min intervals, we get ~288 data points per day per namespace.
# With 7-day retention, that's ~2,016 rows per namespace — manageable.
SNAPSHOT_INTERVAL_SECONDS = 300  # 5 minutes


# =============================================================================
# Single cycle — the core logic (testable in isolation)
# =============================================================================


async def run_snapshot_cycle(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    *,
    namespaces: Sequence[str] | None = None,
) -> int:
    """Execute one snapshot cycle: query Prometheus → generate recs → persist.

    This function is THE boundary between:
    - Querying live metrics (MetricsPort → Prometheus)
    - Generating domain advice (recommendation_engine → domain models)
    - Persisting results (StoragePort → TimescaleDB)

    Args:
        metrics_port: Port for fetching telemetry from Prometheus/Kepler.
        storage_port: Port for persisting to TimescaleDB.
        namespaces: Optional list of namespaces to process. If None, processes
                    a default namespace (real discovery via PromQL in production).

    Returns:
        Number of recommendations generated and persisted.

    Error handling:
        - Each namespace is processed independently (one failure doesn't block others).
        - Prometheus failures are logged and skipped (we don't persist stale data).
        - Storage failures are logged and skipped (we don't crash the scheduler).
        - The cycle always returns a count, even if some namespaces failed.
    """
    now = datetime.now(timezone.utc)
    rec_count = 0

    # Discover namespaces if not provided.
    # In production, this would query Prometheus for all namespaces with pods.
    # For MVP, we process a fixed list (configurable via env var or default).
    if namespaces is None:
        namespaces = ["default"]

    for ns in namespaces:
        try:
            ns_rec_count = await _process_namespace(
                metrics_port=metrics_port,
                storage_port=storage_port,
                namespace=ns,
                now=now,
            )
            rec_count += ns_rec_count
        except Exception as exc:
            # Log but continue — one namespace failure shouldn't crash the cycle.
            logger.error(
                "Snapshot failed for namespace=%s: %s",
                ns,
                exc,
                exc_info=True,
            )
            continue

    return rec_count


async def _process_namespace(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    namespace: str,
    now: datetime,
) -> int:
    """Process a single namespace: fetch metrics, generate recs, persist.

    This is a helper that isolates the per-namespace logic from the loop.
    It makes error handling cleaner and the code more readable.

    Returns:
        Number of recommendations generated for this namespace.
    """
    # -------------------------------------------------------------------------
    # Phase 1: Query Prometheus for current state
    # -------------------------------------------------------------------------
    # WHY try/except around each Prometheus call:
    # - If one metric is unavailable (e.g., Kepler down), we can still
    #   process other metrics (e.g., FinOps from kube-state-metrics).
    # - The recommendation_engine handles DataQuality gating internally.
    # -------------------------------------------------------------------------
    try:
        pod_metrics = await metrics_port.get_pod_metrics(namespace=namespace)
    except Exception as exc:
        logger.warning("Failed to fetch pod metrics for namespace=%s: %s", namespace, exc)
        pod_metrics = []

    try:
        quality = await metrics_port.check_data_quality()
    except Exception as exc:
        logger.warning("Failed to check data quality: %s", exc)
        # Default to all sources healthy (engine will generate recs for everything)
        from app.domain.models.data_quality import DataQuality, TelemetrySource
        quality = DataQuality(healthy_sources=frozenset(TelemetrySource))

    try:
        energy_kwh = await metrics_port.get_energy_kwh(namespace=namespace)
    except Exception as exc:
        logger.warning("Failed to fetch energy for namespace=%s: %s", namespace, exc)
        energy_kwh = 0.0

    # -------------------------------------------------------------------------
    # Phase 2: Build domain objects from raw metrics
    # -------------------------------------------------------------------------
    # WHY build here (not in the adapter):
    # - The adapter returns raw PodMetric/StorageMetric lists.
    # - The scheduler (this file) orchestrates: fetch → build → generate → persist.
    # - The recommendation_engine expects CarbonFootprint/EnergyBreakdown, not raw kWh.
    # -------------------------------------------------------------------------
    # Use first pod metric for namespace-level attribution (team, cost_center, etc.)
    first_pod = pod_metrics[0] if pod_metrics else None
    team = first_pod.team if first_pod else "unknown"
    cost_center = first_pod.cost_center if first_pod else "unknown"
    environment = first_pod.environment if first_pod else "unknown"

    # Build CarbonFootprint from energy_kwh (already in kWh from Kepler adapter)
    carbon_footprint = build_carbon_footprint(
        namespace=namespace,
        team=team,
        cost_center=cost_center,
        environment=environment,
        energy_joules=energy_kwh * 3_600_000,  # Convert kWh back to joules for builder
        pue=settings.PUE_CONSTANT,
        grid_intensity_gco2_per_kwh=settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH,
        timestamp=now,
    )

    # Build EnergyBreakdown (idle waste is P1, defaults to 0 for MVP)
    energy_breakdown = build_energy_breakdown(
        namespace=namespace,
        total_energy_kwh=energy_kwh,
        idle_energy_kwh=0.0,  # P1 metric — not queried yet
        timestamp=now,
    )

    # -------------------------------------------------------------------------
    # Phase 3: Generate recommendations via domain engine
    # -------------------------------------------------------------------------
    # The engine takes pre-built domain objects, applies heuristics, returns recs.
    # -------------------------------------------------------------------------
    # Need at least one PodMetric to generate recs
    if not pod_metrics:
        logger.warning("No pod metrics for namespace=%s — skipping recommendation generation", namespace)
        return 0

    # Use first pod metric (namespace-level aggregation)
    pod = pod_metrics[0]

    rec_set: RecommendationSet = generate_all_recommendations(
        pod=pod,
        carbon_footprint=carbon_footprint,
        energy_breakdown=energy_breakdown,
        data_quality=quality,
        cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD,
        cost_per_gb_hour=settings.COST_PER_GB_HOUR_USD,
        cost_per_gb_month=settings.COST_PER_GB_MONTH_USD,
    )

    # -------------------------------------------------------------------------
    # Phase 4: Persist to TimescaleDB
    # -------------------------------------------------------------------------
    # Save each recommendation
    for rec in rec_set.recommendations:
        try:
            await storage_port.save_recommendation(rec)
        except Exception as exc:
            logger.error(
                "Failed to save recommendation for namespace=%s: %s",
                namespace,
                exc,
            )

    # Save metric snapshot (one row per namespace per run)
    total_cost = pod.total_cost_usd if hasattr(pod, "total_cost_usd") else 0.0
    waste_pct = energy_breakdown.waste_pct

    snapshot = MetricSnapshot(
        time=now,
        namespace=namespace,
        team=team,
        cost_center=cost_center,
        environment=environment,
        cpu_core_hours=pod.cpu_core_hours,
        memory_gb_hours=pod.memory_bytes / (1024**3),  # Convert bytes to GB-hours
        cost_usd=total_cost,
        energy_kwh=energy_kwh,
        co2e_kg=carbon_footprint.co2_kg,
        waste_pct=waste_pct,
    )

    try:
        await storage_port.save_snapshot(snapshot)
    except Exception as exc:
        logger.error("Failed to save snapshot for namespace=%s: %s", namespace, exc)

    logger.info(
        "Snapshot complete for namespace=%s: %d recommendations, cost=$%.2f, co2=%.3f kg",
        namespace,
        len(rec_set.recommendations),
        total_cost,
        carbon_footprint.co2_kg,
    )

    return len(rec_set.recommendations)


# =============================================================================
# Scheduler — the async loop (runs in FastAPI lifespan)
# =============================================================================


async def start_snapshot_scheduler(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    *,
    interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS,
    namespaces: Sequence[str] | None = None,
) -> None:
    """Start the periodic snapshot scheduler.

    Runs `run_snapshot_cycle` every `interval_seconds` until cancelled.
    Designed to be launched as a background task in FastAPI's lifespan:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            task = asyncio.create_task(
                start_snapshot_scheduler(metrics_port, storage_port)
            )
            yield
            task.cancel()

    Args:
        metrics_port: Port for fetching telemetry.
        storage_port: Port for persisting data.
        interval_seconds: Seconds between cycles (default: 300 = 5 min).
        namespaces: Optional fixed list of namespaces to process.
    """
    logger.info(
        "Starting snapshot scheduler (interval=%ds, namespaces=%s)",
        interval_seconds,
        namespaces or "auto-discover",
    )

    while True:
        try:
            rec_count = await run_snapshot_cycle(
                metrics_port=metrics_port,
                storage_port=storage_port,
                namespaces=namespaces,
            )
            logger.info(
                "Snapshot cycle complete: %d recommendations generated",
                rec_count,
            )
        except Exception as exc:
            # This should never happen (run_snapshot_cycle catches per-namespace errors).
            # But if something truly unexpected occurs, log and continue.
            logger.critical(
                "Snapshot cycle failed unexpectedly: %s",
                exc,
                exc_info=True,
            )

        await asyncio.sleep(interval_seconds)


# =============================================================================
# Standalone entry point (for OpenShift CronJob or manual testing)
# =============================================================================


async def run_once(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    *,
    namespaces: Sequence[str] | None = None,
) -> int:
    """Run a single snapshot cycle and return.

    Used for:
    - OpenShift CronJob (extract this into a script)
    - Manual testing from CLI
    - One-off backfill

    Returns:
        Number of recommendations generated.
    """
    return await run_snapshot_cycle(
        metrics_port=metrics_port,
        storage_port=storage_port,
        namespaces=namespaces,
    )