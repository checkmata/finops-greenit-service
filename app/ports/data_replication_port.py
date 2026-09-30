"""
V2.x port: control of ONE replicated database copy on ONE cluster.

The failover service uses it to promote the standby copy on failover, to fence a
stale writable copy (split-brain prevention) and to gate the failback on the data
state. Rebuilding a copy (resync) is deliberately NOT here: it destroys a data
directory, so it is an explicit, reviewed Git change (docs/REPLICATION.md).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.data_replication import DatabaseStatus


class DataReplicationPort(ABC):
    name: str

    @abstractmethod
    async def status(self) -> DatabaseStatus:
        """Never raises: an unreachable database is DatabaseStatus(reachable=False)."""

    @abstractmethod
    async def promote(self) -> None:
        """Standby → primary (new timeline). No-op if already writable."""

    @abstractmethod
    async def fence(self) -> None:
        """Prevent application writes on this copy (role NOLOGIN + terminate its sessions)."""

    async def close(self) -> None:  # pragma: no cover - trivial default
        return None
