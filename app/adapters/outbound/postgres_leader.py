"""
Leader election with a PostgreSQL session-level advisory lock (V2 HA).

WHY: with N replicas behind a Service, every replica serves the API, but only ONE
may run the scheduler (otherwise every cycle is written N times) and the failover
controller (otherwise N replicas would scale the standby concurrently).

HOW: the leader holds `pg_try_advisory_lock(KEY)` on a DEDICATED connection.
The lock lives as long as that connection: if the leader crashes, is killed or
loses the network, PostgreSQL releases it and another replica acquires it on its
next tick. No extra infrastructure: the database is already shared.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.ports.failover_ports import LeaderElectionPort

logger = logging.getLogger(__name__)

# Arbitrary, stable 64-bit key identifying "finops-greenit scheduler leadership".
LEADER_LOCK_KEY = 0x46494E4F5053  # "FINOPS"


def _asyncpg_dsn(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class PostgresAdvisoryLeader(LeaderElectionPort):
    def __init__(self, database_url: str, key: int = LEADER_LOCK_KEY) -> None:
        self._dsn = _asyncpg_dsn(database_url)
        self._key = key
        self._conn: asyncpg.Connection | None = None
        self._leader = False
        # V3 fix (seen live: "now the LEADER" logged twice in the same second): the
        # scheduler, failover and ML loops call try_acquire concurrently; one asyncpg
        # connection cannot run two operations at once ("another operation is in
        # progress" → spurious loss of leadership). Serialize the calls.
        self._lock = asyncio.Lock()

    @property
    def is_leader(self) -> bool:
        return self._leader

    async def try_acquire(self) -> bool:
        async with self._lock:
            return await self._try_acquire()

    async def _try_acquire(self) -> bool:
        try:
            if self._conn is None or self._conn.is_closed():
                self._leader = False
                self._conn = await asyncpg.connect(self._dsn, timeout=5)
            if self._leader:
                # Still leader? The lock is held as long as the session is alive.
                await self._conn.execute("SELECT 1")
                return True
            self._leader = bool(await self._conn.fetchval("SELECT pg_try_advisory_lock($1)", self._key))
            if self._leader:
                logger.info("leader election: this replica is now the LEADER")
            return self._leader
        except Exception as exc:  # connection lost → not leader (the lock is gone with it)
            if self._leader:
                logger.warning("leader election: lost leadership (%s)", exc)
            self._leader = False
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
            self._conn = None
            return False

    async def release(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            try:
                if self._leader:
                    await self._conn.execute("SELECT pg_advisory_unlock($1)", self._key)
            finally:
                await self._conn.close()
        self._conn = None
        self._leader = False


class AlwaysLeader(LeaderElectionPort):
    """Single-replica / SQLite demo mode: this process is the only writer."""

    @property
    def is_leader(self) -> bool:
        return True

    async def try_acquire(self) -> bool:
        return True

    async def release(self) -> None:
        return None
