"""The local cluster simulator must produce coherent, time-varying series (demo correctness)."""

from prometheus_client import generate_latest

from exporter.profiles import PROFILES
from exporter.simulator import IDLE_WATTS_PER_POD, WATTS_PER_CORE, ClusterSimulator


def sample(sim, component, name, **labels):
    for fam in sim.registries[component].collect():
        for s in fam.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


def test_counters_increase_with_time():
    sim = ClusterSimulator("primary", PROFILES["primary"], seed=1)
    sim.tick(10)
    a = sample(sim, "kubelet", "container_cpu_usage_seconds_total", pod="txn-processor-0")
    sim.tick(10)
    b = sample(sim, "kubelet", "container_cpu_usage_seconds_total", pod="txn-processor-0")
    assert 0 < a < b  # MVP exporter incremented once → rate() was 0


def test_energy_is_consistent_with_power_model():
    sim = ClusterSimulator("primary", PROFILES["primary"], seed=1)
    sim.tick(10)
    idle = sample(sim, "kepler", "kepler_container_cpu_joules_total", pod_name="txn-processor-0", mode="idle")
    dyn = sample(sim, "kepler", "kepler_container_cpu_joules_total", pod_name="txn-processor-0", mode="dynamic")
    cpu_s = sample(sim, "kubelet", "container_cpu_usage_seconds_total", pod="txn-processor-0")
    assert idle == IDLE_WATTS_PER_POD * 10
    assert abs(dyn - WATTS_PER_CORE * cpu_s) < 1e-9


def test_kepler_modes():
    sec = ClusterSimulator("secondary", PROFILES["secondary"], seed=1)
    sec.tick(5)
    text = generate_latest(sec.registries["kepler"]).decode()
    assert "kepler_node_cpu_joules_total{" in text
    assert "kepler_container_cpu_joules_total{" not in text  # node_only mode


def test_workload_down_and_scale():
    sim = ClusterSimulator("primary", PROFILES["primary"], seed=1)
    sim.set_workload("ecommerce", "frontend", up=False)
    assert sample(sim, "kube-state-metrics", "kube_deployment_status_replicas_available", deployment="frontend") == 0
    sim.set_workload("ecommerce", "frontend", up=True, replicas=4)
    assert sample(sim, "kube-state-metrics", "kube_deployment_spec_replicas", deployment="frontend") == 4
    assert sample(sim, "kube-state-metrics", "kube_pod_container_resource_requests", pod="frontend-3", resource="cpu") == 0.1


def test_component_and_cluster_serving_flags():
    sim = ClusterSimulator("primary", PROFILES["primary"])
    sim.set_component("kepler", False)
    assert not sim.serving("kepler") and sim.serving("kubelet")
    sim.cluster_up = False
    assert not sim.serving("kubelet")


def test_unlabelled_pod_has_no_app_label():
    sim = ClusterSimulator("primary", PROFILES["primary"])
    text = generate_latest(sim.registries["kube-state-metrics"]).decode()
    line = [l for l in text.splitlines() if l.startswith("kube_pod_labels") and "trading-debug-0" in l][0]
    assert 'label_app=""' in line  # Prometheus treats an empty label as absent
