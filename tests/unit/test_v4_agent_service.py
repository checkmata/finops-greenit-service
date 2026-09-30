"""V4 agent service: explain/prioritize → propose → policy → approval → Git → verify → rollback."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent_service import AgentService, ApprovalError
from app.domain.agent.policy import ActionPolicy
from app.domain.models.recommendation import Category, Recommendation, Severity, StoredRecommendation
from app.ml_service import MLService, MLSettings
from app.ports.agent_ports import ActionRecord, ActionStorePort, GitOpsWriterPort, ObservationPort
from tests.unit.test_v3_ml_service import NOW, snaps

ROOT = Path(__file__).resolve().parents[2]
KUST_PATH = "workloads/banking/k8s/overlays/primary/kustomization.yaml"


class Actions(ActionStorePort):
    def __init__(self):
        self.rows, self.log = {}, []

    async def create(self, *, idempotency_key, recommendation_id, status, proposal, policy, dry_run, timeout_seconds):
        for r in self.rows.values():
            if r.idempotency_key == idempotency_key and r.status in ("pending_approval", "approved", "executing"):
                return r, False
        r = ActionRecord(uuid4(), status, idempotency_key, recommendation_id, proposal, policy, dry_run, {}, None,
                         timeout_seconds)
        self.rows[r.id] = r
        return r, True

    async def get(self, action_id):
        return self.rows.get(action_id)

    async def transition(self, action_id, *, expected, new, **fields):
        r = self.rows.get(action_id)
        if r is None or r.status not in expected:
            return None
        self.rows[action_id] = replace(r, status=new, **fields)
        return self.rows[action_id]

    async def update(self, action_id, **fields):
        self.rows[action_id] = replace(self.rows[action_id], **fields)

    async def audit(self, action_id, actor, event, detail=None):
        self.log.append((action_id, actor, event))

    async def list(self, limit=50):
        return list(self.rows.values())[:limit]

    async def audit_log(self, action_id):
        return [e for e in self.log if e[0] == action_id]


class Repo(GitOpsWriterPort):
    def __init__(self):
        self.files = {KUST_PATH: (ROOT / KUST_PATH).read_text()}
        self.commits = []

    async def read(self, path):
        return self.files.get(path)

    async def commit(self, changes, message):
        for c in changes:
            if self.files.get(c.path) != c.old_content:
                raise RuntimeError("409 file changed")
            if c.new_content is None:
                self.files.pop(c.path, None)
            else:
                self.files[c.path] = c.new_content
        self.commits.append(message)
        return f"sha{len(self.commits)}"


class Cluster(ObservationPort):
    """Simulates Argo CD applying the committed request after a few polls (or never)."""

    def __init__(self, repo, applies=True, n=1):
        self.repo, self.applies, self.request, self.n = repo, applies, 0.4, n

    async def container_resources(self, namespace, deployment):
        return {"txn-processor": {"cpu_request": self.request, "memory_request": 268435456.0,
                                  "memory_limit": 268435456.0}}

    async def resource_spread(self, namespace, deployment, container, resource):
        f = self.repo.files.get("workloads/banking/k8s/overlays/primary/agent-patches.yaml")
        if self.applies:
            self.request = 0.4 if f is None else float(f.split("cpu: ")[1].split("m")[0]) / 1000
        return self.request, self.request

    async def replicas(self, namespace, deployment):
        return self.n, self.n


class RecStorage:
    def __init__(self, rec):
        self.rec, self.status = rec, "pending"

    async def query_workload_snapshots(self, *, start, end, cluster=None, namespace=None, workload=None):
        return [s for s in snaps(1800) if start <= s.time <= end]

    async def list_recommendation_records(self, **kw):
        return [StoredRecommendation(self.rec_id, self.status, NOW, NOW, self.rec)]

    async def get_recommendation_record(self, rec_id):
        return StoredRecommendation(rec_id, self.status, NOW, NOW, self.rec) if rec_id == self.rec_id else None

    async def update_recommendation_status(self, rec_id, status):
        self.status = status
        return True


def build(tmp_path, cluster="primary", applies=True, timeout=1):
    rec = Recommendation(Category.COST_RIGHTSIZING_ML, Severity.INFO, "banking", "cpu", "[ML v3] cpu: …",
                         "0.4 cores", "0.25 cores", estimated_savings_usd=4.3, cluster=cluster,
                         workload="txn-processor", environment="prod")
    storage = RecStorage(rec)
    storage.rec_id = uuid4()
    repo = Repo()
    obs = Cluster(repo, applies)
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    svc = AgentService(storage, MLService(storage, MLSettings(), clock=lambda: NOW), Actions(), repo,
                       {"primary": obs, "secondary": obs}, ActionPolicy(), co2_kg_per_kwh=0.665,
                       approval_token_file=str(token), verify_timeout_seconds=timeout, poll_seconds=0.01,
                       clock=lambda: NOW)
    return svc, storage, repo, obs


async def settle(svc):
    await asyncio.gather(*list(svc._tasks))


@pytest.mark.asyncio
async def test_briefing_explains_prioritizes_and_proposes(tmp_path):
    svc, *_ = build(tmp_path)
    b = await svc.briefing()
    assert b[0]["rank"] == 1 and b[0]["scope"] == "primary/banking/txn-processor"
    assert set(b[0]["explanation"]) == {"why", "metrics", "evolution", "cost_impact", "energy_impact", "co2_impact",
                                        "confidence", "data_sources"}
    assert b[0]["proposed_action"]["requires_approval"] is True and b[0]["policy"]["cluster"] == "pass"


@pytest.mark.asyncio
async def test_full_controlled_flow_commits_verifies_and_is_idempotent(tmp_path):
    svc, storage, repo, obs = build(tmp_path)
    a = await svc.propose_action(storage.rec_id, "tester")
    assert a.status == "pending_approval" and a.dry_run["kind"] == "offline"
    assert "cpu: 250m" in a.dry_run["diff"] and "+  - path: agent-patches.yaml" in a.dry_run["diff"]
    assert (await svc.propose_action(storage.rec_id, "tester")).id == a.id          # idempotent
    with pytest.raises(ApprovalError):
        await svc.approve(a.id, "alice", "wrong")
    await svc.approve(a.id, "alice", "s3cret")
    await settle(svc)
    done = await svc.actions.get(a.id)
    assert done.status == "executed" and done.execution["commit"] == "sha1" and "0.25" in done.execution["verification"]
    assert storage.status == "executed" and obs.request == 0.25
    events = [e[2] for e in svc.actions.log if e[0] == a.id]
    assert events == ["proposed", "approved", "committed", "verified"]
    # manual rollback restores the previous value through an inverse commit
    rb = await svc.rollback(a.id, "alice", "s3cret")
    assert rb.status == "rolled_back" and obs.request == 0.4 and "verified" in rb.execution["rollback"]["verification"]
    assert KUST_PATH in repo.files and "agent-patches.yaml" not in str(repo.files.get(KUST_PATH))


@pytest.mark.asyncio
async def test_verification_timeout_triggers_automatic_rollback(tmp_path):
    svc, storage, repo, obs = build(tmp_path, applies=False, timeout=0.05)
    a = await svc.propose_action(storage.rec_id, "tester")
    await svc.approve(a.id, "alice", "s3cret")
    await settle(svc)
    r = await svc.actions.get(a.id)
    assert r.status == "rolled_back" and "timeout" in r.execution["verification"]
    assert len(repo.commits) == 2 and "rollback" in repo.commits[1] and storage.status == "failed"


@pytest.mark.asyncio
async def test_repository_changed_after_approval_is_never_applied(tmp_path):
    svc, storage, repo, obs = build(tmp_path)
    a = await svc.propose_action(storage.rec_id, "tester")
    repo.files[KUST_PATH] += "# someone edited the overlay\n"
    await svc.approve(a.id, "alice", "s3cret")
    await settle(svc)
    r = await svc.actions.get(a.id)
    assert r.status == "failed" and "changed since the approved dry-run" in r.execution["error"] and repo.commits == []


@pytest.mark.asyncio
async def test_standby_cluster_is_rejected_by_policy_and_cannot_be_approved(tmp_path):
    svc, storage, *_ = build(tmp_path, cluster="secondary")
    a = await svc.propose_action(storage.rec_id, "tester")
    assert a.status == "rejected_by_policy" and not a.policy["cluster"].startswith("pass")
    with pytest.raises(ValueError):
        await svc.approve(a.id, "alice", "s3cret")


@pytest.mark.asyncio
async def test_approvals_disabled_without_token_file(tmp_path):
    svc, storage, *_ = build(tmp_path)
    svc._token_file = None
    a = await svc.propose_action(storage.rec_id, "tester")
    with pytest.raises(ApprovalError, match="disabled"):
        await svc.approve(a.id, "alice", "s3cret")


@pytest.mark.asyncio
async def test_interrupted_execution_is_failed_and_rolled_back_at_startup(tmp_path):
    svc, storage, repo, obs = build(tmp_path)
    a = await svc.propose_action(storage.rec_id, "tester")
    await svc.actions.transition(a.id, expected={"pending_approval"}, new="approved", approver="alice")
    plan = await svc._plan(svc._proposal(a), "txn-processor", a.dry_run["live"])
    await repo.commit(plan.changes, "committed then the replica died")
    from dataclasses import asdict
    await svc.actions.transition(a.id, expected={"approved"}, new="executing",
                                 execution={"commit": "sha1", "changes": [asdict(c) for c in plan.changes]})
    assert await svc.recover_interrupted() == 1
    assert (await svc.actions.get(a.id)).status == "rolled_back" and "rollback" in repo.commits[-1]


@pytest.mark.asyncio
async def test_workload_totals_are_converted_to_per_pod_requests(tmp_path):
    """Real-data finding: a workload of 2 replicas × 0.4 core is reported as 0.8 cores."""
    svc, storage, repo, obs = build(tmp_path)
    storage.rec = replace(storage.rec, current_value="0.8 cores", recommended_value="0.5 cores")
    obs.n = 2
    a = await svc.propose_action(storage.rec_id, "tester")
    assert a.proposal["current"] == "0.4 cores" and a.proposal["proposed"] == "0.25 cores"
    assert "per pod" in a.proposal["reason"] and "cpu: 250m" in a.dry_run["diff"]


@pytest.mark.asyncio
async def test_briefing_survives_pseudo_workloads(tmp_path):
    """Regression (real run): '_unattributed' is not a Kubernetes name → 500 on /briefing."""
    svc, storage, repo, obs = build(tmp_path)
    storage.rec = replace(storage.rec, workload="_unattributed")

    async def refuse(ns, dep):
        raise ValueError(f"not a Kubernetes name: {dep!r}")
    obs.replicas = refuse
    items = await svc.briefing()
    assert items and items[0]["proposed_action"] is None
