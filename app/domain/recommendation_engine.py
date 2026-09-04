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

from app.domain.finops_engine import (
    BYTES_PER_GB,
    NamespaceCostSummary,
    calculate_monthly_cost,
    calculate_overprovisioning_ratio,
    calculate_requested_cost,
)
from app.domain.models.carbon_footprint import CarbonFootprint, EnergyBreakdown
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

# --- Data quality ---
# If any source is degraded, we generate a DATA_QUALITY recommendation.
# No numeric threshold — binary: source is either up or down.


# ---------------------------------------------------------------------------
# Public API — the single entry point for generating ALL recommendations
# ---------------------------------------------------------------------------

def generate_all_recommendations(
    pod: PodMetric,
    carbon_footprint: CarbonFootprint,
    energy_breakdown: EnergyBreakdown,
    data_quality: DataQuality,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
) -> RecommendationSet:
    """
    Generate ALL recommendations for a namespace: FinOps + Green IT + Data Quality.

    This is the TOP-LEVEL orchestrator. It:
    1. Computes FinOps analysis (ratio, waste) from PodMetric.
    2. Interprets Green IT analysis (carbon, idle waste) from CarbonFootprint/EnergyBreakdown.
    3. Checks DataQuality to decide which categories to skip.
    4. Generates Recommendations with appropriate severity.

    WHY this function takes pre-computed objects (not raw Prometheus data):
    - The adapter already queried Prometheus and built PodMetric/CarbonFootprint.
    - The adapter already called greenit_engine.build_carbon_footprint().
    - This function just INTERPRETS those results and decides what to recommend.
    - No duplication: finops_engine functions are called here for ratio/waste,
      but the heavy lifting (Prometheus queries) happened in the adapter.

    Returns:
        RecommendationSet containing all generated recommendations.
    """
    recommendations: list[Recommendation] = []
    now = datetime.now(timezone.utc)

    # --- Phase 1: FinOps (cost) recommendations ---
    # Only if kube-state-metrics is healthy (provides resource requests/limits).
    if data_quality.is_healthy(TelemetrySource.KUBE_STATE_METRICS):
        finops_recs = _generate_finops_recommendations(
            pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month
        )
        recommendations.extend(finops_recs)
    else:
        recommendations.append(_data_quality_recommendation(
            pod.namespace,
            "kube-state-metrics",
            "FinOps metrics unavailable — kube-state-metrics is down.",
        ))

    # --- Phase 2: Green IT (carbon) recommendations ---
    # Only if Kepler is healthy (provides energy data).
    if data_quality.is_healthy(TelemetrySource.KEPLER):
        greenit_recs = _generate_greenit_recommendations(
            pod, carbon_footprint, energy_breakdown, cost_per_core_hour
        )
        recommendations.extend(greenit_recs)
    else:
        recommendations.append(_data_quality_recommendation(
            pod.namespace,
            "kepler",
            "Carbon metrics unavailable — Kepler is down.",
        ))

    # --- Phase 3: Data quality warnings (always included) ---
    for source in data_quality.degraded_sources:
        # Avoid duplicates (already added above for kepler and kube-state-metrics).
        if source not in (TelemetrySource.KEPLER, TelemetrySource.KUBE_STATE_METRICS):
            recommendations.append(_data_quality_recommendation(
                pod.namespace,
                source.value,
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

def _generate_finops_recommendations(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
) -> list[Recommendation]:
    """
    Generate FinOps recommendations by calling finops_engine functions.

    WHY we call finops_engine here (not duplicate logic):
    - finops_engine.calculate_overprovisioning_ratio() does the math.
    - finops_engine.calculate_namespace_waste() does the cost analysis.
    - We just INTERPRET their outputs and decide severity/message.

    This is composition, not duplication.
    """
    ratio = calculate_overprovisioning_ratio(pod)
    waste = _compute_waste(pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month)
    recommendations: list[Recommendation] = []

    # --- Over-provisioned CPU ---
    if ratio.cpu_ratio < OVERPROVISIONED_CPU_THRESHOLD:
        recommendations.append(_recommendation(
            category=Category.COST_RIGHTSIZING,
            severity=_severity_from_waste(waste.waste_ratio),
            namespace=pod.namespace,
            resource="cpu",
            message=(
                f"CPU is {ratio.cpu_ratio:.0%} utilized. "
                f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.1f}. "
                f"Recommend reducing to {round(pod.cpu_core_hours * 1.2, 2)} cores "
                f"(20% headroom)."
            ),
            current_value=f"{pod.cpu_requested}",
            recommended_value=f"{round(pod.cpu_core_hours * 1.2, 2)}",
            estimated_savings_usd=round(waste.wasted_cost_usd * (1 - ratio.cpu_ratio), 4),
            timestamp=pod.timestamp,
        ))

    # --- Over-provisioned memory ---
    if ratio.memory_ratio < OVERPROVISIONED_MEMORY_THRESHOLD:
        recommendations.append(_recommendation(
            category=Category.COST_RIGHTSIZING,
            severity=_severity_from_waste(waste.waste_ratio),
            namespace=pod.namespace,
            resource="memory",
            message=(
                f"Memory is {ratio.memory_ratio:.0%} utilized. "
                f"Requesting {pod.memory_requested / BYTES_PER_GB:.1f} GB "
                f"but using {pod.memory_bytes / BYTES_PER_GB:.1f} GB. "
                f"Recommend reducing to {round(pod.memory_bytes * 1.2 / BYTES_PER_GB, 1)} GB "
                f"(20% headroom)."
            ),
            current_value=f"{pod.memory_requested / BYTES_PER_GB:.1f}",
            recommended_value=f"{round(pod.memory_bytes * 1.2 / BYTES_PER_GB, 1)}",
            estimated_savings_usd=round(waste.wasted_cost_usd * (1 - ratio.memory_ratio), 4),
            timestamp=pod.timestamp,
        ))

    # --- Under-provisioned CPU (throttling risk) ---
    if ratio.cpu_ratio > UNDERPROVISIONED_CPU_THRESHOLD:
        recommendations.append(_recommendation(
            category=Category.COST_RIGHTSIZING,
            severity=Severity.CRITICAL,
            namespace=pod.namespace,
            resource="cpu",
            message=(
                f"CPU is at {ratio.cpu_ratio:.0%} of request — throttling likely. "
                f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.1f}. "
                f"Recommend increasing to {round(pod.cpu_core_hours * 1.25, 2)} cores."
            ),
            current_value=f"{pod.cpu_requested}",
            recommended_value=f"{round(pod.cpu_core_hours * 1.25, 2)}",
            timestamp=pod.timestamp,
        ))

    # --- Under-provisioned memory (OOM risk) ---
    if ratio.memory_ratio > UNDERPROVISIONED_MEMORY_THRESHOLD:
        recommendations.append(_recommendation(
            category=Category.COST_RIGHTSIZING,
            severity=Severity.CRITICAL,
            namespace=pod.namespace,
            resource="memory",
            message=(
                f"Memory is at {ratio.memory_ratio:.0%} of request — OOM risk. "
                f"Requesting {pod.memory_requested / BYTES_PER_GB:.1f} GB "
                f"but using {pod.memory_bytes / BYTES_PER_GB:.1f} GB. "
                f"Recommend increasing to {round(pod.memory_bytes * 1.25 / BYTES_PER_GB, 1)} GB."
            ),
            current_value=f"{pod.memory_requested / BYTES_PER_GB:.1f}",
            recommended_value=f"{round(pod.memory_bytes * 1.25 / BYTES_PER_GB, 1)}",
            timestamp=pod.timestamp,
        ))

    return recommendations


