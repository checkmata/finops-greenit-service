"""ActionStorePort on PostgreSQL/TimescaleDB (migration 007). Shares the TimescaleAdapter engine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.outbound.db.models import AgentActionRecord, AgentAuditRecord
from app.domain.agent.lifecycle import OPEN
from app.ports.agent_ports import ActionRecord, ActionStorePort


def _rec(r: AgentActionRecord) -> ActionRecord:
    return ActionRecord(r.id, r.status, r.idempotency_key, r.recommendation_id, r.proposal or {}, r.policy or {},
                        r.dry_run or {}, r.execution or {}, r.approver, r.timeout_seconds, r.created_at, r.updated_at)


class SqlActionStore(ActionStorePort):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _open_with_key(self, s: AsyncSession, key: str) -> AgentActionRecord | None:
        return (await s.execute(select(AgentActionRecord).where(
            AgentActionRecord.idempotency_key == key, AgentActionRecord.status.in_(OPEN)))).scalar_one_or_none()

    async def create(self, *, idempotency_key, recommendation_id, status, proposal, policy, dry_run,
                     timeout_seconds) -> tuple[ActionRecord, bool]:
        async with self._sessions() as s:
            existing = await self._open_with_key(s, idempotency_key)
            if existing is not None:
                return _rec(existing), False
            r = AgentActionRecord(idempotency_key=idempotency_key, recommendation_id=recommendation_id,
                                  status=status, proposal=proposal, policy=policy, dry_run=dry_run,
                                  timeout_seconds=timeout_seconds)
            s.add(r)
            try:
                await s.commit()
            except IntegrityError:          # a concurrent proposal won the unique open index
                await s.rollback()
                return _rec(await self._open_with_key(s, idempotency_key)), False
            await s.refresh(r)
            return _rec(r), True

    async def get(self, action_id: UUID) -> ActionRecord | None:
        async with self._sessions() as s:
            r = await s.get(AgentActionRecord, action_id)
            return _rec(r) if r else None

    async def transition(self, action_id: UUID, *, expected: set[str], new: str, **fields) -> ActionRecord | None:
        async with self._sessions() as s:
            res = await s.execute(
                update(AgentActionRecord)
                .where(AgentActionRecord.id == action_id, AgentActionRecord.status.in_(expected))
                .values(status=new, updated_at=datetime.now(timezone.utc), **fields)
                .returning(AgentActionRecord))
            row = res.scalar_one_or_none()
            await s.commit()
            return _rec(row) if row else None

    async def update(self, action_id: UUID, **fields) -> None:
        async with self._sessions() as s:
            await s.execute(update(AgentActionRecord).where(AgentActionRecord.id == action_id)
                            .values(updated_at=datetime.now(timezone.utc), **fields))
            await s.commit()

    async def audit(self, action_id: UUID, actor: str, event: str, detail: dict | None = None) -> None:
        async with self._sessions() as s:
            s.add(AgentAuditRecord(action_id=action_id, actor=actor, event=event, detail=detail or {}))
            await s.commit()

    async def list(self, limit: int = 50) -> Sequence[ActionRecord]:
        async with self._sessions() as s:
            rows = (await s.execute(select(AgentActionRecord).order_by(AgentActionRecord.created_at.desc())
                                    .limit(limit))).scalars().all()
            return [_rec(r) for r in rows]

    async def audit_log(self, action_id: UUID) -> list[dict]:
        async with self._sessions() as s:
            rows = (await s.execute(select(AgentAuditRecord).where(AgentAuditRecord.action_id == action_id)
                                    .order_by(AgentAuditRecord.id))).scalars().all()
            return [{"at": r.at.isoformat(), "actor": r.actor, "event": r.event, "detail": r.detail} for r in rows]
