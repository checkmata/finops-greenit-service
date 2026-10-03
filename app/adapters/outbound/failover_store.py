"""FailoverStatePort on PostgreSQL/TimescaleDB (tables from migration 004)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.outbound.db.models import FailoverEventRecord, FailoverStateRecord
from app.domain.failover_controller import ControllerState, FailoverMode
from app.ports.failover_ports import FailoverEvent, FailoverStatePort


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SqlFailoverStore(FailoverStatePort):
    """Shares the TimescaleAdapter engine (one pool per process)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def load_state(self) -> ControllerState | None:
        async with self._sessions() as s:
            r = (await s.execute(select(FailoverStateRecord).where(FailoverStateRecord.id == 1))).scalar_one_or_none()
            if r is None:
                return None
            return ControllerState(FailoverMode(r.mode), r.active_cluster, _utc(r.primary_unhealthy_since),
                                   _utc(r.primary_healthy_since), _utc(r.last_transition_at))

    async def save_state(self, state: ControllerState) -> None:
        async with self._sessions() as s:
            r = (await s.execute(select(FailoverStateRecord).where(FailoverStateRecord.id == 1))).scalar_one_or_none()
            if r is None:
                r = FailoverStateRecord(id=1)
                s.add(r)
            r.mode = state.mode.value
            r.active_cluster = state.active_cluster
            r.primary_unhealthy_since = state.primary_unhealthy_since
            r.primary_healthy_since = state.primary_healthy_since
            r.last_transition_at = state.last_transition_at
            r.updated_at = datetime.now(timezone.utc)
            await s.commit()

    async def record_event(self, event: FailoverEvent) -> UUID:
        async with self._sessions() as s:
            r = FailoverEventRecord(at=event.at, kind=event.kind, from_cluster=event.from_cluster,
                                    to_cluster=event.to_cluster, reason=event.reason,
                                    action_status=event.action_status, action_detail=event.action_detail,
                                    decided_by=event.decided_by)
            s.add(r)
            await s.commit()
            await s.refresh(r)
            return r.id

    async def update_event(self, event_id: UUID, *, action_status: str, action_detail: dict,
                           decided_by: str | None = None) -> bool:
        values = {"action_status": action_status, "action_detail": action_detail}
        if decided_by:
            values["decided_by"] = decided_by
        async with self._sessions() as s:
            res = await s.execute(update(FailoverEventRecord).where(FailoverEventRecord.id == event_id).values(**values))
            await s.commit()
            return res.rowcount > 0

    async def get_event(self, event_id: UUID) -> FailoverEvent | None:
        async with self._sessions() as s:
            r = (await s.execute(select(FailoverEventRecord).where(FailoverEventRecord.id == event_id))).scalar_one_or_none()
            return self._to_domain(r) if r else None

    async def list_events(self, limit: int = 50) -> Sequence[FailoverEvent]:
        async with self._sessions() as s:
            rows = (await s.execute(select(FailoverEventRecord).order_by(FailoverEventRecord.at.desc()).limit(limit))).scalars()
            return [self._to_domain(r) for r in rows]

    @staticmethod
    def _to_domain(r: FailoverEventRecord) -> FailoverEvent:
        return FailoverEvent(r.id, _utc(r.at), r.kind, r.from_cluster, r.to_cluster, r.reason,
                             r.action_status, dict(r.action_detail or {}), r.decided_by)