# ---------------------------------------------------------------------------
# Green IT recommendation generators (private)
# ---------------------------------------------------------------------------

def _generate_greenit_recommendations(
    pod: PodMetric,
    carbon_footprint: CarbonFootprint,
    energy_breakdown: EnergyBreakdown,
    cost_per_core_hour: float,
) -> list[Recommendation]:
    """
    Generate Green IT recommendations from CarbonFootprint and EnergyBreakdown.

    WHY these are informational (not actionable):
    - Carbon footprint is a fact, not a problem to fix.
    - Idle waste is a signal for investigation, not an automatic fix.
    - The engine flags them; the human decides what to do.
    """
    recommendations: list[Recommendation] = []

    # --- Idle energy waste ---
    if energy_breakdown.total_energy_kwh > 0:
        waste_pct = energy_breakdown.waste_pct
        if waste_pct > IDLE_WASTE_CRITICAL_PCT:
            severity = Severity.CRITICAL
        elif waste_pct > IDLE_WASTE_WARNING_PCT:
            severity = Severity.WARNING
        else:
            severity = Severity.INFO

        if waste_pct > IDLE_WASTE_WARNING_PCT:
            recommendations.append(_recommendation(
                category=Category.ENERGY_WASTE,
                severity=severity,
                namespace=pod.namespace,
                resource="energy",
                message=(
                    f"Idle waste is {waste_pct:.0f}% of total energy "
                    f"({energy_breakdown.idle_waste_kwh:.1f} kWh of "
                    f"{energy_breakdown.total_energy_kwh:.1f} kWh). "
                    f"Consider right-sizing or consolidating workloads."
                ),
                current_value=f"{waste_pct:.0f}%",
                recommended_value="< 30%",
                estimated_savings_kg_co2=round(
                    energy_breakdown.idle_waste_kwh * (carbon_footprint.co2_kg / carbon_footprint.energy_kwh)
                    if carbon_footprint.energy_kwh > 0 else 0.0, 4
                ),
                timestamp=pod.timestamp,
            ))

    # --- Carbon footprint (informational) ---
    if carbon_footprint.co2_kg > 0:
        recommendations.append(_recommendation(
            category=Category.CARBON_INTENSITY,
            severity=Severity.INFO,
            namespace=pod.namespace,
            resource="carbon",
            message=(
                f"Carbon footprint: {carbon_footprint.co2_kg:.2f} kgCO2 "
                f"from {carbon_footprint.energy_kwh:.1f} kWh energy consumption."
            ),
            current_value=f"{carbon_footprint.co2_kg:.2f} kgCO2",
            recommended_value="N/A (informational)",
            timestamp=pod.timestamp,
        ))

    return recommendations


