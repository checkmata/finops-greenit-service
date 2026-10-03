"""V4 read path: PromQL generated for a Deployment's pods (regression: invalid '\\-' escape → HTTP 400)."""

import pytest

from app.adapters.outbound.prometheus_observation import PrometheusObservation, _pods


class FakeProm:
    def __init__(self, answers):
        self.queries, self.answers = [], answers

    async def _query(self, q):
        self.queries.append(q)
        return self.answers.get(q.split("(")[0].split("{")[0].strip(), [])


def test_pod_regex_is_valid_promql():
    assert _pods("txn-processor") == "txn-processor-[a-z0-9]+-[a-z0-9]{5}"
    assert _pods("a.b") == "a\\\\.b-[a-z0-9]+-[a-z0-9]{5}"
    with pytest.raises(ValueError):
        _pods('x"}) or vector(1')                         # never injected into PromQL


@pytest.mark.asyncio
async def test_container_resources_and_replicas_parsing():
    req = [{"metric": {"container": "txn-processor", "resource": "cpu"}, "value": [0, "0.4"]},
           {"metric": {"container": "txn-processor", "resource": "memory"}, "value": [0, "268435456"]}]
    prom = FakeProm({"min by": req, "kube_deployment_spec_replicas": [{"metric": {}, "value": [0, "2"]}],
                     "kube_deployment_status_replicas_available": [{"metric": {}, "value": [0, "1"]}]})
    obs = PrometheusObservation(prom)
    res = await obs.container_resources("banking", "txn-processor")
    assert res["txn-processor"]["cpu_request"] == 0.4 and res["txn-processor"]["memory_request"] == 268435456.0
    assert await obs.replicas("banking", "txn-processor") == (2, 1)
    assert all("\\-" not in q for q in prom.queries)
