"""
FastAPI application factory — the entry point for the finops-greenit-service.

WHY a factory function (not a global app):
- Enables testing with different configurations (e.g., different adapters)
- Makes the app's dependencies explicit (passed as parameters)
- Follows FastAPI's recommended pattern for production applications

WHY lifespan context manager:
- Starts the snapshot scheduler as a background task on startup
- Stops the scheduler cleanly on shutdown (prevents orphaned tasks)
- Integrates with FastAPI's lifecycle events (startup/shutdown)
- Ensures the scheduler shares the same event loop as the API server

Architecture:
- Routers handle HTTP (inbound adapters)
- Ports define contracts (interfaces)
- Adapters implement contracts (Prometheus, TimescaleDB)
- Domain engines do business logic (pure functions)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from app.composition import build_container
from app.config import settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: build adapters once (composition root) and start the scheduler.
    Shutdown: cancel the scheduler and close every connection pool."""
    configure_logging(settings)
    logger.info("Starting finops-greenit-service...")

    container = build_container(settings)
    app.state.container = container
    if settings.DATABASE_URL.startswith("sqlite"):
        # Local demo mode ONLY (scripts/run_local_demo.sh): no TimescaleDB,
        # tables created from the ORM. Production uses the SQL migrations.
        await container.storage.ensure_tables_exist()
        logger.warning("SQLite demo storage in use — not for production (no hypertables/retention)")
    logger.info("Observed clusters: %s (primary=%s)", container.registry.names, container.registry.primary)

    tasks: list[asyncio.Task] = []
    if settings.SCHEDULER_ENABLED:
        from app.scheduler.snapshot_job import start_snapshot_scheduler

        tasks.append(asyncio.create_task(
            start_snapshot_scheduler(
                storage_port=container.storage,
                cluster_ports=container.registry.ports(),
                interval_seconds=settings.SCHEDULER_INTERVAL_SECONDS,
                leader=container.leader,
            )
        ))
        logger.info("Snapshot scheduler started (interval=%ss, leader-gated)", settings.SCHEDULER_INTERVAL_SECONDS)
    if container.failover is not None:
        from app.failover_service import run_data_guard_loop, run_failover_loop

        tasks.append(asyncio.create_task(
            run_failover_loop(container.failover, container.leader, settings.FAILOVER_TICK_SECONDS)))
        if container.failover.data:
            tasks.append(asyncio.create_task(
                run_data_guard_loop(container.failover, container.leader, settings.DATA_GUARD_PERIOD_SECONDS)))
            logger.info("Data guard started (period=%ss, split-brain fencing)", settings.DATA_GUARD_PERIOD_SECONDS)
        logger.info("Failover controller started (mode=%s, failure=%ss, recovery=%ss)", settings.FAILOVER_MODE,
                    settings.FAILOVER_FAILURE_THRESHOLD_SECONDS, settings.FAILOVER_RECOVERY_THRESHOLD_SECONDS)

    if container.agent is not None:
        try:
            recovered = await container.agent.recover_interrupted()
            if recovered:
                logger.warning("agent: %d interrupted action(s) failed and rolled back", recovered)
        except Exception as exc:
            logger.error("agent recovery failed: %s", exc)
    if container.ml is not None and settings.ML_ENABLED:
        from app.ml_service import run_ml_loop

        tasks.append(asyncio.create_task(run_ml_loop(
            container.ml, container.registry.names, container.leader, settings.ML_INTERVAL_SECONDS)))
        logger.info("ML cycle started (interval=%ss, history=%sh, leader-gated)",
                    settings.ML_INTERVAL_SECONDS, settings.ML_HISTORY_HOURS)

    yield

    logger.info("Stopping finops-greenit-service...")
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await container.close()
    logger.info("finops-greenit-service stopped")


def create_app() -> FastAPI:
    """Create the FastAPI application with all routers and /metrics."""
    app = FastAPI(
        title="finops-greenit-service",
        description="FinOps and Green IT observability for (multi-)cluster Kubernetes/OpenShift",
        version="0.2.0",
        lifespan=lifespan,
    )

    from app.adapters.inbound.api.clusters_router import router as clusters_router
    from app.adapters.inbound.api.failover_router import router as failover_router
    from app.adapters.inbound.api.finops_router import router as finops_router
    from app.adapters.inbound.api.greenit_router import router as greenit_router
    from app.adapters.inbound.api.health_router import router as health_router
    from app.adapters.inbound.api.history_router import router as history_router
    from app.adapters.inbound.api.agent_router import router as agent_router
    from app.adapters.inbound.api.ml_router import router as ml_router
    from app.adapters.inbound.api.recommendations_router import router as recommendations_router

    for r in (finops_router, greenit_router, recommendations_router, clusters_router,
              history_router, failover_router, health_router, ml_router, agent_router):
        app.include_router(r)

    # Service self-metrics (scheduler health) — the Deployment already declared
    # prometheus.io/path=/metrics but nothing was exposed in the MVP.
    app.mount("/metrics", make_asgi_app())
    return app


# Module-level instance imported by uvicorn:
#   uvicorn app.adapters.inbound.api.main:app
app = create_app()
