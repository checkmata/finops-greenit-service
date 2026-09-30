"""Leader election on the REAL PostgreSQL (advisory lock): exclusivity and takeover."""

import pytest

from app.adapters.outbound.postgres_leader import PostgresAdvisoryLeader
from tests.infra.conftest import env_or_skip

TEST_KEY = 0x7E57  # distinct from the service's key: never steals the running leader's lock


@pytest.mark.asyncio
async def test_exactly_one_leader_then_takeover_after_release():
    (db,) = env_or_skip("database")
    a, b = PostgresAdvisoryLeader(db, key=TEST_KEY), PostgresAdvisoryLeader(db, key=TEST_KEY)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False          # exclusive
        assert await a.try_acquire() is True           # still leader on the next tick
        await a.release()                              # leader goes away (connection closed)
        assert await b.try_acquire() is True           # follower takes over
    finally:
        await a.release()
        await b.release()


@pytest.mark.asyncio
async def test_crashed_leader_connection_frees_the_lock():
    (db,) = env_or_skip("database")
    a, b = PostgresAdvisoryLeader(db, key=TEST_KEY), PostgresAdvisoryLeader(db, key=TEST_KEY)
    try:
        assert await a.try_acquire()
        a._conn.terminate()                            # simulate a crash: no unlock, socket just dies
        assert await b.try_acquire() is True
        assert await a.try_acquire() is False and a.is_leader is False
    finally:
        await a.release()
        await b.release()
