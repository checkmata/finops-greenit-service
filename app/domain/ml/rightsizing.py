"""
V3.3 — Right-sizing from history + forecast + variability (pure domain).

    Current request
          ↓
    Historical p95 (CPU, compressible) / p99 (memory, NOT compressible: OOM-kill)
          ↓
    Forecast upper bound over the next horizon (point + ~95 % band), if available
          ↓
    base = max(percentile, forecast upper)
          ↓
    Safety margin = 15 % + variability term (0.5 × CV, capped at +35 %)
          ↓
    recommended = max(base × (1 + margin), p99 of usage, floor)   rounded (10m CPU, 16 Mi memory)

Difference with the V1 rules (recommendation_engine): V1 compares a 30-day AVERAGE
with fixed thresholds (0.5 / 0.7) and recommends usage × 1.2. V3 sizes on the
high percentiles and the forecast, so a bursty workload is not cut to its average.

Always advice: the output becomes a PENDING recommendation; a human approves.
Confidence is the product of three explicit factors, each returned:
    history   min(1, observed_hours / target_hours)
    stability 1 - min(0.5, CV / 2)
    forecast  1 - min(0.5, test relative error of the forecast)  (0.75 if no forecast)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.domain.ml.series import cv, quantile

HOURS_PER_MONTH = 24 * 30


@dataclass(frozen=True)
class RightSizingProposal:
    resource: str                    # cpu | memory
    unit: str                        # cores | GiB
    current_request: float
    recommended_request: float
    p95: float
    p99: float
    forecast_upper: float | None
    variability_cv: float
    margin: float
    observed_hours: float
    confidence: float
    confidence_factors: dict
    action: str                      # decrease | increase | keep
    expected_monthly_saving_usd: float   # negative = extra cost of an increase
    reason: str


def _round_up(x: float, step: float) -> float:
    return math.ceil(x / step - 1e-9) * step


def propose(resource: str, usage: Sequence[float], current_request: float, *, step_seconds: int,
            price_per_unit_hour: float, forecast_upper: float | None = None,
            forecast_rel_error: float | None = None, target_hours: float = 24.0,
            min_hours: float = 1.0, keep_band: float = 0.10) -> RightSizingProposal | None:
    """None when the history is too short to size anything (never a guess)."""
    hours = len(usage) * step_seconds / 3600
    if hours < min_hours or not usage:
        return None
    unit, rounding, floor = ("cores", 0.01, 0.01) if resource == "cpu" else ("GiB", 1 / 64, 1 / 64)
    p95, p99 = quantile(usage, 0.95), quantile(usage, 0.99)
    pct = p95 if resource == "cpu" else p99
    variability = cv(usage)
    margin = 0.15 + min(0.35, 0.5 * variability)
    base = max(pct, forecast_upper or 0.0)
    rec = _round_up(max(base * (1 + margin), p99, floor), rounding)

    f_hist = min(1.0, hours / target_hours)
    f_stab = 1 - min(0.5, variability / 2)
    f_fc = 0.75 if forecast_rel_error is None else 1 - min(0.5, forecast_rel_error)
    confidence = round(f_hist * f_stab * f_fc, 2)

    if current_request <= 0:
        action = "increase"          # no request at all: the scheduler cannot place it safely
    elif abs(rec - current_request) / current_request < keep_band:
        action = "keep"
    else:
        action = "decrease" if rec < current_request else "increase"
    saving = 0.0 if action == "keep" else (current_request - rec) * price_per_unit_hour * HOURS_PER_MONTH
    fc_txt = f", forecast upper {forecast_upper:.3g}" if forecast_upper is not None else ", no validated forecast"
    reason = (f"{resource}: request {current_request:.3g} {unit} vs usage p95 {p95:.3g} / p99 {p99:.3g}{fc_txt} "
              f"over {hours:.1f} h; variability CV {variability:.2f} → margin {margin:.0%} "
              f"→ recommend {rec:.3g} {unit} ({action})")
    return RightSizingProposal(resource, unit, current_request, rec, p95, p99, forecast_upper, round(variability, 3),
                               round(margin, 3), round(hours, 1), confidence,
                               {"history": round(f_hist, 2), "stability": round(f_stab, 2), "forecast": round(f_fc, 2)},
                               action, round(saving, 2), reason)


def violation_rate(recommended: float, future_usage: Sequence[float]) -> float:
    """Backtest: share of FUTURE points where usage exceeded the recommended request."""
    return sum(1 for u in future_usage if u > recommended) / len(future_usage) if future_usage else 0.0
