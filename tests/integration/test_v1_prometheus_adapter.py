"""V1 PrometheusMetricsAdapter: energy hierarchy, workload breakdown, availability, PromQL fixes."""

import httpx
import pytest

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter, PrometheusQueryError
from app.domain.models.energy import EnergyAttribution
from app.domain.models.pod_metric import UNATTRIBUTED_WORKLOAD


def ok(*series):
    return {"status": "success", "data": {"result": [{"metric": m, "value": [1, str(v)]} for m, v in series]}}


EMPTY = ok()


class RouterClient:
    base_url = "http://fake"

    def __init__(self, route):
        self.route = route
        self.queries: list[str] = []

    async def get(self, path, params=None):
        q = params["query"]
        self.queries.append(q)
        payload = self.route(q)

        class R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return payload
        return R()

    async def aclose(self):
        pass


def adapter(route) -> PrometheusMetricsAdapter:
    a = PrometheusMetricsAdapter("http://fake", 1, cluster_name="primary", workload_label="app")
    a._client = RouterClient(route)
    return a


class TestEnergyHierarchy:
    @pytest.mark.asyncio
    async def test_container_level_with_idle_split_when_power_is_hardware(self):
        """Idle is trusted only with HARDWARE power (kepler_node_info says so)."""
        def route(q):
            if q == "kepler_node_info":
                return ok(({"components_power_source": "rapl-sysfs"}, 1))
            if "kepler_container" in q and "by (mode)" in q:
                return ok(({"mode": "idle"}, 7_200_000), ({"mode": "dynamic"}, 10_800_000))
            if "kepler_container" in q:
                return ok(({}, 18_000_000))
            return EMPTY
        m = await adapter(route).get_energy("banking")
        assert m.attribution is EnergyAttribution.CONTAINER
        assert m.total_kwh == pytest.approx(5.0) and m.idle_kwh == pytest.approx(2.0)
        assert m.power_source.value == "hardware" and m.precision == "measured"

    @pytest.mark.asyncio
    async def test_estimator_mode_never_reports_idle_and_is_estimated(self):
        """Observed on real Kepler 0.8 in a VM: idle series exist but are always 0 → unknown."""
        def route(q):
            if q == "kepler_node_info":
                return ok(({"components_power_source": "estimator", "platform_power_source": "none"}, 1))
            if "kepler_container" in q and "by (mode)" in q:
                return ok(({"mode": "idle"}, 0), ({"mode": "dynamic"}, 3_600_000))
            if "kepler_container" in q:
                return ok(({}, 3_600_000))
            return EMPTY
        m = await adapter(route).get_energy("banking")
        assert m.idle_kwh is None
        assert m.power_source.value == "model" and m.precision == "estimated"

    @pytest.mark.asyncio
    async def test_container_query_uses_container_namespace_label_only(self):
        """V1 also matched `{namespace=...}`. Real Kepler (V2) proved that label is the
        KEPLER pod's namespace (ServiceMonitor target label), not the workload's."""
        a = adapter(lambda q: ok(({}, 3_600_000)) if "kepler_container" in q else EMPTY)
        await a.get_energy("banking")
        q = [x for x in a._client.queries if "kepler_container" in x][0]
        assert 'container_namespace="banking"' in q
        assert '{namespace="banking"}' not in q and ', namespace="banking"' not in q

    @pytest.mark.asyncio
    async def test_pod_level_without_idle(self):
        def route(q):
            if "kepler_pod_cpu_joules_total" in q and "by (mode)" not in q:
                return ok(({}, 3_600_000))
            return EMPTY
        m = await adapter(route).get_energy("banking")
        assert m.attribution is EnergyAttribution.POD and m.total_kwh == pytest.approx(1.0)
        assert m.idle_kwh is None

    @pytest.mark.asyncio
    async def test_node_fallback_is_proportional_not_whole_cluster(self):
        def route(q):
            if "kepler_node_cpu_joules_total" in q:
                return ok(({}, 36_000_000))  # 10 kWh for the whole cluster
            if "container_cpu_usage_seconds_total" in q and 'namespace="banking"' in q:
                return ok(({}, 1.0))
            if "container_cpu_usage_seconds_total" in q:
                return ok(({}, 4.0))
            return EMPTY
        m = await adapter(route).get_energy("banking")
        assert m.attribution is EnergyAttribution.NODE_CPU_SHARE
        assert m.total_kwh == pytest.approx(2.5)  # 25 % CPU share, not 10 kWh
        assert m.attribution.precision == "estimated"

    @pytest.mark.asyncio
    async def test_node_energy_but_no_cpu_share_is_unavailable(self):
        m = await adapter(lambda q: ok(({}, 36_000_000)) if "kepler_node" in q else EMPTY).get_energy("x")
        assert m.attribution is EnergyAttribution.UNAVAILABLE and m.total_kwh is None

    @pytest.mark.asyncio
    async def test_legacy_get_energy_kwh_raises_when_unavailable(self):
        with pytest.raises(PrometheusQueryError):
            await adapter(lambda q: EMPTY).get_energy_kwh("x")


