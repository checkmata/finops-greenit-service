"""
Domain model for FinOps pod-level resource metrics.

Why plain dataclass (not Pydantic):
- Domain must be free of framework dependency. Pydantic lives in the adapter
  layer (inbound DTOs). These dataclasses are what the business logic consumes.
- Simpler, faster, zero import overhead for domain functions.
- Proves architectural boundary: domain never knows about validation frameworks.

Why frozen=True:
- These objects represent a point-in-time snapshot from Prometheus.
- Immutability prevents accidental mutation by recommendation_engine or adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Workload name used when metrics are aggregated at namespace level
# (no per-workload breakdown). Kept as a constant so adapters, engines and
# storage agree on it.
NAMESPACE_AGGREGATE = "*"
# Workload name for usage that could not be attributed to a labelled workload
# (pods without the workload label, or pods that no longer exist).
UNATTRIBUTED_WORKLOAD = "_unattributed"


@dataclass(frozen=True)
class PodMetric:
    """
    Per-namespace aggregated resource metrics.

    Field mapping (from docs/metrics-reference.md):
    - cpu_core_hours  ← container_cpu_usage_seconds_total (rate[30d], converted to hours)
    - memory_bytes    ← container_memory_working_set_bytes (instant)
    - cpu_requested   ← kube_pod_container_resource_requests{resource="cpu"}
    - memory_requested← kube_pod_container_resource_requests{resource="memory"}

    Attribution labels (from kube_pod_labels via kube-state-metrics):
    - team, cost_center, environment — used for chargeback, not for calculation.
    """

    # --- identity (attribution) ---
    namespace: str
    team: str
    cost_center: str
    environment: str

    # --- actual usage (FinOps P0) ---
    cpu_core_hours: float
    memory_bytes: float

    # --- requested resources (FinOps P0) ---
    cpu_requested: float
    memory_requested: float

    # --- metadata ---
    timestamp: datetime

    # --- scope (V1 multicluster / per-workload) ---
    # Defaults keep every MVP call site valid: a PodMetric without these
    # fields is a namespace aggregate on the default cluster.
    cluster: str = "default"
    workload: str = NAMESPACE_AGGREGATE

    # Hours of CPU-usage history behind cpu_core_hours (V1). None = unknown
    # (MVP call sites / sources that cannot tell). A young workload must not be
    # right-sized on a few minutes of data.
    observation_hours: float | None = None

    # V3: POINT-IN-TIME CPU (cores, 5-min rate) next to the long-window average
    # above. The average is right for V1 right-sizing but smooths away every
    # spike — anomaly detection and forecasting need the instantaneous value.
    cpu_cores_now: float | None = None

    @property
    def cpu_utilization_pct(self) -> float:
        """
        CPU utilization as percentage of requested.

        WHY: If cpu_core_hours / cpu_requested > 1.0, the workload exceeds its
        request (throttling risk). If < 0.5, likely over-provisioned.
        """
        if self.cpu_requested == 0:
            return 0.0
        return (self.cpu_core_hours / self.cpu_requested) * 100

    @property
    def memory_utilization_pct(self) -> float:
        """
        Memory utilization as percentage of requested.

        WHY: Same logic as CPU — high % = throttling risk, low % = waste.
        """
        if self.memory_requested == 0:
            return 0.0
        return (self.memory_bytes / self.memory_requested) * 100

    @property
    def is_overprovisioned_cpu(self) -> bool:
        """
        True if actual CPU usage is under 50% of requested.

        WHY: Over-provisioning wastes cluster capacity and inflates cost.
        Threshold of 50% is industry standard for FinOps right-sizing.
        """
        return self.cpu_utilization_pct < 50.0

    @property
    def is_overprovisioned_memory(self) -> bool:
        """
        True if actual memory usage is under 50% of requested.

        WHY: Same reasoning as CPU over-provisioning.
        """
        return self.memory_utilization_pct < 50.0


@dataclass(frozen=True)
class ClusterAllocatable:
    """
    Cluster-level allocatable resources (CPU and memory).

    Field mapping:
    - cpu_allocatable    ← kube_node_status_allocatable{resource="cpu"}
    - memory_allocatable ← kube_node_status_allocatable{resource="memory"}

    WHY: Used as denominator for cluster headroom calculation.
    Not stored on PodMetric because it's cluster-wide, not per-pod.
    """

    cpu_allocatable: float
    memory_allocatable: float
    timestamp: datetime


@dataclass(frozen=True)
class StorageMetric:
    """
    Per-PVC storage usage.

    Field mapping:
    - used_bytes  ← kubelet_volume_stats_used_bytes{namespace="$ns"}
    - capacity_bytes ← kubelet_volume_stats_capacity_bytes{namespace="$ns"}
    - is_bound    ← kube_persistentvolumeclaim_info{phase="Bound"}

    WHY: Storage is part of FinOps cost allocation and orphaned-PVC detection.
    """

    namespace: str
    pvc_name: str
    used_bytes: float
    capacity_bytes: float
    is_bound: bool
    timestamp: datetime
    # False when kube-state-metrics knows the PVC but kubelet volume stats are
    # missing for it. used/capacity are then 0.0 placeholders and MUST NOT be
    # priced (V1: no invented storage cost).
    has_usage_stats: bool = True

    @property
    def usage_pct(self) -> float:
        """Percentage of PVC capacity used."""
        if self.capacity_bytes == 0:
            return 0.0
        return (self.used_bytes / self.capacity_bytes) * 100