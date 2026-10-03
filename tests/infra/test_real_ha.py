"""
HA on REAL infrastructure: two API replicas (compose profile "ha"), one database.

Needs INFRA_API_URL and INFRA_API2_URL (second replica) + INFRA_DATABASE_URL.
"""

import os
from datetime import datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.infra.conftest import env_or_skip


def _replicas():
    (api,) = env_or_skip("api")
    api2 = os.getenv("INFRA_API2_URL")
    if not api2:
        pytest.skip("second replica not configured (INFRA_API2_URL)")
    return api, api2


def test_exactly_one_leader_and_both_ready():
    leaders = []
    for url in _replicas():
        r = httpx.get(f"{url}/health/ready", timeout=60)
        assert r.status_code == 200, r.text
        leaders.append(r.json()["replica"]["is_leader"])
    assert sorted(leaders) == [False, True]


@pytest.mark.asyncio
async def test_no_cycle_written_twice():
    """Two replicas writing the same cycle would create snapshots ~0 s apart."""
    _replicas()
    (db,) = env_or_skip("database")
    interval = float(os.getenv("INFRA_SCHEDULER_INTERVAL_SECONDS", "60"))
    # Steady state only: during an upgrade FROM a version without leader election
    # (V1 image) one overlapping cycle was observed — expected, documented.
    since = os.getenv("INFRA_HA_SINCE")  # ISO time both V2 replicas were running (scripts/infra_env.sh)
    if not since:
        pytest.skip("INFRA_HA_SINCE not set")
    # docker gives nanoseconds + 'Z'; Python wants at most microseconds
    head, _, frac = since.rstrip("Z").partition(".")
    since_dt = datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}+00:00")
    engine = create_async_engine(db.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("""
                SELECT cluster, namespace,
                       EXTRACT(EPOCH FROM time - lag(time) OVER (PARTITION BY cluster, namespace ORDER BY time)) AS gap
                FROM metric_snapshots WHERE time > CAST(:since AS timestamptz)
                  AND cluster IN ('primary', 'secondary')"""), {"since": since_dt})).all()
    finally:
        await engine.dispose()
    gaps = [r.gap for r in rows if r.gap is not None]
    assert gaps, "no consecutive snapshots yet — let the scheduler run a few cycles"
    assert min(gaps) >= 0.8 * interval, f"duplicate cycle: gap {min(gaps):.1f}s < 0.8 × {interval}s"
