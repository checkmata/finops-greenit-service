"""
V3.1 — Anomaly detection (explainable, pure domain).

Two detectors, both "score = distance to a baseline, in units of normal variability":

    rolling_zscore  baseline = MEDIAN of the previous `window` points,
                    spread   = MAD (robust: one past spike does not hide the next one)
    ewma            baseline = exponentially weighted mean, spread = EW std of residuals
                    (reacts to level changes, remembers longer)

Spread floor: a flat series (a cold standby at exactly 0) has zero spread, which
would make ANY change "infinitely" anomalous. The spread is floored at
max(rel_floor × |baseline|, abs_floor) — abs_floor is metric-specific (e.g. 0.01
core) so tiny noise is never reported.

Consecutive flagged points are merged into one EPISODE (start, end, peak): an
operator reads "trading CPU abnormal from 19:38 to 19:44", not 7 separate alerts.
Isolation Forest was evaluated as a third method (scripts/ml/evaluate.py, docs/ML.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from app.domain.ml.series import Series, mad, median


@dataclass(frozen=True)
class AnomalyPoint:
    index: int
    timestamp: datetime
    value: float
    baseline: float
    spread: float
    score: float                 # |value - baseline| / spread
    method: str


@dataclass(frozen=True)
class AnomalyEpisode:
    metric: str
    scope: str
    unit: str
    start: datetime
    end: datetime
    points: int
    peak_value: float
    baseline: float
    score: float                 # max score in the episode
    direction: str               # "spike" | "drop"
    method: str
    reason: str


def _floor(baseline: float, spread: float, rel_floor: float, abs_floor: float) -> float:
    return max(spread, rel_floor * abs(baseline), abs_floor)


def rolling_zscore(values: Sequence[float], times: Sequence[datetime], *, window: int = 12,
                   threshold: float = 4.0, rel_floor: float = 0.05, abs_floor: float = 1e-9) -> list[AnomalyPoint]:
    out = []
    for i in range(window, len(values)):
        past = values[i - window:i]
        base, spread = median(past), _floor(median(past), mad(past), rel_floor, abs_floor)
        score = abs(values[i] - base) / spread
        if score >= threshold:
            out.append(AnomalyPoint(i, times[i], values[i], base, spread, score, "rolling_zscore"))
    return out


def ewma(values: Sequence[float], times: Sequence[datetime], *, alpha: float = 0.2, threshold: float = 4.0,
         warmup: int = 12, rel_floor: float = 0.05, abs_floor: float = 1e-9) -> list[AnomalyPoint]:
    if not values:
        return []
    m, var, out = values[0], 0.0, []
    for i, x in enumerate(values):
        if i >= warmup:
            spread = _floor(m, math.sqrt(var), rel_floor, abs_floor)
            score = abs(x - m) / spread
            if score >= threshold:
                out.append(AnomalyPoint(i, times[i], x, m, spread, score, "ewma"))
                continue          # an anomaly does not update the baseline (no contamination)
        r = x - m
        m += alpha * r
        var = (1 - alpha) * (var + alpha * r * r)
    return out


def episodes(series: Series, points: Sequence[AnomalyPoint], *, max_gap: int = 1) -> list[AnomalyEpisode]:
    """Merge points whose indexes are at most `max_gap` apart into episodes, with a reason."""
    groups: list[list[AnomalyPoint]] = []
    for p in sorted(points, key=lambda p: p.index):
        if groups and p.index - groups[-1][-1].index <= max_gap:
            groups[-1].append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        peak = max(g, key=lambda p: p.score)
        direction = "spike" if peak.value >= peak.baseline else "drop"
        rel = (peak.value - peak.baseline) / abs(peak.baseline) * 100 if peak.baseline else float("inf")
        rel_txt = f"{rel:+.0f}%" if math.isfinite(rel) else "from ~0"
        minutes = len(g) * series.step_seconds // 60
        reason = (f"{series.metric} {direction}: peak {peak.value:.4g} {series.unit} vs baseline "
                  f"{peak.baseline:.4g} ({rel_txt}), {peak.score:.1f}× normal variability "
                  f"({peak.method}), lasting ~{minutes} min")
        out.append(AnomalyEpisode(series.metric, series.scope, series.unit, g[0].timestamp, g[-1].timestamp,
                                  len(g), peak.value, peak.baseline, round(peak.score, 2), direction,
                                  peak.method, reason))
    return out


def detect(series: Series, *, method: str = "rolling_zscore", abs_floor: float = 1e-9,
           threshold: float = 4.0) -> list[AnomalyEpisode]:
    fn = rolling_zscore if method == "rolling_zscore" else ewma
    return episodes(series, fn(series.values, series.times, threshold=threshold, abs_floor=abs_floor))


def precision_recall(flagged: Sequence[int], truth: Sequence[int], tolerance: int = 0) -> tuple[float, float, float]:
    """Evaluation on controlled (injected) anomalies: precision, recall, F1."""
    truth_set, flagged_set = set(truth), set(flagged)
    tp_truth = {t for t in truth_set if any(abs(t - f) <= tolerance for f in flagged_set)}
    tp_flag = {f for f in flagged_set if any(abs(t - f) <= tolerance for t in truth_set)}
    precision = len(tp_flag) / len(flagged_set) if flagged_set else 1.0
    recall = len(tp_truth) / len(truth_set) if truth_set else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1
