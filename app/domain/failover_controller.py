"""
Failover controller WITH HYSTERESIS (V2) — pure, stateful-by-value.

V1's plan_failover() is stateless: one bad probe flips the decision, and one good
probe flips it back (flapping). V2 wraps it with a state machine:

    PRIMARY_ACTIVE ──(primary unhealthy for ≥ failure_threshold_seconds
                      AND secondary healthy)──► FAILED_OVER
    FAILED_OVER    ──(primary healthy for ≥ recovery_threshold_seconds)──► PRIMARY_ACTIVE

Any observation that breaks a streak resets its timer. Thresholds come from the
configuration (FAILOVER_FAILURE_THRESHOLD_SECONDS / FAILOVER_RECOVERY_THRESHOLD_SECONDS).

The controller only DECIDES. Executing the decision (promoting standby replicas)
is done by an outbound adapter through FailoverActuatorPort, in "auto" mode or
after human approval ("manual" mode) — see app/failover_service.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class FailoverMode(str, Enum):
    PRIMARY_ACTIVE = "primary_active"
    FAILED_OVER = "failed_over"


class TransitionKind(str, Enum):
    FAILOVER = "failover"
    FAILBACK = "failback"


@dataclass(frozen=True)
class FailoverPolicy:
    failure_threshold_seconds: float
    recovery_threshold_seconds: float

    def __post_init__(self) -> None:
        if self.failure_threshold_seconds < 0 or self.recovery_threshold_seconds < 0:
            raise ValueError("thresholds must be >= 0")


@dataclass(frozen=True)
class ControllerState:
    mode: FailoverMode
    active_cluster: str
    primary_unhealthy_since: datetime | None = None
    primary_healthy_since: datetime | None = None
    last_transition_at: datetime | None = None

    @classmethod
    def initial(cls, primary: str) -> "ControllerState":
        return cls(mode=FailoverMode.PRIMARY_ACTIVE, active_cluster=primary)


@dataclass(frozen=True)
class Transition:
    kind: TransitionKind
    from_cluster: str
    to_cluster: str
    at: datetime
    reason: str


@dataclass(frozen=True)
class StepResult:
    state: ControllerState
    transition: Transition | None
    status: str  # human-readable: "stable", "primary failing 12s/60s", ...


def step(
    state: ControllerState,
    *,
    primary: str,
    secondary: str | None,
    primary_healthy: bool,
    secondary_healthy: bool,
    now: datetime,
    policy: FailoverPolicy,
) -> StepResult:
    """Feed ONE health observation; return the new state and an optional transition."""
    if state.mode is FailoverMode.PRIMARY_ACTIVE:
        if primary_healthy:
            return StepResult(replace(state, primary_unhealthy_since=None), None, "stable on primary")
        since = state.primary_unhealthy_since or now
        failing_for = (now - since).total_seconds()
        new_state = replace(state, primary_unhealthy_since=since)
        if failing_for < policy.failure_threshold_seconds:
            return StepResult(new_state, None,
                              f"primary failing {failing_for:.0f}s/{policy.failure_threshold_seconds:.0f}s (hysteresis)")
        if secondary is None or not secondary_healthy:
            return StepResult(new_state, None,
                              f"primary failing {failing_for:.0f}s but no healthy secondary — staying")
        transition = Transition(TransitionKind.FAILOVER, primary, secondary, now,
                                f"primary unhealthy for {failing_for:.0f}s ≥ {policy.failure_threshold_seconds:.0f}s")
        return StepResult(
            ControllerState(FailoverMode.FAILED_OVER, secondary, last_transition_at=now), transition,
            "failed over to secondary",
        )

    # FAILED_OVER
    if not primary_healthy:
        return StepResult(replace(state, primary_healthy_since=None), None, "primary still unhealthy")
    since = state.primary_healthy_since or now
    healthy_for = (now - since).total_seconds()
    new_state = replace(state, primary_healthy_since=since)
    if healthy_for < policy.recovery_threshold_seconds:
        return StepResult(new_state, None,
                          f"primary healthy {healthy_for:.0f}s/{policy.recovery_threshold_seconds:.0f}s (hysteresis)")
    transition = Transition(TransitionKind.FAILBACK, state.active_cluster, primary, now,
                            f"primary healthy for {healthy_for:.0f}s ≥ {policy.recovery_threshold_seconds:.0f}s")
    return StepResult(
        ControllerState(FailoverMode.PRIMARY_ACTIVE, primary, last_transition_at=now), transition,
        "failed back to primary",
    )
