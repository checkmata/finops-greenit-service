"""V3 on REAL data: TimescaleDB history → features → models → recommendations (+ API)."""

import json
import urllib.request

import pytest
from sqlalchemy import text

from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.ml_service import MLService, MLSettings
from tests.infra.conftest import env_or_skip


@pytest.mark.asyncio
async def test_real_history_gives_point_in_time_series_validated_forecasts_and_sizing():
    (url,) = env_or_skip("database")
    storage = TimescaleAdapter(url)
    try:
        a = await MLService(storage, MLSettings(min_hours=0.25)).analyze_workload("primary", "banking", "txn-processor")
        async with storage._engine.connect() as conn:
            filled = (await conn.execute(text(
                "SELECT count(*) FILTER (WHERE cpu_cores_used_now IS NOT NULL)::float / count(*) FROM workload_snapshots "
                "WHERE cluster='primary' AND namespace='banking' AND time > now() - interval '24 hours'"))).scalar()
    finally:
        await storage.close()
    assert a is not None and a.observed_hours >= 20
    assert filled > 0.9                                       # point-in-time CPU stored (live + backfill)
    fc = a.forecasts["cpu"]
    assert fc is not None and set(fc.test) >= {"naive", "ses", "ar6"} and fc.chosen in fc.test
    rs = a.rightsizing["cpu"]
    assert rs is not None and rs.p95 <= rs.p99 <= rs.recommended_request and 0 < rs.confidence <= 1


def test_ml_api_and_persisted_recommendations():
    (api,) = env_or_skip("api")
    with urllib.request.urlopen(f"{api}/api/ml/anomalies?cluster=primary&hours=48", timeout=120) as r:
        body = json.load(r)
    assert body["count"] == len(body["episodes"]) and all("reason" in e for e in body["episodes"])
    with urllib.request.urlopen(f"{api}/api/ml/workloads/primary/banking/txn-processor", timeout=120) as r:
        w = json.load(r)
    assert w["rightsizing"]["cpu"]["confidence_factors"] and "cpu" in w["forecasts"]
    with urllib.request.urlopen(f"{api}/api/recommendations/history?limit=500&status=pending", timeout=60) as r:
        recs = json.load(r)
    items = recs if isinstance(recs, list) else recs.get("items", recs.get("recommendations", []))
    ml = [x for x in items if str(x.get("category", "")).startswith(("anomaly", "cost_right-sizing_ml"))]
    assert ml, "the leader's ML cycle should have persisted recommendations"
    assert not [x for x in ml if x.get("category") == "cost_right-sizing_ml" and x.get("cluster") == "secondary"
                and x.get("status") == "pending"], "standby clusters must not be right-sized"
