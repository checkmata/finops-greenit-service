"""
Fake Prometheus exporter for local dev — exposes P0 metrics from docs/metrics-reference.md.

WHY a fake exporter (not injecting data into Prometheus's storage):
- Prometheus's remote-write API is for ingestion, not mocking.
- Pushing fake data tests Prometheus's storage layer, not our PromQL→domain→API pipeline.
- A fake exporter means Prometheus receives metrics exactly as real cAdvisor/Kepler would.
- Our adapter queries it identically to production — same PromQL, same JSON response.
- Edge cases (empty namespaces, degraded sources) are tested by changing the exporter.

WHAT this exposes:
- container_cpu_usage_seconds_total — FinOps P0
- container_memory_working_set_bytes — FinOps P0
- kube_pod_container_resource_requests — FinOps P0
- kube_node_status_allocatable — FinOps P0
- kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes — FinOps P0
- kube_persistentvolumeclaim_info — FinOps P0
- kepler_node_cpu_joules_total — Green IT P0
- kepler_container_cpu_joules_total — Green IT P0
- up{job="kube-state-metrics|node-exporter|kepler"} — data quality guard

Simulated namespaces: team-alpha, team-beta.
"""

from prometheus_client import Counter, Gauge, start_http_server

# =============================================================================
# FinOps P0 metrics
# =============================================================================

# container_cpu_usage_seconds_total — rate over 30d → cpu_core_hours
# Simulates cAdvisor per-container CPU usage (monotonically increasing counter).
CONTAINER_CPU_USAGE = Counter(
    "container_cpu_usage_seconds_total",
    "Cumulative CPU usage in seconds",
    ["namespace", "pod", "container"],
)

# container_memory_working_set_bytes — instant → memory_bytes
CONTAINER_MEMORY_WORKING_SET = Gauge(
    "container_memory_working_set_bytes",
    "Current working set memory in bytes",
    ["namespace", "pod", "container"],
)

# kube_pod_container_resource_requests — CPU and memory requests
KUBE_POD_CONTAINER_RESOURCE_REQUESTS_CPU = Gauge(
    "kube_pod_container_resource_requests",
    "CPU resource requests in cores",
    ["namespace", "pod", "container", "resource"],
)

KUBE_POD_CONTAINER_RESOURCE_REQUESTS_MEMORY = Gauge(
    "kube_pod_container_resource_requests",
    "Memory resource requests in bytes",
    ["namespace", "pod", "container", "resource"],
)

# kube_node_status_allocatable — cluster-wide allocatable CPU and memory
KUBE_NODE_STATUS_ALLOCATABLE = Gauge(
    "kube_node_status_allocatable",
    "Allocatable resources on a node",
    ["node", "resource"],
)

# kubelet_volume_stats — PVC storage metrics
KUBELET_VOLUME_STATS_USED = Gauge(
    "kubelet_volume_stats_used_bytes",
    "Used bytes on a PVC",
    ["namespace", "persistentvolumeclaim"],
)

KUBELET_VOLUME_STATS_CAPACITY = Gauge(
    "kubelet_volume_stats_capacity_bytes",
    "Capacity bytes on a PVC",
    ["namespace", "persistentvolumeclaim"],
)

# kube_persistentvolumeclaim_info — PVC phase
KUBE_PVCLAIM_INFO = Gauge(
    "kube_persistentvolumeclaim_info",
    "PVC metadata",
    ["namespace", "persistentvolumeclaim", "phase"],
)

# =============================================================================
# Green IT P0 metrics
# =============================================================================

# kepler_node_cpu_joules_total — node-level energy (monotonically increasing counter)
KEPLER_NODE_CPU_JOULES = Counter(
    "kepler_node_cpu_joules_total",
    "Cumulative CPU energy consumption in joules",
    ["instance", "mode"],
)

# kepler_container_cpu_joules_total — per-container energy
KEPLER_CONTAINER_CPU_JOULES = Counter(
    "kepler_container_cpu_joules_total",
    "Cumulative container CPU energy consumption in joules",
    ["namespace", "pod", "container"],
)

# =============================================================================
# Data quality guard
# =============================================================================

# up{job="..."} — 1 = healthy, 0 = down
UP = Gauge(
    "up",
    "Target liveness (1=alive, 0=down)",
    ["job"],
)

# =============================================================================
# Pod attribution labels
# =============================================================================

KUBE_POD_LABELS = Gauge(
    "kube_pod_labels",
    "Kubernetes labels for a pod",
    ["namespace", "pod", "label_team", "label_cost_center", "label_environment"],
)


