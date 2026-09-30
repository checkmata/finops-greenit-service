"""
V3 application service: TimescaleDB history → features → anomaly / forecast / right-sizing
→ recommendations (PENDING, human-approved).

    storage.query_workload_snapshots (1 row per workload per scheduler cycle)
      → per-metric regular series (5-min means; gaps are NOT filled)
      → app.domain.ml.{anomaly, forecasting, rightsizing}  (pure, explainable)
      → Recommendation(category=anomaly | cost_right-sizing_ml)

Metrics (all derived from stored, measured snapshots):
    cpu     cores used NOW (5-min rate, cpu_cores_used_now — migration 006). NOT the
            long-window average cpu_cores_used: that one is smoothed over days and hides
            every spike (found on the real data: 0 anomalies during 6 real failovers).
    memory  GiB working set (already point-in-time)
    cost    $/month at the current usage = cpu_now × cpu price + memory × memory price (× 720 h)
    energy  W (interval kWh / interval length)
    co2     gCO2e/h (interval kg / interval length) — CO2 = energy × PUE × constant
            grid intensity, so its signal is the energy signal rescaled (docs/ML.md).

Writes happen only from the leader's ML loop (run_ml_loop), like the snapshot job.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from app.domain.ml import anomaly as anomaly_mod
from app.domain.ml.anomaly import AnomalyEpisode
from app.domain.ml.forecasting import ForecastReport, evaluate
from app.domain.ml.rightsizing import RightSizingProposal, propose
from app.domain.ml.series import Series, mean, resample
from app.domain.models.recommendation import Category, Recommendation, Severity
from app.domain.models.snapshot import WorkloadSnapshot
from app.ports.failover_ports import LeaderElectionPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)

# metric → (unit, absolute spread floor: changes smaller than this are never anomalies).
# Floors sized on the REAL workloads (most use 1–70 millicores, 10–200 MiB, 0.01–0.5 W).
METRICS: dict[str, tuple[str, float]] = {
    "cpu": ("cores", 0.002),
    "memory": ("GiB", 1 / 256),
    "cost": ("USD/month", 0.05),
    "energy": ("W", 0.02),
    "co2": ("gCO2e/h", 0.003),
}


@dataclass(frozen=True)
class MLSettings:
    step_seconds: int = 300
    history_hours: float = 48
    horizon: int = 12                      # 12 × 5 min = 1 h ahead
    anomaly_method: str = "rolling_zscore"
    anomaly_threshold: float = 4.0
    recent_anomaly_hours: float = 1.0      # only recent episodes become recommendations
    cpu_price_hour: float = 0.04
    memory_price_hour: float = 0.005
    min_hours: float = 1.0
    min_confidence: float = 0.3            # below: no right-sizing recommendation
    # Standby clusters are NOT right-sized on their own (idle) usage: on failover they
    # must absorb the primary's load. Seen on the real data: V3 proposed 0.1 → 0.01 core
    # for the standby txn-processor. Their proposals stay visible in the API, flagged.
    standby_clusters: frozenset[str] = frozenset()


@dataclass
class WorkloadAnalysis:
    cluster: str
    namespace: str
    workload: str
    team: str
    cost_center: str
    environment: str
    observed_hours: float
    requests: dict[str, float]
    anomalies: list[AnomalyEpisode] = field(default_factory=list)
    forecasts: dict[str, ForecastReport | None] = field(default_factory=dict)
    rightsizing: dict[str, RightSizingProposal | None] = field(default_factory=dict)

    @property
    def scope(self) -> str:
        return f"{self.cluster}/{self.namespace}/{self.workload}"

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["anomalies"] = [asdict(a) | {"start": a.start.isoformat(), "end": a.end.isoformat()} for a in self.anomalies]
        return d


def _metric_points(snaps: Sequence[WorkloadSnapshot], cpu_price: float,
                   mem_price: float) -> dict[str, list[tuple[datetime, float | None]]]:
    out: dict[str, list] = defaultdict(list)
    prev: datetime | None = None
    for s in snaps:
        now = s.cpu_cores_used_now
        out["cpu"].append((s.time, now))
        out["memory"].append((s.time, s.memory_gb_used))
        out["cost"].append((s.time, None if now is None else
                            (now * cpu_price + s.memory_gb_used * mem_price) * 720))
        dt = (s.time - prev).total_seconds() if prev else None
        # interval energy → average power over the interval actually covered
        if dt and 10 <= dt <= 900:
            out["energy"].append((s.time, None if s.energy_interval_kwh is None else s.energy_interval_kwh * 3.6e6 / dt))
            out["co2"].append((s.time, None if s.co2_interval_kg is None else s.co2_interval_kg * 1000 * 3600 / dt))
        prev = s.time
    return out


class MLService:
    def __init__(self, storage: StoragePort, settings: MLSettings = MLSettings(),
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.storage, self.settings, self.clock = storage, settings, clock

    async def _snapshots(self, cluster: str, namespace: str | None = None,
                         workload: str | None = None) -> dict[tuple[str, str], list[WorkloadSnapshot]]:
        end = self.clock()
        start = end - timedelta(hours=self.settings.history_hours)
        rows = await self.storage.query_workload_snapshots(start=start, end=end, cluster=cluster,
                                                           namespace=namespace, workload=workload)
        grouped: dict[tuple[str, str], list[WorkloadSnapshot]] = defaultdict(list)
        for r in rows:
            grouped[(r.namespace, r.workload)].append(r)
        return grouped

    def series(self, cluster: str, namespace: str, workload: str, snaps: Sequence[WorkloadSnapshot]) -> dict[str, Series]:
        out = {}
        for metric, pts in _metric_points(snaps, self.settings.cpu_price_hour, self.settings.memory_price_hour).items():
            ts, vs = resample(pts, self.settings.step_seconds)
            if vs:
                out[metric] = Series(metric, f"{cluster}/{namespace}/{workload}", METRICS[metric][0],
                                     self.settings.step_seconds, tuple(ts), tuple(vs))
        return out

    def analyze(self, cluster: str, namespace: str, workload: str, snaps: Sequence[WorkloadSnapshot],
                *, with_forecast: bool = True) -> WorkloadAnalysis:
        cfg, last = self.settings, snaps[-1]
        series = self.series(cluster, namespace, workload, snaps)
        a = WorkloadAnalysis(cluster, namespace, workload, last.team, last.cost_center, last.environment,
                             round(len(series.get("cpu", ())) * cfg.step_seconds / 3600, 1),
                             {"cpu": last.cpu_cores_requested, "memory": last.memory_gb_requested})
        for metric, s in series.items():
            a.anomalies += anomaly_mod.detect(s, method=cfg.anomaly_method, abs_floor=METRICS[metric][1],
                                              threshold=cfg.anomaly_threshold)
            if with_forecast:
                a.forecasts[metric] = evaluate(list(s.values), horizon=cfg.horizon, step_seconds=cfg.step_seconds)
        for res, price in (("cpu", cfg.cpu_price_hour), ("memory", cfg.memory_price_hour)):
            s, fc = series.get(res), a.forecasts.get(res)
            upper = rel = None
            if fc is not None:
                upper = max(fc.forecast) + fc.interval_halfwidth
                level = mean(s.values[-fc.horizon * 6:]) if s else 0
                rel = fc.test[fc.chosen].mae / level if level else None
            a.rightsizing[res] = propose(res, list(s.values) if s else [], a.requests[res],
                                         step_seconds=cfg.step_seconds, price_per_unit_hour=price,
                                         forecast_upper=upper, forecast_rel_error=rel, min_hours=cfg.min_hours)
        if cluster in cfg.standby_clusters:
            a.notes.append("standby cluster: right-sizing shown for information only, never recommended "
                           "(a standby must be able to absorb the primary's load on failover)")
        return a

    async def analyze_workload(self, cluster: str, namespace: str, workload: str) -> WorkloadAnalysis | None:
        snaps = (await self._snapshots(cluster, namespace, workload)).get((namespace, workload))
        return self.analyze(cluster, namespace, workload, snaps) if snaps else None

    async def anomalies(self, cluster: str, since_hours: float) -> list[AnomalyEpisode]:
        cutoff = self.clock() - timedelta(hours=since_hours)
        out = []
        for (ns, wl), snaps in (await self._snapshots(cluster)).items():
            a = self.analyze(cluster, ns, wl, snaps, with_forecast=False)
            out += [e for e in a.anomalies if e.end >= cutoff]
        return sorted(out, key=lambda e: e.start)

    def to_recommendations(self, a: WorkloadAnalysis) -> list[Recommendation]:
        cfg, recs = self.settings, []
        scope = dict(cluster=a.cluster, workload=a.workload, team=a.team, cost_center=a.cost_center,
                     environment=a.environment, namespace=a.namespace)
        for res, p in a.rightsizing.items():
            if (p is None or p.action == "keep" or p.confidence < cfg.min_confidence
                    or a.cluster in cfg.standby_clusters):
                continue
            recs.append(Recommendation(
                category=Category.COST_RIGHTSIZING_ML,
                severity=Severity.WARNING if p.action == "increase" else Severity.INFO,
                resource=res,
                message=(f"[ML v3] {p.reason}; confidence {p.confidence} {p.confidence_factors}"
                         + (f"; extra cost ≈ ${-p.expected_monthly_saving_usd:.2f}/month" if p.action == "increase" else "")),
                current_value=f"{p.current_request:.3g} {p.unit}",
                recommended_value=f"{p.recommended_request:.3g} {p.unit}",
                # API contract: savings ≥ 0. An increase is a COST, stated in the message,
                # never a negative "saving" (a −0.18 broke /api/recommendations/history).
                estimated_savings_usd=p.expected_monthly_saving_usd if p.action == "decrease" else None,
                estimation_method="ml:v3 p95/p99 + validated forecast + CV margin",
                **scope))
        recent = self.clock() - timedelta(hours=cfg.recent_anomaly_hours)
        for e in a.anomalies:
            if e.end < recent:
                continue
            recs.append(Recommendation(
                category=Category.ANOMALY,
                severity=Severity.CRITICAL if e.score >= 3 * cfg.anomaly_threshold else Severity.WARNING,
                resource=e.metric,
                message=f"[ML v3] {e.reason} ({e.start:%H:%M}–{e.end:%H:%M} UTC) — investigate",
                current_value=f"{e.peak_value:.4g} {e.unit}",
                recommended_value=f"baseline {e.baseline:.4g} {e.unit}",
                estimation_method=f"ml:v3 {e.method} (score {e.score})",
                **scope))
        return recs

    async def run_once(self, clusters: Sequence[str]) -> dict[str, int]:
        """One ML cycle (leader only): analyse every workload, upsert recommendations."""
        counts: dict[str, int] = defaultdict(int)
        ml_categories = {Category.ANOMALY.value, Category.COST_RIGHTSIZING_ML.value}
        for cluster in clusters:
            keep: dict[str, set[str]] = defaultdict(set)
            for (ns, wl), snaps in (await self._snapshots(cluster)).items():
                a = self.analyze(cluster, ns, wl, snaps)
                for rec in self.to_recommendations(a):
                    _, action = await self.storage.upsert_recommendation(rec)
                    counts[action] += 1
                    keep[ns].add(rec.fingerprint)
                keep.setdefault(ns, set())
            for ns, fps in keep.items():   # close ML advice that no longer holds (only ML categories)
                closed = await self.storage.mark_obsolete(cluster=cluster, namespace=ns, keep_fingerprints=fps,
                                                          categories=ml_categories)
                counts["obsolete"] += closed if isinstance(closed, int) else 0
        return dict(counts)


async def run_ml_loop(service: MLService, clusters: Sequence[str], leader: LeaderElectionPort,
                      interval_seconds: float) -> None:
    """Leader-only. A follower (or a replica that just started, before the election)
    re-checks every `retry_seconds` instead of sleeping a whole interval — found live:
    the first cycle was skipped at startup and postponed by one hour."""
    retry_seconds = min(15.0, interval_seconds)
    while True:
        ran = False
        try:
            if leader.is_leader:
                counts = await service.run_once(clusters)
                logger.info("ML cycle: %s", counts)
                ran = True
        except Exception as exc:
            logger.error("ML cycle failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds if ran else retry_seconds)
