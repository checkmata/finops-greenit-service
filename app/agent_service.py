"""
V4 application service — the FinOps/GreenIT/multicluster recommendation agent.

    Prometheus · TimescaleDB · rules (V1) · ML (V3)
          ↓  pending recommendations + measured evidence
    briefing()   EXPLAIN (7 questions, data only) · PRIORITIZE (explicit factors) · PROPOSE
          ↓
    propose()    ActionProposal → POLICY (allowlist, active cluster, staging, bounds…)
                 → OFFLINE dry-run (exact Git diff) → pending_approval | rejected_by_policy
          ↓
    approve()    HUMAN (approval token) → background execution:
                 re-plan (refuse if the repo changed since the approved diff) → Git commit
                 (agent identity) → Argo CD applies → VERIFY via Prometheus (timeout)
                 → executed | failed → automatic rollback (inverse commit) → rolled_back
          ↓
    rollback()   human-requested inverse commit of an executed action, verified.

Every step is written to agent_audit. Idempotent: the same change on the same target
returns the open action instead of creating another one.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from uuid import UUID

from app.agent_gitops import ChangePlan, PlanError, inverse, overlay_dir, plan_resize, plan_scale
from app.domain.agent.lifecycle import idempotency_key
from app.domain.agent.model import ActionProposal, Evidence
from app.domain.agent.policy import ActionPolicy, validate
from app.domain.agent.reasoning import Item, explain, group_anomalies, prioritize, propose
from app.domain.ml.series import mean, median
from app.domain.models.recommendation import Category, StoredRecommendation
from app.ml_service import MLService, WorkloadAnalysis
from app.ports.agent_ports import ActionRecord, ActionStorePort, FileChange, GitOpsWriterPort, ObservationPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)


class ApprovalError(PermissionError):
    """Missing / wrong approval token, or approvals not configured."""


class AgentService:
    def __init__(self, storage: StoragePort, ml: MLService, actions: ActionStorePort, writer: GitOpsWriterPort | None,
                 observations: Mapping[str, ObservationPort], policy: ActionPolicy, *, co2_kg_per_kwh: float,
                 approval_token_file: str | None, verify_timeout_seconds: int = 300, poll_seconds: float = 10,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.storage, self.ml, self.actions, self.writer = storage, ml, actions, writer
        self.observations, self.policy = dict(observations), policy
        self.co2 = co2_kg_per_kwh
        self._token_file = Path(approval_token_file) if approval_token_file else None
        self.verify_timeout, self.poll = verify_timeout_seconds, poll_seconds
        self.clock = clock
        self._cache: dict[tuple, tuple[float, WorkloadAnalysis | None]] = {}
        self._tasks: set[asyncio.Task] = set()

    # ================================================================== evidence
    async def _analysis(self, cluster: str, namespace: str, workload: str) -> WorkloadAnalysis | None:
        key = (cluster, namespace, workload)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        a = await self.ml.analyze_workload(cluster, namespace, workload)
        self._cache[key] = (time.monotonic(), a)
        return a

    async def evidence(self, rec: StoredRecommendation) -> tuple[Evidence, datetime | None]:
        r = rec.recommendation
        a = await self._analysis(r.cluster, r.namespace, r.workload)
        if a is None:
            return Evidence(sources=("recommendations",)), None
        snaps = await self.ml._snapshots(r.cluster, r.namespace, r.workload)
        series = self.ml.series(r.cluster, r.namespace, r.workload, snaps.get((r.namespace, r.workload), []))
        metric = r.resource if r.resource in series else "cpu"
        s = series.get(metric)
        half = len(s.values) // 2 if s else 0
        cost, power = series.get("cost"), series.get("energy")
        rs = a.rightsizing.get(r.resource) if r.category is Category.COST_RIGHTSIZING_ML else None
        start = excess = None
        if r.category is Category.ANOMALY:
            eps = [e for e in a.anomalies if e.metric == r.resource]
            if eps:
                ep = eps[-1]
                start = ep.start
                if power:
                    pts = [(t, v) for t, v in zip(power.times, power.values)]
                    base = [v for t, v in pts if ep.start - timedelta(hours=1) <= t < ep.start]
                    during = [v for t, v in pts if ep.start <= t <= ep.end]
                    if base and during:
                        b = median(base)
                        excess = sum(max(0.0, v - b) for v in during) * power.step_seconds / 3.6e6
        ev = Evidence(
            metric=metric, unit=s.unit if s else None, window_hours=s.hours if s else None,
            first_half_mean=mean(s.values[:half]) if half else None,
            second_half_mean=mean(s.values[half:]) if half else None,
            p95=rs.p95 if rs else None, p99=rs.p99 if rs else None,
            current_request=rs.current_request if rs else None,
            monthly_cost_usd=cost.values[-1] if cost else None,
            power_w=mean(power.values[-12:]) if power else None,
            excess_energy_kwh=excess, co2_kg_per_kwh=self.co2,
            confidence=rs.confidence if rs else None,
            confidence_factors=rs.confidence_factors if rs else {},
            sources=("timescaledb.workload_snapshots (1 row/min)", "ml v3 analysis", "recommendations"))
        return ev, start

    # ================================================================== explain + prioritize
    async def briefing(self, cluster: str | None = None, limit: int = 20) -> list[dict]:
        recs = await self.storage.list_recommendation_records(cluster=cluster, status="pending", limit=500)
        items, by_id = [], {}
        for rec in recs:
            ev, start = await self.evidence(rec)
            items.append(Item(str(rec.id), (rec.recommendation,), ev, start))
            by_id[str(rec.id)] = rec
        out = []
        for item, prio in prioritize(group_anomalies(items))[:limit]:
            r0 = item.recs[0]
            proposal = propose(r0, item.evidence.confidence or 0.0) if not item.grouped else None
            p99 = item.evidence.p99
            if proposal is not None and r0.cluster in self.policy.active_clusters:
                try:
                    proposal, p99 = await self.per_pod(proposal, p99)
                except PlanError:
                    pass
            decision = validate(proposal, self.policy, observed_p99=p99) if proposal else None
            out.append({
                "rank": prio.rank, "score": prio.score, "key": item.key,
                "recommendation_ids": [item.key] if not item.grouped else None,
                "scope": f"{r0.cluster}/{r0.namespace}/{r0.workload}" if not item.grouped
                         else f"{r0.cluster} ({len({r.workload for r in item.recs})} workloads)",
                "category": "cluster_event" if item.grouped else r0.category.value,
                "priority": asdict(prio), "explanation": asdict(explain(r0, item.evidence)),
                "proposed_action": decision.proposal.as_dict() if decision and decision.allowed else None,
                "policy": decision.checks if decision else {"note": "no automatic action: investigate"},
            })
        return out

    # ================================================================== actions
    def _check_token(self, token: str | None) -> None:
        if self._token_file is None or not self._token_file.is_file():
            raise ApprovalError("approvals are disabled (AGENT_APPROVAL_TOKEN_FILE not configured)")
        if not token or not hmac.compare_digest(token.encode(), self._token_file.read_text().strip().encode()):
            raise ApprovalError("invalid approval token")

    async def _plan(self, p: ActionProposal, container: str, live: dict) -> ChangePlan:
        if self.writer is None:
            raise PlanError("no GitOps writer configured")
        base = overlay_dir(p.namespace, p.cluster)
        kust = await self.writer.read(f"{base}/kustomization.yaml")
        if kust is None:
            raise PlanError(f"{base}/kustomization.yaml not found in the GitOps repository")
        if p.action == "scale_workload":
            return plan_scale(namespace=p.namespace, cluster=p.cluster, deployment=p.target,
                              replicas=int(float(p.proposed.split()[0])), kustomization=kust)
        agent = await self.writer.read(f"{base}/agent-patches.yaml")
        return plan_resize(namespace=p.namespace, cluster=p.cluster, deployment=p.target, container=container,
                           resource=p.resource, value=float(p.proposed.split()[0]), live=live,
                           kustomization=kust, agent_file=agent)

    async def _container(self, p: ActionProposal) -> tuple[str, dict]:
        obs = self.observations.get(p.cluster)
        if obs is None:
            raise PlanError(f"no observation access to cluster {p.cluster}")
        cont = await obs.container_resources(p.namespace, p.target)
        if len(cont) != 1:
            raise PlanError(f"{p.target} has {len(cont)} containers visible; only single-container Deployments "
                            "are resized by the agent")
        return next(iter(cont.items()))

    async def per_pod(self, p: ActionProposal, p99_total: float | None) -> tuple[ActionProposal, float | None]:
        """ML recommendations are WORKLOAD TOTALS (usage summed over replicas); a resize sets the
        request PER POD. Found on the real data: txn-processor "0.8 cores" = 2 × 0.4 — applying
        "0.03" per pod would have halved the intended total. Divide by the current replica count."""
        if p.action != "resize_workload":
            return p, p99_total
        obs = self.observations.get(p.cluster)
        try:
            rep = await obs.replicas(p.namespace, p.target) if obs else None
        except ValueError as exc:              # not a Kubernetes object (e.g. "_unattributed")
            raise PlanError(str(exc)) from exc
        n = rep[0] if rep and rep[0] > 0 else None
        if n is None:
            raise PlanError(f"replica count of {p.target} unknown: cannot convert workload totals to per-pod values")
        if n == 1:
            return p, p99_total
        (cur, unit), new = p.current.split(" ", 1), float(p.proposed.split()[0])
        return (ActionProposal(p.action, p.cluster, p.namespace, p.target, p.resource,
                               f"{float(cur) / n:.4g} {unit}", f"{new / n:.4g} {unit}",
                               f"{p.reason} [per pod: workload total {p.current} → {p.proposed} over {n} replicas]",
                               p.confidence), None if p99_total is None else p99_total / n)

    async def propose_action(self, rec_id: UUID, actor: str, proposal: ActionProposal | None = None) -> ActionRecord:
        rec = await self.storage.get_recommendation_record(rec_id)
        if rec is None:
            raise KeyError(rec_id)
        if rec.status != "pending":
            raise ValueError(f"recommendation is '{rec.status}', not pending")
        ev, _ = await self.evidence(rec)
        p = proposal or propose(rec.recommendation, ev.confidence or 0.0)
        if p is None:
            raise ValueError("no automatic action exists for this recommendation (investigate)")
        try:
            p, p99 = await self.per_pod(p, ev.p99)
        except PlanError as exc:
            raise ValueError(str(exc)) from exc
        decision = validate(p, self.policy, observed_p99=p99)
        final = decision.proposal or p
        key = idempotency_key(final.cluster, final.namespace, final.target, final.resource, final.proposed)
        dry: dict = {}
        status = "rejected_by_policy"
        if decision.allowed:
            try:
                container, live = await self._container(final)
                plan = await self._plan(final, container, live)
                dry = {"kind": "offline", "container": container, "live": live, "summary": plan.summary,
                       "diff": plan.diff, "files": [c.path for c in plan.changes]}
                status = "pending_approval"
            except PlanError as exc:
                dry = {"kind": "offline", "error": str(exc)}
                decision.checks["dry_run"] = str(exc)
        record, created = await self.actions.create(
            idempotency_key=key, recommendation_id=rec.id, status=status, proposal=final.as_dict()
            | {"cluster": final.cluster, "namespace": final.namespace, "deployment": final.target},
            policy=decision.checks, dry_run=dry, timeout_seconds=self.verify_timeout)
        if created:
            await self.actions.audit(record.id, actor, "proposed", {"status": status, "policy": decision.checks})
        return record

    async def approve(self, action_id: UUID, approver: str, token: str | None) -> ActionRecord:
        self._check_token(token)
        rec = await self.actions.transition(action_id, expected={"pending_approval"}, new="approved", approver=approver)
        if rec is None:
            cur = await self.actions.get(action_id)
            if cur is None:
                raise KeyError(action_id)
            if cur.approver == approver and cur.status in ("approved", "executing", "executed"):
                return cur                         # idempotent re-approval
            raise ValueError(f"action is '{cur.status}', not pending_approval")
        await self.actions.audit(action_id, approver, "approved")
        if rec.recommendation_id:
            await self.storage.update_recommendation_status(rec.recommendation_id, "approved")
        task = asyncio.create_task(self.execute(action_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return rec

    async def reject(self, action_id: UUID, approver: str, token: str | None) -> ActionRecord:
        self._check_token(token)
        rec = await self.actions.transition(action_id, expected={"pending_approval"}, new="rejected", approver=approver)
        if rec is None:
            raise ValueError("action is not pending_approval")
        await self.actions.audit(action_id, approver, "rejected")
        return rec

    def _proposal(self, rec: ActionRecord) -> ActionProposal:
        p = rec.proposal
        return ActionProposal(p["action"], p["cluster"], p["namespace"], p["deployment"], p["resource"], p["current"],
                              p["proposed"], p.get("reason", ""), p.get("confidence", 0.0), True,
                              p.get("staged", False), p.get("final_target"))

    async def _verify(self, p: ActionProposal, container: str, target: float) -> tuple[bool, str]:
        obs = self.observations[p.cluster]
        deadline = time.monotonic() + self.verify_timeout
        last = "no observation yet"
        while time.monotonic() < deadline:
            try:
                if p.action == "scale_workload":
                    rep = await obs.replicas(p.namespace, p.target)
                    last = f"replicas spec/available = {rep}"
                    if rep and rep[0] == int(target) and rep[1] == int(target):
                        return True, last
                else:
                    spread = await obs.resource_spread(p.namespace, p.target, container, p.resource)
                    last = f"{p.resource} request min/max over pods = {spread}"
                    if spread and abs(spread[0] - target) / target < 0.01 and abs(spread[1] - target) / target < 0.01:
                        return True, last
            except Exception as exc:                        # observation hiccup: keep polling
                last = f"observation error: {exc}"
            await asyncio.sleep(self.poll)
        return False, f"timeout after {self.verify_timeout}s — last: {last}"

    @staticmethod
    def _target_value(p: ActionProposal, dry: dict) -> float:
        v = float(p.proposed.split()[0])
        if p.action == "scale_workload":
            return v
        q = dry["summary"].rsplit("→ ", 1)[1]                # rounded quantity actually written
        return float(q[:-1]) / 1000 if q.endswith("m") else float(q[:-2]) * 2**20

    async def execute(self, action_id: UUID) -> ActionRecord | None:
        rec = await self.actions.transition(action_id, expected={"approved"}, new="executing")
        if rec is None:
            return await self.actions.get(action_id)        # already executing/executed: idempotent
        p = self._proposal(rec)
        actor = "agent-executor"
        try:
            container, live = rec.dry_run["container"], rec.dry_run["live"]
            plan = await self._plan(p, container, live)
            if plan.diff != rec.dry_run["diff"]:
                raise PlanError("the GitOps repository changed since the approved dry-run: refusing to apply "
                                "a diff nobody approved (re-propose the action)")
            sha = await self.writer.commit(plan.changes, f"finops-agent: {plan.summary} (action {action_id}, "
                                                         f"approved by {rec.approver})")
            execution = {"commit": sha, "changes": [asdict(c) for c in plan.changes], "started": self.clock().isoformat()}
            await self.actions.update(action_id, execution=execution)
            await self.actions.audit(action_id, actor, "committed", {"commit": sha, "summary": plan.summary})
            ok, detail = await self._verify(p, container, self._target_value(p, rec.dry_run))
            execution |= {"verification": detail, "finished": self.clock().isoformat()}
            if ok:
                done = await self.actions.transition(action_id, expected={"executing"}, new="executed",
                                                     execution=execution)
                await self.actions.audit(action_id, actor, "verified", {"observation": detail})
                if rec.recommendation_id:
                    await self.storage.update_recommendation_status(rec.recommendation_id, "executed")
                return done
            await self.actions.audit(action_id, actor, "verification_failed", {"observation": detail})
            await self.actions.transition(action_id, expected={"executing"}, new="failed", execution=execution)
            if rec.recommendation_id:
                await self.storage.update_recommendation_status(rec.recommendation_id, "failed")
            return await self._rollback(action_id, "automatic rollback after failed verification", actor)
        except Exception as exc:
            logger.error("agent action %s failed: %s", action_id, exc, exc_info=True)
            await self.actions.audit(action_id, actor, "failed", {"error": str(exc)})
            cur = await self.actions.get(action_id)
            execution = (cur.execution if cur else {}) | {"error": str(exc)}
            await self.actions.transition(action_id, expected={"executing"}, new="failed", execution=execution)
            if rec.recommendation_id:
                await self.storage.update_recommendation_status(rec.recommendation_id, "failed")
            if execution.get("commit"):
                return await self._rollback(action_id, "automatic rollback after error", actor)
            return await self.actions.get(action_id)

    async def rollback(self, action_id: UUID, actor: str, token: str | None) -> ActionRecord | None:
        self._check_token(token)
        cur = await self.actions.get(action_id)
        if cur is None:
            raise KeyError(action_id)
        if cur.status != "executed":
            raise ValueError(f"action is '{cur.status}', only an executed action can be rolled back")
        return await self._rollback(action_id, f"rollback requested by {actor}", actor)

    async def _rollback(self, action_id: UUID, reason: str, actor: str) -> ActionRecord | None:
        cur = await self.actions.get(action_id)
        changes = [FileChange(**c) for c in cur.execution.get("changes", [])]
        if not changes:
            return cur
        for c in changes:                                   # never overwrite someone else's later edit
            if await self.writer.read(c.path) != c.new_content:
                await self.actions.audit(action_id, actor, "rollback_refused",
                                         {"reason": f"{c.path} changed after the action"})
                raise ValueError(f"{c.path} changed after the action: manual rollback required")
        undo = inverse(ChangePlan(tuple(changes), "", ""))
        sha = await self.writer.commit(undo, f"finops-agent: rollback of action {action_id} — {reason}")
        await self.actions.audit(action_id, actor, "rolled_back", {"commit": sha, "reason": reason})
        p = self._proposal(cur)
        # verify the PREVIOUS value is back: replicas from the proposal, requests from the live
        # values recorded at dry-run time (bytes for memory, cores for cpu — as Prometheus reports)
        restored = (float(p.current.split()[0]) if p.action == "scale_workload"
                    else cur.dry_run.get("live", {}).get(f"{p.resource}_request"))
        detail = "not verified (previous value unknown)"
        if restored:
            ok, detail = await self._verify(p, cur.dry_run.get("container", ""), restored)
            detail = ("verified: " if ok else "NOT verified: ") + detail
        return await self.actions.transition(action_id, expected={"executed", "failed"}, new="rolled_back",
                                             execution=cur.execution | {"rollback": {"commit": sha, "reason": reason,
                                                                                     "verification": detail}})

    async def recover_interrupted(self) -> int:
        """At startup: an action left 'executing' by a crashed replica is failed and rolled back."""
        n = 0
        for rec in await self.actions.list(200):
            if rec.status == "executing":
                await self.actions.transition(rec.id, expected={"executing"}, new="failed",
                                              execution=rec.execution | {"error": "interrupted (replica restart)"})
                await self.actions.audit(rec.id, "agent-recovery", "interrupted")
                if rec.execution.get("commit"):
                    await self._rollback(rec.id, "automatic rollback of an interrupted action", "agent-recovery")
                n += 1
        return n
