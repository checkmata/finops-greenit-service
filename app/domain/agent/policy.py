"""
V4 — action policy (pure domain): the gate between an agent proposal and any change.

    Agent → proposal → POLICY → human approval → actuator → Git → Argo CD → Kubernetes

Checks (all reported, pass or reason):
    action      allowlist of action types (resize_workload, scale_workload). Nothing that
                deletes, migrates or moves data is even representable.
    cluster     only the ACTIVE (primary) cluster; the standby's replicas belong to the
                failover controller, its sizing must absorb the primary's load
    namespace   allowlist
    target      not excluded (e.g. databases: ledger-db is never resized by the agent)
    confidence  ≥ minimum
    bounds      floor (10 m CPU, 32 Mi memory), ceiling, replicas within [min, max]
    step        at most ±50 % per action: a larger recommendation is STAGED (the proposal
                is adjusted to the first step and says so) — never a single 0.8 → 0.03 cut
    memory      a memory decrease below the observed p99 is refused (OOM kill risk)
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.domain.agent.model import ActionProposal, PolicyDecision


@dataclass(frozen=True)
class ActionPolicy:
    allowed_actions: frozenset[str] = frozenset({"resize_workload", "scale_workload"})
    active_clusters: frozenset[str] = frozenset({"primary"})
    namespaces: frozenset[str] = frozenset({"banking", "ecommerce", "trading"})
    excluded_targets: frozenset[str] = frozenset({"ledger-db", "loadgenerator", "_unattributed", "*"})
    min_confidence: float = 0.3
    max_step_ratio: float = 0.5
    cpu_floor_cores: float = 0.01
    cpu_ceiling_cores: float = 4.0
    memory_floor_gib: float = 1 / 32
    memory_ceiling_gib: float = 8.0
    replicas_min: int = 1
    replicas_max: int = 5
    extra: dict = field(default_factory=dict)


def _num(v: str) -> float:
    return float(v.split(" ")[0])


def validate(p: ActionProposal, policy: ActionPolicy, *, observed_p99: float | None = None) -> PolicyDecision:
    checks: dict[str, str] = {}

    def check(name: str, ok: bool, why: str) -> None:
        checks[name] = "pass" if ok else why

    check("action", p.action in policy.allowed_actions, f"action '{p.action}' not in the allowlist")
    check("cluster", p.cluster in policy.active_clusters,
          f"'{p.cluster}' is not the active cluster (standby replicas/sizing are owned by the failover plan)")
    check("namespace", p.namespace in policy.namespaces, f"namespace '{p.namespace}' not allowed")
    check("target", p.target not in policy.excluded_targets, f"'{p.target}' is excluded from agent actions")
    check("confidence", p.confidence >= policy.min_confidence,
          f"confidence {p.confidence} < {policy.min_confidence}")
    if p.action == "scale_workload":
        n = int(_num(p.proposed))
        check("bounds", policy.replicas_min <= n <= policy.replicas_max,
              f"replicas {n} outside [{policy.replicas_min}, {policy.replicas_max}]")
        checks["step"] = "pass"
        adjusted = p
    else:
        cur, new = _num(p.current), _num(p.proposed)
        floor, ceil, unit = ((policy.cpu_floor_cores, policy.cpu_ceiling_cores, "cores") if p.resource == "cpu"
                             else (policy.memory_floor_gib, policy.memory_ceiling_gib, "GiB"))
        adjusted = p
        lo, hi = cur * (1 - policy.max_step_ratio), cur * (1 + policy.max_step_ratio)
        if cur > 0 and not lo <= new <= hi:
            step_to = round(max(lo, min(hi, new)), 4)
            adjusted = replace(p, proposed=f"{step_to:g} {unit}", staged=True, final_target=p.proposed)
            checks["step"] = f"pass (staged: {p.proposed} exceeds ±{policy.max_step_ratio:.0%}; first step {step_to:g} {unit})"
        else:
            checks["step"] = "pass"
        target = _num(adjusted.proposed)
        check("bounds", floor <= target <= ceil, f"{target:g} {unit} outside [{floor:g}, {ceil:g}]")
        if p.resource == "memory" and target < cur:
            check("memory_p99", observed_p99 is not None and target >= observed_p99,
                  "memory decrease below the observed p99 (or p99 unknown): OOM-kill risk")
    allowed = all(v.startswith("pass") for v in checks.values())
    return PolicyDecision(allowed, adjusted if allowed else None, checks)
