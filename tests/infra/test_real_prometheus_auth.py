"""Authenticated remote Prometheus on REAL infrastructure (basic-auth proxy on the secondary)."""

import os
from pathlib import Path

import pytest

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.cluster_config import Credentials


def _cfg():
    url = os.getenv("INFRA_PROMETHEUS_SECONDARY_AUTH_URL")
    pw_file = Path(os.getenv("INFRA_PROMETHEUS_SECONDARY_PASSWORD_FILE", ".secrets/prom_secondary_password"))
    if not url or not pw_file.is_file():
        pytest.skip("authenticated Prometheus not configured")
    return url, pw_file.read_text().strip()


@pytest.mark.asyncio
async def test_right_credentials_reach_prometheus():
    url, pw = _cfg()
    a = PrometheusMetricsAdapter(url, 10, cluster_name="secondary", credentials=Credentials("basic", "finops", pw))
    assert await a.ping() is True
    assert "ecommerce" in await a.list_namespaces()
    await a.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("creds", [None, Credentials("basic", "finops", "wrong")])
async def test_missing_or_wrong_credentials_mean_unreachable(creds):
    url, _ = _cfg()
    a = PrometheusMetricsAdapter(url, 10, cluster_name="secondary", credentials=creds)
    assert await a.ping() is False     # 401 → PrometheusUnavailableError → not reachable
    await a.close()
