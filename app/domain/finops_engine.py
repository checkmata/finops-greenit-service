"""
FinOps engine — pure calculation logic for cost and over-provisioning.

WHY this file cannot import from adapters/ or ports/:
- If it imported PrometheusAdapter, it would know about HTTP and JSON.
- Then you could not test it without a real Prometheus running.
- By importing ONLY domain models, every function here is a pure math formula:
  feed it numbers, get numbers back. No network, no filesystem, no side effects.
- This is what makes the domain testable with plain fixtures (AGENTS.md rule).

WHY "pure function" matters:
- A pure function: same inputs ALWAYS produce the same output, no hidden state.
- Cost of pod X is always $Y. Every time. No matter where it runs.
- This makes unit tests trivial (no mocks), refactoring safe (behavior doesn't change),
  and parallel execution correct (no shared mutable state).
- I/O-dependent code is untestable in isolation — pure functions are the antidote.

WHY cost parameters are function arguments (not imported from config):
- The domain engine must not import app.config (that would break purity).
- The adapter/layer that calls these functions injects the config values:
    finops_engine.calculate_monthly_cost(pod, cost_per_core_hour=settings.COST_PER_VCPU_HOUR_USD, ...)
- This is dependency injection: the caller provides the strategy, the engine applies it.
- It also means the same function works for any cost model (dev cluster vs prod cluster).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.models.data_quality import MetricStatus
from app.domain.models.pod_metric import PodMetric, StorageMetric
from app.domain.models.recommendation import (
    Category,
    Recommendation,
    RecommendationSet,
    Severity,
)

# ---------------------------------------------------------------------------
# Result dataclasses — shaped by the engine, consumed by the API adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostBreakdown:
    """
    Monthly cost for a namespace, broken down by resource type.

    Fields are named _usd to make the unit explicit in code:
    cost.cpu_cost_usd vs cost.cpu_cost — the suffix prevents accidental
    misuse (e.g., adding USD to EUR without conversion).

    WHY frozen: these are point-in-time calculations, not mutable ledgers.
    """

    cpu_cost_usd: float
    memory_cost_usd: float
    storage_cost_usd: float
    total_cost_usd: float

    # Metadata
    namespace: str
    cost_per_core_hour: float
    cost_per_gb_hour: float
    cost_per_gb_month: float
    timestamp: datetime

    # V1: whether storage_cost_usd reflects real PVC measurements.
    # UNAVAILABLE means "storage not priced", NOT "storage costs nothing".
    storage_status: MetricStatus = MetricStatus.UNAVAILABLE


@dataclass(frozen=True)
class OverprovisioningRatio:
    """
    Ratio of actual usage to requested, per resource type.

    WHY ratio (not percentage): ratios compose cleanly in math:
    - cpu_ratio = 0.3 → workload uses 30% of what it asked for
    - waste_ratio = 1 - cpu_ratio → 70% is wasted
    - percentage is just ratio × 100 for human display

    Thresholds:
    - ratio < 0.5 → over-provisioned (waste > 50%)
    - ratio 0.5–0.8 → slightly over-provisioned (common, acceptable)
    - ratio 0.8–1.0 → well-provisioned
    - ratio > 1.0 → under-provisioned (throttling risk)
    """

    cpu_ratio: float
    memory_ratio: float
    namespace: str
    timestamp: datetime

    @property
    def is_overprovisioned_cpu(self) -> bool:
        """True if CPU usage is below 50% of requested."""
        return self.cpu_ratio < 0.5

    @property
    def is_overprovisioned_memory(self) -> bool:
        """True if memory usage is below 50% of requested."""
        return self.memory_ratio < 0.5

    @property
    def is_underprovisioned_cpu(self) -> bool:
        """True if CPU usage exceeds 100% of requested (throttling risk)."""
        return self.cpu_ratio > 1.0

    @property
    def is_underprovisioned_memory(self) -> bool:
        """True if memory usage exceeds 100% of requested (OOM risk)."""
        return self.memory_ratio > 1.0


@dataclass(frozen=True)
class NamespaceCostSummary:
    """
    Aggregated cost summary for a namespace.
    """

    namespace: str
    requested_cost_usd: float
    actual_cost_usd: float
    wasted_cost_usd: float
    waste_ratio: float
    timestamp: datetime

    # --- V1: waste attributed per resource (all >= 0) ---
    # wasted_cost_usd == cpu + memory + storage components (see calculate_namespace_waste)
    cpu_waste_usd: float = 0.0
    memory_waste_usd: float = 0.0
    storage_waste_usd: float = 0.0
    storage_status: MetricStatus = MetricStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Core calculation functions — pure math, no I/O
# ---------------------------------------------------------------------------

BYTES_PER_GB = 1024 ** 3  # 1,073,741,824
HOURS_PER_MONTH = 24 * 30  # 720 hours in a 30-day month


def storage_status(storage_metrics: list[StorageMetric] | None) -> MetricStatus:
    """
    How trustworthy the storage part of a cost is.

    - None                         → UNAVAILABLE (not fetched / query failed)
    - []                           → NOT_APPLICABLE (namespace has no PVC: 0 is true)
    - every PVC has kubelet stats  → AVAILABLE
    - some / none have stats       → PARTIAL / UNAVAILABLE
    """
    if storage_metrics is None:
        return MetricStatus.UNAVAILABLE
    if not storage_metrics:
        return MetricStatus.NOT_APPLICABLE
    with_stats = sum(1 for s in storage_metrics if s.has_usage_stats)
    if with_stats == len(storage_metrics):
        return MetricStatus.AVAILABLE
    return MetricStatus.PARTIAL if with_stats else MetricStatus.UNAVAILABLE


def _priced_pvcs(storage_metrics: list[StorageMetric] | None) -> list[StorageMetric]:
    """Only PVCs with real kubelet stats are priced (never price a placeholder 0)."""
    return [s for s in (storage_metrics or []) if s.has_usage_stats]


def rightsizing_savings_usd(
    current_request: float,
    recommended_request: float,
    price_per_unit_hour: float,
) -> float:
    """
    Monthly saving of changing a request from `current` to `recommended`.

    WHY this formula (V1 fix for double counting):
    - The MVP computed each saving as `total_waste × (1 − ratio)` using the
      TOTAL namespace waste for both CPU and memory, so CPU + memory savings
      could exceed the total waste.
    - The saving of an action is exactly what stops being reserved:
      (current − recommended) × price × 720 h. Because recommended ≥ usage
      (we add headroom), it is always ≤ that resource's own waste component,
      hence Σ savings ≤ Σ components = total waste.
    """
    return round(max(0.0, current_request - recommended_request) * price_per_unit_hour * HOURS_PER_MONTH, 4)


def calculate_monthly_cost(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
    storage_metrics: list[StorageMetric] | None = None,
) -> CostBreakdown:
    """
    Calculate monthly cost for a namespace based on ACTUAL usage.

    Formula:
        cpu_cost    = cpu_core_hours × cost_per_core_hour × 24 × 30
        memory_cost = (memory_bytes / BYTES_PER_GB) × cost_per_gb_hour × 24 × 30
        storage_cost = sum(used_bytes / BYTES_PER_GB) × cost_per_gb_month per PVC

    WHY "actual" (not "requested"):
    - This is what the workload is actually consuming.
    - Compare with calculate_requested_cost() to find waste.
    - Useful for chargeback: "team X consumed $Y of actual resources."
    """
    cpu_cost = pod.cpu_core_hours * cost_per_core_hour * HOURS_PER_MONTH

    memory_gb = pod.memory_bytes / BYTES_PER_GB
    memory_cost = memory_gb * cost_per_gb_hour * HOURS_PER_MONTH

    priced = _priced_pvcs(storage_metrics)
    storage_cost = sum(s.used_bytes for s in priced) / BYTES_PER_GB * cost_per_gb_month

    total = cpu_cost + memory_cost + storage_cost

    return CostBreakdown(
        cpu_cost_usd=round(cpu_cost, 4),
        memory_cost_usd=round(memory_cost, 4),
        storage_cost_usd=round(storage_cost, 4),
        total_cost_usd=round(total, 4),
        namespace=pod.namespace,
        cost_per_core_hour=cost_per_core_hour,
        cost_per_gb_hour=cost_per_gb_hour,
        cost_per_gb_month=cost_per_gb_month,
        timestamp=pod.timestamp,
        storage_status=storage_status(storage_metrics),
    )


def calculate_requested_cost(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
    storage_metrics: list[StorageMetric] | None = None,
) -> CostBreakdown:
    """
    Calculate monthly cost at REQUESTED levels (what the cluster charges you for).

    Formula:
        cpu_cost    = cpu_requested × cost_per_core_hour × 24 × 30
        memory_cost = (memory_requested / BYTES_PER_GB) × cost_per_gb_hour × 24 × 30

    WHY "requested":
    - Kubernetes charges for requested resources (they're reserved, can't be used by others).
    - This is your "committed cost" — the bill you pay regardless of actual usage.
    - The gap between requested and actual is your waste.
    """
    cpu_cost = pod.cpu_requested * cost_per_core_hour * HOURS_PER_MONTH

    memory_gb = pod.memory_requested / BYTES_PER_GB
    memory_cost = memory_gb * cost_per_gb_hour * HOURS_PER_MONTH

    priced = _priced_pvcs(storage_metrics)
    storage_cost = sum(s.capacity_bytes for s in priced) / BYTES_PER_GB * cost_per_gb_month

    total = cpu_cost + memory_cost + storage_cost

    return CostBreakdown(
        cpu_cost_usd=round(cpu_cost, 4),
        memory_cost_usd=round(memory_cost, 4),
        storage_cost_usd=round(storage_cost, 4),
        total_cost_usd=round(total, 4),
        namespace=pod.namespace,
        cost_per_core_hour=cost_per_core_hour,
        cost_per_gb_hour=cost_per_gb_hour,
        cost_per_gb_month=cost_per_gb_month,
        timestamp=pod.timestamp,
        storage_status=storage_status(storage_metrics),
    )


def calculate_overprovisioning_ratio(
    pod: PodMetric,
) -> OverprovisioningRatio:
    """
    Calculate the ratio of actual usage to requested, per resource type.

    Formula:
        cpu_ratio    = cpu_core_hours / cpu_requested
        memory_ratio = memory_bytes / memory_requested

    Interpretation:
        ratio = 0.3 → workload uses 30% of what it asked for (70% wasted)
        ratio = 1.0 → workload uses exactly what it asked for (ideal)
        ratio = 1.2 → workload exceeds request (throttling risk)
    """
    cpu_ratio = (pod.cpu_core_hours / pod.cpu_requested) if pod.cpu_requested > 0 else 0.0
    memory_ratio = (pod.memory_bytes / pod.memory_requested) if pod.memory_requested > 0 else 0.0

    return OverprovisioningRatio(
        cpu_ratio=round(cpu_ratio, 4),
        memory_ratio=round(memory_ratio, 4),
        namespace=pod.namespace,
        timestamp=pod.timestamp,
    )


def calculate_namespace_waste(
    pod: PodMetric,
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
    storage_metrics: list[StorageMetric] | None = None,
) -> NamespaceCostSummary:
    """
    Calculate total waste for a namespace: the gap between requested and actual cost.

    Formula (V1 — per-component, gross waste):
        cpu_waste     = max(0, requested_cpu_cost     − actual_cpu_cost)
        memory_waste  = max(0, requested_memory_cost  − actual_memory_cost)
        storage_waste = max(0, capacity_storage_cost  − used_storage_cost)
        wasted        = cpu_waste + memory_waste + storage_waste
        waste_ratio   = wasted / requested_cost (0.0 = no waste, 1.0 = 100% waste)

    WHY gross (sum of positive gaps) instead of requested − actual:
    - Unused reserved CPU is paid for even if memory is over-used; an
      under-provisioned resource must not "cancel" waste on another one.
    - It makes the waste decomposable, which bounds recommendation savings
      (Σ savings ≤ wasted). When no resource is under-provisioned both
      definitions give the same number.
    """
    requested = calculate_requested_cost(
        pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month, storage_metrics
    )
    actual = calculate_monthly_cost(
        pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month, storage_metrics
    )
    return summarize_waste(pod, requested, actual)


def summarize_waste(
    pod: PodMetric,
    requested: CostBreakdown,
    actual: CostBreakdown,
) -> NamespaceCostSummary:
    """Build the per-component waste summary from two cost breakdowns."""
    cpu_waste = max(0.0, requested.cpu_cost_usd - actual.cpu_cost_usd)
    memory_waste = max(0.0, requested.memory_cost_usd - actual.memory_cost_usd)
    storage_waste = max(0.0, requested.storage_cost_usd - actual.storage_cost_usd)
    wasted = cpu_waste + memory_waste + storage_waste
    waste_ratio = (wasted / requested.total_cost_usd) if requested.total_cost_usd > 0 else 0.0

    return NamespaceCostSummary(
        namespace=pod.namespace,
        requested_cost_usd=requested.total_cost_usd,
        actual_cost_usd=actual.total_cost_usd,
        wasted_cost_usd=round(wasted, 4),
        waste_ratio=round(waste_ratio, 4),
        timestamp=pod.timestamp,
        cpu_waste_usd=round(cpu_waste, 4),
        memory_waste_usd=round(memory_waste, 4),
        storage_waste_usd=round(storage_waste, 4),
        storage_status=requested.storage_status,
    )


def generate_recommendations(
    pods: list[PodMetric],
    cost_per_core_hour: float,
    cost_per_gb_hour: float,
    cost_per_gb_month: float,
    storage_metrics: list[StorageMetric] | None = None,
) -> RecommendationSet:
    """
    Generate FinOps recommendations for a list of pods/namespaces.

    Logic:
        1. For each pod, compute requested cost, actual cost, and waste.
        2. If waste > 50% of requested → recommend right-sizing.
        3. If CPU ratio > 1.0 → warn about throttling.
        4. Severity is proportional to waste amount.

    Returns:
        RecommendationSet with all generated recommendations.
    """
    recommendations: list[Recommendation] = []
    now = datetime.now()

    for pod in pods:
        ratio = calculate_overprovisioning_ratio(pod)
        waste = calculate_namespace_waste(
            pod, cost_per_core_hour, cost_per_gb_hour, cost_per_gb_month, storage_metrics
        )

        # Over-provisioned CPU
        if ratio.is_overprovisioned_cpu:
            recommendations.append(_cpu_rightsize_recommendation(pod, ratio, waste, cost_per_core_hour))

        # Over-provisioned memory
        if ratio.is_overprovisioned_memory:
            recommendations.append(_memory_rightsize_recommendation(pod, ratio, waste, cost_per_gb_hour))

        # Under-provisioned CPU (throttling risk)
        if ratio.is_underprovisioned_cpu:
            recommendations.append(_cpu_throttle_recommendation(pod, ratio))

        # Under-provisioned memory (OOM risk)
        if ratio.is_underprovisioned_memory:
            recommendations.append(_memory_throttle_recommendation(pod, ratio))

    return RecommendationSet(
        namespace=pods[0].namespace if pods else "unknown",
        generated_at=now,
        recommendations=tuple(recommendations),
    )


# ---------------------------------------------------------------------------
# Recommendation builders (private helpers)
# ---------------------------------------------------------------------------

def _cpu_rightsize_recommendation(
    pod: PodMetric,
    ratio: OverprovisioningRatio,
    waste: NamespaceCostSummary,
    cost_per_core_hour: float,
) -> Recommendation:
    """
    Recommendation: reduce CPU request to match actual usage.

    Savings = wasted CPU cost (what you're paying for but not using).
    """
    recommended_cores = round(pod.cpu_core_hours * 1.2, 2)  # 20% headroom
    return Recommendation(
        category=Category.COST_RIGHTSIZING,
        severity=Severity.WARNING if waste.waste_ratio < 0.7 else Severity.CRITICAL,
        namespace=pod.namespace,
        resource="cpu",
        message=(
            f"CPU is {ratio.cpu_ratio:.0%} utilized. "
            f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.1f}. "
            f"Recommend reducing to {recommended_cores} cores."
        ),
        current_value=f"{pod.cpu_requested}",
        recommended_value=f"{recommended_cores}",
        estimated_savings_usd=rightsizing_savings_usd(
            pod.cpu_requested, recommended_cores, cost_per_core_hour
        ),
        timestamp=pod.timestamp,
    )


def _memory_rightsize_recommendation(
    pod: PodMetric,
    ratio: OverprovisioningRatio,
    waste: NamespaceCostSummary,
    cost_per_gb_hour: float,
) -> Recommendation:
    """
    Recommendation: reduce memory request to match actual usage.
    """
    recommended_bytes = round(pod.memory_bytes * 1.2)  # 20% headroom
    return Recommendation(
        category=Category.COST_RIGHTSIZING,
        severity=Severity.WARNING if waste.waste_ratio < 0.7 else Severity.CRITICAL,
        namespace=pod.namespace,
        resource="memory",
        message=(
            f"Memory is {ratio.memory_ratio:.0%} utilized. "
            f"Requesting {pod.memory_requested / BYTES_PER_GB:.1f} GB "
            f"but using {pod.memory_bytes / BYTES_PER_GB:.1f} GB. "
            f"Recommend reducing to {recommended_bytes / BYTES_PER_GB:.1f} GB."
        ),
        current_value=f"{pod.memory_requested / BYTES_PER_GB:.1f}",
        recommended_value=f"{recommended_bytes / BYTES_PER_GB:.1f}",
        estimated_savings_usd=rightsizing_savings_usd(
            pod.memory_requested / BYTES_PER_GB, recommended_bytes / BYTES_PER_GB, cost_per_gb_hour
        ),
        timestamp=pod.timestamp,
    )


def _cpu_throttle_recommendation(
    pod: PodMetric,
    ratio: OverprovisioningRatio,
) -> Recommendation:
    """
    Recommendation: increase CPU request to prevent throttling.
    """
    recommended_cores = round(pod.cpu_core_hours * 1.25, 2)  # 25% headroom
    return Recommendation(
        category=Category.COST_RIGHTSIZING,
        severity=Severity.CRITICAL,
        namespace=pod.namespace,
        resource="cpu",
        message=(
            f"CPU is at {ratio.cpu_ratio:.0%} of request — throttling likely. "
            f"Requesting {pod.cpu_requested} cores but using {pod.cpu_core_hours:.1f}. "
            f"Recommend increasing to {recommended_cores} cores."
        ),
        current_value=f"{pod.cpu_requested}",
        recommended_value=f"{recommended_cores}",
        estimated_savings_kg_co2=None,
        estimated_savings_usd=None,
        timestamp=pod.timestamp,
    )


def _memory_throttle_recommendation(
    pod: PodMetric,
    ratio: OverprovisioningRatio,
) -> Recommendation:
    """
    Recommendation: increase memory request to prevent OOM kills.
    """
    recommended_bytes = round(pod.memory_bytes * 1.25)
    return Recommendation(
        category=Category.COST_RIGHTSIZING,
        severity=Severity.CRITICAL,
        namespace=pod.namespace,
        resource="memory",
        message=(
            f"Memory is at {ratio.memory_ratio:.0%} of request — OOM risk. "
            f"Requesting {pod.memory_requested / BYTES_PER_GB:.1f} GB "
            f"but using {pod.memory_bytes / BYTES_PER_GB:.1f} GB. "
            f"Recommend increasing to {recommended_bytes / BYTES_PER_GB:.1f} GB."
        ),
        current_value=f"{pod.memory_requested / BYTES_PER_GB:.1f}",
        recommended_value=f"{recommended_bytes / BYTES_PER_GB:.1f}",
        estimated_savings_kg_co2=None,
        estimated_savings_usd=None,
        timestamp=pod.timestamp,
    )