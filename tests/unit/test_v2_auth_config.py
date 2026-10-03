"""V2 remote Prometheus auth + Kubernetes access: credentials only from env/secret files."""

import httpx
import pytest

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter, PrometheusUnavailableError
from app.cluster_config import ClusterConfigError, parse_clusters

BASE = {"name": "p", "role": "primary", "prometheus_url": "http://p:9090"}


def test_basic_auth_from_env(monkeypatch):
    monkeypatch.setenv("PU", "finops"); monkeypatch.setenv("PP", "s3cret")
    c = parse_clusters({"clusters": [{**BASE, "prometheus_auth": {"type": "basic", "username_env": "PU",
                                                                   "password_env": "PP"}}]})[0]
    assert (c.prometheus_auth.kind, c.prometheus_auth.username, c.prometheus_auth.secret) == ("basic", "finops", "s3cret")
    assert "s3cret" not in repr(c.prometheus_auth)            # never leaks in logs


def test_bearer_from_file(tmp_path):
    f = tmp_path / "tok"; f.write_text("abc.def\n")
    c = parse_clusters({"clusters": [{**BASE, "prometheus_auth": {"type": "bearer", "token_file": str(f)},
                                      "prometheus_tls": {"ca_file": "/ca.crt"}}]})[0]
    assert c.prometheus_auth.secret == "abc.def" and c.prometheus_tls.verify == "/ca.crt"


@pytest.mark.parametrize("auth", [
    {"type": "basic", "username": "u", "password": "inline"},     # inline secret forbidden
    {"type": "bearer", "token": "inline"},
    {"type": "bearer"},                                            # nowhere to read it from
    {"type": "oauth"},
])
def test_invalid_auth_rejected(auth):
    with pytest.raises(ClusterConfigError):
        parse_clusters({"clusters": [{**BASE, "prometheus_auth": auth}]})


def test_missing_env_fails_fast(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ClusterConfigError):
        parse_clusters({"clusters": [{**BASE, "prometheus_auth": {"type": "bearer", "token_env": "NOPE"}}]})


def test_kubernetes_access_parsed(tmp_path):
    f = tmp_path / "t"; f.write_text("tok")
    c = parse_clusters({"clusters": [{**BASE, "kubernetes": {"api_url": "https://k:6443", "token_file": str(f),
                                                             "ca_file": "/k.crt"}}]})[0]
    assert c.kubernetes.api_url == "https://k:6443" and c.kubernetes.credentials.secret == "tok"


@pytest.mark.asyncio
async def test_adapter_sends_credentials_and_maps_401_to_unavailable():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(401) if seen["auth"] != "Bearer good" else httpx.Response(
            200, json={"status": "success", "data": {"result": [{"metric": {}, "value": [1, "1"]}]}})

    from app.cluster_config import Credentials
    good = PrometheusMetricsAdapter("http://p", 5, credentials=Credentials("bearer", None, "good"))
    good._client._transport = httpx.MockTransport(handler)
    assert await good.ping() is True and seen["auth"] == "Bearer good"

    bad = PrometheusMetricsAdapter("http://p", 5, credentials=Credentials("bearer", None, "bad"))
    bad._client._transport = httpx.MockTransport(handler)
    with pytest.raises(PrometheusUnavailableError):
        await bad._query("up")
