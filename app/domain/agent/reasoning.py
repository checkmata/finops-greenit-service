"""
V4 — Explain · Prioritize · Propose (pure domain, deterministic, explainable).

EXPLAIN   answers the seven questions of the brief from Evidence only:
          why · which metrics · historical evolution · cost · energy · CO₂ · confidence.
          Honesty rule: a request (reservation) change does NOT change measured energy —
          the workload consumes what it uses. The agent says so instead of inventing a
          kWh "saving"; only anomalies carry a measured excess energy.

PRIORITIZE  score = 100 × (Σ wᵢ·fᵢ − w_risk·risk), clipped to [0, 100]; each factor fᵢ ∈ [0, 1]
          and its contribution are returned (no opaque score):
            impact      monthly saving (or extra cost of a needed increase) vs the largest in the batch
            urgency     increase needed (throttling/OOM risk) > recent strong anomaly > decrease
            confidence  the ML confidence (history × stability × forecast quality)
            cost        workload monthly cost vs the largest in the batch (big workloads first)
            energy      workload power vs the largest in the batch
            co2         = energy × constant intensity, reported separately as asked
            risk        memory decrease > cpu decrease > increase; production doubles it
          Simultaneous anomalies on ≥ 3 workloads of one cluster are grouped into ONE
          cluster event (a host/cluster problem, not N application problems).

PROPOSE   right-sizing → resize_workload (requests), never for anomalies (investigate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.domain.agent.model import ActionProposal, Evidence, Explanation, Priority
from app.domain.models.recommendation import Category, Recommendation

WEIGHTS = {"impact": 0.30, "urgency": 0.20, "confidence": 0.20, "cost": 0.10, "energy": 0.05, "co2": 0.05,
           "risk": 0.20}


def _fmt(v: float | None, unit: str = "", nd: int = 3) -> str:
    return "unknown" if v is None else f"{v:.{nd}g}{(' ' + unit) if unit else ''}"


def explain(rec: Recommendation, ev: Evidence) -> Explanation:
    is_anomaly = rec.category is Category.ANOMALY
    why = rec.message.replace("[ML v3] ", "")
    metrics = (f"{ev.metric or rec.resource} ({ev.unit or '?'}) of {rec.cluster}/{rec.namespace}/{rec.workload}"
               + (f"; p95 {_fmt(ev.p95)}, p99 {_fmt(ev.p99)}, request {_fmt(ev.current_request)}"
                  if ev.p95 is not None else ""))
    if ev.first_half_mean is not None and ev.second_half_mean is not None and ev.window_hours:
        delta = ev.second_half_mean - ev.first_half_mean
        rel = delta / abs(ev.first_half_mean) * 100 if ev.first_half_mean else None
        trend = "stable" if rel is not None and abs(rel) < 10 else ("rising" if delta > 0 else "falling")
        evolution = (f"over {ev.window_hours:.0f} h: mean {_fmt(ev.first_half_mean)} → {_fmt(ev.second_half_mean)} "
                     f"{ev.unit or ''} ({trend}{'' if rel is None else f', {rel:+.0f} %'})")
    else:
        evolution = "not enough history to describe an evolution"
    if rec.estimated_savings_usd:
        cost = f"≈ ${rec.estimated_savings_usd:.2f}/month of reserved capacity released (workload cost now {_fmt(ev.monthly_cost_usd, '$/month')})"
    elif "extra cost" in rec.message:
        cost = "an INCREASE: it costs more, to avoid throttling / OOM (" + rec.message.split("extra cost ")[-1] + ")"
    else:
        cost = f"no direct cost change (workload cost now {_fmt(ev.monthly_cost_usd, '$/month')})"
    if is_anomaly and ev.excess_energy_kwh is not None:
        energy = f"≈ {ev.excess_energy_kwh * 1000:.3g} Wh above baseline during the episode (measured, Kepler)"
        co2 = (f"≈ {ev.excess_energy_kwh * ev.co2_kg_per_kwh * 1000:.3g} gCO₂e" if ev.co2_kg_per_kwh is not None
               else "unknown (no emission factor)")
    else:
        energy = (f"none directly: a request is a reservation, the workload keeps consuming what it uses "
                  f"(now {_fmt(ev.power_w, 'W')}). Indirect gain only if released capacity lets nodes be "
                  f"consolidated — not modelled")
        co2 = "none directly (follows energy)"
    conf = ("unknown" if ev.confidence is None else
            f"{ev.confidence:.2f} = " + " × ".join(f"{k} {v}" for k, v in ev.confidence_factors.items())
            if ev.confidence_factors else f"{ev.confidence:.2f}")
    if is_anomaly:
        conf = "statistical signal (robust z-score); cause unknown — investigate before acting"
    return Explanation(why, metrics, evolution, cost, energy, co2, conf, ev.sources)


@dataclass(frozen=True)
class Item:
    """A recommendation (or a group of simultaneous anomalies) with its evidence."""

    key: str
    recs: tuple[Recommendation, ...]
    evidence: Evidence
    anomaly_start: datetime | None = None
    grouped: bool = False


def group_anomalies(items: Sequence[Item], min_workloads: int = 3) -> list[Item]:
    """Merge anomalies starting at the same time on ≥ min_workloads workloads of a cluster."""
    buckets: dict[tuple, list[Item]] = {}
    out: list[Item] = []
    for it in items:
        r = it.recs[0]
        if r.category is Category.ANOMALY and it.anomaly_start is not None:
            buckets.setdefault((r.cluster, it.anomaly_start), []).append(it)
        else:
            out.append(it)
    for (cluster, start), group in buckets.items():
        wls = {g.recs[0].workload for g in group}
        if len(wls) >= min_workloads:
            recs = tuple(r for g in group for r in g.recs)
            out.append(Item(f"cluster-event|{cluster}|{start.isoformat()}", recs, group[0].evidence, start, True))
        else:
            out.extend(group)
    return out


def _risk(rec: Recommendation) -> float:
    if rec.category is Category.ANOMALY:
        return 0.0                                      # no action proposed
    increase = "extra cost" in rec.message
    base = 0.2 if increase else (0.6 if rec.resource == "memory" else 0.3)
    return min(1.0, base * (2 if rec.environment == "prod" else 1))


def _urgency(item: Item) -> float:
    r = item.recs[0]
    if item.grouped:
        return 1.0
    if r.category is Category.ANOMALY:
        return 0.7 if r.severity.value == "critical" else 0.5
    return 0.9 if "extra cost" in r.message else 0.3


def prioritize(items: Sequence[Item]) -> list[tuple[Item, Priority]]:
    savings = [max((r.estimated_savings_usd or 0) for r in it.recs) for it in items]
    costs = [it.evidence.monthly_cost_usd or 0 for it in items]
    powers = [it.evidence.power_w or 0 for it in items]
    ms, mc, mp = max(savings, default=0) or 1, max(costs, default=0) or 1, max(powers, default=0) or 1
    scored = []
    for it, sv, c, p in zip(items, savings, costs, powers):
        r = it.recs[0]
        extra = 0.5 if "extra cost" in r.message else 0.0   # a needed increase has impact too
        f = {
            "impact": round(max(sv / ms, extra), 3),
            "urgency": _urgency(it),
            "confidence": round(it.evidence.confidence if it.evidence.confidence is not None else 0.5, 3),
            "cost": round(c / mc, 3),
            "energy": round(p / mp, 3),
            "co2": round(p / mp, 3),
            "risk": _risk(r),
        }
        contrib = {k: round(WEIGHTS[k] * v * (-1 if k == "risk" else 1), 4) for k, v in f.items()}
        score = round(max(0.0, min(100.0, 100 * sum(contrib.values()))), 1)
        top = sorted(((k, v) for k, v in contrib.items() if k != "risk"), key=lambda kv: -kv[1])[:2]
        rationale = (f"score {score}: driven by {top[0][0]} ({top[0][1]:+.2f}) and {top[1][0]} ({top[1][1]:+.2f})"
                     f", risk {contrib['risk']:+.2f}" + (f"; {len(it.recs)} anomalies grouped as one cluster event"
                                                         if it.grouped else ""))
        scored.append((it, score, f, contrib, rationale))
    scored.sort(key=lambda t: -t[1])
    return [(it, Priority(score, i + 1, f, dict(WEIGHTS), contrib, rationale))
            for i, (it, score, f, contrib, rationale) in enumerate(scored)]


def _parse(value: str) -> tuple[float, str]:
    num, _, unit = value.partition(" ")
    return float(num), unit


def propose(rec: Recommendation, confidence: float) -> ActionProposal | None:
    """Right-sizing → a resize proposal. Anomalies and rule-based advice without a
    machine-readable target → None (a human investigates)."""
    if rec.category is not Category.COST_RIGHTSIZING_ML or rec.resource not in ("cpu", "memory"):
        return None
    cur, unit = _parse(rec.current_value)
    new, _ = _parse(rec.recommended_value)
    return ActionProposal("resize_workload", rec.cluster, rec.namespace, rec.workload, rec.resource,
                          f"{cur:g} {unit}", f"{new:g} {unit}", rec.message.replace("[ML v3] ", ""), confidence)