# ---------------------------------------------------------------------------
# Data quality recommendation (private)
# ---------------------------------------------------------------------------

def _data_quality_recommendation(
    namespace: str,
    source: str,
    message: str,
) -> Recommendation:
    """Generate a DATA_QUALITY recommendation for a degraded source."""
    return _recommendation(
        category=Category.DATA_QUALITY,
        severity=Severity.WARNING,
        namespace=namespace,
        resource="telemetry",
        message=message,
        current_value=f"{source}: down",
        recommended_value=f"{source}: up",
        timestamp=datetime.now(timezone.utc),
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
    """
    Compute waste for a pod (calls finops_engine internally).

    WHY duplicated from finops_engine.generate_recommendations():
    - That function generates FinOps-only recommendations.
    - This engine needs JUST the waste calculation (not the recommendations).
    - Calling the calculation function directly avoids generating intermediate
      Recommendation objects that would be thrown away.
    """
    requested = calculate_requested_cost(
        pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month
    )
    actual = calculate_monthly_cost(
        pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month
    )

    wasted = requested.total_cost_usd - actual.total_cost_usd
    waste_ratio = (wasted / requested.total_cost_usd) if requested.total_cost_usd > 0 else 0.0

    return NamespaceCostSummary(
        namespace=pod.namespace,
        requested_cost_usd=requested.total_cost_usd,
        actual_cost_usd=actual.total_cost_usd,
        wasted_cost_usd=round(wasted, 4),
        waste_ratio=round(waste_ratio, 4),
        timestamp=pod.timestamp,
    )


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
    category: Category,
    severity: Severity,
    namespace: str,
    resource: str,
    message: str,
    current_value: str,
    recommended_value: str,
    estimated_savings_kg_co2: float | None = None,
    estimated_savings_usd: float | None = None,
    timestamp: datetime | None = None,
) -> Recommendation:
    """
    Build a Recommendation with consistent defaults.

    WHY a helper:
    - Avoids repeating 8+ keyword arguments at every call site.
    - Ensures timestamp is always set (never None).
    - Centralizes the constructor so changes to Recommendation shape
      only need updating here.
    """
    return Recommendation(
        category=category,
        severity=severity,
        namespace=namespace,
        resource=resource,
        message=message,
        current_value=current_value,
        recommended_value=recommended_value,
        estimated_savings_kg_co2=estimated_savings_kg_co2,
        estimated_savings_usd=estimated_savings_usd,
        timestamp=timestamp or datetime.now(timezone.utc),
    )