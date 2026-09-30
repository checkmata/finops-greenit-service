"""
V2 ports for failover and high availability.

- FailoverStatePort:    persist the controller state + transition log (shared by
                        all replicas, survives restarts).
- FailoverActuatorPort: execute a failover/failback on a cluster (scale Deployments).
- LeaderElectionPort:   only ONE replica runs the scheduler and the failover loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence
from uuid import UUID

from app.domain.failover_controller import ControllerState


@dataclass(frozen=True)
class FailoverEvent:
    id: UUID | None
    at: datetime
    kind: str               # failover | failback
    from_cluster: str
    to_cluster: str
    reason: str
    action_status: str      # awaiting_approval | executed | failed | decision_only | rejected
    action_detail: dict = field(default_factory=dict)
    decided_by: str = "controller"


class FailoverStatePort(ABC):
    @abstractmethod
    async def load_state(self) -> ControllerState | None: ...

    @abstractmethod
    async def save_state(self, state: ControllerState) -> None: ...

    @abstractmethod
    async def record_event(self, event: FailoverEvent) -> UUID: ...

    @abstractmethod
    async def update_event(self, event_id: UUID, *, action_status: str, action_detail: dict,
                           decided_by: str | None = None) -> bool: ...

    @abstractmethod
    async def get_event(self, event_id: UUID) -> FailoverEvent | None: ...

    @abstractmethod
    async def list_events(self, limit: int = 50) -> Sequence[FailoverEvent]: ...


@dataclass(frozen=True)
class ScaleChange:
    namespace: str
    deployment: str
    from_replicas: int
    to_replicas: int


class FailoverActuatorPort(ABC):
    """Scale Deployments on ONE cluster. Never deletes anything."""

    @abstractmethod
    async def get_replicas(self, namespace: str, deployment: str) -> int | None:
        """Current spec.replicas, None if the Deployment does not exist."""

    @abstractmethod
    async def scale(self, namespace: str, deployment: str, replicas: int) -> None: ...

    async def probe(self, namespace: str) -> str:
        """V2.x pre-flight: can this identity act right now? ok | unauthorized |
        forbidden | unreachable | unknown. Surfaces an expired token BEFORE an incident."""
        return "unknown"


class LeaderElectionPort(ABC):
    @abstractmethod
    async def try_acquire(self) -> bool:
        """True if this replica is (still) the leader. Must be cheap; called every tick."""

    @abstractmethod
    async def release(self) -> None: ...

    @property
    @abstractmethod
    def is_leader(self) -> bool: ...
