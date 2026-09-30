"""V2.x lesson from the real DR run: a rotated short-lived token must be picked up
without restart, and an expired identity must be visible BEFORE an incident."""

import httpx
import pytest

from app.adapters.outbound.kubernetes_actuator import KubernetesActuator
from app.cluster_config import Credentials, KubernetesAccess, TlsConfig


def actuator(token_file, handler):
    a = KubernetesActuator(KubernetesAccess("https://k8s:6443", Credentials("bearer", None, "startup-token"),
                                            TlsConfig(), token_file=str(token_file)))
    a._client._transport = httpx.MockTransport(handler)
    return a


@pytest.mark.asyncio
async def test_rotated_token_file_is_used_on_the_next_call(tmp_path):
    f = tmp_path / "token"
    f.write_text("expired\n")
    seen = []

    def handler(req):
        seen.append(req.headers["Authorization"])
        return httpx.Response(401 if req.headers["Authorization"] == "Bearer expired" else 200,
                              json={"spec": {"replicas": 1}, "items": []})

    a = actuator(f, handler)
    assert await a.probe("banking") == "unauthorized"
    f.write_text("fresh\n")                              # scripts/k8s/make-actuator-secrets.sh rotates it
    assert await a.probe("banking") == "ok"
    assert await a.get_replicas("banking", "txn-processor") == 1
    assert seen == ["Bearer expired", "Bearer fresh", "Bearer fresh"]
    await a.close()


@pytest.mark.asyncio
async def test_probe_reports_forbidden_and_unreachable(tmp_path):
    f = tmp_path / "token"
    f.write_text("t")
    assert await actuator(f, lambda r: httpx.Response(403)).probe("x") == "forbidden"

    def boom(req):
        raise httpx.ConnectError("node frozen")
    assert await actuator(f, boom).probe("x") == "unreachable"
