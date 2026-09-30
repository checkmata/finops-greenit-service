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

import re
from collections import Counter
from datetime import datetime, timezone

import httpx

from app.cluster_config import Credentials
from app.config import settings
from app.domain.models.cluster import DeploymentAvailability
from app.domain.models.data_quality import DataQuality, TelemetrySource
from app.domain.models.energy import EnergyAttribution, EnergyMeasurement, PowerSource, WorkloadEnergy
from app.domain.models.pod_metric import (
    UNATTRIBUTED_WORKLOAD,
    ClusterAllocatable,
    PodMetric,
    StorageMetric,
)
from app.ports.metrics_port import MetricsPort

# Conversion constant: 1 kWh = 3,600,000 joules.
JOULES_PER_KWH = 3_600_000

# cAdvisor exposes, for every pod, one series per container PLUS a pod-level
# cgroup series (container="") and the pause container (container="POD").
# Summing all of them counts usage roughly twice (V1 fix). Real containers only:
REAL_CONTAINERS = 'container!="", container!="POD"'
# Pod-level cgroup series (no `container` label). Some runtimes expose ONLY these:
# observed on minikube + cri-dockerd + cgroup v2 in V2 (29 cAdvisor series, all
# pod-level), whereas OpenShift/CRI-O exposes per-container series.
POD_CGROUP = 'container=""'


def usage_sum(metric: str, selector: str, by: str = "", rate_window: str | None = None) -> str:
    """
    Sum a cAdvisor metric WITHOUT double counting, whatever the runtime exposes:

        sum by (<by>, namespace, pod) (<metric>{<selector>, container!="", container!="POD"})
      or
        sum by (<by>, namespace, pod) (<metric>{<selector>, container=""})

    `or` keeps, PER POD, the container-level series when they exist and falls
    back to the pod-level cgroup series otherwise — never both. The result is
    then summed to the requested grouping.
    """
    def inner(filt: str) -> str:
        sel = ", ".join(x for x in (selector, filt) if x)
        series = f"{metric}{{{sel}}}"
        if rate_window:
            series = f"rate({series}[{rate_window}])"
        return f"sum by (namespace, pod) ({series})"

    per_pod = f"({inner(REAL_CONTAINERS)} or {inner(POD_CGROUP)})"
    return f"sum by ({by}) {per_pod}" if by else f"sum {per_pod}"


# CPU usage = AVERAGE cores over the analysis window, computed as the time-average
# of short 5-minute rates (a subquery).
#
# WHY not rate(x[30d]) (MVP): rate() divides by the WHOLE 30-day range, so any
# series younger than 30 days (new deployment, restarted pod) is reported near 0
# cores → a false "over-provisioned, reduce it" advice. Found in V1 by running
# the service against a real Prometheus (scripts/run_local_demo.sh).
CPU_WINDOW = "30d"
CPU_STEP = "5m"


def avg_cores(inner_sum: str) -> str:
    """Wrap `sum(...rate(cpu[5m])...)` into its average over CPU_WINDOW."""
    return f"avg_over_time(({inner_sum})[{CPU_WINDOW}:{CPU_STEP}])"


def observed_steps(inner_sum: str) -> str:
    """Number of CPU_STEP points behind avg_cores() → observation length."""
    return f"count_over_time(({inner_sum})[{CPU_WINDOW}:{CPU_STEP}])"


STEP_HOURS = 5 / 60  # CPU_STEP expressed in hours


def _window(seconds: int) -> str:
    """PromQL range for an energy window (e.g. 86400 → '86400s')."""
    return f"{max(int(seconds), 1)}s"


