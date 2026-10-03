"""V1: recommendation lifecycle, upsert rules, namespace selection, failover decisions."""

from datetime import timedelta

import pytest

from app.domain.failover import plan_failover
from app.domain.models.cluster import ClusterRole, ClusterState, DeploymentAvailability, PlacementStatus
from app.domain.namespace_selection import select_namespaces
from app.domain.recommendation_lifecycle import REJECTION_COOLDOWN, UpsertAction, can_transition, decide_upsert
from tests.helpers_v1 import NOW


class TestTransitions:
    @pytest.mark.parametrize("cur,new", [
        ("pending", "approved"), ("pending", "rejected"), ("approved", "executed"),
        ("approved", "failed"), ("approved", "rejected"), ("failed", "approved"), ("failed", "rejected"),
    ])
    def test_allowed(self, cur, new):
        assert can_transition(cur, new)

    @pytest.mark.parametrize("cur,new", [
        ("executed", "pending"), ("rejected", "approved"), ("pending", "executed"),
        ("pending", "pending"), ("dismissed", "approved"), ("pending", "bogus"),
    ])
    def test_forbidden(self, cur, new):
        assert not can_transition(cur, new)


class TestUpsert:
    def test_rules(self):
        assert decide_upsert(None, None, NOW) is UpsertAction.INSERT
        assert decide_upsert("pending", NOW, NOW) is UpsertAction.REFRESH
        assert decide_upsert("approved", NOW, NOW) is UpsertAction.SKIP
        assert decide_upsert("failed", NOW, NOW) is UpsertAction.SKIP
        assert decide_upsert("rejected", NOW - timedelta(days=1), NOW) is UpsertAction.SKIP
        assert decide_upsert("rejected", NOW - REJECTION_COOLDOWN, NOW) is UpsertAction.INSERT
        assert decide_upsert("executed", NOW, NOW) is UpsertAction.INSERT


class TestNamespaceSelection:
    def test_default_excludes_platform(self):
        found = ["banking", "trading", "ecommerce", "kube-system", "openshift-monitoring", "kepler", "default"]
        assert select_namespaces(found) == ["banking", "default", "ecommerce", "trading"]

    def test_include_regex(self):
        assert select_namespaces(["banking", "trading", "x"], include_regex="banking|trading") == ["banking", "trading"]

    def test_explicit_wins(self):
        assert select_namespaces(["a"], explicit=["kube-system", " b ", ""]) == ["b", "kube-system"]


def cluster(name, role, *, reachable=True, ksm=True, deps=(), declared=False):
    return ClusterState(name, role, reachable, ksm, tuple(DeploymentAvailability(*d) for d in deps), declared, "maint" if declared else None)


P, S = ClusterRole.PRIMARY, ClusterRole.SECONDARY
PRIMARY_DEPS = [("ecommerce", "frontend", 3, 3), ("trading", "matcher", 2, 2)]
STANDBY_DEPS = [("ecommerce", "frontend", 1, 1)]


class TestFailover:
    def test_nominal(self):
        plan = plan_failover([cluster("a", P, deps=PRIMARY_DEPS), cluster("b", S, deps=STANDBY_DEPS)])
        assert plan.active_cluster == "a" and not plan.failover_active
        assert {p.status for p in plan.placements} == {PlacementStatus.NOMINAL}

    def test_primary_unreachable_fails_over_where_standby_exists(self):
        plan = plan_failover([cluster("a", P, reachable=False, deps=PRIMARY_DEPS), cluster("b", S, deps=STANDBY_DEPS)])
        by = {(p.namespace, p.workload): p for p in plan.placements}
        assert plan.active_cluster == "b"
        assert by[("ecommerce", "frontend")].status is PlacementStatus.FAILOVER
        assert by[("ecommerce", "frontend")].serving_cluster == "b"
        assert by[("trading", "matcher")].status is PlacementStatus.OUTAGE
        assert "unreachable" in by[("trading", "matcher")].reason

    def test_declared_unavailable_takes_precedence(self):
        plan = plan_failover([cluster("a", P, deps=PRIMARY_DEPS, declared=True), cluster("b", S, deps=STANDBY_DEPS)])
        fe = [p for p in plan.placements if p.workload == "frontend"][0]
        assert fe.status is PlacementStatus.FAILOVER and "declared unavailable" in fe.reason

    def test_workload_down_on_primary_only(self):
        deps = [("ecommerce", "frontend", 3, 0), ("trading", "matcher", 2, 2)]
        plan = plan_failover([cluster("a", P, deps=deps), cluster("b", S, deps=STANDBY_DEPS)])
        assert plan.active_cluster == "a"
        fe = [p for p in plan.placements if p.workload == "frontend"][0]
        assert fe.status is PlacementStatus.FAILOVER and "0/3" in fe.reason

    def test_degraded(self):
        plan = plan_failover([cluster("a", P, deps=[("ecommerce", "frontend", 3, 1)])])
        assert plan.placements[0].status is PlacementStatus.DEGRADED

    def test_ksm_down_is_not_trusted(self):
        plan = plan_failover([cluster("a", P, ksm=False, deps=PRIMARY_DEPS), cluster("b", S, deps=STANDBY_DEPS)])
        assert plan.active_cluster == "b"

    def test_failback_is_automatic_when_primary_recovers(self):
        down = plan_failover([cluster("a", P, reachable=False, deps=PRIMARY_DEPS), cluster("b", S, deps=STANDBY_DEPS)])
        up = plan_failover([cluster("a", P, deps=PRIMARY_DEPS), cluster("b", S, deps=STANDBY_DEPS)])
        assert down.failover_active and not up.failover_active

    def test_no_healthy_cluster(self):
        plan = plan_failover([cluster("a", P, reachable=False, deps=PRIMARY_DEPS), cluster("b", S, reachable=False)])
        assert plan.active_cluster is None
        assert {p.status for p in plan.placements} == {PlacementStatus.OUTAGE}