class TestPodMetricsV1:
    @pytest.mark.asyncio
    async def test_cadvisor_pod_level_series_excluded(self):
        a = adapter(lambda q: EMPTY)
        await a.get_pod_metrics("banking")
        cpu_q = [q for q in a._client.queries if "container_cpu_usage_seconds_total" in q][0]
        assert 'container!=""' in cpu_q and 'container!="POD"' in cpu_q

    @pytest.mark.asyncio
    async def test_cpu_is_time_average_of_short_rates_not_30d_rate(self):
        """rate(x[30d]) divides by 30 days → young series read ~0 cores (found in the E2E demo)."""
        a = adapter(lambda q: EMPTY)
        await a.get_pod_metrics("banking")
        await a.get_workload_metrics("banking")
        cpu_qs = [q for q in a._client.queries if "container_cpu_usage_seconds_total" in q]
        # V3 adds ONE instantaneous query per workload set (cpu_cores_now, 5-min rate) that
        # feeds anomaly detection / forecasting — never cost. Everything else stays windowed.
        instant = [q for q in cpu_qs if q.startswith("sum by (label_app)")]
        windowed = [q for q in cpu_qs if q not in instant]
        assert len(instant) == 1 and "[5m]" in instant[0] and "30d" not in instant[0]
        assert windowed and all(
            (q.startswith("avg_over_time(") or q.startswith("count_over_time(")) and "[5m]" in q and "[30d:5m]" in q
            for q in windowed
        )
        assert not any("[30d])" in q for q in cpu_qs)

    @pytest.mark.asyncio
    async def test_observation_hours_from_step_count(self):
        a = adapter(lambda q: ok(({}, 288)) if q.startswith("count_over_time(") else EMPTY)
        p = (await a.get_pod_metrics("banking"))[0]
        assert p.observation_hours == pytest.approx(24.0)  # 288 × 5 min

    @pytest.mark.asyncio
    async def test_majority_attribution_and_cluster_stamp(self):
        def route(q):
            if q.startswith("kube_pod_labels"):
                return ok(({"label_team": "a"}, 1), ({"label_team": "b"}, 1), ({"label_team": "b"}, 1))
            return EMPTY
        p = (await adapter(route).get_pod_metrics("ns"))[0]
        assert p.team == "b" and p.cluster == "primary"


