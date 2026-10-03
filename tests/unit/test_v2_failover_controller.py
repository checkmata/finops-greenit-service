"""V2 hysteresis: no flapping, thresholds honoured, recovery requires stability."""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.failover_controller import (
    ControllerState,
    FailoverMode,
    FailoverPolicy,
    TransitionKind,
    step,
)

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
POLICY = FailoverPolicy(failure_threshold_seconds=60, recovery_threshold_seconds=120)


def run(observations, state=None, policy=POLICY):
    """observations: list of (seconds_from_T0, primary_healthy, secondary_healthy)."""
    state = state or ControllerState.initial("primary")
    transitions = []
    for sec, p, s in observations:
        r = step(state, primary="primary", secondary="secondary", primary_healthy=p,
                 secondary_healthy=s, now=T0 + timedelta(seconds=sec), policy=policy)
        state = r.state
        if r.transition:
            transitions.append((sec, r.transition.kind))
    return state, transitions


def test_short_blip_does_not_fail_over():
    state, tr = run([(0, False, True), (30, False, True), (45, True, True), (70, False, True)])
    assert tr == [] and state.mode is FailoverMode.PRIMARY_ACTIVE


def test_persistent_failure_fails_over_exactly_at_threshold():
    _, tr = run([(0, False, True), (30, False, True), (59, False, True), (60, False, True)])
    assert tr == [(60, TransitionKind.FAILOVER)]


def test_blip_resets_the_failure_timer():
    _, tr = run([(0, False, True), (50, True, True), (55, False, True), (100, False, True), (115, False, True)])
    assert tr == [(115, TransitionKind.FAILOVER)]  # 55 → 115 = 60 s, not 0 → 100


def test_no_failover_without_healthy_secondary():
    state, tr = run([(0, False, False), (300, False, False)])
    assert tr == [] and state.active_cluster == "primary"


def test_recovery_requires_stable_primary_and_no_flapping():
    obs = [(0, False, True), (60, False, True),            # failover at 60
           (100, True, True), (150, False, True),           # recovery interrupted
           (160, True, True), (279, True, True),            # 119 s healthy: not yet
           (280, True, True)]                               # 120 s: failback
    state, tr = run(obs)
    assert tr == [(60, TransitionKind.FAILOVER), (280, TransitionKind.FAILBACK)]
    assert state.mode is FailoverMode.PRIMARY_ACTIVE and state.active_cluster == "primary"


def test_flapping_primary_never_flip_flops():
    obs = [(i * 10, i % 2 == 0, True) for i in range(60)]  # up/down every 10 s for 10 min
    _, tr = run(obs)
    assert tr == []


def test_zero_thresholds_behave_like_v1_instant_decision():
    _, tr = run([(0, False, True), (1, True, True)], policy=FailoverPolicy(0, 0))
    assert tr == [(0, TransitionKind.FAILOVER), (1, TransitionKind.FAILBACK)]


def test_policy_validation():
    with pytest.raises(ValueError):
        FailoverPolicy(-1, 10)
