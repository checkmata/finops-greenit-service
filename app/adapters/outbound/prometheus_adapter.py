"""
Prometheus adapter — concrete implementation of MetricsPort.

WHY this file is ALLOWED to know about HTTP and JSON (while the domain is not):
- Hexagonal Architecture draws a wall between the DOMAIN (pure business logic)
  and the OUTSIDE WORLD (HTTP, databases, exporters).
- The adapter is the outbound "side" of that wall. It is the ONLY code allowed
  to speak PromQL, parse JSON, and handle transport errors.
- The domain engine calls MetricsPort (the interface). It never sees this file.
  If Prometheus is replaced (e.g., Datadog), only this file changes.

WHY Kepler queries go through THIS SAME adapter (not a separate one):
- Kepler is scraped by the same Prometheus instance (via ServiceMonitor).
- One client, one base URL, one set of errors, one code path = less duplication.
- AGENTS.md: "All telemetry ... goes through the single prometheus_adapter.py".
  A separate Kepler adapter would duplicate HTTP/JSON/error handling for nothing.

Every PromQL string below is copied verbatim from docs/metrics-reference.md
(P0 sections only). No metric names are invented here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.config import settings
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.pod_metric import (
    ClusterAllocatable,
    PodMetric,
    StorageMetric,
)
from app.ports.metrics_port import MetricsPort

# Conversion constant: 1 kWh = 3,600,000 joules.
JOULES_PER_KWH = 3_600_000


class PrometheusUnavailableError(Exception):
    """
    Prometheus itself cannot be reached (connection refused, DNS, timeout).

    WHY a dedicated exception type:
    - The API layer catches this and returns 503 Service Unavailable
      (distinct from a 500 for logic bugs or a 502 for bad queries).
    - Gives operators a clear signal: "the monitoring stack is down".
    """


class PrometheusQueryError(Exception):
    """
    Prometheus is reachable but rejected the query or returned no usable data.

    WHY a dedicated exception type:
    - Distinguishes "Prometheus down" (transport) from "query bad/empty" (logic).
    - Empty-but-expected data is handled gracefully inside methods instead,
      so this is only raised when data is truly unusable.
    """


class PrometheusMetricsAdapter(MetricsPort):
    """
    Fetches telemetry from OpenShift's built-in Prometheus via the HTTP API.

    Uses httpx.AsyncClient: a single reusable connection pool (like a database
    connection pool) instead of opening a new TCP connection per request — this
    matters on OpenShift where each request would otherwise pay TLS + TCP cost.

    Constructor takes config from app.config (adapters may read settings;
    only the domain is forbidden from touching config/adapters).
    """

    def __init__(
        self,
        prometheus_url: str = settings.PROMETHEUS_URL,
        timeout_seconds: int = settings.PROMETHEUS_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=prometheus_url,
            timeout=httpx.Timeout(timeout_seconds),
        )

    # ------------------------------------------------------------------
    # Low-level query helper (single place where HTTP + JSON live)
    # ------------------------------------------------------------------

    async def _query(self, promql: str) -> list[dict]:
        """
        Execute an instant PromQL query and return the `result` list.

        Raises:
            PrometheusUnavailableError: transport failure (connect/timeout).
            PrometheusQueryError: non-2xx response or PromQL execution error.

        WHY an instant query (not range):
        - `/api/v1/query` returns the current value — correct for utilizations,
          requests, allocatable, and 24h increases.
        - Range queries (`/api/v1/query_range`) are only needed for time-series
          charts, which are out of scope for this MVP.
        """
        try:
            response = await self._client.get(
                "/api/v1/query", params={"query": promql}
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise PrometheusUnavailableError(
                f"Prometheus unreachable at {self._client.base_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise PrometheusQueryError(
                f"Prometheus returned HTTP {exc.response.status_code}"
            ) from exc

        payload = response.json()
        if payload.get("status") != "success":
            raise PrometheusQueryError(
                f"PromQL execution failed: {payload.get('error', 'unknown')}"
            )
        return payload.get("data", {}).get("result", [])

    @staticmethod
    def _instant_value(result: list[dict]) -> float:
        """
        Extract the numeric value from an instant query result.

        Prometheus returns values as `[unix_epoch, "4.2"]` — the number is a
        STRING. This helper safely converts it, defaulting to 0.0 when the
        series does not exist (namespace has no data for that metric).

        WHY default to 0.0 instead of raising:
        - A namespace with no running workload legitimately has no data.
        - A missing series should mean "nothing used", not "the query broke".
        """
        if not result:
            return 0.0
        return float(result[0]["value"][1])

    @staticmethod
    def _metric_label(result_entry: dict, label: str, default: str = "") -> str:
        """Safely read a label from a Prometheus result series."""
        return result_entry.get("metric", {}).get(label, default)

    # ------------------------------------------------------------------
    # FinOps P0 — pod-level resource metrics
    # ------------------------------------------------------------------

    async def get_pod_metrics(self, namespace: str) -> list[PodMetric]:
        """
        Build a PodMetric for a namespace (FinOps P0).

        The four PromQL strings below are verbatim from metrics-reference.md:
        1. sum(rate(container_cpu_usage_seconds_total{...}[30d]))
           → sustained vCPU cores over the window (rate removes the counter
             accumulation and gives cores-per-second = average cores).
           WHY it is "core-hours": a workload sustaining X cores for the whole
           window contributes X × window-hours; kept aligned to the reference.
        2. sum(container_memory_working_set_bytes{...})
           → current resident working set in bytes (instant, no rate).
        3. sum(kube_pod_container_resource_requests{..., resource="cpu"})
           → total requested CPU cores (kube-state-metrics reports cores).
        4. sum(kube_pod_container_resource_requests{..., resource="memory"})
           → total requested memory in bytes.

        Attribution labels (team/cost_center/environment) come from
        kube_pod_labels (label_<key>), defaults to "unknown" when absent.
        """
        cpu_usage = await self._query(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}"}}[30d]))'
        )
        memory_usage = await self._query(
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}"}})'
        )
        cpu_requests = await self._query(
            f'sum(kube_pod_container_resource_requests{{namespace="{namespace}", resource="cpu"}})'
        )
        memory_requests = await self._query(
            f'sum(kube_pod_container_resource_requests{{namespace="{namespace}", resource="memory"}})'
        )

        # Attribution labels: one query, read label_<key> off the first series.
        labels_result = await self._query(
            f'kube_pod_labels{{namespace="{namespace}"}}'
        )
        team = "unknown"
        cost_center = "unknown"
        environment = "unknown"
        if labels_result:
            team = self._metric_label(labels_result[0], "label_team", "unknown")
            cost_center = self._metric_label(labels_result[0], "label_cost_center", "unknown")
            environment = self._metric_label(labels_result[0], "label_environment", "unknown")

        pod = PodMetric(
            namespace=namespace,
            team=team,
            cost_center=cost_center,
            environment=environment,
            cpu_core_hours=self._instant_value(cpu_usage),
            memory_bytes=self._instant_value(memory_usage),
            cpu_requested=self._instant_value(cpu_requests),
            memory_requested=self._instant_value(memory_requests),
            timestamp=datetime.now(timezone.utc),
        )
        return [pod]

    async def get_cluster_allocatable(self) -> ClusterAllocatable:
        """
        Sum allocatable CPU/memory across all nodes (FinOps P0).

        PromQL verbatim from metrics-reference.md:
        - kube_node_status_allocatable{resource="cpu"}   → cores
        - kube_node_status_allocatable{resource="memory"} → bytes

        WHY sum(): the raw series has one entry PER NODE. sum() adds them
        together so we get cluster-wide capacity, not per-node.
        """
        cpu = await self._query(
            'sum(kube_node_status_allocatable{resource="cpu"})'
        )
        memory = await self._query(
            'sum(kube_node_status_allocatable{resource="memory"})'
        )
        return ClusterAllocatable(
            cpu_allocatable=self._instant_value(cpu),
            memory_allocatable=self._instant_value(memory),
            timestamp=datetime.now(timezone.utc),
        )

    async def get_storage_metrics(self, namespace: str) -> list[StorageMetric]:
        """
        Per-PVC storage usage for a namespace (FinOps P0).

        Three queries, joined by the PVC name label:
        - kubelet_volume_stats_used_bytes      → label `persistentvolumeclaim`
        - kubelet_volume_stats_capacity_bytes  → label `persistentvolumeclaim`
        - kube_persistentvolumeclaim_info      → label `persistentvolumeclaim`,
          healthy only when phase="Bound"

        WHY three queries instead of one:
        - Prometheus stores used, capacity, and claim info as SEPARATE series.
        - The adapter joins them in Python by PVC name (the domain never needs
          to know the data was scattered across three series).
        """
        used_result = await self._query(
            f'kubelet_volume_stats_used_bytes{{namespace="{namespace}"}}'
        )
        capacity_result = await self._query(
            f'kubelet_volume_stats_capacity_bytes{{namespace="{namespace}"}}'
        )
        pvc_info_result = await self._query(
            f'kube_persistentvolumeclaim_info{{namespace="{namespace}"}}'
        )

        # Build a lookup: pvc_name -> bytes (float).
        used_by_pvc = {
            self._metric_label(entry, "persistentvolumeclaim"): float(entry["value"][1])
            for entry in used_result
        }
        capacity_by_pvc = {
            self._metric_label(entry, "persistentvolumeclaim"): float(entry["value"][1])
            for entry in capacity_result
        }
        # A claim is "bound" if its info series carries phase="Bound".
        bound_pvcs = {
            self._metric_label(entry, "persistentvolumeclaim")
            for entry in pvc_info_result
            if entry.get("metric", {}).get("phase") == "Bound"
        }

        # Union of all PVC names we saw in any of the three series.
        pvc_names = set(used_by_pvc) | set(capacity_by_pvc) | bound_pvcs

        storage_metrics = [
            StorageMetric(
                namespace=namespace,
                pvc_name=pvc_name,
                used_bytes=used_by_pvc.get(pvc_name, 0.0),
                capacity_bytes=capacity_by_pvc.get(pvc_name, 0.0),
                is_bound=pvc_name in bound_pvcs,
                timestamp=datetime.now(timezone.utc),
            )
            for pvc_name in sorted(pvc_names)
        ]
        return storage_metrics

    # ------------------------------------------------------------------
    # Data quality — which telemetry sources are healthy
    # ------------------------------------------------------------------

    async def check_data_quality(self) -> DataQuality:
        """
        Determine which telemetry sources are healthy.

        PromQL verbatim from metrics-reference.md:
        - up{job=~"kube-state-metrics|node-exporter|kepler"} → up==0 means DOWN.

        WHY not raising when Prometheus itself is unreachable:
        - If Prometheus is down, EVERYTHING is unavailable. Returning an empty
          healthy_sources set lets the engine flag every category as "no data"
          instead of crashing the whole dashboard (graceful degradation).
        """
        try:
            result = await self._query(
                r'up{job=~"kube-state-metrics|node-exporter|kepler"}'
            )
        except (PrometheusUnavailableError, PrometheusQueryError):
            # Prometheus or the query itself is broken → nothing is healthy.
            return DataQuality(
                healthy_sources=frozenset(), timestamp=datetime.now(timezone.utc)
            )

        healthy: set[TelemetrySource] = set()
        for entry in result:
            job = self._metric_label(entry, "job")
            is_up = entry["value"][1] == "1"
            if not is_up:
                continue
            # Map the Prometheus job name back to our domain source enum.
            if job == "kube-state-metrics":
                healthy.add(TelemetrySource.KUBE_STATE_METRICS)
            elif job == "node-exporter":
                healthy.add(TelemetrySource.NODE_EXPORTER)
            elif job == "kepler":
                healthy.add(TelemetrySource.KEPLER)

        return DataQuality(
            healthy_sources=frozenset(healthy),
            timestamp=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Green IT P0 — Kepler energy
    # ------------------------------------------------------------------

    async def get_energy_kwh(self, namespace: str) -> float:
        """
        Energy consumed by a namespace over the last 24h, in kWh (Green IT P0).

        Metric selection (per metrics-reference.md):
        1. PRIMARY: kepler_container_cpu_joules_total — has a `namespace` label,
           so we can attribute energy per namespace.
        2. FALLBACK: kepler_node_cpu_joules_total — node-level only, no
           namespace label; summed across the cluster (coarse).
        3. LAST RESORT: kepler_node_package_joules_total — older Kepler metric
           name; exact name varies by Kepler version, hence the runtime probe.

        PromQL shape: increase(<metric>[24h]) → joules consumed in the last 24h.
        Conversion: joules / 3,600,000 = kWh.

        WHY we try container → node → package instead of hardcoding one name:
        - Kepler's exact series set differs across versions.
        - A runtime probe is cheaper than misconfiguring the metric name in
          config and discovering it only after Prom shows zero carbon.
        """
        # 1. Primary: container-level (per-namespace granularity).
        container_energy = await self._query(
            f'sum(increase(kepler_container_cpu_joules_total{{namespace="{namespace}"}}[24h]))'
        )
        container_kwh = self._instant_value(container_energy) / JOULES_PER_KWH
        if container_energy:
            return container_kwh

        # 2. Fallback: node-level CPU joules (cluster-wide, coarse attribution).
        node_energy = await self._query(
            "sum(increase(kepler_node_cpu_joules_total[24h]))"
        )
        if bool(node_energy):
            return self._instant_value(node_energy) / JOULES_PER_KWH

        # 3. Last resort: older Kepler metric name.
        package_energy = await self._query(
            "sum(increase(kepler_node_package_joules_total[24h]))"
        )
        if bool(package_energy):
            return self._instant_value(package_energy) / JOULES_PER_KWH

        # No Kepler series at all → raising beats returning 0.0.
        # WHY: a silent 0.0 means "you produced zero carbon" — a LIE. An
        # explicit error tells the operator the energy pipeline has a hole.
        raise PrometheusQueryError(
            "No Kepler energy series found "
            "(checked container/node/package joules metrics)"
        )

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool.

        WHY: prevents resource leaks when the app shuts down.
        Called by main.py lifespan on shutdown.
        """
        await self._client.aclose()