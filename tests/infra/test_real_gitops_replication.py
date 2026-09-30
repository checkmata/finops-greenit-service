"""V2.x on the REAL kind clusters: GitOps state, workload replication, ledger streaming replication.

Read-only checks (the destructive scenarios are scripts/dr/*.sh with evidence in docs/evidence/).
"""

import json
import subprocess
from pathlib import Path

import pytest

from app.adapters.outbound.postgres_replication import PostgresReplicationAdapter
from tests.infra.conftest import env_or_skip


def kubectl(ctx: str, *args: str) -> str:
    return subprocess.run(["kubectl", "--context", ctx, *args], capture_output=True, text=True,
                          check=True, timeout=60).stdout


def test_argocd_applications_generated_from_ocm_placement_are_synced_and_healthy():
    env_or_skip("kube")
    decisions = json.loads(kubectl("kind-secondary", "-n", "argocd", "get", "placementdecisions", "-o", "json"))
    clusters = {d["clusterName"] for item in decisions["items"] for d in item["status"]["decisions"]}
    assert clusters == {"primary", "secondary"}
    apps = json.loads(kubectl("kind-secondary", "-n", "argocd", "get", "applications", "-o", "json"))["items"]
    by = {a["metadata"]["name"]: (a["status"]["sync"]["status"], a["status"]["health"]["status"]) for a in apps}
    expected = {f"{w}-{c}" for w in ("banking", "ecommerce", "trading") for c in clusters} | \
               {f"finops-platform-{c}" for c in clusters}
    assert expected <= set(by)
    assert all(by[n] == ("Synced", "Healthy") for n in expected), by


def test_every_primary_workload_is_declared_on_the_standby():
    env_or_skip("kube")
    for ns in ("banking", "ecommerce", "trading"):
        prim = set(kubectl("kind-primary", "-n", ns, "get", "deploy,statefulset,svc", "-o", "name").split())
        sec = set(kubectl("kind-secondary", "-n", ns, "get", "deploy,statefulset,svc", "-o", "name").split())
        assert prim <= sec, f"{ns}: missing on standby {prim - sec}"


@pytest.mark.asyncio
async def test_ledger_streams_from_primary_to_standby_with_small_lag():
    ph, sh, pw_file = env_or_skip("ledger")
    pw = Path(pw_file).read_text().strip()
    p = PostgresReplicationAdapter("banking-ledger", ph, 30432, "ledger", "postgres", pw, "ledger_app")
    s = PostgresReplicationAdapter("banking-ledger", sh, 30432, "ledger", "postgres", pw, "ledger_app")
    pst, sst = await p.status(), await s.status()
    assert pst.writable and pst.app_login is True
    assert sst.reachable and sst.in_recovery is True
    assert sst.timeline == pst.timeline                    # same lineage after the last failback
    assert sst.replay_lag_seconds is not None and sst.replay_lag_seconds < 5   # txn-processor writes every ~1 s


def test_service_sees_data_and_actuator_ready():
    import urllib.request
    (api,) = env_or_skip("api")
    with urllib.request.urlopen(f"{api}/api/failover", timeout=60) as r:
        body = json.load(r)
    assert body["actuators"] and all(v == "ok" for v in body["actuators"].values()), body["actuators"]
    led = {c: d["banking-ledger"] for c, d in body["data"].items()}
    assert led["primary"]["in_recovery"] is False and led["secondary"]["in_recovery"] is True
