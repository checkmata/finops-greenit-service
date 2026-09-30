"""V2 features on REAL infrastructure: long-term aggregates, CO2 per workload, failover trail."""

import httpx
import pytest

from tests.infra.conftest import env_or_skip


@pytest.fixture
def api():
    (url,) = env_or_skip("api")
    with httpx.Client(base_url=url, timeout=60) as c:
        yield c


@pytest.mark.parametrize("granularity", ["hour", "day", "week", "month"])
def test_long_term_aggregates_by_cluster(api, granularity):
    r = api.get("/api/history/aggregate", params={"granularity": granularity, "group_by": "cluster", "days": 30})
    assert r.status_code == 200, r.text
    clusters = {p["cluster"] for p in r.json()["points"]}
    assert {"primary", "secondary"} <= clusters


def test_workload_comparison_uses_measured_energy(api):
    pts = api.get("/api/history/aggregate", params={"granularity": "hour", "scope": "workload",
                                                    "group_by": "namespace", "cluster": "primary", "days": 1}).json()["points"]
    namespaces = {p["namespace"] for p in pts if p["energy_kwh"]}
    assert {"banking", "trading", "ecommerce"} <= namespaces


def test_live_co2_per_workload_and_pod(api):
    b = api.get("/api/greenit/namespace/ecommerce/workloads", params={"cluster": "primary", "window_seconds": 900}).json()
    wls = {w["workload"]: w for w in b["workloads"]}
    assert {"frontend", "cartservice"} <= set(wls)
    fe = wls["frontend"]
    assert fe["energy_source"] in ("measured", "estimated") and fe["pods"]
    assert abs(sum(p["energy_kwh"] for p in fe["pods"]) - fe["energy_kwh"]) < 1e-9


def test_failover_state_and_audit_trail(api):
    b = api.get("/api/failover").json()
    assert b["state"]["mode"] in ("primary_active", "failed_over")
    kinds = [(e["kind"], e["action_status"]) for e in b["events"]]
    assert ("failover", "executed") in kinds and ("failback", "executed") in kinds   # real demo trail
