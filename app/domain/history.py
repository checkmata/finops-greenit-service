"""
History aggregation — turn stored snapshots into time series (V1).

Answers "cost / energy / CO2 / waste / savings over time, by cluster,
namespace or workload, over a period".

Aggregation rule (important for correctness):
    Each snapshot is a SAMPLE of a rate-like value (a monthly cost estimate,
    24 h of energy). Two samples of the same series in the same bucket must be
    AVERAGED, not summed — summing would double the cost when the scheduler
    runs twice per bucket. Different series (e.g. two namespaces) in the same
    group ARE summed.

    value(group, bucket) = Σ_series  mean(samples of that series in the bucket)

None values (energy not measurable) are ignored; a point whose samples are
all None is reported as None, never as 0.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from app.domain.models.snapshot import AggregateRow, MetricSnapshot, WorkloadSnapshot

NAMESPACE_METRICS: dict[str, Callable[[MetricSnapshot], float | None]] = {
    "cost": lambda s: s.cost_usd,
    "requested_cost": lambda s: s.requested_cost_usd,
    "waste": lambda s: s.waste_cost_usd,
    "savings": lambda s: s.potential_savings_usd,
    "energy": lambda s: s.energy_kwh,
    "co2": lambda s: s.co2e_kg,
}

WORKLOAD_METRICS: dict[str, Callable[[WorkloadSnapshot], float | None]] = {
    "cost": lambda s: s.actual_cost_usd,
    "requested_cost": lambda s: s.requested_cost_usd,
    "waste": lambda s: s.waste_cost_usd,
    "savings": lambda s: s.potential_savings_usd,
    "cpu_used": lambda s: s.cpu_cores_used,
    "cpu_requested": lambda s: s.cpu_cores_requested,
}

GROUP_KEYS = ("cluster", "namespace", "workload")


@dataclass(frozen=True)
class SeriesPoint:
    time: datetime
    value: float | None


@dataclass(frozen=True)
class Series:
    group: dict[str, str]
    metric: str
    points: tuple[SeriesPoint, ...]


def _bucket_start(t: datetime, bucket_seconds: int) -> datetime:
    ts = t.timestamp()
    return datetime.fromtimestamp(ts - (ts % bucket_seconds), tz=timezone.utc)


def aggregate(
    snapshots: Iterable[MetricSnapshot | WorkloadSnapshot],
    *,
    metric: str,
    group_by: Sequence[str],
    bucket_seconds: int = 3600,
) -> list[Series]:
    rows = list(snapshots)
    if not rows:
        return []
    table = WORKLOAD_METRICS if isinstance(rows[0], WorkloadSnapshot) else NAMESPACE_METRICS
    if metric not in table:
        raise ValueError(f"Unknown metric '{metric}'. Allowed: {sorted(table)}")
    for g in group_by:
        if g not in GROUP_KEYS or (g == "workload" and table is NAMESPACE_METRICS):
            raise ValueError(f"Cannot group by '{g}' for this snapshot type")
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be > 0")

    getter = table[metric]
    # (group, bucket) -> series identity -> samples
    samples: dict[tuple, dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(list))
    seen_buckets: dict[tuple, set[datetime]] = defaultdict(set)

    for s in rows:
        workload = getattr(s, "workload", "*")
        group = tuple(getattr(s, g) for g in group_by)
        series_id = (s.cluster, s.namespace, workload)
        bucket = _bucket_start(s.time, bucket_seconds)
        seen_buckets[group].add(bucket)
        v = getter(s)
        if v is not None:
            samples[(group, bucket)][series_id].append(float(v))

    out: list[Series] = []
    for group in sorted(seen_buckets):
        points = []
        for bucket in sorted(seen_buckets[group]):
            per_series = samples.get((group, bucket), {})
            if not per_series:
                points.append(SeriesPoint(bucket, None))
            else:
                total = sum(sum(v) / len(v) for v in per_series.values())
                points.append(SeriesPoint(bucket, round(total, 6)))
        out.append(Series(dict(zip(group_by, group)), metric, tuple(points)))
    return out


# ---------------------------------------------------------------------------
# V2 — long-term roll-up of continuous-aggregate rows
# ---------------------------------------------------------------------------

ROLLUP_KEYS = ("cluster", "namespace", "workload")
_AVG_FIELDS = ("cost_usd", "requested_cost_usd", "waste_cost_usd", "savings_usd",
               "cpu_cores", "memory_gb", "storage_cost_usd")
_SUM_FIELDS = ("energy_kwh", "co2_kg")


@dataclass(frozen=True)
class RollupPoint:
    bucket: datetime
    group: dict[str, str]
    values: dict[str, float | None]


def rollup(rows: Iterable[AggregateRow], group_by: Sequence[str]) -> list[RollupPoint]:
    """
    Group aggregate rows (one per series and bucket) by `group_by`, per bucket.

    Every row is already one series' value for the bucket, so across series:
    - costs / CPU / memory add up (two namespaces cost the sum of both);
    - energy / CO2 add up (they are amounts).
    A field that is None for every series of a group stays None (never 0).
    """
    for g in group_by:
        if g not in ROLLUP_KEYS:
            raise ValueError(f"Cannot group by '{g}'")
    acc: dict[tuple, dict[str, float | None]] = {}
    for r in rows:
        key = (r.bucket, tuple(getattr(r, g) for g in group_by))
        values = acc.setdefault(key, {f: None for f in _AVG_FIELDS + _SUM_FIELDS})
        for f in _AVG_FIELDS + _SUM_FIELDS:
            v = getattr(r, f)
            if v is not None:
                values[f] = (values[f] or 0.0) + v
    return [
        RollupPoint(bucket, dict(zip(group_by, grp)), {k: (round(v, 9) if v is not None else None)
                                                       for k, v in vals.items()})
        for (bucket, grp), vals in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]