def populate_metrics() -> None:
    """Set initial metric values for team-alpha and team-beta.

    WHY specific values:
    - team-alpha: over-provisioned (CPU usage ~30% of requested) → triggers COST_RIGHTSIZING
    - team-beta: well-utilized (CPU usage ~70% of requested) → no recommendation
    - Both have Kepler energy data → triggers Green IT carbon footprint
    """

    # --- team-alpha: over-provisioned namespace ---
    # 2 pods, each with 2 containers (web + sidecar)
    for pod_idx in range(2):
        pod_name = f"alpha-web-{pod_idx}"
        for container in ["web", "sidecar"]:
            # CPU: 1200 seconds / 30 days = 0.00139 core-hours (low usage)
            CONTAINER_CPU_USAGE.labels(
                namespace="team-alpha", pod=pod_name, container=container
            ).inc(1200)

            # Memory: 256 MiB working set
            CONTAINER_MEMORY_WORKING_SET.labels(
                namespace="team-alpha", pod=pod_name, container=container
            ).set(256 * 1024 * 1024)

            # Requested: 2.0 cores CPU, 1 GiB memory (over-provisioned)
            KUBE_POD_CONTAINER_RESOURCE_REQUESTS_CPU.labels(
                namespace="team-alpha", pod=pod_name, container=container, resource="cpu"
            ).set(2.0)
            KUBE_POD_CONTAINER_RESOURCE_REQUESTS_MEMORY.labels(
                namespace="team-alpha", pod=pod_name, container=container, resource="memory"
            ).set(1024 * 1024 * 1024)

            # Kepler per-container energy: ~500 joules over 24h
            KEPLER_CONTAINER_CPU_JOULES.labels(
                namespace="team-alpha", pod=pod_name, container=container
            ).inc(500)

        # Pod attribution labels
        KUBE_POD_LABELS.labels(
            namespace="team-alpha",
            pod=pod_name,
            label_team="team-alpha",
            label_cost_center="cc-001",
            label_environment="production",
        ).set(1)

    # --- team-beta: well-utilized namespace ---
    for pod_idx in range(3):
        pod_name = f"beta-api-{pod_idx}"
        for container in ["api", "cache"]:
            # CPU: 5000 seconds / 30 days = 0.00694 core-hours (higher usage)
            CONTAINER_CPU_USAGE.labels(
                namespace="team-beta", pod=pod_name, container=container
            ).inc(5000)

            # Memory: 512 MiB working set
            CONTAINER_MEMORY_WORKING_SET.labels(
                namespace="team-beta", pod=pod_name, container=container
            ).set(512 * 1024 * 1024)

            # Requested: 1.0 cores CPU, 768 MiB memory (well-utilized)
            KUBE_POD_CONTAINER_RESOURCE_REQUESTS_CPU.labels(
                namespace="team-beta", pod=pod_name, container=container, resource="cpu"
            ).set(1.0)
            KUBE_POD_CONTAINER_RESOURCE_REQUESTS_MEMORY.labels(
                namespace="team-beta", pod=pod_name, container=container, resource="memory"
            ).set(768 * 1024 * 1024)

            # Kepler per-container energy: ~1200 joules over 24h
            KEPLER_CONTAINER_CPU_JOULES.labels(
                namespace="team-beta", pod=pod_name, container=container
            ).inc(1200)

        # Pod attribution labels
        KUBE_POD_LABELS.labels(
            namespace="team-beta",
            pod=pod_name,
            label_team="team-beta",
            label_cost_center="cc-002",
            label_environment="production",
        ).set(1)

    # --- Cluster-wide allocatable ---
    KUBE_NODE_STATUS_ALLOCATABLE.labels(node="node-1", resource="cpu").set(16.0)
    KUBE_NODE_STATUS_ALLOCATABLE.labels(node="node-1", resource="memory").set(64 * 1024**3)
    KUBE_NODE_STATUS_ALLOCATABLE.labels(node="node-2", resource="cpu").set(16.0)
    KUBE_NODE_STATUS_ALLOCATABLE.labels(node="node-2", resource="memory").set(64 * 1024**3)

    # --- PVC metrics (team-alpha has a large unused PVC) ---
    KUBELET_VOLUME_STATS_USED.labels(
        namespace="team-alpha", persistentvolumeclaim="data-alpha-0"
    ).set(2 * 1024**3)  # 2 GiB used
    KUBELET_VOLUME_STATS_CAPACITY.labels(
        namespace="team-alpha", persistentvolumeclaim="data-alpha-0"
    ).set(50 * 1024**3)  # 50 GiB capacity (4% used → mostly wasted)
    KUBE_PVCLAIM_INFO.labels(
        namespace="team-alpha", persistentvolumeclaim="data-alpha-0", phase="Bound"
    ).set(1)

    KUBELET_VOLUME_STATS_USED.labels(
        namespace="team-beta", persistentvolumeclaim="data-beta-0"
    ).set(30 * 1024**3)  # 30 GiB used
    KUBELET_VOLUME_STATS_CAPACITY.labels(
        namespace="team-beta", persistentvolumeclaim="data-beta-0"
    ).set(50 * 1024**3)  # 50 GiB capacity (60% used → reasonable)
    KUBE_PVCLAIM_INFO.labels(
        namespace="team-beta", persistentvolumeclaim="data-beta-0", phase="Bound"
    ).set(1)

    # --- Kepler node-level energy ---
    KEPLER_NODE_CPU_JOULES.labels(instance="node-1", mode="dynamic").inc(15000)
    KEPLER_NODE_CPU_JOULES.labels(instance="node-2", mode="dynamic").inc(18000)

    # --- Data quality: all sources healthy ---
    UP.labels(job="kube-state-metrics").set(1)
    UP.labels(job="node-exporter").set(1)
    UP.labels(job="kepler").set(1)


if __name__ == "__main__":
    print("Starting fake metrics exporter on :8000...")
    populate_metrics()
    print("Metrics populated. Serving...")
    start_http_server(8000)
    # Keep running
    import time

    while True:
        time.sleep(60)
