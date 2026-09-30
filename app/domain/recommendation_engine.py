"""
Recommendation engine — top-level orchestrator for FinOps + Green IT advice.

WHY this file composes finops_engine and greenit_engine outputs:
- finops_engine knows about COST (requested vs actual, waste ratio).
- greenit_engine knows about CARBON (joules → kWh → kgCO2, idle waste).
- recommendation_engine sits ABOVE both, deciding WHAT to recommend
  based on their outputs. It doesn't recompute — it interprets.

WHY recommendations are generated here (not in a router/adapter):
- A router's job is HTTP: parse request, call service, return response.
- If recommendations were generated in the router, the same logic would be
  duplicated for every endpoint (API, CLI, future gRPC). That's a maintenance trap.
- By putting it in the domain, the engine is testable without FastAPI,
  reusable across any interface, and changes to heuristics are in ONE place.

WHY this file has zero I/O or framework dependency:
- Same rule as finops_engine and greenit_engine: the domain must be testable
  with plain fixtures. No Prometheus, no HTTP, no database.
- The adapter calls this engine with pre-computed inputs. The engine doesn't
  know where the data came from — it just applies heuristics and returns Recommendations.
"""

from __future__ import annotations

from datetime import datetime, timezone

from typing import Sequence

from app.domain.finops_engine import (
    BYTES_PER_GB,
    NamespaceCostSummary,
    calculate_namespace_waste,
    calculate_overprovisioning_ratio,
    rightsizing_savings_usd,
)
from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown
from app.domain.models.energy import EnergyAttribution, PowerSource
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.pod_metric import PodMetric
from app.domain.models.recommendation import (
    Category,
    Recommendation,
    RecommendationSet,
    Severity,
)

# ---------------------------------------------------------------------------
# Heuristics / Thresholds
#
# These define the BOUNDARIES between severity levels.
# WHY as module-level constants:
# - Centralizes tuning: change one number, all recommendations update.
# - Makes the logic testable: we can assert against named thresholds.
# - Defensible in oral exam: "we chose 50% because industry standard..."
# ---------------------------------------------------------------------------

# --- FinOps thresholds (over-provisioning) ---
# Below this ratio, we recommend right-sizing.
OVERPROVISIONED_CPU_THRESHOLD = 0.5      # usage < 50% of requested
OVERPROVISIONED_MEMORY_THRESHOLD = 0.5   # usage < 50% of requested

# Above this ratio, we warn about throttling/OOM risk.
UNDERPROVISIONED_CPU_THRESHOLD = 1.0     # usage > 100% of requested
UNDERPROVISIONED_MEMORY_THRESHOLD = 1.0  # usage > 100% of requested

# Severity escalation based on waste ratio (wasted / requested).
WASTE_CRITICAL_THRESHOLD = 0.7           # > 70% waste → CRITICAL
WASTE_WARNING_THRESHOLD = 0.5            # > 50% waste → WARNING

# --- Green IT thresholds (idle waste) ---
# Idle waste percentage above which we flag energy waste.
IDLE_WASTE_CRITICAL_PCT = 50.0           # > 50% idle → CRITICAL
IDLE_WASTE_WARNING_PCT = 30.0            # > 30% idle → WARNING

# --- Right-sizing headroom ---
REDUCE_HEADROOM = 1.2    # reduce to usage + 20 %
INCREASE_HEADROOM = 1.25  # increase to usage + 25 %

# --- Minimum history before right-sizing ---
# Right-sizing on minutes of data is dangerous (a new deployment looks idle).
# Default 24 h covers one daily cycle; 7 days is advisable in production
# (weekly patterns). Overridable per call (MIN_OBSERVATION_HOURS setting).
DEFAULT_MIN_OBSERVATION_HOURS = 24.0

# --- Data quality ---
# If any source is degraded, we generate a DATA_QUALITY recommendation.
# No numeric threshold — binary: source is either up or down.


# ---------------------------------------------------------------------------
# Public API — the single entry point for generating ALL recommendations
# ---------------------------------------------------------------------------

