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

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# Lifespan context manager — starts/stops background tasks
# =============================================================================
# WHY asynccontextmanager:
# - FastAPI's lifespan expects an async context manager
# - The yield point is where the app is running
# - Before yield: startup logic (start scheduler)
# - After yield: shutdown logic (stop scheduler)
#
# HOW it works:
# 1. FastAPI starts → calls __aenter__ → starts scheduler task
# 2. App runs → serves requests
# 3. FastAPI stops → calls __aexit__ → cancels scheduler task
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan context manager.

    Starts the snapshot scheduler as a background task on startup.
    Stops the scheduler cleanly on shutdown.

    WHY this pattern:
    - The scheduler shares the same event loop as the API server
    - No separate process to manage (simpler than Celery/RQ)
    - Clean shutdown prevents orphaned tasks and resource leaks
    - Integrates with FastAPI's lifecycle events
    """
    # -------------------------------------------------------------------------
    # Startup: start the snapshot scheduler
    # -------------------------------------------------------------------------
    logger.info("Starting finops-greenit-service...")

    # Create adapters for the scheduler
    metrics_adapter = PrometheusMetricsAdapter(
        prometheus_url=settings.PROMETHEUS_URL,
        timeout_seconds=settings.PROMETHEUS_QUERY_TIMEOUT_SECONDS,
    )
    storage_adapter = TimescaleAdapter(database_url=settings.DATABASE_URL)

    # Start the scheduler as a background task
    from app.scheduler.snapshot_job import start_snapshot_scheduler

    scheduler_task = asyncio.create_task(
        start_snapshot_scheduler(
            metrics_port=metrics_adapter,
            storage_port=storage_adapter,
            interval_seconds=300,  # 5 minutes
        )
    )
    logger.info("Snapshot scheduler started (interval=300s)")

    # -------------------------------------------------------------------------
    # App is running — yield control back to FastAPI
    # -------------------------------------------------------------------------
    yield

    # -------------------------------------------------------------------------
    # Shutdown: stop the snapshot scheduler
    # -------------------------------------------------------------------------
    logger.info("Stopping finops-greenit-service...")

    # Cancel the scheduler task
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        logger.info("Snapshot scheduler cancelled")

    # Close adapter connections
    await metrics_adapter.close()
    await storage_adapter.close()

    logger.info("finops-greenit-service stopped")


# =============================================================================
# Application factory
# =============================================================================


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    WHY a factory:
    - Enables testing with different configurations
    - Makes dependencies explicit
    - Follows FastAPI's recommended pattern

    Returns:
        Configured FastAPI application with all routers.
    """
    app = FastAPI(
        title="finops-greenit-service",
        description="FinOps and Green IT optimization service for OpenShift",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------------
    # Register routers
    # -------------------------------------------------------------------------
    from app.adapters.inbound.api.finops_router import router as finops_router
    from app.adapters.inbound.api.greenit_router import router as greenit_router
    from app.adapters.inbound.api.health_router import router as health_router
    from app.adapters.inbound.api.recommendations_router import router as recommendations_router

    app.include_router(finops_router)
    app.include_router(greenit_router)
    app.include_router(recommendations_router)
    app.include_router(health_router)

    logger.info("Routers registered: finops, greenit, recommendations, health")

    return app


# =============================================================================
# Create the app instance (for uvicorn)
# =============================================================================
# This module-level instance is what uvicorn imports:
#   uvicorn app.adapters.inbound.api.main:app --reload
# =============================================================================

app = create_app()