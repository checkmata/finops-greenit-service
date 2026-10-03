"""
Time-series primitives for V3 (pure Python — the domain stays free of numpy/pandas).

WHY pure Python: the models used in V3 (rolling statistics, EWMA, exponential
smoothing, small autoregressions) need a few hundred points and simple algebra.
Keeping them dependency-free keeps them unit-testable with plain fixtures and
explainable line by line; heavier ML (Isolation Forest) lives in adapters/scripts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence


@dataclass(frozen=True)
class Series:
    """A regular series: `values[i]` is the mean over [times[i], times[i] + step)."""

    metric: str
    scope: str                       # e.g. "primary/banking/txn-processor"
    unit: str
    step_seconds: int
    times: tuple[datetime, ...]
    values: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.values)

    @property
    def hours(self) -> float:
        return len(self.values) * self.step_seconds / 3600


def resample(points: Sequence[tuple[datetime, float | None]], step_seconds: int) -> tuple[list[datetime], list[float]]:
    """Bucket irregular points to a fixed step (mean per bucket). Empty buckets are
    dropped, never filled with 0 (an unknown value is not a zero)."""
    buckets: dict[int, list[float]] = {}
    for t, v in points:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        ts = int(t.timestamp()) // step_seconds * step_seconds
        buckets.setdefault(ts, []).append(float(v))
    keys = sorted(buckets)
    return ([datetime.fromtimestamp(k, timezone.utc) for k in keys],
            [sum(buckets[k]) / len(buckets[k]) for k in keys])


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (same definition as numpy's default)."""
    if not xs:
        raise ValueError("quantile of an empty series")
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def median(xs: Sequence[float]) -> float:
    return quantile(xs, 0.5)


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation, scaled to be comparable to a standard deviation."""
    m = median(xs)
    return 1.4826 * median([abs(x - m) for x in xs])


def cv(xs: Sequence[float]) -> float:
    """Coefficient of variation (variability relative to the level)."""
    m = mean(xs)
    return std(xs) / abs(m) if m else 0.0


def step_delta(step_seconds: int) -> timedelta:
    return timedelta(seconds=step_seconds)