def generate_all_recommendations(
    pod: PodMetric,
    carbon_footprint: CarbonFootprint | None,
    energy_breakdown: EnergyBreakdown | None,
    data_quality: DataQuality,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
    workloads: Sequence[PodMetric] | None = None,
    min_observation_hours: float = DEFAULT_MIN_OBSERVATION_HOURS,
) -> RecommendationSet:
    """
    Generate ALL recommendations for a namespace: FinOps + Green IT + Data Quality.

    Phases:
    1. FinOps right-sizing — per WORKLOAD when a breakdown is given (V1: requests
       are set on Deployments, so that is where advice is actionable), otherwise
       on the namespace aggregate (MVP behaviour).
    2. Green IT — namespace level (energy is attributed per namespace in V1).
    3. Data quality — one advisory per degraded telemetry source.

    `carbon_footprint` is None when energy could not be attributed at all;
    `energy_breakdown.idle_waste_kwh` is None when idle energy is not measurable.
    In both cases NO Green IT figure is invented.
    """
    recommendations: list[Recommendation] = []
    now = datetime.now(timezone.utc)

    # --- Phase 1: FinOps (cost) recommendations ---
    if data_quality.is_healthy(TelemetrySource.KUBE_STATE_METRICS):
        targets = list(workloads) if workloads else [pod]
        for target in targets:
            if target.observation_hours is not None and target.observation_hours < min_observation_hours:
                recommendations.append(_insufficient_history_recommendation(target, min_observation_hours))
                continue
            recommendations.extend(_generate_finops_recommendations(
                target, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month
            ))
    else:
        recommendations.append(_data_quality_recommendation(
            pod, "kube-state-metrics",
            "FinOps metrics unavailable — kube-state-metrics is down.",
        ))

    # --- Phase 2: Green IT (carbon) recommendations ---
    if data_quality.is_healthy(TelemetrySource.KEPLER):
        if carbon_footprint is None:
            recommendations.append(_data_quality_recommendation(
                pod, "kepler",
                "Kepler is up but no energy series could be attributed to this "
                "namespace — carbon footprint not computed (no value invented).",
                resource="energy_attribution",
            ))
        else:
            recommendations.extend(_generate_greenit_recommendations(
                pod, carbon_footprint, energy_breakdown
            ))
    else:
        recommendations.append(_data_quality_recommendation(
            pod, "kepler", "Carbon metrics unavailable — Kepler is down.",
        ))

    # --- Phase 3: other degraded sources ---
    for source in sorted(data_quality.degraded_sources, key=lambda s: s.value):
        if source not in (TelemetrySource.KEPLER, TelemetrySource.KUBE_STATE_METRICS):
            recommendations.append(_data_quality_recommendation(
                pod, source.value,
                f"Telemetry source '{source.value}' is down — some metrics may be stale.",
            ))

    return RecommendationSet(
        namespace=pod.namespace,
        generated_at=now,
        recommendations=tuple(recommendations),
    )


# ---------------------------------------------------------------------------
# FinOps recommendation generators (private)
# ---------------------------------------------------------------------------

def _scope(pod: PodMetric) -> str:
    """Human prefix for messages: 'workload frontend' or 'namespace'."""
    return f"Workload '{pod.workload}': " if pod.workload != "*" else ""


