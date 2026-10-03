"""
Snapshot job — periodic collection, analysis and persistence (V1: multicluster).

Every cycle, for every cluster, for every selected namespace:

    MetricsPort (that cluster's Prometheus)
        get_pod_metrics            namespace aggregate
        get_workload_metrics       per-workload breakdown (label `app` by default)
        get_storage_metrics        PVCs (None if unavailable → not priced)
        get_energy                 energy + provenance (container/pod/node-share/unavailable)
        check_data_quality         which telemetry sources are up
          ↓
    Domain (pure)
        finops_engine              requested / actual cost, per-component waste
        greenit_engine             kWh → kgCO2 (None if energy unavailable)
        recommendation_engine      rules → Recommendations (per workload when possible)
          ↓
    StoragePort (TimescaleDB)
        upsert_recommendation      de-duplicated by fingerprint
        save_workload_snapshots    per-workload cost history
        save_snapshot              per-namespace cost + energy + CO2 history

WHY this is still the ONLY writer: routers must never write on each request
(Grafana refreshes would duplicate rows) — unchanged from the MVP.

WHY asyncio.sleep (not Celery/CronJob): single-replica MVP; run_once() is the
entry point to move to an OpenShift CronJob later (docs/ROADMAP.md).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Mapping, Sequence

from app import observability as obs
from app.config import settings
from app.domain.finops_engine import (
    BYTES_PER_GB,
    calculate_monthly_cost,
    calculate_requested_cost,
    summarize_waste,
)
from app.domain.greenit_engine import (
    build_energy_breakdown,
    calculate_carbon_footprint,
    carbon_footprint_from_measurement,
)
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.energy import EnergyAttribution, EnergyMeasurement, WorkloadEnergy
from app.domain.models.pod_metric import PodMetric, StorageMetric
from app.domain.models.recommendation import Category, RecommendationSet
from app.domain.models.snapshot import MetricSnapshot, WorkloadSnapshot
from app.domain.namespace_selection import select_namespaces
from app.domain.recommendation_engine import generate_all_recommendations
from app.ports.metrics_port import MetricsPort
from app.ports.failover_ports import LeaderElectionPort
from app.ports.storage_port import StoragePort

logger = logging.getLogger(__name__)

# Default interval between snapshot runs (seconds) — overridable by
# SCHEDULER_INTERVAL_SECONDS. 5 min ≈ 288 points/day/namespace.
SNAPSHOT_INTERVAL_SECONDS = 300


# =============================================================================
# Namespace resolution
# =============================================================================


async def resolve_namespaces(
    metrics_port: MetricsPort,
    namespaces: Sequence[str] | None,
) -> list[str]:
    """
    V1 fix — the MVP processed only ["default"].

    - explicit list given (argument or SCHEDULER_NAMESPACES) → used as-is
    - otherwise discovered from Prometheus and filtered by
      NAMESPACE_INCLUDE_REGEX / NAMESPACE_EXCLUDE_REGEX
    """
    explicit = list(namespaces) if namespaces else settings.explicit_namespaces
    if explicit:
        return select_namespaces([], explicit=explicit)
    try:
        discovered = await metrics_port.list_namespaces()
        discovered = [n for n in discovered if isinstance(n, str)]
    except Exception as exc:
        logger.warning("Namespace discovery failed: %s", exc)
        discovered = []
    return select_namespaces(
        discovered,
        include_regex=settings.NAMESPACE_INCLUDE_REGEX,
        exclude_regex=settings.NAMESPACE_EXCLUDE_REGEX,
    )


# =============================================================================
# One cluster, one cycle
# =============================================================================


async def run_snapshot_cycle(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    *,
    namespaces: Sequence[str] | None = None,
    cluster: str = "default",
) -> int:
    """Execute one cycle for ONE cluster. Returns the number of recommendations generated.

    Each namespace is processed independently: one failure never blocks the others.
    """
    now = datetime.now(timezone.utc)
    rec_count = 0
    started = time.monotonic()
    selected = await resolve_namespaces(metrics_port, namespaces)
    if not selected:
        logger.warning("cluster=%s: no namespace selected (discovery empty or all excluded)", cluster)

    for ns in selected:
        try:
            rec_count += await _process_namespace(
                metrics_port=metrics_port,
                storage_port=storage_port,
                namespace=ns,
                now=now,
                cluster=cluster,
            )
        except Exception as exc:
            obs.NAMESPACE_ERRORS.labels(cluster).inc()
            logger.error("Snapshot failed for cluster=%s namespace=%s: %s", cluster, ns, exc, exc_info=True)

    obs.NAMESPACES_PROCESSED.labels(cluster).set(len(selected))
    obs.SCHEDULER_CYCLE_SECONDS.labels(cluster).observe(time.monotonic() - started)
    obs.LAST_CYCLE_TIMESTAMP.labels(cluster).set(time.time())
    return rec_count


async def run_multicluster_cycle(
    cluster_ports: Mapping[str, MetricsPort],
    storage_port: StoragePort,
    *,
    namespaces: Sequence[str] | None = None,
) -> int:
    """One cycle over every cluster. An unreachable cluster is skipped (logged + counted)."""
    total = 0
    for name, port in cluster_ports.items():
        try:
            reachable = await port.ping()
        except Exception:
            reachable = False
        if not reachable:
            obs.SCHEDULER_CYCLES.labels(name, "unreachable").inc()
            logger.warning("cluster=%s: Prometheus unreachable, cycle skipped", name)
            continue
        total += await run_snapshot_cycle(port, storage_port, namespaces=namespaces, cluster=name)
        obs.SCHEDULER_CYCLES.labels(name, "ok").inc()
    return total


# =============================================================================
# One namespace
# =============================================================================


ML_CATEGORIES = {Category.ANOMALY.value, Category.COST_RIGHTSIZING_ML.value}


def _is_absent(pod: PodMetric) -> bool:
    """No request and no usage at all: the namespace is not deployed on this cluster."""
    return not any((pod.cpu_requested, pod.memory_requested, pod.cpu_core_hours, pod.memory_bytes))


def _unavailable_energy(namespace: str) -> EnergyMeasurement:
    return EnergyMeasurement(namespace, None, None, EnergyAttribution.UNAVAILABLE, "none")


async def _process_namespace(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    namespace: str,
    now: datetime,
    cluster: str = "default",
) -> int:
    # --- Phase 1: collect ------------------------------------------------------
    try:
        pod_metrics = await metrics_port.get_pod_metrics(namespace=namespace)
    except Exception as exc:
        logger.warning("Failed to fetch pod metrics for namespace=%s: %s", namespace, exc)
        pod_metrics = []

    if not pod_metrics:
        logger.warning("No pod metrics for namespace=%s — skipping", namespace)
        return 0
    pod = dataclasses.replace(pod_metrics[0], cluster=cluster)
    if _is_absent(pod):
        # Found by the REAL E2E test (V2): an explicit namespace list is shared by
        # all clusters, and PromQL sum() over no series became 0.0 → a zero-cost
        # snapshot (plus recommendations) for a namespace that does not exist on
        # this cluster. Nothing requested and nothing used = not deployed here.
        logger.info("cluster=%s namespace=%s: no workload on this cluster — skipped", cluster, namespace)
        return 0

    try:
        quality = await metrics_port.check_data_quality()
    except Exception as exc:
        # V1 fix: the MVP fallback omitted `timestamp` and raised TypeError.
        # Unknown health → assume NOTHING is healthy (safer than assuming all is).
        logger.warning("Failed to check data quality: %s", exc)
        quality = DataQuality(healthy_sources=frozenset(), timestamp=now)

    try:
        raw_workloads = await metrics_port.get_workload_metrics(namespace, namespace_total=pod)
        workloads = [dataclasses.replace(w, cluster=cluster) for w in raw_workloads if isinstance(w, PodMetric)]
    except Exception as exc:
        logger.warning("Workload breakdown unavailable for namespace=%s: %s", namespace, exc)
        workloads = []

    storage: list[StorageMetric] | None
    try:
        raw_storage = await metrics_port.get_storage_metrics(namespace=namespace)
        storage = [s for s in raw_storage if isinstance(s, StorageMetric)] if isinstance(raw_storage, list) else None
    except Exception as exc:
        logger.warning("Storage metrics unavailable for namespace=%s: %s", namespace, exc)
        storage = None  # → storage_status "unavailable", NOT priced as 0

    try:
        energy = await metrics_port.get_energy(namespace=namespace)
        if not isinstance(energy, EnergyMeasurement):
            energy = _unavailable_energy(namespace)
    except Exception as exc:
        logger.warning("Energy unavailable for namespace=%s: %s", namespace, exc)
        energy = _unavailable_energy(namespace)

    # V2: energy over THIS cycle's interval (summable for hour/day/month totals)
    # and per workload (CO2 per workload).
    interval = settings.SCHEDULER_INTERVAL_SECONDS
    try:
        interval_energy = await metrics_port.get_energy(namespace=namespace, window_seconds=interval)
        if not isinstance(interval_energy, EnergyMeasurement):
            interval_energy = _unavailable_energy(namespace)
    except Exception as exc:
        logger.warning("Interval energy unavailable for namespace=%s: %s", namespace, exc)
        interval_energy = _unavailable_energy(namespace)
    try:
        raw_wl_energy = await metrics_port.get_workload_energy(namespace, window_seconds=interval)
        wl_energy = {e.workload: e for e in raw_wl_energy if isinstance(e, WorkloadEnergy)}
    except Exception as exc:
        logger.warning("Workload energy unavailable for namespace=%s: %s", namespace, exc)
        wl_energy = {}

    # --- Phase 2: domain calculations --------------------------------------------
    prices = dict(
        cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD,
        cost_per_gb_hour=settings.COST_PER_GB_HOUR_USD,
        cost_per_gb_month=settings.COST_PER_GB_MONTH_USD,
    )
    requested = calculate_requested_cost(pod, storage_metrics=storage, **prices)
    actual = calculate_monthly_cost(pod, storage_metrics=storage, **prices)
    waste = summarize_waste(pod, requested, actual)

    carbon = carbon_footprint_from_measurement(
        energy,
        team=pod.team, cost_center=pod.cost_center, environment=pod.environment,
        pue=settings.PUE_CONSTANT,
        grid_intensity_gco2_per_kwh=settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH,
        timestamp=now,
    )
    breakdown = (
        build_energy_breakdown(namespace, energy.total_kwh or 0.0, energy.idle_kwh, now)
        if energy.is_available else None
    )

    rec_set: RecommendationSet = generate_all_recommendations(
        pod=pod,
        carbon_footprint=carbon,
        energy_breakdown=breakdown,
        data_quality=quality,
        workloads=workloads or None,
        min_observation_hours=settings.MIN_OBSERVATION_HOURS,
        **prices,
    )

    # --- Phase 3: persist ----------------------------------------------------------
    all_saved = True
    for rec in rec_set.recommendations:
        try:
            _, action = await storage_port.upsert_recommendation(rec)
            obs.RECOMMENDATIONS_WRITTEN.labels(cluster, str(action)).inc()
        except Exception as exc:
            all_saved = False
            logger.error("Failed to save recommendation for namespace=%s: %s", namespace, exc)

    # Pending advice not produced this cycle → obsolete (condition resolved).
    # Only when this cycle saw the namespace completely; otherwise a transient
    # failure would wrongly close valid advice.
    if all_saved:
        try:
            closed = await storage_port.mark_obsolete(
                cluster=cluster, namespace=namespace,
                keep_fingerprints={r.fingerprint for r in rec_set.recommendations},
                # V3: ML recommendations have their own (hourly) producer and lifecycle.
                exclude_categories=ML_CATEGORIES,
            )
            if isinstance(closed, int) and closed:
                obs.RECOMMENDATIONS_WRITTEN.labels(cluster, "obsolete").inc(closed)
        except Exception as exc:
            logger.error("Failed to close obsolete recommendations for namespace=%s: %s", namespace, exc)

    def savings_of(workload: str | None) -> float:
        return round(sum(
            r.estimated_savings_usd or 0.0
            for r in rec_set.recommendations
            if r.category is Category.COST_RIGHTSIZING and (workload is None or r.workload == workload)
        ), 4)

    def co2_of(kwh: float | None) -> float | None:
        if kwh is None:
            return None
        return round(calculate_carbon_footprint(
            kwh, settings.PUE_CONSTANT, settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH), 9)

    workload_rows = []
    for w in workloads:
        w_req = calculate_requested_cost(w, **prices)
        w_act = calculate_monthly_cost(w, **prices)
        w_waste = summarize_waste(w, w_req, w_act)
        we = wl_energy.get(w.workload)
        workload_rows.append(WorkloadSnapshot(
            time=now, cluster=cluster, namespace=namespace, workload=w.workload,
            team=w.team, cost_center=w.cost_center, environment=w.environment,
            cpu_cores_used=w.cpu_core_hours, cpu_cores_requested=w.cpu_requested,
            memory_gb_used=w.memory_bytes / BYTES_PER_GB, memory_gb_requested=w.memory_requested / BYTES_PER_GB,
            requested_cost_usd=w_req.total_cost_usd, actual_cost_usd=w_act.total_cost_usd,
            waste_cost_usd=w_waste.wasted_cost_usd, potential_savings_usd=savings_of(w.workload),
            energy_interval_kwh=we.kwh if we else None,
            co2_interval_kg=co2_of(we.kwh) if we else None,
            energy_precision=we.precision if we else "unavailable",
            energy_power_source=we.power_source.value if we else "unknown",
            cpu_cores_used_now=w.cpu_cores_now,
        ))
    if workload_rows:
        try:
            await storage_port.save_workload_snapshots(workload_rows)
        except Exception as exc:
            logger.error("Failed to save workload snapshots for namespace=%s: %s", namespace, exc)

    snapshot = MetricSnapshot(
        time=now,
        cluster=cluster,
        namespace=namespace,
        team=pod.team,
        cost_center=pod.cost_center,
        environment=pod.environment,
        cpu_core_hours=pod.cpu_core_hours,
        memory_gb_hours=pod.memory_bytes / BYTES_PER_GB,
        cost_usd=actual.total_cost_usd,  # V1 fix: was always 0.0
        requested_cost_usd=requested.total_cost_usd,
        waste_cost_usd=waste.wasted_cost_usd,
        waste_pct=round(waste.waste_ratio * 100, 2),
        potential_savings_usd=savings_of(None),
        # NULL unless at least one PVC was really priced (seen on the live FinOps
        # dashboard: status "unavailable" displayed next to a 0.0 cost).
        storage_cost_usd=(
            actual.storage_cost_usd
            if requested.storage_status.value in ("available", "partial", "not_applicable") else None
        ),
        storage_status=requested.storage_status.value,
        energy_kwh=energy.total_kwh,
        co2e_kg=carbon.co2_kg if carbon else None,
        idle_energy_kwh=energy.idle_kwh,
        energy_attribution=energy.attribution.value,
        energy_power_source=energy.power_source.value,
        energy_scope=energy.scope,
        energy_interval_kwh=interval_energy.total_kwh,
        co2_interval_kg=co2_of(interval_energy.total_kwh),
        interval_seconds=float(interval),
    )
    try:
        await storage_port.save_snapshot(snapshot)
    except Exception as exc:
        logger.error("Failed to save snapshot for namespace=%s: %s", namespace, exc)

    logger.info(
        "Snapshot cluster=%s namespace=%s: %d recs, cost=$%.2f/month, waste=$%.2f, co2=%s kg (%s)",
        cluster, namespace, len(rec_set.recommendations), actual.total_cost_usd,
        waste.wasted_cost_usd, f"{carbon.co2_kg:.3f}" if carbon else "n/a", energy.attribution.value,
    )
    return len(rec_set.recommendations)


# =============================================================================
# Scheduler loop (runs in the FastAPI lifespan)
# =============================================================================


async def start_snapshot_scheduler(
    metrics_port: MetricsPort | None = None,
    storage_port: StoragePort | None = None,
    *,
    interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS,
    namespaces: Sequence[str] | None = None,
    cluster_ports: Mapping[str, MetricsPort] | None = None,
    leader: "LeaderElectionPort | None" = None,
) -> None:
    """Run a cycle every `interval_seconds` until cancelled.

    V2 HA: with a `leader`, a cycle only runs on the replica holding the lock —
    N replicas behind a Service never write the same cycle N times.

    - cluster_ports: {cluster_name: MetricsPort} (V1 multicluster)
    - metrics_port:  single-cluster shortcut (MVP signature, cluster "default")
    """
    if storage_port is None:
        raise ValueError("storage_port is required")
    ports = dict(cluster_ports) if cluster_ports else {"default": metrics_port}
    if any(p is None for p in ports.values()):
        raise ValueError("metrics_port or cluster_ports is required")
    logger.info("Starting snapshot scheduler (interval=%ds, clusters=%s)", interval_seconds, list(ports))

    while True:
        try:
            if leader is not None and not await leader.try_acquire():
                obs.SCHEDULER_SKIPPED_NOT_LEADER.inc()
                logger.debug("not the leader — cycle skipped")
            else:
                count = await run_multicluster_cycle(ports, storage_port, namespaces=namespaces)
                logger.info("Snapshot cycle complete: %d recommendations generated", count)
        except Exception as exc:
            logger.critical("Snapshot cycle failed unexpectedly: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds)


async def run_once(
    metrics_port: MetricsPort,
    storage_port: StoragePort,
    *,
    namespaces: Sequence[str] | None = None,
    cluster: str = "default",
) -> int:
    """Single cycle for one cluster (CronJob / CLI / backfill)."""
    return await run_snapshot_cycle(metrics_port, storage_port, namespaces=namespaces, cluster=cluster)
