"""
V4 — the recommendation agent's data model (pure domain).

The agent is NOT a chatbot and uses no LLM: every sentence it produces is built
from stored measurements (recommendations, ML analysis, snapshots). It can
therefore never state a number that is not in the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Evidence:
    """Facts gathered for one recommendation (all optional: missing = unknown, never 0)."""

    metric: str | None = None
    unit: str | None = None
    window_hours: float | None = None
    first_half_mean: float | None = None      # historical evolution: mean of the older half
    second_half_mean: float | None = None     # … and of the recent half
    p95: float | None = None
    p99: float | None = None
    current_request: float | None = None
    monthly_cost_usd: float | None = None     # workload cost now ($/month)
    power_w: float | None = None              # workload average power (W) over the window
    excess_energy_kwh: float | None = None    # anomalies: energy above baseline during the episode
    co2_kg_per_kwh: float | None = None       # PUE × grid intensity (config constants)
    confidence: float | None = None
    confidence_factors: dict = field(default_factory=dict)
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Explanation:
    why: str
    metrics: str
    evolution: str
    cost_impact: str
    energy_impact: str
    co2_impact: str
    confidence: str
    data_sources: tuple[str, ...]


@dataclass(frozen=True)
class Priority:
    score: float                               # 0–100
    rank: int
    factors: dict[str, float]                  # each factor in [0, 1]
    weights: dict[str, float]
    contributions: dict[str, float]            # weight × factor (risk is subtracted)
    rationale: str


@dataclass(frozen=True)
class ActionProposal:
    action: str                                # resize_workload | scale_workload
    cluster: str
    namespace: str
    target: str                                # Deployment name
    resource: str                              # cpu | memory | replicas
    current: str
    proposed: str
    reason: str
    confidence: float
    requires_approval: bool = True             # always: no action without a human
    staged: bool = False                       # policy limited the step (e.g. -50 % max)
    final_target: str | None = None            # the full recommendation when staged

    def as_dict(self) -> dict:
        return {"action": self.action, "target": f"{self.cluster}/{self.namespace}/{self.target}",
                "resource": self.resource, "current": self.current, "proposed": self.proposed,
                "reason": self.reason, "confidence": self.confidence, "requires_approval": self.requires_approval,
                "staged": self.staged, "final_target": self.final_target}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    proposal: ActionProposal | None            # possibly adjusted (staged)
    checks: dict[str, str]                     # check → "pass" | reason of failure
