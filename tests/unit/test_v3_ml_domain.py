"""V3 ML domain — plain fixtures, no mocks (the real-data validation is scripts/ml/evaluate.py)."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.ml.anomaly import detect, ewma, precision_recall, rolling_zscore
from app.domain.ml.forecasting import CANDIDATES, ar, evaluate, holt, naive, score, ses
from app.domain.ml.rightsizing import propose, violation_rate
from app.domain.ml.series import Series, cv, mad, quantile, resample

T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)


def times(n, step=300):
    return [T0 + timedelta(seconds=i * step) for i in range(n)]


def noisy(n, level=1.0, sd=0.05, seed=1):
    rng = random.Random(seed)
    return [level + rng.gauss(0, sd) for _ in range(n)]


# ------------------------------------------------------------------ series
def test_resample_means_per_bucket_and_never_fills_gaps():
    pts = [(T0, 1.0), (T0 + timedelta(seconds=60), 3.0), (T0 + timedelta(seconds=900), 5.0), (T0, None)]
    ts, vs = resample(pts, 300)
    assert vs == [2.0, 5.0] and len(ts) == 2          # the empty 5-10 min bucket is NOT a zero


def test_quantile_and_robust_spread():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5 and quantile([5], 0.99) == 5
    assert mad([1, 1, 1, 1, 100]) == 0.0              # one outlier does not inflate the spread
    assert cv([2, 2, 2]) == 0


# ------------------------------------------------------------------ anomaly
def test_injected_spikes_are_found_by_both_detectors_without_false_alarms():
    vals = noisy(300)
    truth = [100, 180, 260]
    for i in truth:
        vals[i] *= 3
    ts = times(300)
    for fn in (rolling_zscore, ewma):
        flagged = [p.index for p in fn(vals, ts, threshold=5.0)]
        p, r, f1 = precision_recall(flagged, truth)
        assert r == 1.0 and p == 1.0, (fn.__name__, flagged)


def test_flat_standby_series_needs_a_meaningful_change():
    vals = [0.0] * 100 + [0.004] + [0.0] * 20 + [0.5] * 10     # tiny noise, then a real activation
    s = Series("cpu", "secondary/trading/order-matcher", "cores", 300, tuple(times(131)), tuple(vals))
    eps = detect(s, abs_floor=0.01)
    assert len(eps) == 1 and eps[0].start == s.times[121]          # the 0.004 blip is ignored
    assert eps[0].direction == "spike" and "from ~0" in eps[0].reason


def test_consecutive_points_form_one_explained_episode():
    vals = noisy(200)
    for i in range(150, 156):
        vals[i] = 4.0
    s = Series("cpu", "primary/banking/txn-processor", "cores", 300, tuple(times(200)), tuple(vals))
    eps = detect(s, threshold=5.0)
    assert len(eps) == 1 and eps[0].points >= 5
    e = eps[0]
    assert e.metric == "cpu" and e.peak_value == 4.0 and e.baseline == pytest.approx(1.0, abs=0.1)
    assert "baseline" in e.reason and "min" in e.reason and e.score > 5


def test_precision_recall_definitions():
    assert precision_recall([1, 2], [2, 3]) == (0.5, 0.5, 0.5)
    assert precision_recall([], []) == (1.0, 1.0, 1.0)


# ------------------------------------------------------------------ forecasting
def test_models_on_known_shapes():
    trend = [0.1 * i for i in range(60)]
    assert naive(trend, 2) == [trend[-1]] * 2
    assert holt(damping=1.0)(trend, 3)[2] == pytest.approx(trend[-1] + 0.3, abs=0.05)
    assert ses()(noisy(100), 1)[0] == pytest.approx(1.0, abs=0.05)
    ar_series = [math.sin(i / 3) for i in range(200)]
    assert ar(6)(ar_series, 1)[0] == pytest.approx(math.sin(200 / 3), abs=0.02)


def test_scores_and_mape_only_when_defined():
    s = score([1, 2], [1, 4])
    assert s.mae == 1.0 and s.rmse == pytest.approx(math.sqrt(2)) and s.mape == 50.0
    assert score([0.0, 1], [0.1, 1]).mape is None


def test_evaluation_is_temporal_and_selection_uses_validation_only():
    vals = [0.02 * i + 0.01 * math.sin(i) for i in range(400)]     # steady growth
    rep = evaluate(vals, horizon=12, min_train=100, n_val=4, n_test=4)
    assert rep is not None and set(rep.test) == set(CANDIDATES)
    assert rep.chosen in ("holt_damped", "ar6")                     # trend-aware wins on a trend
    assert rep.test[rep.chosen].mae < rep.test["naive"].mae and rep.improvement_vs_naive_pct > 0
    assert len(rep.forecast) == 12 and rep.interval_halfwidth >= 0


def test_too_short_history_is_not_evaluated():
    assert evaluate([1.0] * 50) is None


# ------------------------------------------------------------------ right-sizing
def test_overprovisioned_bursty_workload_is_cut_to_percentile_not_average():
    usage = [0.1] * 270 + [0.6] * 18                                # 6 % of the time at 0.6
    p = propose("cpu", usage, 2.0, step_seconds=300, price_per_unit_hour=0.04)
    assert p.action == "decrease" and p.recommended_request >= 0.6  # keeps the bursts (p99)
    assert p.recommended_request < 2.0 and p.expected_monthly_saving_usd > 0
    assert set(p.confidence_factors) == {"history", "stability", "forecast"}
    assert 0 < p.confidence <= 1 and "p95" in p.reason


def test_memory_uses_p99_and_forecast_upper():
    usage = noisy(300, level=0.5, sd=0.01)
    p = propose("memory", usage, 0.5, step_seconds=300, price_per_unit_hour=0.005, forecast_upper=0.8)
    assert p.recommended_request >= 0.8 * 1.15 and p.action == "increase"
    assert p.expected_monthly_saving_usd < 0                        # an increase costs money


def test_no_proposal_without_enough_history_and_keep_band():
    assert propose("cpu", [0.1] * 5, 1.0, step_seconds=300, price_per_unit_hour=0.04) is None
    p = propose("cpu", [0.5] * 300, 0.62, step_seconds=300, price_per_unit_hour=0.04)
    assert p.action == "keep" and p.expected_monthly_saving_usd == 0


def test_violation_rate_backtest():
    assert violation_rate(1.0, [0.5, 1.5, 0.9, 2.0]) == 0.5
