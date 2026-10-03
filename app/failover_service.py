"""
Failover service (V2) — application layer around the pure hysteresis controller.

Each tick (leader replica only):
    observe every cluster (app.cluster_observer)
      → one hysteresis step (app.domain.failover_controller)
      → persist state (FailoverStatePort)
      → on a transition: record an event and, depending on FAILOVER_MODE:
          decision_only  record the decision only
          manual         status "awaiting_approval"; a human approves via the API
          auto           execute immediately (FailoverActuatorPort)

What "execute" does — and does NOT do:
    failover  scale UP, on the target cluster, the standby Deployments of the
              allowed namespaces to the replica count last seen on the primary
              (capped). Deployments missing on the target are reported as
              "not_replicated" (nothing is created: manifests/data are NOT
              replicated by this service — that is GitOps/DR tooling).
    failback  restore exactly the replica counts recorded by the failover.
    Nothing is ever deleted; traffic routing (DNS/GSLB) is out of scope.

V2.x — replicated data (DataReplicationPort, rules in app/domain/data_replication):
    failover  the standby database copy is PROMOTED first, then the workloads scale.
    fence     every tick, a writable copy on an older timeline than another writable
              copy is fenced at once (split-brain prevention; automatic in manual and
              auto modes because it only REMOVES write access; recorded as an event).
    failback  gated: while failed over, the primary cluster only counts as healthy
              once its database is the authoritative writable copy again (resync +
              controlled switchover, scripts/dr/ledger-failback.sh). The workloads
              therefore never return to a stale or rebuilding database.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence
from uuid import UUID

from app.cluster_observer import observe_cluster
from app.cluster_registry import ClusterRegistry
from app.domain.data_replication import DatabaseStatus, failback_ready, stale_writable
from app.domain.failover_controller import (
    ControllerState,
    FailoverMode,
    FailoverPolicy,
    StepResult,
    Transition,
    TransitionKind,
    step,
)
from app.domain.models.cluster import ClusterRole
from app.ports.failover_ports import (
    FailoverActuatorPort,
    FailoverEvent,
    FailoverStatePort,
    LeaderElectionPort,
)
from app.ports.data_replication_port import DataReplicationPort

logger = logging.getLogger(__name__)

MODES = ("decision_only", "manual", "auto")


@dataclass(frozen=True)
class FailoverSettings:
    mode: str
    policy: FailoverPolicy
    namespaces: frozenset[str]
    max_replicas: int = 5
    # Not application workloads (e.g. a load generator): never promoted.
    exclude_deployments: frozenset[str] = frozenset({"loadgenerator"})

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"FAILOVER_MODE must be one of {MODES}")


class FailoverService:
    def __init__(
        self,
        registry: ClusterRegistry,
        store: FailoverStatePort,
        actuators: Mapping[str, FailoverActuatorPort],
        settings: FailoverSettings,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        data: Mapping[str, Sequence[DataReplicationPort]] | None = None,
    ) -> None:
        self.registry = registry
        self.data = {c: list(ports) for c, ports in (data or {}).items()}
        self._fence_reported: set[tuple] = set()   # no duplicate event per tick when not fenced
        self.store = store
        self.actuators = dict(actuators)
        self.settings = settings
        self.clock = clock
        self.last_status: str = "not started"

    @property
    def primary(self) -> str:
        return self.registry.primary

    @property
    def secondary(self) -> str | None:
        return next((n for n in self.registry.names if self.registry.role(n) is ClusterRole.SECONDARY), None)

    async def tick(self) -> StepResult:
        observed = {n: await observe_cluster(self.registry, n) for n in self.registry.names}
        now = self.clock()
        state = await self.store.load_state() or ControllerState.initial(self.primary)
        sec = self.secondary
        data = await self.data_status()
        primary_healthy = observed[self.primary][0].is_healthy
        gate = None
        if state.mode is FailoverMode.FAILED_OVER and primary_healthy and sec and data:
            ready, why = self._data_failback_ready(data, sec)
            if not ready:
                primary_healthy, gate = False, why   # recovery timer does not start
        result = step(
            state, primary=self.primary, secondary=sec,
            primary_healthy=primary_healthy,
            secondary_healthy=bool(sec and observed[sec][0].is_healthy),
            now=now, policy=self.settings.policy,
        )
        await self.store.save_state(result.state)
        self.last_status = result.status + (f" | data gate: {gate}" if gate else "")
        if result.transition:
            await self._on_transition(result.transition)
        return result

    async def actuator_health(self) -> dict[str, str]:
        """Pre-flight of every actuator identity (V2.x lesson: expired token → 401 mid-incident)."""
        ns = sorted(self.settings.namespaces)[0] if self.settings.namespaces else "default"
        return {c: await a.probe(ns) for c, a in self.actuators.items()}

    # ------------------------------------------------------------------ data (V2.x)
    async def data_status(self) -> dict[str, dict[str, DatabaseStatus]]:
        return {c: {p.name: await p.status() for p in ports} for c, ports in self.data.items()}

    def _data_port(self, cluster: str, name: str) -> DataReplicationPort | None:
        return next((p for p in self.data.get(cluster, []) if p.name == name), None)

    def _data_failback_ready(self, data: dict[str, dict[str, DatabaseStatus]], sec: str) -> tuple[bool, str]:
        for name, pst in data.get(self.primary, {}).items():
            sst = data.get(sec, {}).get(name, DatabaseStatus(reachable=False))
            ready, why = failback_ready(pst, sst)
            if not ready:
                return False, f"{name}: {why}"
        return True, "data ready"

    async def _fence_stale(self, data: dict[str, dict[str, DatabaseStatus]], now: datetime) -> None:
        clusters = list(data)
        for a in clusters:
            for name, ast in data[a].items():
                for b in clusters:
                    bst = data[b].get(name) if b != a else None
                    if bst is None or not stale_writable(ast, bst):
                        continue
                    detail = {"service": name, "stale_timeline": ast.timeline, "authoritative_timeline": bst.timeline,
                              "authoritative_cluster": b}
                    key = (a, name, ast.timeline, bst.timeline)
                    if self.settings.mode == "decision_only":
                        if key in self._fence_reported:
                            continue
                        status = "decision_only"
                    else:
                        try:
                            await self._data_port(a, name).fence()
                            status = "executed"
                        except Exception as exc:
                            status, detail = "failed", {**detail, "error": str(exc)}
                            if key in self._fence_reported:
                                continue           # retried every tick, recorded once
                    self._fence_reported.add(key)
                    logger.warning("DATA FENCE %s on %s: %s", name, a, status)
                    await self.store.record_event(FailoverEvent(
                        None, now, "fence", a, b,
                        f"{name}: writable copy on timeline {ast.timeline} < {bst.timeline} on {b} (split-brain risk)",
                        status, detail, "controller"))

    async def data_guard(self) -> None:
        """Split-brain guard: fence any stale writable copy. Runs in its OWN fast loop
        (run_data_guard_loop, ~1 s) — V2.x measurement: inside the failover tick it waited
        behind Prometheus probe timeouts (≈11 s) while the returning old primary kept
        accepting writes that are discarded at resync."""
        if self.data:
            await self._fence_stale(await self.data_status(), self.clock())

    async def _promote_data(self, cluster: str) -> tuple[list[dict], list[str]]:
        done, errors = [], []
        for port in self.data.get(cluster, []):
            try:
                before = await port.status()
                if not before.reachable:
                    errors.append(f"{port.name}: standby database unreachable")
                    continue
                if before.in_recovery:
                    await port.promote()
                after = await port.status()
                done.append({"service": port.name, "promoted": bool(before.in_recovery),
                             "replay_lag_seconds_before": before.replay_lag_seconds,
                             "lsn_before": before.lsn, "timeline_after": after.timeline,
                             "writable_after": after.writable})
            except Exception as exc:
                errors.append(f"{port.name}: {exc}")
        return done, errors

    # ------------------------------------------------------------------ actions
    async def _on_transition(self, t: Transition) -> UUID:
        logger.warning("FAILOVER CONTROLLER: %s %s → %s (%s)", t.kind.value, t.from_cluster, t.to_cluster, t.reason)
        mode = self.settings.mode
        if mode == "decision_only" or self._actuator_for(t) is None:
            status = "decision_only"
            detail = {"note": "no actuation configured" if mode != "decision_only" else "decision_only mode"}
        elif mode == "manual":
            status, detail = "awaiting_approval", {}
        else:
            status, detail = await self._execute(t)
        return await self.store.record_event(FailoverEvent(
            None, t.at, t.kind.value, t.from_cluster, t.to_cluster, t.reason, status, detail,
            "controller" if mode != "manual" else "controller (awaiting human)",
        ))

    def _actuator_for(self, t: Transition) -> FailoverActuatorPort | None:
        # failover acts on the target (standby) cluster; failback restores that same standby
        standby = t.to_cluster if t.kind is TransitionKind.FAILOVER else t.from_cluster
        return self.actuators.get(standby)

    async def approve(self, event_id: UUID, approver: str) -> FailoverEvent:
        ev = await self.store.get_event(event_id)
        if ev is None:
            raise KeyError(event_id)
        if ev.action_status != "awaiting_approval":
            raise ValueError(f"event is '{ev.action_status}', not awaiting approval")
        t = Transition(TransitionKind(ev.kind), ev.from_cluster, ev.to_cluster, ev.at, ev.reason)
        status, detail = await self._execute(t)
        await self.store.update_event(event_id, action_status=status, action_detail=detail,
                                      decided_by=f"approved by {approver}")
        return await self.store.get_event(event_id)

    async def reject(self, event_id: UUID, approver: str) -> FailoverEvent:
        ev = await self.store.get_event(event_id)
        if ev is None:
            raise KeyError(event_id)
        if ev.action_status != "awaiting_approval":
            raise ValueError(f"event is '{ev.action_status}', not awaiting approval")
        await self.store.update_event(event_id, action_status="rejected", action_detail=ev.action_detail,
                                      decided_by=f"rejected by {approver}")
        return await self.store.get_event(event_id)

    async def _execute(self, t: Transition) -> tuple[str, dict]:
        actuator = self._actuator_for(t)
        if actuator is None:
            return "decision_only", {"note": "no actuator for this cluster"}
        try:
            if t.kind is TransitionKind.FAILOVER:
                return await self._promote(actuator, t)
            return await self._restore(actuator, t)
        except Exception as exc:
            logger.error("failover actuation failed: %s", exc, exc_info=True)
            return "failed", {"error": str(exc)}

    async def _promote(self, actuator: FailoverActuatorPort, t: Transition) -> tuple[str, dict]:
        """
        Scale each allowed standby Deployment up, ONE BY ONE.

        V2 fix (found by the real demo): an error on one Deployment aborted the
        loop after others had already been scaled, and the event kept no trace of
        them → the failback restored nothing. Every change is now recorded even
        when another one fails, so the failback can always undo exactly what was done.
        """
        # V2.x: data first — workloads scaled before the promotion would only find a
        # read-only standby (they would skip writes, not corrupt anything, but the
        # service would not be restored).
        data_done, errors = await self._promote_data(t.to_cluster)
        changes, not_replicated = [], []
        for dep in self.registry.last_seen_deployments(t.from_cluster):
            if dep.namespace not in self.settings.namespaces or dep.workload in self.settings.exclude_deployments:
                continue
            target = min(max(dep.desired, 1), self.settings.max_replicas)
            try:
                current = await actuator.get_replicas(dep.namespace, dep.workload)
                if current is None:
                    not_replicated.append(f"{dep.namespace}/{dep.workload}")
                    continue
                if current < target:
                    await actuator.scale(dep.namespace, dep.workload, target)
                    changes.append({"namespace": dep.namespace, "deployment": dep.workload,
                                    "from": current, "to": target})
            except Exception as exc:
                errors.append(f"{dep.namespace}/{dep.workload}: {exc}")
        detail = {"scaled": changes, "not_replicated": sorted(not_replicated)}
        if data_done:
            detail["data"] = data_done
        if errors:
            detail["errors"] = errors
        return ("failed" if errors else "executed"), detail

    async def _restore(self, actuator: FailoverActuatorPort, t: Transition) -> tuple[str, dict]:
        """Undo the failover of THIS episode (the most recent one towards this standby), change by change.

        V2.x fix (found by the real DR run): the lookup used to skip failovers that had
        scaled nothing and fell back to an OLDER episode — re-applying replica counts
        of a failover already undone days before. Only the latest failover is
        eligible, and never twice.
        """
        events = await self.store.list_events(200)
        last = next((e for e in events if e.kind == "failover" and e.to_cluster == t.from_cluster), None)
        already = last is not None and any(e.kind == "failback" and e.action_detail.get("undoes") == str(last.id)
                                           for e in events)
        # "failed" too: a partially executed failover still changed replicas.
        if (last is None or already or last.action_status not in ("executed", "failed")
                or not last.action_detail.get("scaled")):
            return "executed", {"restored": [], "note": "nothing to undo for this episode"
                                + (f" (failover {last.id}: {last.action_status})" if last else "")}
        restored, errors = [], []
        for ch in last.action_detail.get("scaled", []):
            try:
                await actuator.scale(ch["namespace"], ch["deployment"], ch["from"])
                restored.append({**ch, "from": ch["to"], "to": ch["from"]})
            except Exception as exc:
                errors.append(f"{ch['namespace']}/{ch['deployment']}: {exc}")
        detail = {"restored": restored, "undoes": str(last.id)}
        if errors:
            detail["errors"] = errors
        return ("failed" if errors else "executed"), detail


async def run_failover_loop(service: FailoverService, leader: LeaderElectionPort, tick_seconds: float) -> None:
    """Background loop: only the elected leader evaluates and acts."""
    while True:
        try:
            if await leader.try_acquire():
                await service.tick()
            else:
                service.last_status = "standby replica (not leader)"
        except Exception as exc:
            logger.error("failover tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(tick_seconds)


async def run_data_guard_loop(service: FailoverService, leader: LeaderElectionPort, period_seconds: float) -> None:
    """Leader-only fast loop: split-brain fencing of replicated databases (V2.x)."""
    while True:
        try:
            if leader.is_leader:
                await service.data_guard()
        except Exception as exc:
            logger.error("data guard failed: %s", exc, exc_info=True)
        await asyncio.sleep(period_seconds)


def parse_namespaces(value: str) -> frozenset[str]:
    return frozenset(n.strip() for n in value.split(",") if n.strip())


__all__: Sequence[str] = ("FailoverService", "FailoverSettings", "run_failover_loop", "run_data_guard_loop",
                          "parse_namespaces")
