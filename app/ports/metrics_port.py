"""
Abstract port for outbound telemetry (Prometheus / Kepler).

What a "port" is in Hexagonal Architecture:
- A port is a CONTRACT — it declares what the domain needs from the outside world.
- It defines method signatures (what to call) but NOT implementation (how it works).
- The domain depends on this abstraction, never on the concrete adapter.

Why the domain depends on this, not on prometheus_adapter.py:
- If domain imported PrometheusAdapter directly, it would know about HTTP, JSON, PromQL.
- Swapping Prometheus for Datadog would require rewriting domain code.
- With a port, the domain just calls `port.get_pod_metrics(namespace)`.
  The adapter implements that method. The domain doesn't care HOW.

How this enables swappability:
- To replace Prometheus: write a new adapter (e.g., DatadogAdapter) that implements MetricsPort.
- Zero changes to domain code or engine.
- Tests can use a MockMetricsPort that returns fake data — no real Prometheus needed.

IMPORTANT: Only P0 methods are defined here. P1 methods (throttling, OOMKilled,
idle waste) will be added when explicitly requested per metrics-reference.md scope.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models.data_quality import DataQuality
from app.domain.models.pod_metric import (
    ClusterAllocatable,
    PodMetric,
    StorageMetric,
)


class MetricsPort(ABC):
    """
    Abstract contract for fetching telemetry data.

    The domain engine depends on this interface. The adapter (PrometheusAdapter)
    implements it. The engine never knows which implementation is behind it.

    Method design rationale:
    - Each method returns a domain model object (PodMetric, CarbonFootprint, etc.)
    - The adapter handles: PromQL queries, JSON parsing, unit conversion
    - The engine receives: typed Python objects ready for business logic
    """

    @abstractmethod
    async def get_pod_metrics(self, namespace: str) -> list[PodMetric]:
        """
        Fetch per-namespace aggregated resource metrics.

        Metrics fetched (from metrics-reference.md P0):
        - container_cpu_usage_seconds_total → cpu_core_hours (rate[30d], converted to hours)
        - container_memory_working_set_bytes → memory_bytes (instant)
        - kube_pod_container_resource_requests → cpu_requested, memory_requested (instant)
        - kube_pod_labels → team, cost_center, environment (attribution labels)

        Args:
            namespace: Kubernetes namespace to query (e.g., "team-alpha")

        Returns:
            List of PodMetric objects, one per namespace (typically one entry).
        """

    @abstractmethod
    async def get_cluster_allocatable(self) -> ClusterAllocatable:
        """
        Fetch cluster-level allocatable resources.

        Metric (from metrics-reference.md P0):
        - kube_node_status_allocatable{resource="cpu"} → cpu_allocatable
        - kube_node_status_allocatable{resource="memory"} → memory_allocatable

        WHY: Used as denominator for cluster headroom calculation.
        Not per-namespace — this is the total cluster capacity.

        Returns:
            ClusterAllocatable with total CPU (cores) and memory (bytes).
        """

    @abstractmethod
    async def get_storage_metrics(self, namespace: str) -> list[StorageMetric]:
        """
        Fetch per-PVC storage usage for a namespace.

        Metrics (from metrics-reference.md P0):
        - kubelet_volume_stats_used_bytes → used_bytes
        - kubelet_volume_stats_capacity_bytes → capacity_bytes
        - kube_persistentvolumeclaim_info{phase="Bound"} → is_bound

        Args:
            namespace: Kubernetes namespace to query

        Returns:
            List of StorageMetric objects, one per PVC in the namespace.
        """

    @abstractmethod
    async def check_data_quality(self) -> DataQuality:
        """
        Check which telemetry sources are healthy.

        Metric (from metrics-reference.md P0):
        - up{job=~"kube-state-metrics|node-exporter|kepler"} → per-source health

        WHY per-source instead of one status:
        - If Kepler is down but kube-state-metrics is up, the engine can still
          produce FinOps (cost) recommendations while flagging Green-IT (carbon)
          data as unavailable. Selective rendering instead of all-or-nothing.

        Returns:
            DataQuality snapshot with the set of healthy_sources.
            Sources not in the set are DOWN (flag their data as missing).
        """

    @abstractmethod
    async def get_energy_kwh(self, namespace: str) -> float:
        """
        Fetch energy consumption for a namespace from Kepler (Green IT P0).

        Metric (from metrics-reference.md Green IT P0):
        - kepler_node_cpu_joules_total (or kepler_container_cpu_joules_total)
        - increase(...[24h]) → converted from joules to kWh

        Conversion: joules / 3,600,000 = kWh

        WHY energy_kwh only (not co2_kg):
        - CO2 calculation requires PUE and grid_intensity (static constants).
        - Those are applied by the GreenIT engine, not the adapter.
        - This keeps the port generic — Kepler doesn't know about carbon models.

        Args:
            namespace: Kubernetes namespace to query

        Returns:
            Energy consumption in kilowatt-hours (kWh) for the last 24 hours.
        """