"""
V3.2 — Forecasting with honest temporal validation (pure domain).

Candidates, from the simplest baseline up:
    naive          last value repeated                         (the bar to beat)
    moving_average mean of the last k points
    ses            simple exponential smoothing (alpha fitted on the training part)
    holt           exponential smoothing with a linear trend (alpha, beta fitted)
    ar             autoregression AR(p) with intercept, least squares — the "ML" model
                   (a linear model on lag features; no deep learning: ~500 points)

Evaluation — rolling origin, never shuffled:
    |------ train ------|-- validation origins --|-- test origins --|
    For each origin t the model sees values[:t] only and forecasts `horizon` steps.
    Parameters/model are CHOSEN on the validation origins; the reported MAE/RMSE
    come from the TEST origins, which were never used for any choice.
    MAPE is reported only when no actual value is near zero (else undefined/meaningless).

Daily seasonality is NOT modelled: the available history (~45 h) has fewer than
two full cycles, so a seasonal model could not be validated (docs/ML.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

from app.domain.ml.series import mean, std

Forecaster = Callable[[Sequence[float], int], list[float]]


# ------------------------------------------------------------------ models
def naive(history: Sequence[float], h: int) -> list[float]:
    return [history[-1]] * h


def moving_average(k: int) -> Forecaster:
    def f(history: Sequence[float], h: int) -> list[float]:
        return [mean(history[-k:])] * h
    return f


def _ses_level(history: Sequence[float], alpha: float) -> tuple[float, float]:
    level, sse = history[0], 0.0
    for x in history[1:]:
        sse += (x - level) ** 2
        level += alpha * (x - level)
    return level, sse


def ses(alpha: float | None = None) -> Forecaster:
    grid = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9)

    def f(history: Sequence[float], h: int) -> list[float]:
        a = alpha if alpha is not None else min(grid, key=lambda g: _ses_level(history, g)[1])
        return [_ses_level(history, a)[0]] * h
    return f


def _holt(history: Sequence[float], a: float, b: float) -> tuple[float, float, float]:
    level, trend, sse = history[0], (history[1] - history[0]) if len(history) > 1 else 0.0, 0.0
    for x in history[1:]:
        sse += (x - (level + trend)) ** 2
        prev = level
        level = a * x + (1 - a) * (level + trend)
        trend = b * (level - prev) + (1 - b) * trend
    return level, trend, sse


def holt(damping: float = 0.9) -> Forecaster:
    """Damped trend (phi) so a short-lived slope is not extrapolated forever."""
    grid = [(a, b) for a in (0.1, 0.3, 0.5, 0.8) for b in (0.05, 0.1, 0.3)]

    def f(history: Sequence[float], h: int) -> list[float]:
        if len(history) < 3:
            return naive(history, h)
        a, b = min(grid, key=lambda ab: _holt(history, *ab)[2])
        level, trend, _ = _holt(history, a, b)
        out, damp = [], 0.0
        for i in range(1, h + 1):
            damp += damping ** i
            out.append(level + damp * trend)
        return out
    return f


def _solve(a: list[list[float]], y: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting and a tiny ridge (stability)."""
    n = len(y)
    m = [row[:] + [y[i]] for i, row in enumerate(a)]
    for i in range(n):
        m[i][i] += 1e-8
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-15:
            continue
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / m[r][r] if abs(m[r][r]) > 1e-15 else 0.0
    return x


def ar(p: int = 6, ridge: float = 1e-3) -> Forecaster:
    """AR(p) + intercept fitted by (ridge) least squares on the history; recursive multi-step."""
    def f(history: Sequence[float], h: int) -> list[float]:
        if len(history) < 3 * (p + 1):
            return naive(history, h)
        rows = [[1.0, *history[i - p:i]] for i in range(p, len(history))]
        ys = list(history[p:])
        k = p + 1
        xtx = [[sum(r[i] * r[j] for r in rows) + (ridge if i == j and i else 0.0) for j in range(k)] for i in range(k)]
        xty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(k)]
        coef = _solve(xtx, xty)
        buf, out = list(history[-p:]), []
        for _ in range(h):
            nxt = coef[0] + sum(c * v for c, v in zip(coef[1:], buf))
            out.append(nxt)
            buf = buf[1:] + [nxt]
        return out
    return f


CANDIDATES: dict[str, Forecaster] = {
    "naive": naive,
    "moving_average_12": moving_average(12),
    "ses": ses(),
    "holt_damped": holt(),
    "ar6": ar(6),
}


# ------------------------------------------------------------------ evaluation
@dataclass(frozen=True)
class Scores:
    mae: float
    rmse: float
    mape: float | None
    n: int


@dataclass(frozen=True)
class ForecastReport:
    horizon: int
    step_seconds: int
    validation: dict[str, Scores]
    test: dict[str, Scores]
    chosen: str                        # best on VALIDATION (no peeking at test)
    baseline: str                      # "naive"
    improvement_vs_naive_pct: float | None   # on TEST
    forecast: list[float]              # next `horizon` points from the full history
    interval_halfwidth: float          # ~95 % band from test residuals of the chosen model
    notes: list[str] = field(default_factory=list)


def score(actual: Sequence[float], predicted: Sequence[float], mape_floor: float = 1e-6) -> Scores:
    errs = [a - p for a, p in zip(actual, predicted)]
    mae = sum(abs(e) for e in errs) / len(errs)
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    mape = None
    if all(abs(a) > mape_floor for a in actual):
        mape = sum(abs(e) / abs(a) for e, a in zip(errs, actual)) / len(errs) * 100
    return Scores(mae, rmse, mape, len(errs))


def backtest(values: Sequence[float], model: Forecaster, horizon: int, origins: Sequence[int]) -> tuple[Scores, list[float]]:
    actual, pred = [], []
    for t in origins:
        fc = model(values[:t], horizon)
        n = min(horizon, len(values) - t)
        actual.extend(values[t:t + n])
        pred.extend(fc[:n])
    return score(actual, pred), [a - p for a, p in zip(actual, pred)]


def evaluate(values: Sequence[float], *, horizon: int = 12, step_seconds: int = 300,
             min_train: int = 144, n_val: int = 6, n_test: int = 6,
             candidates: dict[str, Forecaster] | None = None) -> ForecastReport | None:
    """Rolling-origin selection + test. None if the history is too short to validate."""
    cands = candidates or CANDIDATES
    need = min_train + (n_val + n_test) * horizon
    if len(values) < need:
        return None
    test_start = len(values) - n_test * horizon
    val_start = test_start - n_val * horizon
    val_origins = [val_start + i * horizon for i in range(n_val)]
    test_origins = [test_start + i * horizon for i in range(n_test)]
    val = {n: backtest(values[:test_start], m, horizon, val_origins)[0] for n, m in cands.items()}
    chosen = min(val, key=lambda n: val[n].mae)
    test, resid = {}, {}
    for n, m in cands.items():
        test[n], resid[n] = backtest(values, m, horizon, test_origins)
    base = test["naive"].mae if "naive" in test else None
    imp = (1 - test[chosen].mae / base) * 100 if base else None
    notes = []
    if chosen != "naive" and imp is not None and imp < 0:
        notes.append("the model chosen on validation is WORSE than naive on test (non-stationary series)")
    return ForecastReport(horizon, step_seconds, val, test, chosen, "naive", None if imp is None else round(imp, 1),
                          cands[chosen](values, horizon), 1.96 * std(resid[chosen]) if len(resid[chosen]) > 1 else 0.0,
                          notes)
