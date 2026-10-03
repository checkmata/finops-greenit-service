"""
V4 ports for controlled actions.

Separation of permissions (the agent's three kinds of access):
    READ        ObservationPort — Prometheus (kube-state-metrics): current requests, replicas,
                container names. Used for the offline dry-run and the post-change verification.
    WRITE       GitOpsWriterPort — commits to the GitOps repository with the agent's OWN
                identity (write on that repo only). Argo CD applies; the agent has NO
                Kubernetes write identity at all.
    DANGEROUS   none: no delete, no RBAC, no Secret, no data movement is representable
                (policy allowlist + these ports have no such method).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID


@dataclass(frozen=True)
class ActionRecord:
    id: UUID
    status: str
    idempotency_key: str
    recommendation_id: UUID | None
    proposal: dict
    policy: dict
    dry_run: dict
    execution: dict
    approver: str | None
    timeout_seconds: int
    created_at: object = None
    updated_at: object = None


class ActionStorePort(ABC):
    @abstractmethod
    async def create(self, *, idempotency_key: str, recommendation_id: UUID | None, status: str, proposal: dict,
                     policy: dict, dry_run: dict, timeout_seconds: int) -> tuple[ActionRecord, bool]:
        """(record, created). If an OPEN action has the same key, return it with created=False."""

    @abstractmethod
    async def get(self, action_id: UUID) -> ActionRecord | None: ...

    @abstractmethod
    async def transition(self, action_id: UUID, *, expected: set[str], new: str, **fields) -> ActionRecord | None:
        """Atomic compare-and-set on the status. None if the current status is not expected."""

    @abstractmethod
    async def update(self, action_id: UUID, **fields) -> None: ...

    @abstractmethod
    async def audit(self, action_id: UUID, actor: str, event: str, detail: dict | None = None) -> None: ...

    @abstractmethod
    async def list(self, limit: int = 50) -> Sequence[ActionRecord]: ...

    @abstractmethod
    async def audit_log(self, action_id: UUID) -> list[dict]: ...


@dataclass(frozen=True)
class FileChange:
    path: str
    old_content: str | None        # None = file did not exist
    new_content: str | None        # None = delete the file (only used to undo a creation)


class GitOpsWriterPort(ABC):
    @abstractmethod
    async def read(self, path: str) -> str | None:
        """Current content on the main branch, None if absent."""

    @abstractmethod
    async def commit(self, changes: Sequence[FileChange], message: str) -> str:
        """One atomic commit of all changes; returns the commit SHA. Fails if a file changed
        since it was read (optimistic concurrency on old_content)."""


class ObservationPort(ABC):
    @abstractmethod
    async def container_resources(self, namespace: str, deployment: str) -> dict[str, dict[str, float]]:
        """{container: {"cpu_request", "memory_request", "memory_limit", …}} for the RUNNING pods
        (min over pods, so a rollout in progress is visible as disagreement)."""

    @abstractmethod
    async def resource_spread(self, namespace: str, deployment: str, container: str, resource: str) -> tuple[float, float] | None:
        """(min, max) of the request over the Deployment's current pods — equal once rolled out."""

    @abstractmethod
    async def replicas(self, namespace: str, deployment: str) -> tuple[int, int] | None:
        """(spec replicas, available replicas)."""
