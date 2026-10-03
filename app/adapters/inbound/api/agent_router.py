"""
V4 agent endpoints.

    GET  /api/agent/briefing?cluster=&limit=         explained + prioritized recommendations (read-only)
    POST /api/agent/actions {recommendation_id}       propose → policy → offline dry-run (no change)
    GET  /api/agent/actions                           list · GET /api/agent/actions/{id} (+ audit log)
    POST /api/agent/actions/{id}/approve {approver}   HUMAN approval (header X-Approval-Token) → execution
    POST /api/agent/actions/{id}/reject  {approver}
    POST /api/agent/actions/{id}/rollback {approver}  inverse commit of an executed action

Approval endpoints are disabled unless AGENT_APPROVAL_TOKEN_FILE is configured (403).
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.agent_service import ApprovalError

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ProposeBody(BaseModel):
    recommendation_id: UUID
    requested_by: str = Field(default="api", max_length=100)


class DecisionBody(BaseModel):
    approver: str = Field(..., min_length=1, max_length=100)


def _agent(request: Request):
    c = getattr(request.app.state, "container", None)
    if c is None or getattr(c, "agent", None) is None:
        raise HTTPException(status_code=404, detail="agent not configured")
    return c.agent


def _out(rec) -> dict:
    d = asdict(rec)
    d["id"], d["recommendation_id"] = str(rec.id), rec.recommendation_id and str(rec.recommendation_id)
    for k in ("created_at", "updated_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


async def _call(fn, *a):
    try:
        return await fn(*a)
    except ApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/briefing", summary="Explained and prioritized recommendations (no side effect)")
async def briefing(request: Request, cluster: str | None = None, limit: int = Query(20, ge=1, le=100)) -> dict:
    items = await _agent(request).briefing(cluster, limit)
    return {"count": len(items), "items": items,
            "note": "Deterministic agent: every statement is computed from stored measurements (no LLM)."}


@router.post("/actions", status_code=201, summary="Propose an action for a recommendation (policy + dry-run)")
async def propose(request: Request, body: ProposeBody) -> dict:
    return _out(await _call(_agent(request).propose_action, body.recommendation_id, body.requested_by))


@router.get("/actions", summary="Actions, newest first")
async def list_actions(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    return [_out(r) for r in await _agent(request).actions.list(limit)]


@router.get("/actions/{action_id}", summary="One action and its audit log")
async def get_action(request: Request, action_id: UUID) -> dict:
    agent = _agent(request)
    rec = await agent.actions.get(action_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="action not found")
    return _out(rec) | {"audit": await agent.actions.audit_log(action_id)}


@router.post("/actions/{action_id}/approve", status_code=202, summary="Human approval → execution")
async def approve(request: Request, action_id: UUID, body: DecisionBody,
                  x_approval_token: str | None = Header(default=None)) -> dict:
    return _out(await _call(_agent(request).approve, action_id, body.approver, x_approval_token))


@router.post("/actions/{action_id}/reject", summary="Human rejection")
async def reject(request: Request, action_id: UUID, body: DecisionBody,
                 x_approval_token: str | None = Header(default=None)) -> dict:
    return _out(await _call(_agent(request).reject, action_id, body.approver, x_approval_token))


@router.post("/actions/{action_id}/rollback", summary="Roll back an executed action (inverse commit)")
async def rollback(request: Request, action_id: UUID, body: DecisionBody,
                   x_approval_token: str | None = Header(default=None)) -> dict:
    return _out(await _call(_agent(request).rollback, action_id, body.approver, x_approval_token))