def _generate_finops_recommendations(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
) -> list[Recommendation]:
    """
    Right-sizing advice for one scope (workload or namespace).

    Savings (V1): `rightsizing_savings_usd(current, recommended, price)` — the
    cost that stops being reserved. Bounded by the resource's own waste
    component, so CPU + memory savings can never exceed total waste.
    """
    ratio = calculate_overprovisioning_ratio(pod)
    waste = _compute_waste(pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month)
    recommendations: list[Recommendation] = []
    prefix = _scope(pod)
    mem_req_gb = pod.memory_requested / BYTES_PER_GB
    mem_used_gb = pod.memory_bytes / BYTES_PER_GB

    # --- Over-provisioned CPU ---
    if pod.cpu_requested > 0 and ratio.cpu_ratio < OVERPROVISIONED_CPU_THRESHOLD:
        target = round(pod.cpu_core_hours * REDUCE_HEADROOM, 2)
        recommendations.append(_recommendation(
            pod,
            category=Category.COST_RIGHTSIZING,
            severity=_severity_from_waste(waste.waste_ratio),
            resource="cpu",
            message=(
                f"{prefix}CPU is {ratio.cpu_ratio:.0%} utilized. "
                f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.2f}. "
                f"Recommend reducing to {target} cores (20% headroom)."
            ),
            current_value=f"{pod.cpu_requested}",
            recommended_value=f"{target}",
            estimated_savings_usd=rightsizing_savings_usd(pod.cpu_requested, target, cost_per_core_hour),
        ))

    # --- Over-provisioned memory ---
    if pod.memory_requested > 0 and ratio.memory_ratio < OVERPROVISIONED_MEMORY_THRESHOLD:
        target_gb = round(mem_used_gb * REDUCE_HEADROOM, 1)
        recommendations.append(_recommendation(
            pod,
            category=Category.COST_RIGHTSIZING,
            severity=_severity_from_waste(waste.waste_ratio),
            resource="memory",
            message=(
                f"{prefix}Memory is {ratio.memory_ratio:.0%} utilized. "
                f"Requesting {mem_req_gb:.1f} GB but using {mem_used_gb:.1f} GB. "
                f"Recommend reducing to {target_gb} GB (20% headroom)."
            ),
            current_value=f"{mem_req_gb:.1f}",
            recommended_value=f"{target_gb}",
            # Savings computed on the unrounded target so they stay ≤ the memory waste.
            estimated_savings_usd=rightsizing_savings_usd(
                mem_req_gb, mem_used_gb * REDUCE_HEADROOM, cost_per_gb_hour
            ),
        ))

    # --- Under-provisioned CPU (throttling risk) ---
    if ratio.cpu_ratio > UNDERPROVISIONED_CPU_THRESHOLD:
        target = round(pod.cpu_core_hours * INCREASE_HEADROOM, 2)
        recommendations.append(_recommendation(
            pod,
            category=Category.COST_RIGHTSIZING,
            severity=Severity.CRITICAL,
            resource="cpu",
            message=(
                f"{prefix}CPU is at {ratio.cpu_ratio:.0%} of request — throttling likely. "
                f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.2f}. "
                f"Recommend increasing to {target} cores."
            ),
            current_value=f"{pod.cpu_requested}",
            recommended_value=f"{target}",
        ))

    # --- Under-provisioned memory (OOM risk) ---
    if ratio.memory_ratio > UNDERPROVISIONED_MEMORY_THRESHOLD:
        target_gb = round(mem_used_gb * INCREASE_HEADROOM, 1)
        recommendations.append(_recommendation(
            pod,
            category=Category.COST_RIGHTSIZING,
            severity=Severity.CRITICAL,
            resource="memory",
            message=(
                f"{prefix}Memory is at {ratio.memory_ratio:.0%} of request — OOM risk. "
                f"Requesting {mem_req_gb:.1f} GB but using {mem_used_gb:.1f} GB. "
                f"Recommend increasing to {target_gb} GB."
            ),
            current_value=f"{mem_req_gb:.1f}",
            recommended_value=f"{target_gb}",
        ))

    return recommendations


# ---------------------------------------------------------------------------
# Green IT recommendation generators (private)
# ---------------------------------------------------------------------------

def _energy_method(cf: CarbonFootprint) -> str | None:
    """Provenance note for recommendations based on estimated energy."""
    notes = []
    if cf.energy_attribution is EnergyAttribution.NODE_CPU_SHARE:
        notes.append("node-level Kepler × namespace CPU share")
    if cf.energy_power_source is PowerSource.MODEL:
        notes.append("Kepler power model, no hardware counters")
    return f"energy: {' + '.join(notes)} (estimated)" if notes else None


def _generate_greenit_recommendations(
    pod: PodMetric,
    carbon_footprint: CarbonFootprint,
    energy_breakdown: EnergyBreakdown | None,
) -> list[Recommendation]:
    """
    Green IT advice. Informational by design: the human decides.

    V1 rules:
    - ENERGY_WASTE only when idle energy is MEASURED (idle_waste_kwh is not None).
    - Estimated (node-level fallback) energy is flagged in the message and in
      `estimation_method`.
    """
    recommendations: list[Recommendation] = []
    method = _energy_method(carbon_footprint)
    flag = ""
    if carbon_footprint.energy_attribution is EnergyAttribution.NODE_CPU_SHARE:
        flag = " [estimated: node-level fallback]"
    elif carbon_footprint.energy_power_source is PowerSource.MODEL:
        flag = " [estimated: Kepler power model]"

    # --- Idle energy waste (only if measured) ---
    if (
        energy_breakdown is not None
        and energy_breakdown.idle_is_known
        and energy_breakdown.total_energy_kwh > 0
    ):
        waste_pct = energy_breakdown.waste_pct or 0.0
        if waste_pct > IDLE_WASTE_WARNING_PCT:
            severity = Severity.CRITICAL if waste_pct > IDLE_WASTE_CRITICAL_PCT else Severity.WARNING
            idle = energy_breakdown.idle_waste_kwh or 0.0
            recommendations.append(_recommendation(
                pod,
                category=Category.ENERGY_WASTE,
                severity=severity,
                resource="energy",
                message=(
                    f"Idle energy is {waste_pct:.0f}% of total "
                    f"({idle:.2f} kWh of {energy_breakdown.total_energy_kwh:.2f} kWh). "
                    f"Consider right-sizing or consolidating workloads.{flag}"
                ),
                current_value=f"{waste_pct:.0f}%",
                recommended_value="< 30%",
                estimated_savings_kg_co2=round(
                    idle * (carbon_footprint.co2_kg / carbon_footprint.energy_kwh)
                    if carbon_footprint.energy_kwh > 0 else 0.0, 4
                ),
                estimation_method=method,
            ))

    # --- Carbon footprint (informational) ---
    if carbon_footprint.co2_kg > 0:
        recommendations.append(_recommendation(
            pod,
            category=Category.CARBON_INTENSITY,
            severity=Severity.INFO,
            resource="carbon",
            message=(
                f"Carbon footprint: {carbon_footprint.co2_kg:.2f} kgCO2 "
                f"from {carbon_footprint.energy_kwh:.1f} kWh energy consumption.{flag}"
            ),
            current_value=f"{carbon_footprint.co2_kg:.2f} kgCO2",
            recommended_value="N/A (informational)",
            estimation_method=method,
        ))

    return recommendations


