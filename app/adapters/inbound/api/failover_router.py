"""
Failover router (V2).

- GET  /api/failover                        controller state, policy, recent transitions
- POST /api/failover/events/{id}/approve    human approves a pending failover/failback (manual mode)
- POST /api/failover/events/{id}/reject

The controller runs in the background on the LEADER replica; this router only
reads its persisted state and relays human decisions.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import observability as obs

router = APIRouter(prefix="/api/failover", tags=["failover"])


class Decision(BaseModel):
    approver: str = Field(..., min_length=1, description="Who takes the decision (audit trail)")


def get_failover_service(request: Request):
    container = getattr(request.app.state, "container", None)
    if container is None or container.failover is None:
        raise HTTPException(status_code=404, detail="Failover not configured (needs >= 2 clusters)")
    return container.failover


def _event(e) -> dict:
    return {"id": str(e.id), "at": e.at.isoformat(), "kind": e.kind, "from": e.from_cluster, "to": e.to_cluster,
            "reason": e.reason, "action_status": e.action_status, "action_detail": e.action_detail,
            "decided_by": e.decided_by}


@router.get("", summary="Failover controller state", response_model=None)
async def failover_status(request: Request, service=Depends(get_failover_service)) -> dict:
    state = await service.store.load_state()
    leader = request.app.state.container.leader
    if state is not None:
        for name in service.registry.names:
            obs.FAILOVER_ACTIVE_CLUSTER.labels(name).set(1 if name == state.active_cluster else 0)
    return {
        "mode": service.settings.mode,
        "policy": {"failure_threshold_seconds": service.settings.policy.failure_threshold_seconds,
                   "recovery_threshold_seconds": service.settings.policy.recovery_threshold_seconds},
        "state": None if state is None else {
            "mode": state.mode.value, "active_cluster": state.active_cluster,
            "primary_unhealthy_since": state.primary_unhealthy_since and state.primary_unhealthy_since.isoformat(),
            "primary_healthy_since": state.primary_healthy_since and state.primary_healthy_since.isoformat(),
            "last_transition_at": state.last_transition_at and state.last_transition_at.isoformat(),
        },
        "this_replica": {"is_leader": bool(leader and leader.is_leader), "last_status": service.last_status},
        "events": [_event(e) for e in await service.store.list_events(20)],
        # V2.x: can each actuator identity act right now? (read-only probe)
        "actuators": await service.actuator_health(),
        # V2.x hardening: automatically renewed credentials (never the token values)
        "credentials": {r.spec.cluster: r.status() for r in (request.app.state.container.credentials or [])},
        # V2.x: live state of each replicated database copy (read-only queries).
        "data": {cluster: {name: {"reachable": st.reachable, "in_recovery": st.in_recovery,
                                  "timeline": st.timeline, "app_login": st.app_login,
                                  "replay_lag_seconds": st.replay_lag_seconds, "lsn": st.lsn}
                           for name, st in by_name.items()}
                 for cluster, by_name in (await service.data_status()).items()},
        "note": ("Scales standby Deployments only (never deletes). V2.x: replicated databases are promoted on "
                 "failover, stale copies fenced, failback gated on data. Traffic routing is not moved."),
    }


async def _decide(action: str, event_id: UUID, body: Decision, service) -> dict:
    try:
        ev = await (service.approve(event_id, body.approver) if action == "approve"
                    else service.reject(event_id, body.approver))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _event(ev)


@router.post("/events/{event_id}/approve", summary="Approve and execute a pending transition", response_model=None)
async def approve(event_id: UUID, body: Decision, service=Depends(get_failover_service)) -> dict:
    return await _decide("approve", event_id, body, service)


@router.post("/events/{event_id}/reject", summary="Reject a pending transition", response_model=None)
async def reject(event_id: UUID, body: Decision, service=Depends(get_failover_service)) -> dict:
    return await _decide("reject", event_id, body, service)


__all__ = ["router", "get_failover_service", "Annotated"]
