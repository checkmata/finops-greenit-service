"""V2 failover service: hysteresis → decision → (human approval) → scaling on the standby → exact restore."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.cluster_registry import ClusterRegistry
from app.domain.failover_controller import FailoverPolicy
from app.domain.models.cluster import ClusterRole
from app.failover_service import FailoverService, FailoverSettings
from app.ports.failover_ports import FailoverActuatorPort, FailoverStatePort, LeaderElectionPort
from tests.helpers_v1 import InMemoryMetricsPort, pod

PRIMARY_DEPS = [("ecommerce", "frontend", 3, 3), ("ecommerce", "checkoutservice", 2, 2),
                ("trading", "order-matcher", 1, 1), ("kube-system", "coredns", 2, 2), ("banking", "big", 50, 50)]


class MemStore(FailoverStatePort):
    def __init__(self):
        self.state, self.events = None, []

    async def load_state(self):
        return self.state

    async def save_state(self, state):
        self.state = state

    async def record_event(self, event):
        from dataclasses import replace
        e = replace(event, id=uuid4())
        self.events.insert(0, e)
        return e.id

    async def update_event(self, event_id, *, action_status, action_detail, decided_by=None):
        from dataclasses import replace
        for i, e in enumerate(self.events):
            if e.id == event_id:
                self.events[i] = replace(e, action_status=action_status, action_detail=action_detail,
                                         decided_by=decided_by or e.decided_by)
                return True
        return False

    async def get_event(self, event_id):
        return next((e for e in self.events if e.id == event_id), None)

    async def list_events(self, limit=50):
        return self.events[:limit]


class RecordingActuator(FailoverActuatorPort):
    def __init__(self, replicas):
        self.replicas = dict(replicas)
        self.calls = []

    async def get_replicas(self, namespace, deployment):
        return self.replicas.get((namespace, deployment))

    async def scale(self, namespace, deployment, replicas):
        self.calls.append((namespace, deployment, replicas))
        self.replicas[(namespace, deployment)] = replicas

    async def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


def build(mode="auto"):
    primary = InMemoryMetricsPort("primary", {"ecommerce": {"total": pod("ecommerce")}}, deployments=PRIMARY_DEPS)
    secondary = InMemoryMetricsPort("secondary", {"ecommerce": {"total": pod("ecommerce")}},
                                    deployments=[("ecommerce", "frontend", 1, 1), ("ecommerce", "checkoutservice", 0, 0),
                                                 ("banking", "big", 1, 1)])
    reg = ClusterRegistry({"primary": ClusterRole.PRIMARY, "secondary": ClusterRole.SECONDARY},
                          {"primary": primary, "secondary": secondary})
    act = RecordingActuator({("ecommerce", "frontend"): 1, ("ecommerce", "checkoutservice"): 0,
                             ("banking", "big"): 1, ("kube-system", "coredns"): 2})
    clock = Clock()
    svc = FailoverService(reg, MemStore(), {"secondary": act},
                          FailoverSettings(mode, FailoverPolicy(30, 60),
                                           frozenset({"ecommerce", "trading", "banking"}), max_replicas=5), clock)
    return svc, primary, act, clock


async def fail_primary_for(svc, primary, clock, seconds, step=10):
    primary.reachable = False
    for _ in range(seconds // step + 1):
        await svc.tick()
        clock.advance(step)


@pytest.mark.asyncio
async def test_auto_failover_after_threshold_scales_standby_within_limits():
    svc, primary, act, clock = build("auto")
    await svc.tick()                                   # primary observed healthy once (deployments remembered)
    await fail_primary_for(svc, primary, clock, 20)    # < 30 s: nothing
    assert act.calls == [] and svc.store.events == []
    await fail_primary_for(svc, primary, clock, 20)    # crosses 30 s
    ev = svc.store.events[0]
    assert ev.kind == "failover" and ev.action_status == "executed"
    assert set(act.calls) == {("ecommerce", "frontend", 3), ("ecommerce", "checkoutservice", 2), ("banking", "big", 5)}
    assert ev.action_detail["not_replicated"] == ["trading/order-matcher"]      # never created
    assert ("kube-system", "coredns", 2) not in act.calls                       # not in allow-list
    assert svc.store.state.active_cluster == "secondary"


@pytest.mark.asyncio
async def test_failback_after_stable_recovery_restores_exact_counts():
    svc, primary, act, clock = build("auto")
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)
    primary.reachable = True
    for _ in range(4):                                   # 30 s healthy < 60 s
        await svc.tick()
        clock.advance(10)
    assert svc.store.state.active_cluster == "secondary"
    for _ in range(4):
        await svc.tick()
        clock.advance(10)
    back = svc.store.events[0]
    assert back.kind == "failback" and back.action_status == "executed"
    assert act.replicas[("ecommerce", "frontend")] == 1 and act.replicas[("ecommerce", "checkoutservice")] == 0
    assert act.replicas[("banking", "big")] == 1
    assert svc.store.state.active_cluster == "primary"


@pytest.mark.asyncio
async def test_manual_mode_waits_for_human_then_executes():
    svc, primary, act, clock = build("manual")
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)
    ev = svc.store.events[0]
    assert ev.action_status == "awaiting_approval" and act.calls == []
    done = await svc.approve(ev.id, "ops-oncall")
    assert done.action_status == "executed" and "ops-oncall" in done.decided_by and act.calls
    with pytest.raises(ValueError):
        await svc.approve(ev.id, "twice")


@pytest.mark.asyncio
async def test_reject_and_decision_only():
    svc, primary, act, clock = build("manual")
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)
    rejected = await svc.reject(svc.store.events[0].id, "ops")
    assert rejected.action_status == "rejected" and act.calls == []

    svc2, primary2, act2, clock2 = build("decision_only")
    await svc2.tick()
    await fail_primary_for(svc2, primary2, clock2, 40)
    assert svc2.store.events[0].action_status == "decision_only" and act2.calls == []


@pytest.mark.asyncio
async def test_actuation_failure_is_recorded_not_raised():
    svc, primary, act, clock = build("auto")

    async def boom(*a, **k):
        raise RuntimeError("apiserver down")
    act.scale = boom
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)
    ev = svc.store.events[0]
    assert ev.action_status == "failed" and "apiserver down" in ev.action_detail["errors"][0]


class FlagLeader(LeaderElectionPort):
    def __init__(self, leader):
        self._leader = leader

    @property
    def is_leader(self):
        return self._leader

    async def try_acquire(self):
        return self._leader

    async def release(self):
        pass


@pytest.mark.asyncio
async def test_scheduler_does_nothing_on_a_follower_replica():
    import asyncio

    from app.scheduler.snapshot_job import start_snapshot_scheduler
    from tests.integration.test_api import FakeStoragePort

    port = InMemoryMetricsPort("primary", {"banking": {"total": pod("banking")}})
    storage = FakeStoragePort()
    task = asyncio.create_task(start_snapshot_scheduler(storage_port=storage, cluster_ports={"primary": port},
                                                        interval_seconds=1, namespaces=["banking"],
                                                        leader=FlagLeader(False)))
    await asyncio.sleep(0.2)
    task.cancel()
    assert port.calls == [] and storage._snapshots == []   # follower: no cycle at all


@pytest.mark.asyncio
async def test_partial_failure_is_recorded_and_fully_undone_on_failback():
    """Regression from the REAL demo: an error mid-way left scaled Deployments unrecorded."""
    svc, primary, act, clock = build("auto")
    original_scale = act.scale

    async def flaky(namespace, deployment, replicas):
        if deployment == "checkoutservice":
            raise RuntimeError("403 Forbidden")
        await original_scale(namespace, deployment, replicas)
    act.scale = flaky
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)
    ev = svc.store.events[0]
    assert ev.action_status == "failed"
    assert {c["deployment"] for c in ev.action_detail["scaled"]} == {"frontend", "big"}   # recorded anyway
    assert "checkoutservice" in ev.action_detail["errors"][0]

    act.scale = original_scale
    primary.reachable = True
    for _ in range(8):
        await svc.tick()
        clock.advance(10)
    back = svc.store.events[0]
    assert back.kind == "failback" and back.action_status == "executed"
    assert act.replicas[("ecommerce", "frontend")] == 1 and act.replicas[("banking", "big")] == 1


@pytest.mark.asyncio
async def test_excluded_deployments_are_never_promoted():
    svc, primary, act, clock = build("auto")
    svc.registry._last_seen["primary"] = svc.registry.last_seen_deployments("primary")
    await svc.tick()
    from app.domain.models.cluster import DeploymentAvailability
    svc.registry.remember_deployments("primary", svc.registry.last_seen_deployments("primary") +
                                      (DeploymentAvailability("ecommerce", "loadgenerator", 1, 1),))
    act.replicas[("ecommerce", "loadgenerator")] = 0
    await fail_primary_for(svc, primary, clock, 40)
    assert ("ecommerce", "loadgenerator", 1) not in act.calls


@pytest.mark.asyncio
async def test_failback_never_undoes_an_older_episode_again():
    """V2.x regression (real DR run 2026-09-30): episode 2's failover scaled nothing
    (401) and its failback re-applied episode 1's counts, already undone."""
    svc, primary, act, clock = build("auto")
    await svc.tick()
    await fail_primary_for(svc, primary, clock, 40)            # episode 1: executed
    primary.reachable = True
    for _ in range(8):
        await svc.tick(); clock.advance(10)
    assert svc.store.events[0].kind == "failback" and svc.store.events[0].action_detail["restored"]
    calls_after_ep1 = list(act.calls)

    async def unauthorized(*a):
        raise RuntimeError("401 Unauthorized")
    act.get_replicas = unauthorized                               # episode 2: token expired
    await fail_primary_for(svc, primary, clock, 40)
    assert svc.store.events[0].kind == "failover" and svc.store.events[0].action_status == "failed"
    primary.reachable = True
    for _ in range(8):
        await svc.tick(); clock.advance(10)
    fb = svc.store.events[0]
    assert fb.kind == "failback" and fb.action_detail["restored"] == []
    assert "nothing to undo" in fb.action_detail["note"]
    assert act.calls == calls_after_ep1                           # no stale replica counts re-applied
