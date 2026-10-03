"""The RUNNING service (docker compose on the real Docker daemon), over HTTP."""

import httpx
import pytest

from tests.infra.conftest import env_or_skip


@pytest.fixture
def api():
    (url,) = env_or_skip("api")
    with httpx.Client(base_url=url, timeout=60) as client:
        yield client


def test_live_and_ready(api):
    assert api.get("/health/live").json() == {"status": "alive"}
    ready = api.get("/health/ready")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["components"]["database"]["status"] == "healthy"
    assert {c["cluster"] for c in body["components"]["prometheus"]["clusters"]} == {"primary", "secondary"}


def test_clusters_plan_from_real_clusters(api):
    body = api.get("/api/clusters").json()
    assert {c["name"] for c in body["clusters"]} == {"primary", "secondary"}
    placements = {(p["namespace"], p["workload"]): p for p in body["placements"]}
    assert ("ecommerce", "frontend") in placements and ("trading", "order-matcher") in placements


def test_finops_live_per_cluster(api):
    a = api.get("/api/finops/namespace/banking", params={"cluster": "primary"}).json()
    assert a["cluster"] == "primary" and a["actual_cost_usd"] > 0
    assert {w["workload"] for w in a["workloads"]} >= {"txn-processor", "fraud-scorer"}


def test_greenit_live_precision_follows_power_source(api):
    """kind + host RAPL → measured (cpu_package+dram); model mode → estimated. Never mixed up."""
    g = api.get("/api/greenit/namespace/trading", params={"cluster": "primary"}).json()
    src = g["energy_source"]
    assert src["attribution"] == "container"
    expected = "measured" if src["power_source"] == "hardware" else "estimated"
    assert src["precision"] == expected == g["carbon_footprint"]["precision"]
    if src["power_source"] == "hardware":
        assert src["scope"] == "cpu_package+dram"


def test_metrics_endpoint_exposes_scheduler(api):
    assert "finops_scheduler_cycles_total" in api.get("/metrics/").text
