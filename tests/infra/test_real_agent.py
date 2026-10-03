"""V4 agent on the REAL stack — non-destructive checks (the full action E2E with commit,
Argo CD sync, verification and rollback is scripts/e2e/agent_e2e.py → docs/evidence/)."""

import json
import urllib.error
import urllib.request

from tests.infra.conftest import env_or_skip


def _call(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_briefing_is_explained_prioritized_and_policy_checked():
    (api,) = env_or_skip("api")
    code, b = _call(f"{api}/api/agent/briefing?limit=30")
    assert code == 200 and b["count"] > 0
    scores = [i["score"] for i in b["items"]]
    assert scores == sorted(scores, reverse=True) and [i["rank"] for i in b["items"]] == list(range(1, len(scores) + 1))
    for i in b["items"]:
        assert set(i["explanation"]) >= {"why", "metrics", "evolution", "cost_impact", "energy_impact",
                                         "co2_impact", "confidence"}
        assert set(i["priority"]["factors"]) == {"impact", "urgency", "confidence", "cost", "energy", "co2", "risk"}
        if i["proposed_action"]:
            assert i["proposed_action"]["requires_approval"] is True
            assert i["scope"].startswith("primary/")                       # never the standby


def test_standby_recommendation_is_refused_by_policy_without_any_change():
    (api,) = env_or_skip("api")
    _, recs = _call(f"{api}/api/recommendations/history?cluster=secondary&status=pending&limit=500")
    rec = next((r for r in recs if r["category"] == "cost_right-sizing_ml"), None)
    if rec is None:   # standby right-sizing is not even recommended (V3) — nothing to act on
        return
    code, a = _call(f"{api}/api/agent/actions", "POST", {"recommendation_id": rec["id"]})
    assert code == 201 and a["status"] == "rejected_by_policy" and a["execution"] == {}


def test_approval_requires_the_token():
    (api,) = env_or_skip("api")
    code, body = _call(f"{api}/api/agent/actions/00000000-0000-0000-0000-000000000000/approve", "POST",
                       {"approver": "x"})
    assert code == 403