class TestWorkloadBreakdown:
    @pytest.mark.asyncio
    async def test_breakdown_with_unattributed_remainder(self):
        def route(q):
            if "sum by (label_app)" in q:
                if "container_cpu_usage" in q:
                    return ok(({"label_app": "ledger"}, 0.5), ({"label_app": "api"}, 1.0))
                if "memory_working_set" in q:
                    return ok(({"label_app": "ledger"}, 100), ({"label_app": "api"}, 200))
                if 'resource="cpu"' in q:
                    return ok(({"label_app": "ledger"}, 2), ({"label_app": "api"}, 1))
                if 'resource="memory"' in q:
                    return ok(({"label_app": "ledger"}, 400), ({"label_app": "api"}, 400))
            if q.startswith("max by (label_app"):
                return ok(({"label_app": "ledger", "label_team": "payments"}, 1))
            # namespace totals
            if "container_cpu_usage" in q:
                return ok(({}, 2.0))
            if "memory_working_set" in q:
                return ok(({}, 300))
            if 'resource="cpu"' in q:
                return ok(({}, 3))
            if 'resource="memory"' in q:
                return ok(({}, 800))
            return EMPTY
        wls = {w.workload: w for w in await adapter(route).get_workload_metrics("banking")}
        assert set(wls) == {"ledger", "api", UNATTRIBUTED_WORKLOAD}
        assert wls["ledger"].team == "payments" and wls["api"].team == "unknown"
        assert wls[UNATTRIBUTED_WORKLOAD].cpu_core_hours == pytest.approx(0.5)
        assert sum(w.cpu_core_hours for w in wls.values()) == pytest.approx(2.0)
        assert all(w.cluster == "primary" for w in wls.values())

    @pytest.mark.asyncio
    async def test_no_labelled_pods_returns_empty(self):
        assert await adapter(lambda q: EMPTY).get_workload_metrics("banking") == []

    def test_ksm_label_name(self):
        from app.adapters.outbound.prometheus_adapter import ksm_label
        assert ksm_label("app.kubernetes.io/name") == "label_app_kubernetes_io_name"


class TestDiscoveryAvailabilityPing:
    @pytest.mark.asyncio
    async def test_list_namespaces(self):
        a = adapter(lambda q: ok(({"namespace": "b"}, 1), ({"namespace": "a"}, 2), ({}, 1)))
        assert await a.list_namespaces() == ["a", "b"]

    @pytest.mark.asyncio
    async def test_deployment_availability(self):
        def route(q):
            if "spec_replicas" in q:
                return ok(({"namespace": "ecommerce", "deployment": "frontend"}, 3))
            return ok(({"namespace": "ecommerce", "deployment": "frontend"}, 1))
        d = (await adapter(route).get_deployment_availability())[0]
        assert (d.desired, d.available, d.is_degraded) == (3, 1, True)

    @pytest.mark.asyncio
    async def test_ping_false_on_transport_error(self):
        a = adapter(lambda q: EMPTY)

        async def boom(path, params=None):
            raise httpx.ConnectError("refused")
        a._client.get = boom
        assert await a.ping() is False
        assert await adapter(lambda q: ok(({}, 1))).ping() is True

    @pytest.mark.asyncio
    async def test_storage_without_kubelet_stats_flagged(self):
        def route(q):
            if "kube_persistentvolumeclaim_info" in q:
                return ok(({"persistentvolumeclaim": "a", "phase": "Bound"}, 1), ({"persistentvolumeclaim": "b", "phase": "Bound"}, 1))
            if "used_bytes" in q or "capacity_bytes" in q:
                return ok(({"persistentvolumeclaim": "a"}, 10))
            return EMPTY
        pvcs = {p.pvc_name: p for p in await adapter(route).get_storage_metrics("ns")}
        assert pvcs["a"].has_usage_stats and not pvcs["b"].has_usage_stats


class TestRealKubeStateMetricsPVC:
    """Shapes observed on real kube-state-metrics v2 + minikube hostPath provisioner (V2)."""

    @pytest.mark.asyncio
    async def test_pvc_known_via_info_bound_via_status_phase_no_kubelet_stats(self):
        def route(q):
            if q.startswith("kube_persistentvolumeclaim_info"):
                return ok(({"persistentvolumeclaim": "ledger-db", "storageclass": "standard"}, 1))
            if q.startswith("kube_persistentvolumeclaim_status_phase"):
                return ok(({"persistentvolumeclaim": "ledger-db", "phase": "Bound"}, 1))
            return EMPTY  # hostPath volumes: no kubelet_volume_stats_*
        pvcs = await adapter(route).get_storage_metrics("banking")
        assert [(p.pvc_name, p.is_bound, p.has_usage_stats) for p in pvcs] == [("ledger-db", True, False)]

        from app.domain.finops_engine import storage_status
        assert storage_status(pvcs).value == "unavailable"  # was wrongly "not_applicable"
