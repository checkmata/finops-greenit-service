"""V4 agent domain — explain / prioritize / propose / policy (plain fixtures)."""

from datetime import datetime, timezone

import pytest

from app.domain.agent.model import ActionProposal, Evidence
from app.domain.agent.policy import ActionPolicy, validate
from app.domain.agent.reasoning import Item, WEIGHTS, explain, group_anomalies, prioritize, propose
from app.domain.models.recommendation import Category, Recommendation, Severity

T = datetime(2026, 9, 30, 19, 40, tzinfo=timezone.utc)


def rs(workload="txn-processor", res="cpu", cur="0.8 cores", new="0.03 cores", saving=16.63, increase=False,
       cluster="primary", env="prod"):
    msg = "[ML v3] cpu: request 0.8 vs usage p95 0.018 …" + ("; extra cost ≈ $0.18/month" if increase else "")
    return Recommendation(Category.COST_RIGHTSIZING_ML, Severity.INFO, "banking", res, msg, cur, new,
                          estimated_savings_usd=None if increase else saving, cluster=cluster, workload=workload,
                          environment=env)


def an(workload, cluster="primary", severity=Severity.WARNING):
    return Recommendation(Category.ANOMALY, severity, "ecommerce", "cpu", "[ML v3] cpu spike: peak …", "0.1 cores",
                          "baseline 0.003 cores", cluster=cluster, workload=workload)


EV = Evidence("cpu", "cores", 46, 0.0080, 0.0074, 0.018, 0.024, 0.8, 30.5, 0.06, None, 0.665e-3 * 1000 / 1000, 0.72,
              {"history": 1.0, "stability": 0.77, "forecast": 0.94}, ("timescaledb.workload_snapshots",))


# ------------------------------------------------------------------ explain
def test_explanation_answers_the_seven_questions_from_data_only():
    e = explain(rs(), EV)
    assert "p95" in e.why and "p95 0.018" in e.metrics
    assert "0.008 → 0.0074" in e.evolution and "stable" in e.evolution
    assert "$16.63/month" in e.cost_impact
    assert e.energy_impact.startswith("none directly") and "0.06 W" in e.energy_impact   # no invented kWh saving
    assert e.co2_impact.startswith("none directly")
    assert e.confidence.startswith("0.72") and "stability 0.77" in e.confidence
    assert e.data_sources == ("timescaledb.workload_snapshots",)


def test_anomaly_energy_is_the_measured_excess_and_unknowns_stay_unknown():
    e = explain(an("frontend"), Evidence(excess_energy_kwh=0.002, co2_kg_per_kwh=0.665))
    assert "2 Wh" in e.energy_impact and "1.33 gCO₂e" in e.co2_impact and "investigate" in e.confidence
    assert explain(rs(), Evidence()).evolution.startswith("not enough history")


# ------------------------------------------------------------------ prioritize
def test_scores_are_decomposed_and_consistent():
    items = [Item("a", (rs(),), EV), Item("b", (rs("fraud-scorer", saving=4.3),), Evidence(confidence=0.5))]
    ranked = prioritize(items)
    top, p = ranked[0]
    assert top.key == "a" and p.rank == 1 and ranked[1][1].rank == 2
    assert set(p.factors) == set(WEIGHTS) and all(0 <= v <= 1 for v in p.factors.values())
    assert p.score == pytest.approx(max(0, min(100, 100 * sum(p.contributions.values()))), abs=0.1)
    assert p.contributions["risk"] <= 0 and "driven by" in p.rationale


def test_needed_increase_outranks_a_small_saving():
    items = [Item("up", (rs(res="memory", increase=True),), Evidence(confidence=0.8)),
             Item("down", (rs("fraud-scorer", saving=0.5),), Evidence(confidence=0.8))]
    assert prioritize(items)[0][0].key == "up"


def test_simultaneous_anomalies_become_one_cluster_event():
    items = [Item(f"x{i}", (an(w),), Evidence(), anomaly_start=T) for i, w in enumerate(("a", "b", "c"))]
    items.append(Item("y", (an("d"),), Evidence(), anomaly_start=datetime(2026, 9, 30, 10, tzinfo=timezone.utc)))
    grouped = group_anomalies(items)
    assert len(grouped) == 2 and sum(1 for g in grouped if g.grouped) == 1
    ranked = prioritize(grouped)
    assert ranked[0][0].grouped and "cluster event" in ranked[0][1].rationale


# ------------------------------------------------------------------ propose + policy
def test_only_ml_right_sizing_becomes_an_action():
    p = propose(rs(), 0.72)
    assert p.as_dict() == {"action": "resize_workload", "target": "primary/banking/txn-processor", "resource": "cpu",
                           "current": "0.8 cores", "proposed": "0.03 cores", "reason": p.reason,
                           "confidence": 0.72, "requires_approval": True, "staged": False, "final_target": None}
    assert propose(an("frontend"), 0.9) is None


def test_policy_stages_a_large_cut_and_reports_every_check():
    d = validate(propose(rs(), 0.72), ActionPolicy())
    assert d.allowed and d.proposal.staged and d.proposal.proposed == "0.4 cores" and d.proposal.final_target == "0.03 cores"
    assert set(d.checks) >= {"action", "cluster", "namespace", "target", "confidence", "bounds", "step"}


@pytest.mark.parametrize("prop,why", [
    (ActionProposal("delete_workload", "primary", "banking", "x", "cpu", "1 cores", "0.5 cores", "", 0.9), "action"),
    (ActionProposal("resize_workload", "secondary", "banking", "x", "cpu", "1 cores", "0.6 cores", "", 0.9), "cluster"),
    (ActionProposal("resize_workload", "primary", "kube-system", "x", "cpu", "1 cores", "0.6 cores", "", 0.9), "namespace"),
    (ActionProposal("resize_workload", "primary", "banking", "ledger-db", "cpu", "1 cores", "0.6 cores", "", 0.9), "target"),
    (ActionProposal("resize_workload", "primary", "banking", "x", "cpu", "1 cores", "0.6 cores", "", 0.1), "confidence"),
    (ActionProposal("resize_workload", "primary", "banking", "x", "cpu", "0.01 cores", "0.006 cores", "", 0.9), "bounds"),
    (ActionProposal("scale_workload", "primary", "banking", "x", "replicas", "2", "9", "", 0.9), "bounds"),
])
def test_policy_refusals(prop, why):
    d = validate(prop, ActionPolicy())
    assert not d.allowed and d.proposal is None and not d.checks[why].startswith("pass")


def test_memory_decrease_never_below_observed_p99():
    p = ActionProposal("resize_workload", "primary", "banking", "x", "memory", "0.25 GiB", "0.2 GiB", "", 0.9)
    assert not validate(p, ActionPolicy(), observed_p99=0.22).allowed
    assert validate(p, ActionPolicy(), observed_p99=0.15).allowed
    assert not validate(p, ActionPolicy()).allowed               # p99 unknown → refused
