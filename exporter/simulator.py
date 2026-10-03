"""
Cluster SIMULATOR — produces the Prometheus series a real OpenShift cluster
would expose, with load that varies over time.

Honesty note: every number here is synthetic. It exists so the whole pipeline
(PromQL → adapter → domain → API → DB → Grafana) can be demonstrated on a
laptop without Kubernetes. The real-cluster path is workloads/ + scripts/k8s/.

Components (one CollectorRegistry each, served on separate ports so that
Prometheus' own `up{job=...}` reflects failures — the MVP exporter faked `up`):
    kube-state-metrics  requests, labels, deployments, PVC info, node allocatable
    kubelet             cAdvisor container_* series + kubelet_volume_stats_*
    node-exporter       node_cpu_seconds_total (minimal)
    kepler              energy, exposure depends on `kepler_mode`:
                        container_legacy → kepler_container_cpu_joules_total{container_namespace, mode}
                        pod              → kepler_pod_cpu_joules_total{pod_namespace}
                        node_only        → kepler_node_cpu_joules_total only
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Gauge

COMPONENTS = ("kube-state-metrics", "kubelet", "node-exporter", "kepler")

# Simple, documented power model for the simulation (NOT a measurement):
IDLE_WATTS_PER_POD = 2.0
WATTS_PER_CORE = 12.0
NODE_IDLE_WATTS = 40.0


@dataclass
class WorkloadState:
    spec: dict
    replicas: int
    up: bool = True
    load_factor: float = 1.0

    @property
    def key(self) -> tuple[str, str]:
        return self.spec["namespace"], self.spec["name"]


@dataclass
class ClusterSimulator:
    name: str
    profile: dict
    day_seconds: float = 3600.0
    seed: int = 42
    t: float = 0.0
    cluster_up: bool = True
    component_up: dict = field(default_factory=lambda: {c: True for c in COMPONENTS})

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.lock = threading.Lock()
        self.kepler_mode = self.profile["kepler_mode"]
        self.workloads = {
            (w["namespace"], w["name"]): WorkloadState(w, w["replicas"]) for w in self.profile["workloads"]
        }
        self.registries = {c: CollectorRegistry() for c in COMPONENTS}
        self._define_metrics()
        self._static_series()

    # ------------------------------------------------------------------ metrics
    def _define_metrics(self) -> None:
        r = self.registries
        ksm, kubelet, node, kepler = r["kube-state-metrics"], r["kubelet"], r["node-exporter"], r["kepler"]
        self.requests = Gauge("kube_pod_container_resource_requests", "requests",
                              ["namespace", "pod", "container", "resource"], registry=ksm)
        self.pod_labels = Gauge("kube_pod_labels", "pod labels",
                                ["namespace", "pod", "label_app", "label_team", "label_cost_center",
                                 "label_environment"], registry=ksm)
        self.dep_spec = Gauge("kube_deployment_spec_replicas", "desired", ["namespace", "deployment"], registry=ksm)
        self.dep_avail = Gauge("kube_deployment_status_replicas_available", "available",
                               ["namespace", "deployment"], registry=ksm)
        self.allocatable = Gauge("kube_node_status_allocatable", "allocatable", ["node", "resource"], registry=ksm)
        self.pvc_info = Gauge("kube_persistentvolumeclaim_info", "pvc", ["namespace", "persistentvolumeclaim", "phase"],
                              registry=ksm)
        self.cpu = Counter("container_cpu_usage_seconds_total", "cpu", ["namespace", "pod", "container"],
                           registry=kubelet)
        self.mem = Gauge("container_memory_working_set_bytes", "mem", ["namespace", "pod", "container"],
                         registry=kubelet)
        self.net_rx = Counter("container_network_receive_bytes_total", "rx", ["namespace", "pod"], registry=kubelet)
        self.net_tx = Counter("container_network_transmit_bytes_total", "tx", ["namespace", "pod"], registry=kubelet)
        self.vol_used = Gauge("kubelet_volume_stats_used_bytes", "used", ["namespace", "persistentvolumeclaim"],
                              registry=kubelet)
        self.vol_cap = Gauge("kubelet_volume_stats_capacity_bytes", "cap", ["namespace", "persistentvolumeclaim"],
                             registry=kubelet)
        self.node_cpu = Counter("node_cpu_seconds_total", "node cpu", ["node", "mode"], registry=node)
        self.k_container = Counter("kepler_container_cpu_joules_total", "joules",
                                   ["container_namespace", "pod_name", "container_name", "mode"], registry=kepler)
        self.k_pod = Counter("kepler_pod_cpu_joules_total", "joules", ["pod_namespace", "pod_name"], registry=kepler)
        self.k_node = Counter("kepler_node_cpu_joules_total", "joules", ["node", "zone"], registry=kepler)

    def _static_series(self) -> None:
        for node, cores in self.profile["nodes"].items():
            self.allocatable.labels(node, "cpu").set(cores)
            self.allocatable.labels(node, "memory").set(cores * 4 * 1024 ** 3)
        for p in self.profile["pvcs"]:
            self.pvc_info.labels(p["namespace"], p["name"], "Bound").set(1)
            if p["stats"]:
                self.vol_used.labels(p["namespace"], p["name"]).set(p["used"])
                self.vol_cap.labels(p["namespace"], p["name"]).set(p["capacity"])
        self._refresh_ksm()

    def _pods(self, w: WorkloadState) -> list[str]:
        return [f"{w.spec['name']}-{i}" for i in range(w.replicas)]

    def _refresh_ksm(self) -> None:
        """Rebuild per-pod KSM series (replica count may change via the admin API)."""
        self.requests.clear()
        self.pod_labels.clear()
        for w in self.workloads.values():
            ns, name = w.key
            self.dep_spec.labels(ns, name).set(w.replicas)
            self.dep_avail.labels(ns, name).set(w.replicas if w.up else 0)
            lbl = dict(label_app=name if w.spec["labelled"] else "", **{f"label_{k}": v for k, v in
                                                                       _labels(ns).items()})
            for pod in self._pods(w):
                self.requests.labels(ns, pod, name, "cpu").set(w.spec["cpu_request"])
                self.requests.labels(ns, pod, name, "memory").set(w.spec["mem_request"])
                self.pod_labels.labels(namespace=ns, pod=pod, **lbl).set(1)

    # --------------------------------------------------------------- simulation
    def cores(self, w: WorkloadState, t: float) -> float:
        """CPU cores used by ONE pod at time t (diurnal sine × trend × noise × load factor)."""
        if not w.up:
            return 0.0
        s = w.spec
        diurnal = 1 + s["diurnal"] * math.sin(2 * math.pi * t / self.day_seconds)
        trend = 1 + s["trend_per_day"] * (t / self.day_seconds)
        noise = 1 + self.rng.uniform(-0.05, 0.05)
        return max(0.0, s["cpu_base"] * diurnal * trend * noise * w.load_factor)

    def tick(self, dt: float) -> None:
        with self.lock:
            self.t += dt
            node_names = list(self.profile["nodes"])
            node_joules = {n: NODE_IDLE_WATTS * dt for n in node_names}
            total_busy = 0.0
            for i, w in enumerate(self.workloads.values()):
                ns, name = w.key
                for j, pod in enumerate(self._pods(w)):
                    c = self.cores(w, self.t)
                    total_busy += c
                    self.cpu.labels(ns, pod, name).inc(c * dt)
                    mem = w.spec["mem_base"] * (1 + 0.05 * math.sin(self.t / 300)) if w.up else 0.0
                    self.mem.labels(ns, pod, name).set(mem)
                    self.net_rx.labels(ns, pod).inc(c * 2e6 * dt)
                    self.net_tx.labels(ns, pod).inc(c * 1e6 * dt)
                    idle_j = IDLE_WATTS_PER_POD * dt if w.up else 0.0
                    dyn_j = WATTS_PER_CORE * c * dt
                    node_joules[node_names[(i + j) % len(node_names)]] += idle_j + dyn_j
                    if self.kepler_mode == "container_legacy":
                        self.k_container.labels(ns, pod, name, "idle").inc(idle_j)
                        self.k_container.labels(ns, pod, name, "dynamic").inc(dyn_j)
                    elif self.kepler_mode == "pod":
                        self.k_pod.labels(ns, pod).inc(idle_j + dyn_j)
            for n, j in node_joules.items():
                self.k_node.labels(n, "package").inc(j)
            cores_total = sum(self.profile["nodes"].values())
            busy = min(total_busy, cores_total)
            self.node_cpu.labels(node_names[0], "idle").inc((cores_total - busy) * dt)
            self.node_cpu.labels(node_names[0], "user").inc(busy * dt)

    # -------------------------------------------------------------- fault admin
    def serving(self, component: str) -> bool:
        return self.cluster_up and self.component_up.get(component, False)

    def set_component(self, component: str, up: bool) -> None:
        if component not in COMPONENTS:
            raise KeyError(component)
        self.component_up[component] = up

    def set_workload(self, ns: str, name: str, *, up: bool | None = None,
                     replicas: int | None = None, load: float | None = None) -> None:
        with self.lock:
            w = self.workloads[(ns, name)]
            if up is not None:
                w.up = up
            if replicas is not None:
                w.replicas = max(0, int(replicas))
            if load is not None:
                w.load_factor = max(0.0, float(load))
            self._refresh_ksm()

    def state(self) -> dict:
        return {
            "cluster": self.name,
            "cluster_up": self.cluster_up,
            "components": dict(self.component_up),
            "kepler_mode": self.kepler_mode,
            "sim_time_s": round(self.t, 1),
            "workloads": [
                {"namespace": w.key[0], "workload": w.key[1], "replicas": w.replicas, "up": w.up,
                 "load_factor": w.load_factor}
                for w in self.workloads.values()
            ],
        }


def _labels(ns: str) -> dict:
    from exporter.profiles import LABELS

    return LABELS.get(ns, {"team": "unknown", "cost_center": "unknown", "environment": "dev"})