def ksm_label(pod_label: str) -> str:
    """kube-state-metrics name of a pod label: app.kubernetes.io/name → label_app_kubernetes_io_name."""
    return "label_" + re.sub(r"[^a-zA-Z0-9_]", "_", pod_label)


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
        cluster_name: str = "default",
        workload_label: str = "app",
        credentials: "Credentials | None" = None,
        verify: bool | str = True,
    ) -> None:
        # V2: optional authentication for REMOTE Prometheus (basic or bearer),
        # credentials resolved from env/secret files by app.cluster_config.
        auth = None
        headers = {}
        if credentials is not None and credentials.kind == "basic":
            auth = httpx.BasicAuth(credentials.username or "", credentials.secret or "")
        elif credentials is not None and credentials.kind == "bearer":
            headers["Authorization"] = f"Bearer {credentials.secret}"
        self._client = httpx.AsyncClient(
            base_url=prometheus_url,
            timeout=httpx.Timeout(timeout_seconds),
            auth=auth,
            headers=headers,
            verify=verify,
        )
        # V1: one adapter instance per cluster; every PodMetric is stamped with it.
        self.cluster_name = cluster_name
        self.workload_label = workload_label

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
            if exc.response.status_code in (401, 403):
                # V2: an auth failure means we cannot observe this cluster at all.
                raise PrometheusUnavailableError(
                    f"Prometheus refused credentials (HTTP {exc.response.status_code})"
                ) from exc
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
        1. avg_over_time(sum(rate(container_cpu_usage_seconds_total{...}[5m]))[30d:5m])
           → AVERAGE cores used over the last 30 days (V1; see avg_cores()).
           The field keeps its MVP name `cpu_core_hours` but holds cores.
        2. sum(container_memory_working_set_bytes{...})
           → current resident working set in bytes (instant, no rate).
        3. sum(kube_pod_container_resource_requests{..., resource="cpu"})
           → total requested CPU cores (kube-state-metrics reports cores).
        4. sum(kube_pod_container_resource_requests{..., resource="memory"})
           → total requested memory in bytes.

        Attribution labels (team/cost_center/environment) come from
        kube_pod_labels (label_<key>), defaults to "unknown" when absent.
        """
        cpu_inner = usage_sum("container_cpu_usage_seconds_total", f'namespace="{namespace}"', rate_window=CPU_STEP)
        cpu_usage = await self._query(avg_cores(cpu_inner))
        cpu_steps = await self._query(observed_steps(cpu_inner))
        memory_usage = await self._query(
            usage_sum("container_memory_working_set_bytes", f'namespace="{namespace}"')
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
        team, cost_center, environment = self._dominant_attribution(labels_result)

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
            cluster=getattr(self, "cluster_name", "default"),
            observation_hours=self._instant_value(cpu_steps) * STEP_HOURS,
        )
        return [pod]

    @classmethod
    def _dominant_attribution(cls, labels_result: list[dict]) -> tuple[str, str, str]:
        """
        Most frequent (team, cost_center, environment) among the pods.

        WHY (V1): the MVP took the FIRST series, i.e. an arbitrary pod. Using
        the majority is deterministic and correct for single-team namespaces;
        multi-team namespaces are handled by the per-workload breakdown.
        """
        if not labels_result:
            return "unknown", "unknown", "unknown"
        counts = Counter(
            (
                cls._metric_label(e, "label_team", "unknown") or "unknown",
                cls._metric_label(e, "label_cost_center", "unknown") or "unknown",
                cls._metric_label(e, "label_environment", "unknown") or "unknown",
            )
            for e in labels_result
        )
        # Ties are broken by value order so the result is deterministic.
        best = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        return best[0]

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
        # V2 (verified on real kube-state-metrics v2): the phase is NOT a label of
        # kube_persistentvolumeclaim_info; it is kube_persistentvolumeclaim_status_phase.
        # The MVP/V1 read `phase` from _info → no PVC ever looked bound, and a PVC
        # without kubelet stats was invisible (storage "not_applicable" instead of
        # "unavailable").
        phase_result = await self._query(
            f'kube_persistentvolumeclaim_status_phase{{namespace="{namespace}", phase="Bound"}} == 1'
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
        known_pvcs = {self._metric_label(e, "persistentvolumeclaim") for e in pvc_info_result} - {""}
        bound_pvcs = {self._metric_label(e, "persistentvolumeclaim") for e in phase_result} - {""}
        # Backward compatibility with exporters that DO put `phase` on _info.
        bound_pvcs |= {
            self._metric_label(entry, "persistentvolumeclaim")
            for entry in pvc_info_result
            if entry.get("metric", {}).get("phase") == "Bound"
        }

        # Union of all PVC names we saw in any of the three series.
        pvc_names = set(used_by_pvc) | set(capacity_by_pvc) | bound_pvcs | known_pvcs

        storage_metrics = [
            StorageMetric(
                namespace=namespace,
                pvc_name=pvc_name,
                used_bytes=used_by_pvc.get(pvc_name, 0.0),
                capacity_bytes=capacity_by_pvc.get(pvc_name, 0.0),
                is_bound=pvc_name in bound_pvcs,
                timestamp=datetime.now(timezone.utc),
                # V1: without BOTH kubelet stats the PVC is not priced.
                has_usage_stats=pvc_name in used_by_pvc and pvc_name in capacity_by_pvc,
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
        Energy of a namespace over the last 24 h, in kWh (MVP contract kept).

        V1: delegates to get_energy() and raises PrometheusQueryError when the
        energy cannot be attributed. It no longer returns the WHOLE cluster's
        node energy for one namespace (MVP over-attribution bug).
        """
        measurement = await self.get_energy(namespace)
        if not measurement.is_available:
            raise PrometheusQueryError(
                "No Kepler energy series found "
                "(checked container/pod/node joules metrics)"
            )
        return measurement.total_kwh or 0.0

    async def get_energy(self, namespace: str, window_seconds: int = 86400) -> EnergyMeasurement:
        """
        Energy of a namespace over 24 h WITH provenance (attribution + power source).

        Label names VERIFIED on a real Kepler (release-0.8.0, V2):
          kepler_container_joules_total{container_namespace, pod_name, container_name, mode}
        The `namespace` / `pod` labels on that series belong to the KEPLER POD
        (added by the ServiceMonitor), NOT to the measured workload — the V1
        query `{namespace="<ns>"}` matched nothing (or, for ns="kepler",
        everything). Only `container_namespace` identifies the workload.

        Hierarchy (most precise first):
        1. CONTAINER: kepler_container_joules_total (0.7/0.8) or
           kepler_container_cpu_joules_total (other releases), by container_namespace
        2. POD: kepler_pod_cpu_joules_total{pod_namespace} (newer Kepler)
        3. NODE_CPU_SHARE: node joules × namespace CPU share — estimated
        4. UNAVAILABLE

        Power source comes from kepler_node_info{components_power_source}:
        "estimator" → PowerSource.MODEL (VMs without RAPL). Idle energy is only
        trusted with HARDWARE power: in estimator mode Kepler publishes
        mode="idle" series that are always 0 (observed), which must not be read
        as "no idle waste".
        """
        ns = namespace
        w = _window(window_seconds)
        power_source = await self._power_source()

        # 1. Container level (real label: container_namespace)
        c_sel = f'container_namespace="{ns}"'
        if power_source is PowerSource.HARDWARE:
            # MEASURED scope only: RAPL components (package + dram). The Kepler
            # "joules_total" adds an "other" term derived from the platform power,
            # which is modelled when no ACPI power meter exists (observed: 83 % of
            # the total on the kind host). Mixing it in would pass an estimate
            # off as a measurement.
            measured = await self._query(
                f"sum(increase(kepler_container_package_joules_total{{{c_sel}}}[{w}]))"
                f" + (sum(increase(kepler_container_dram_joules_total{{{c_sel}}}[{w}])) or vector(0))"
            )
            if measured:
                idle = await self._idle_joules(
                    f"sum by (mode) (increase(kepler_container_package_joules_total{{{c_sel}}}[{w}]))"
                )
                return self._measurement(ns, measured, idle, EnergyAttribution.CONTAINER,
                                         "kepler_container_package+dram_joules_total (RAPL)",
                                         power_source, scope="cpu_package+dram")
        container = await self._query(
            f"sum(increase(kepler_container_joules_total{{{c_sel}}}[{w}]))"
            f" or sum(increase(kepler_container_cpu_joules_total{{{c_sel}}}[{w}]))"
        )
        if container:
            # Model mode: the whole Kepler figure is an estimate; idle series are 0.
            source = PowerSource.MODEL if power_source is PowerSource.HARDWARE else power_source
            return self._measurement(ns, container, None, EnergyAttribution.CONTAINER,
                                     "kepler_container_joules_total", source, scope="platform")

        # 2. Pod level (newer Kepler)
        p_sel = f'kepler_pod_cpu_joules_total{{pod_namespace="{ns}"}}'
        pod = await self._query(f"sum(increase({p_sel}[{w}]))")
        if pod:
            idle = None
            if power_source is PowerSource.HARDWARE:
                idle = await self._idle_joules(f"sum by (mode) (increase({p_sel}[{w}]))")
            return self._measurement(ns, pod, idle, EnergyAttribution.POD, "kepler_pod_cpu_joules_total",
                                     power_source, scope="cpu")

        # 3. Node level × CPU share (estimated)
        for node_metric in ("kepler_node_cpu_joules_total", "kepler_node_package_joules_total",
                            "kepler_node_platform_joules_total"):
            node = await self._query(f"sum(increase({node_metric}[{w}]))")
            if not node:
                continue
            ns_cpu = self._instant_value(await self._query(
                usage_sum("container_cpu_usage_seconds_total", f'namespace="{ns}"', rate_window=w)
            ))
            cluster_cpu = self._instant_value(await self._query(
                usage_sum("container_cpu_usage_seconds_total", 'namespace!=""', rate_window=w)
            ))
            if cluster_cpu <= 0:
                break  # cannot compute a share → unavailable, not "everything"
            share = min(1.0, max(0.0, ns_cpu / cluster_cpu))
            kwh = self._instant_value(node) * share / JOULES_PER_KWH
            return EnergyMeasurement(
                namespace=ns,
                total_kwh=kwh,
                idle_kwh=None,
                attribution=EnergyAttribution.NODE_CPU_SHARE,
                source_metric=f"{node_metric} × CPU share ({share:.3f})",
                power_source=power_source,
                scope="node_share",
            )

        return EnergyMeasurement(
            namespace=ns, total_kwh=None, idle_kwh=None,
            attribution=EnergyAttribution.UNAVAILABLE, source_metric="none",
        )

    async def _power_source(self) -> PowerSource:
        """Kepler's own statement of how it obtains power (kepler_node_info)."""
        try:
            info = await self._query("kepler_node_info")
        except PrometheusQueryError:
            return PowerSource.UNKNOWN
        sources = {self._metric_label(e, "components_power_source") for e in info} - {""}
        if not sources:
            return PowerSource.UNKNOWN
        if sources <= {"estimator", "none"}:
            return PowerSource.MODEL
        if "estimator" in sources:  # mixed nodes: at least part is modelled
            return PowerSource.MODEL
        return PowerSource.HARDWARE

    async def _idle_joules(self, promql: str) -> float | None:
        """Joules of the `mode="idle"` series, None if the split does not exist."""
        try:
            result = await self._query(promql)
        except PrometheusQueryError:
            return None
        for entry in result:
            if self._metric_label(entry, "mode") == "idle":
                return float(entry["value"][1])
        return None

    def _measurement(
        self,
        ns: str,
        total_result: list[dict],
        idle_joules: float | None,
        attribution: EnergyAttribution,
        source: str,
        power_source: PowerSource = PowerSource.UNKNOWN,
        scope: str = "unknown",
    ) -> EnergyMeasurement:
        return EnergyMeasurement(
            namespace=ns,
            total_kwh=self._instant_value(total_result) / JOULES_PER_KWH,
            idle_kwh=None if idle_joules is None else idle_joules / JOULES_PER_KWH,
            attribution=attribution,
            source_metric=source,
            power_source=power_source,
            scope=scope,
        )

    # ------------------------------------------------------------------
    # V1 — discovery, per-workload breakdown, availability, ping
    # ------------------------------------------------------------------

    async def get_workload_energy(self, namespace: str, window_seconds: int = 86400) -> list[WorkloadEnergy]:
        """
        V2 — energy per workload and per pod.

        Kepler (real labels, verified V2) gives per-container series with
        `container_namespace` + `pod_name`. We sum per pod, rename those labels to
        `namespace` / `pod` (label_replace) and join kube_pod_labels to find the
        workload label. Hardware mode: RAPL package+dram only (measured scope);
        model mode: Kepler's total (estimated). Pods without the workload label
        (or already deleted) are reported as "_unattributed".
        """
        ns, w = namespace, _window(window_seconds)
        power_source = await self._power_source()
        c_sel = f'container_namespace="{ns}"'
        if power_source is PowerSource.HARDWARE:
            pkg = f"sum by (container_namespace, pod_name) (increase(kepler_container_package_joules_total{{{c_sel}}}[{w}]))"
            dram = f"sum by (container_namespace, pod_name) (increase(kepler_container_dram_joules_total{{{c_sel}}}[{w}]))"
            # `(dram or pkg*0)`: a pod without a DRAM series counts 0 J of DRAM instead
            # of being dropped by the vector match.
            per_pod = f"{pkg} + on (container_namespace, pod_name) ({dram} or {pkg} * 0)"
            scope = "cpu_package+dram"
        else:
            per_pod = f"sum by (container_namespace, pod_name) (increase(kepler_container_joules_total{{{c_sel}}}[{w}]))"
            scope = "platform"
        pods = await self._query(per_pod)
        if not pods:
            return []
        L = ksm_label(getattr(self, "workload_label", "app"))
        labels = await self._query(f'max by (namespace, pod, {L}) (kube_pod_labels{{namespace="{ns}", {L}!=""}})')
        workload_of = {self._metric_label(e, "pod"): self._metric_label(e, L) for e in labels}

        by_workload: dict[str, list[tuple[str, float]]] = {}
        for e in pods:
            pod = self._metric_label(e, "pod_name")
            kwh = float(e["value"][1]) / JOULES_PER_KWH
            by_workload.setdefault(workload_of.get(pod, UNATTRIBUTED_WORKLOAD), []).append((pod, kwh))
        return [
            WorkloadEnergy(namespace=ns, workload=wl, kwh=sum(k for _, k in items), power_source=power_source,
                           scope=scope, pods=tuple(sorted(items)))
            for wl, items in sorted(by_workload.items())
        ]

    async def ping(self) -> bool:
        """Transport-level reachability (a trivial query must succeed)."""
        try:
            await self._query("vector(1)")
            return True
        except (PrometheusUnavailableError, PrometheusQueryError):
            return False

    async def list_namespaces(self) -> list[str]:
        result = await self._query("count by (namespace) (kube_pod_container_resource_requests)")
        return sorted({self._metric_label(e, "namespace") for e in result} - {""})

    async def get_workload_metrics(
        self,
        namespace: str,
        namespace_total: PodMetric | None = None,
    ) -> list[PodMetric]:
        """
        Per-workload breakdown via a join on kube_pod_labels.

        PromQL shape (L = kube-state-metrics name of the workload label):
            sum by (L) (
                <per-pod metric>
              * on (namespace, pod) group_left(L)
                max by (namespace, pod, L) (kube_pod_labels{namespace="ns", L!=""})
            )
        Pods without the label drop out of the join; their usage is kept as
        the "_unattributed" workload (namespace total − Σ labelled).
        NOTE: on OpenShift/kube-state-metrics v2 the label must be allow-listed
        (--metric-labels-allowlist=pods=[app,team,...]) or kube_pod_labels is empty.
        """
        L = ksm_label(getattr(self, "workload_label", "app"))
        ns = namespace
        labels_sel = f'kube_pod_labels{{namespace="{ns}", {L}!=""}}'
        join = f"* on (namespace, pod) group_left({L}) max by (namespace, pod, {L}) ({labels_sel})"

        def by_workload(inner: str) -> str:
            return f"sum by ({L}) ({inner} {join})"

        # Join INSIDE the time-average: at each step the pod→workload mapping of
        # that moment is used, so rolling updates are not double counted.
        cpu_inner = by_workload(
            usage_sum("container_cpu_usage_seconds_total", f'namespace="{ns}"', by="namespace, pod", rate_window=CPU_STEP)
        )
        cpu = await self._query(avg_cores(cpu_inner))
        cpu_now = await self._query(cpu_inner)          # V3: instantaneous (5-min rate)
        steps = await self._query(observed_steps(cpu_inner))
        mem = await self._query(by_workload(
            usage_sum("container_memory_working_set_bytes", f'namespace="{ns}"', by="namespace, pod")
        ))
        cpu_req = await self._query(by_workload(
            f'sum by (namespace, pod) (kube_pod_container_resource_requests{{namespace="{ns}", resource="cpu"}})'
        ))
        mem_req = await self._query(by_workload(
            f'sum by (namespace, pod) (kube_pod_container_resource_requests{{namespace="{ns}", resource="memory"}})'
        ))
        attribution = await self._query(
            f"max by ({L}, label_team, label_cost_center, label_environment) ({labels_sel})"
        )

        def as_map(result: list[dict]) -> dict[str, float]:
            return {self._metric_label(e, L): float(e["value"][1]) for e in result if self._metric_label(e, L)}

        cpu_m, mem_m, cpu_req_m, mem_req_m = as_map(cpu), as_map(mem), as_map(cpu_req), as_map(mem_req)
        cpu_now_m = as_map(cpu_now)
        hours_m = {k: v * STEP_HOURS for k, v in as_map(steps).items()}
        names = sorted(set(cpu_m) | set(mem_m) | set(cpu_req_m) | set(mem_req_m))
        if not names:
            return []

        attr_by_wl: dict[str, list[dict]] = {}
        for e in attribution:
            attr_by_wl.setdefault(self._metric_label(e, L), []).append(e)

        now = datetime.now(timezone.utc)
        cluster = getattr(self, "cluster_name", "default")
        out = []
        for wl in names:
            team, cc, env = self._dominant_attribution(attr_by_wl.get(wl, []))
            out.append(PodMetric(
                namespace=ns, team=team, cost_center=cc, environment=env,
                cpu_core_hours=cpu_m.get(wl, 0.0), memory_bytes=mem_m.get(wl, 0.0),
                cpu_requested=cpu_req_m.get(wl, 0.0), memory_requested=mem_req_m.get(wl, 0.0),
                timestamp=now, cluster=cluster, workload=wl,
                observation_hours=hours_m.get(wl, 0.0),
                cpu_cores_now=cpu_now_m.get(wl),
            ))

        total = namespace_total or (await self.get_pod_metrics(ns))[0]
        rest = {
            "cpu": total.cpu_core_hours - sum(p.cpu_core_hours for p in out),
            "mem": total.memory_bytes - sum(p.memory_bytes for p in out),
            "cpu_req": total.cpu_requested - sum(p.cpu_requested for p in out),
            "mem_req": total.memory_requested - sum(p.memory_requested for p in out),
        }
        totals = {"cpu": total.cpu_core_hours, "mem": total.memory_bytes,
                  "cpu_req": total.cpu_requested, "mem_req": total.memory_requested}
        # Only report a remainder that is material (> 0.1 % of the total), to
        # avoid floating-point dust becoming a fake workload.
        if any(rest[k] > max(1e-9, 0.001 * totals[k]) for k in rest):
            out.append(PodMetric(
                namespace=ns, team=total.team, cost_center=total.cost_center,
                environment=total.environment,
                cpu_core_hours=max(0.0, rest["cpu"]), memory_bytes=max(0.0, rest["mem"]),
                cpu_requested=max(0.0, rest["cpu_req"]), memory_requested=max(0.0, rest["mem_req"]),
                timestamp=now, cluster=cluster, workload=UNATTRIBUTED_WORKLOAD,
                observation_hours=total.observation_hours,
            ))
        return out

    async def get_deployment_availability(self) -> list[DeploymentAvailability]:
        desired = await self._query("sum by (namespace, deployment) (kube_deployment_spec_replicas)")
        available = await self._query(
            "sum by (namespace, deployment) (kube_deployment_status_replicas_available)"
        )

        def key(e: dict) -> tuple[str, str]:
            return self._metric_label(e, "namespace"), self._metric_label(e, "deployment")

        avail_map = {key(e): int(float(e["value"][1])) for e in available}
        out = []
        for e in desired:
            ns, dep = key(e)
            if not ns or not dep:
                continue
            out.append(DeploymentAvailability(
                namespace=ns, workload=dep,
                desired=int(float(e["value"][1])),
                available=avail_map.get((ns, dep), 0),
            ))
        return sorted(out, key=lambda d: (d.namespace, d.workload))

    async def close(self) -> None:
        """Close the underlying httpx.AsyncClient connection pool.

        WHY: prevents resource leaks when the app shuts down.
        Called by main.py lifespan on shutdown.
        """
        await self._client.aclose()