# ---------------------------------------------------------------------------
# Data quality recommendation (private)
# ---------------------------------------------------------------------------

def _data_quality_recommendation(
    pod: PodMetric,
    source: str,
    message: str,
    resource: str = "telemetry",
) -> Recommendation:
    """Generate a DATA_QUALITY recommendation (namespace scope)."""
    return _recommendation(
        pod,
        category=Category.DATA_QUALITY,
        severity=Severity.WARNING,
        resource=resource,
        message=message,
        current_value=f"{source}: down" if resource == "telemetry" else f"{source}: no attributable series",
        recommended_value=f"{source}: up" if resource == "telemetry" else f"{source}: per-namespace series",
        timestamp=datetime.now(timezone.utc),
        namespace_scope=True,
    )


def _insufficient_history_recommendation(pod: PodMetric, min_hours: float) -> Recommendation:
    """INFO: right-sizing deferred until enough CPU history exists."""
    observed = pod.observation_hours or 0.0
    return _recommendation(
        pod,
        category=Category.DATA_QUALITY,
        severity=Severity.INFO,
        resource="history",
        message=(
            f"{_scope(pod)}Only {observed:.2f} h of CPU history (< {min_hours:g} h required). "
            "Right-sizing deferred — a young workload must not be sized on a few minutes of data."
        ),
        current_value=f"{observed:.2f} h",
        recommended_value=f">= {min_hours:g} h",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_waste(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
) -> NamespaceCostSummary:
    """Per-component waste of one scope (CPU + memory; storage is namespace-level)."""
    return calculate_namespace_waste(pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month)


def _severity_from_waste(waste_ratio: float) -> Severity:
    """
    Map waste ratio to severity level.

    Heuristic:
        waste_ratio > 0.7 → CRITICAL (wasting more than 70% of committed cost)
        waste_ratio > 0.5 → WARNING  (wasting 50-70%)
        else              → INFO     (slightly over, acceptable)
    """
    if waste_ratio > WASTE_CRITICAL_THRESHOLD:
        return Severity.CRITICAL
    elif waste_ratio > WASTE_WARNING_THRESHOLD:
        return Severity.WARNING
    return Severity.INFO


def _recommendation(
    pod: PodMetric,
    *,
    category: Category,
    severity: Severity,
    resource: str,
    message: str,
    current_value: str,
    recommended_value: str,
    estimated_savings_kg_co2: float | None = None,
    estimated_savings_usd: float | None = None,
    timestamp: datetime | None = None,
    estimation_method: str | None = None,
    namespace_scope: bool = False,
) -> Recommendation:
    """
    Build a Recommendation carrying the full scope of `pod`
    (cluster, namespace, workload, team, cost_center, environment).

    WHY (V1): the MVP lost team/cost_center on every recommendation.
    """
    return Recommendation(
        category=category,
        severity=severity,
        namespace=pod.namespace,
        resource=resource,
        message=message,
        current_value=current_value,
        recommended_value=recommended_value,
        estimated_savings_kg_co2=estimated_savings_kg_co2,
        estimated_savings_usd=estimated_savings_usd,
        timestamp=timestamp or pod.timestamp or datetime.now(timezone.utc),
        cluster=pod.cluster,
        workload="*" if namespace_scope else pod.workload,
        team=pod.team,
        cost_center=pod.cost_center,
        environment=pod.environment,
        estimation_method=estimation_method,
    )
