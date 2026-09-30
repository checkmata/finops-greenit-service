"""
V4 — controlled-action lifecycle (pure domain). Every transition is explicit; anything
else is refused (409 at the API), exactly like the recommendation lifecycle of V1.

    pending_approval ─approve─► approved ─execute─► executing ─► executed ─rollback─► rolled_back
          │                                             │
          └─reject─► rejected                           └─► failed ─(automatic rollback)─► rolled_back
    rejected_by_policy : terminal, created as such (never approvable)
"""

from __future__ import annotations

import hashlib

TERMINAL = frozenset({"rejected_by_policy", "rejected", "failed", "rolled_back"})
OPEN = frozenset({"pending_approval", "approved", "executing"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_approval": frozenset({"approved", "rejected"}),
    "approved": frozenset({"executing"}),
    "executing": frozenset({"executed", "failed"}),
    "executed": frozenset({"rolled_back"}),
    "failed": frozenset({"rolled_back"}),
}


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, frozenset())


def idempotency_key(cluster: str, namespace: str, target: str, resource: str, proposed: str) -> str:
    """Same change on the same target = same key: re-proposing returns the open action."""
    return hashlib.sha256("|".join((cluster, namespace, target, resource, proposed)).encode()).hexdigest()[:32]
