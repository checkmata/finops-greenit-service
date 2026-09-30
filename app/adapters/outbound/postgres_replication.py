"""
PostgreSQL implementation of DataReplicationPort (V2.x, banking ledger).

Connects as the database superuser (password from a mounted secret file, see
cluster_config `data_services`) to the copy exposed on the cluster's NodePort.
Every call opens a short-lived connection with tight timeouts: during an outage the
failover loop must never hang on a frozen node.

Why each query:
- pg_is_in_recovery()             standby or primary
- timeline                        primary: from the current WAL file name (changes at
                                  promotion, before any checkpoint); standby: the
                                  timeline it receives (pg_stat_wal_receiver)
- rolcanlogin of the app role     fenced or not
- now() - pg_last_xact_replay_timestamp()   standby replay lag (seconds)
"""

from __future__ import annotations

import logging

import asyncpg

from app.domain.data_replication import DatabaseStatus
from app.ports.data_replication_port import DataReplicationPort

logger = logging.getLogger(__name__)


class PostgresReplicationAdapter(DataReplicationPort):
    def __init__(self, name: str, host: str, port: int, database: str, username: str, password: str,
                 app_role: str, timeout_seconds: float = 3.0) -> None:
        self.name = name
        self._kw = dict(host=host, port=port, database=database, user=username, password=password,
                        timeout=timeout_seconds, command_timeout=timeout_seconds * 2)
        self.app_role = app_role

    def __repr__(self) -> str:  # never leak the password
        return f"PostgresReplicationAdapter({self.name!r}, {self._kw['host']}:{self._kw['port']})"

    async def _connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(**self._kw)

    async def status(self) -> DatabaseStatus:
        try:
            conn = await self._connect()
        except Exception as exc:
            logger.debug("%s unreachable: %s", self.name, exc)
            return DatabaseStatus(reachable=False)
        try:
            in_recovery = bool(await conn.fetchval("SELECT pg_is_in_recovery()"))
            login = await conn.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = $1", self.app_role)
            if in_recovery:
                tli = await conn.fetchval("SELECT received_tli FROM pg_stat_wal_receiver")
                if tli is None:
                    tli = await conn.fetchval("SELECT timeline_id FROM pg_control_checkpoint()")
                lag = await conn.fetchval(
                    "SELECT EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())::float8")
                lsn = await conn.fetchval("SELECT pg_last_wal_replay_lsn()::text")
            else:
                walfile = await conn.fetchval("SELECT pg_walfile_name(pg_current_wal_lsn())")
                tli, lag = int(walfile[:8], 16), None
                lsn = await conn.fetchval("SELECT pg_current_wal_lsn()::text")
            return DatabaseStatus(True, in_recovery, int(tli) if tli is not None else None,
                                  bool(login) if login is not None else None, lag, lsn)
        except Exception as exc:
            logger.warning("%s status query failed: %s", self.name, exc)
            return DatabaseStatus(reachable=False)
        finally:
            await conn.close()

    async def promote(self) -> None:
        conn = await self._connect()
        try:
            if await conn.fetchval("SELECT pg_is_in_recovery()"):
                ok = await conn.fetchval("SELECT pg_promote(true, 60)")
                if not ok:
                    raise RuntimeError(f"{self.name}: pg_promote did not complete within 60 s")
        finally:
            await conn.close()

    async def fence(self) -> None:
        conn = await self._connect()
        try:
            # Identifier quoting: the role name comes from configuration, not from users.
            role = self.app_role.replace('"', '""')
            await conn.execute(f'ALTER ROLE "{role}" NOLOGIN')
            await conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = $1",
                               self.app_role)
        finally:
            await conn.close()
