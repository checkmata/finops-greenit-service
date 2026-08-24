# Metrics Reference — Scoped for This Build

Only the metrics below are in scope. Do not query or reference any metric not listed here — anything else was deliberately deferred.

All metrics are queried through Prometheus (PromQL), via `app/adapters/outbound/prometheus_adapter.py`. Kepler metrics use the same client, no separate integration.

## FinOps — P0 (build first)

| Metric | PromQL pattern | Maps to domain field |
|---|---|---|
| `container_cpu_usage_seconds_total` | `sum(rate(container_cpu_usage_seconds_total{namespace="$ns"}[30d]))` | `PodMetric.cpu_core_hours` |
| `container_memory_working_set_bytes` | `sum(container_memory_working_set_bytes{namespace="$ns"})` | `PodMetric.memory_bytes` |
| `kube_pod_container_resource_requests` | `sum(kube_pod_container_resource_requests{namespace="$ns", resource="cpu"})` / same for `memory` | `PodMetric.cpu_requested`, `PodMetric.memory_requested` |
| `kube_node_status_allocatable` | `kube_node_status_allocatable{resource="cpu"}` / `resource="memory"` | denominator for cluster headroom, not stored on PodMetric |
| `up{job=~"kube-state-metrics\|node-exporter\|kepler"}` | `up == 0` | data-quality guard, raises adapter warning, not a domain field |
| `kubelet_volume_stats_used_bytes` / `kubelet_volume_stats_capacity_bytes` | `used/capacity per PVC` | storage cost allocation |
| `kube_persistentvolumeclaim_info` | `phase="Bound"` + unmounted duration | orphaned-PVC recommendation input |

## FinOps — P1 (after P0 works)

| Metric | PromQL pattern | Maps to domain field |
|---|---|---|
| `kube_pod_container_resource_limits` | cpu/memory limits per pod | throttling/oversizing input |
| `container_cpu_cfs_throttled_seconds_total` | `rate(...[30d])` | throttling-detection recommendation |
| `node_cpu_seconds_total` | `1 - rate(...{mode="idle"}[30d])` | node utilization |
| `node_memory_MemAvailable_bytes` / `node_memory_MemTotal_bytes` | `1 - (avail/total)` | node memory utilization |
| `kube_pod_container_status_terminated_reason{reason="OOMKilled"}` | count over 30d | memory under-provisioning flag |
| `kube_deployment_spec_replicas` / `kube_deployment_status_replicas_available` | spec vs available | over-replication / zombie deployment flag |

## Green IT — P0

| Metric | PromQL pattern | Maps to domain field |
|---|---|---|
| `kepler_node_cpu_joules_total` (fallback: `rate(kepler_node_package_joules_total[5m])` — confirm exact name against your cluster's Kepler version) | `increase(...[24h])` → convert to kWh | `CarbonFootprint.energy_kwh` |
| `kepler_container_cpu_joules_total` (if exposed by your Kepler version) | per-container joules | `CarbonFootprint.energy_kwh` (per-namespace granularity) |
| PUE | **not scraped** — static constant `config.PUE_CONSTANT` (e.g. 1.5–1.6, cite source in code comment) | multiplier in `greenit_engine.py` |
| Grid Carbon Intensity | **not scraped** — static constant `config.GRID_CARBON_INTENSITY_GCO2_PER_KWH` | multiplier in `greenit_engine.py` |

## Green IT — P1

| Metric | PromQL pattern | Maps to domain field |
|---|---|---|
| Idle energy waste | `node_cpu_seconds_total{idle} × Kepler power` | `CarbonFootprint.idle_waste_kwh` |
| Carbon overhead of over-provisioning | `(requests − actual usage) × carbon model` | reuses FinOps oversizing calc, different multiplier |

## Metadata & attribution labels (attach to every query above via PromQL label matchers)

| Label | Source | Purpose |
|---|---|---|
| `namespace` | implicit on all metrics above | primary grouping key |
| `pod`, `container` | implicit on `container_*` metrics | drill-down |
| `label_team` (from `kube_pod_labels`) | kube-state-metrics | chargeback attribution |
| `label_cost_center` (from `kube_pod_labels`) | kube-state-metrics | finance grouping |
| `label_environment` (from `kube_pod_labels`) | kube-state-metrics | prod vs dev/staging split |

**Explicitly out of scope for this build:** anything under Category 4 (Forecasting), GPU metrics, Istio metrics, license/hardware-refresh correlation, live grid-intensity API, node hardware-generation labels. See the project's Deferred - Phase 2 documentation if these come up.
