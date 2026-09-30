"""V2: long-term roll-up rules and per-workload energy precision."""

from datetime import datetime, timezone

import pytest

from app.domain.history import rollup
from app.domain.models.energy import PowerSource, WorkloadEnergy
from app.domain.models.snapshot import AggregateRow

B = datetime(2026, 9, 29, tzinfo=timezone.utc)


def row(cluster, ns, wl="*", cost=10.0, kwh=1.0, co2=0.5, bucket=B):
    return AggregateRow(bucket, cluster, ns, wl, cost, 20.0, 10.0, 5.0, 1.0, 2.0, None, kwh, co2, 12)


def test_rollup_by_cluster_sums_series_and_keeps_none():
    pts = rollup([row("primary", "banking"), row("primary", "trading", cost=5, kwh=None, co2=None),
                  row("secondary", "banking", cost=1)], ["cluster"])
    by = {p.group["cluster"]: p.values for p in pts}
    assert by["primary"]["cost_usd"] == 15 and by["primary"]["energy_kwh"] == 1.0
    assert by["secondary"]["cost_usd"] == 1
    assert by["primary"]["storage_cost_usd"] is None      # never measured → never 0


def test_rollup_by_workload_and_validation():
    pts = rollup([row("primary", "ecommerce", "frontend"), row("primary", "ecommerce", "cart", kwh=2)],
                 ["namespace", "workload"])
    assert {p.group["workload"]: p.values["energy_kwh"] for p in pts} == {"frontend": 1.0, "cart": 2.0}
    with pytest.raises(ValueError):
        rollup([], ["pod"])


def test_workload_energy_precision():
    assert WorkloadEnergy("ns", "w", 1.0, PowerSource.HARDWARE, "cpu_package+dram").precision == "measured"
    assert WorkloadEnergy("ns", "w", 1.0, PowerSource.MODEL, "platform").precision == "estimated"
    assert WorkloadEnergy("ns", "w", None, PowerSource.UNKNOWN, "?").precision == "unavailable"
