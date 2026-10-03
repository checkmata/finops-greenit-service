"""PrometheusMetricsAdapter against the REAL Prometheus of each minikube cluster."""

import pytest

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.domain.models.energy import EnergyAttribution, PowerSource
from tests.infra.conftest import env_or_skip


@pytest.fixture
def adapters():
    primary, secondary = env_or_skip("prometheus")
    return {
        "primary": PrometheusMetricsAdapter(primary, 20, cluster_name="primary"),
        "secondary": PrometheusMetricsAdapter(secondary, 20, cluster_name="secondary"),
    }


@pytest.mark.asyncio
async def test_both_clusters_reachable_and_ksm_up(adapters):
    for name, a in adapters.items():
        assert await a.ping(), name
        q = await a.check_data_quality()
        assert "kube-state-metrics" in {s.value for s in q.healthy_sources}, name
        await a.close()


@pytest.mark.asyncio
async def test_namespace_discovery_sees_real_workloads(adapters):
    ns = await adapters["primary"].list_namespaces()
    assert {"banking", "trading", "ecommerce"} <= set(ns)
    await adapters["primary"].close()


@pytest.mark.asyncio
async def test_real_cpu_usage_and_requests(adapters):
    """Needs the runtime-robust usage query: this cAdvisor exposes pod-level series only."""
    a = adapters["primary"]
    pod = (await a.get_pod_metrics("banking"))[0]
    assert pod.cpu_requested > 0 and pod.memory_requested > 0
    assert pod.cpu_core_hours > 0, "no CPU usage read (container/pod-level series fallback broken?)"
    assert pod.memory_bytes > 0
    assert pod.observation_hours and pod.observation_hours > 0
    assert pod.team == "payments"  # KSM label allow-list works
    await a.close()


@pytest.mark.asyncio
async def test_real_workload_breakdown(adapters):
    a = adapters["primary"]
    wls = {w.workload: w for w in await a.get_workload_metrics("ecommerce")}
    assert {"frontend", "cartservice", "productcatalogservice"} <= set(wls)
    assert all(w.cluster == "primary" for w in wls.values())
    await a.close()


@pytest.mark.asyncio
async def test_real_kepler_energy_provenance_matches_what_kepler_declares(adapters):
    """kind + host RAPL → hardware (package+dram measured); VMs → model (estimated).
    Whatever the environment, the label must follow Kepler's own kepler_node_info."""
    a = adapters["primary"]
    info = await a._query("kepler_node_info")
    declared = {e["metric"].get("components_power_source") for e in info}
    m = await a.get_energy("trading")
    assert m.attribution is EnergyAttribution.CONTAINER
    assert m.total_kwh and m.total_kwh > 0
    if declared & {"rapl-sysfs", "rapl-msr", "rapl"}:
        assert m.power_source is PowerSource.HARDWARE
        assert m.precision == "measured" and m.scope == "cpu_package+dram"
    else:
        assert m.power_source is PowerSource.MODEL
        assert m.precision == "estimated" and m.idle_kwh is None
    await a.close()


@pytest.mark.asyncio
async def test_real_storage_pvc_without_kubelet_stats(adapters):
    a = adapters["primary"]
    pvcs = await a.get_storage_metrics("banking")
    ledger = [p for p in pvcs if p.pvc_name == "ledger-db"]
    assert ledger and ledger[0].is_bound and not ledger[0].has_usage_stats
    await a.close()


@pytest.mark.asyncio
async def test_real_deployment_availability(adapters):
    deps = {(d.namespace, d.workload): d for d in await adapters["secondary"].get_deployment_availability()}
    assert deps[("ecommerce", "frontend")].available >= 1          # warm standby
    assert deps[("ecommerce", "checkoutservice")].desired == 0      # promoted on failover
    await adapters["secondary"].close()
