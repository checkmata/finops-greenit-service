"""V2.x replicated data: promotion before scaling, split-brain fencing, data-gated failback."""

import pytest

from app.cluster_config import ClusterConfigError, parse_clusters
from app.domain.data_replication import DatabaseStatus, failback_ready, stale_writable
from app.ports.data_replication_port import DataReplicationPort
from tests.unit.test_v2_failover_service import build, fail_primary_for

PRIMARY_DB = DatabaseStatus(True, in_recovery=False, timeline=1, app_login=True)
STANDBY_DB = DatabaseStatus(True, in_recovery=True, timeline=1, app_login=True, replay_lag_seconds=0.4)


# ---------------------------------------------------------------- domain rules
def test_stale_writable_only_when_both_writable_and_older_timeline():
    promoted = DatabaseStatus(True, False, 2, True)
    assert stale_writable(PRIMARY_DB, promoted)                  # old primary came back → fence it
    assert not stale_writable(promoted, PRIMARY_DB)              # the newest copy is never fenced
    assert not stale_writable(PRIMARY_DB, STANDBY_DB)            # normal replication
    assert not stale_writable(DatabaseStatus(True, False, 1, False), promoted)  # already fenced
    assert not stale_writable(DatabaseStatus(False), promoted)   # unreachable: nothing to do


@pytest.mark.parametrize("primary,secondary,ready", [
    (PRIMARY_DB, STANDBY_DB, True),                                           # nominal
    (DatabaseStatus(False), DatabaseStatus(True, False, 2, True), False),     # primary DB down
    (DatabaseStatus(True, False, 1, False), DatabaseStatus(True, False, 2, True), False),  # fenced stale copy
    (DatabaseStatus(True, True, 2, False), DatabaseStatus(True, False, 2, False), False),  # resync in progress
    (DatabaseStatus(True, False, 3, True), DatabaseStatus(True, False, 2, False), True),   # after switchover
])
def test_failback_ready(primary, secondary, ready):
    assert failback_ready(primary, secondary)[0] is ready


# ---------------------------------------------------------------- service
class FakeDb(DataReplicationPort):
    def __init__(self, name, st):
        self.name, self.st, self.calls = name, st, []

    async def status(self):
        return self.st

    async def promote(self):
        self.calls.append("promote")
        self.st = DatabaseStatus(True, False, (self.st.timeline or 1) + 1, self.st.app_login)

    async def fence(self):
        self.calls.append("fence")
        self.st = DatabaseStatus(self.st.reachable, self.st.in_recovery, self.st.timeline, False)


def with_data(mode="auto"):
    svc, primary, act, clock = build(mode)
    pdb, sdb = FakeDb("ledger", PRIMARY_DB), FakeDb("ledger", STANDBY_DB)
    svc.data = {"primary": [pdb], "secondary": [sdb]}
    return svc, primary, act, clock, pdb, sdb


@pytest.mark.asyncio
async def test_failover_promotes_standby_database_before_scaling():
    svc, primary, act, clock, pdb, sdb = with_data()
    await svc.tick()
    pdb.st = DatabaseStatus(False)                      # the whole primary cluster is frozen
    await fail_primary_for(svc, primary, clock, 40)
    ev = next(e for e in svc.store.events if e.kind == "failover")
    assert sdb.calls == ["promote"] and act.calls      # promoted, then scaled
    d = ev.action_detail["data"][0]
    assert d["promoted"] and d["timeline_after"] == 2 and d["replay_lag_seconds_before"] == 0.4


@pytest.mark.asyncio
async def test_returning_old_primary_is_fenced_once_and_failback_waits_for_data():
    svc, primary, act, clock, pdb, sdb = with_data()
    await svc.tick()
    pdb.st = DatabaseStatus(False)
    await fail_primary_for(svc, primary, clock, 40)
    # primary cluster back: its database is writable on the OLD timeline → split-brain risk
    primary.reachable, pdb.st = True, DatabaseStatus(True, False, 1, True)
    for _ in range(20):                                  # 200 s of healthy primary cluster
        await svc.data_guard()
        await svc.tick()
        clock.advance(10)
    assert pdb.calls == ["fence"]                        # fenced once, not every tick
    fences = [e for e in svc.store.events if e.kind == "fence"]
    assert len(fences) == 1 and fences[0].from_cluster == "primary" and fences[0].action_status == "executed"
    assert svc.store.state.active_cluster == "secondary"  # NO failback onto a stale database
    assert "data gate" in svc.last_status
    # resync + controlled switchover done (scripts/dr/ledger-failback.sh): primary newest again
    pdb.st, sdb.st = DatabaseStatus(True, False, 3, True), DatabaseStatus(True, False, 2, False)
    for _ in range(8):
        await svc.tick()
        clock.advance(10)
    assert svc.store.state.active_cluster == "primary"
    assert any(e.kind == "failback" and e.action_status == "executed" for e in svc.store.events)


@pytest.mark.asyncio
async def test_decision_only_mode_records_the_fence_decision_once_without_acting():
    svc, primary, act, clock, pdb, sdb = with_data("decision_only")
    pdb.st, sdb.st = DatabaseStatus(True, False, 1, True), DatabaseStatus(True, False, 2, True)
    for _ in range(3):
        await svc.data_guard()
    assert pdb.calls == []
    assert [e.action_status for e in svc.store.events if e.kind == "fence"] == ["decision_only"]


@pytest.mark.asyncio
async def test_unreachable_standby_database_is_reported_not_hidden():
    svc, primary, act, clock, pdb, sdb = with_data()
    await svc.tick()
    pdb.st, sdb.st = DatabaseStatus(False), DatabaseStatus(False)
    await fail_primary_for(svc, primary, clock, 40)
    ev = next(e for e in svc.store.events if e.kind == "failover")
    assert ev.action_status == "failed" and "standby database unreachable" in ev.action_detail["errors"][0]
    assert act.calls                                     # workloads still promoted (stateless parts serve)


# ---------------------------------------------------------------- configuration
def _cfg(ds):
    return {"clusters": [
        {"name": "primary", "role": "primary", "prometheus_url": "http://p", "data_services": [ds]},
        {"name": "secondary", "role": "secondary", "prometheus_url": "http://s"}]}


def test_data_service_password_only_from_file_or_env(tmp_path, monkeypatch):
    base = {"name": "banking-ledger", "host": "h", "database": "ledger", "username": "postgres", "app_role": "ledger_app"}
    with pytest.raises(ClusterConfigError, match="inline"):
        parse_clusters(_cfg({**base, "password": "x"}))
    f = tmp_path / "pw"
    f.write_text("s3cret\n")
    c = parse_clusters(_cfg({**base, "password_file": str(f)}))[0].data_services[0]
    assert c.password == "s3cret" and c.port == 5432 and "s3cret" not in repr(c)
    monkeypatch.setenv("LEDGER_PW", "fromenv")
    assert parse_clusters(_cfg({**base, "password_env": "LEDGER_PW"}))[0].data_services[0].password == "fromenv"
    with pytest.raises(ClusterConfigError, match="postgres"):
        parse_clusters(_cfg({**base, "kind": "mysql", "password_env": "LEDGER_PW"}))


@pytest.mark.asyncio
async def test_data_guard_loop_fences_only_on_the_leader():
    import asyncio

    from app.adapters.outbound.postgres_leader import AlwaysLeader
    from app.failover_service import run_data_guard_loop
    svc, primary, act, clock, pdb, sdb = with_data()
    pdb.st, sdb.st = DatabaseStatus(True, False, 1, True), DatabaseStatus(True, False, 2, True)

    class Follower(AlwaysLeader):
        @property
        def is_leader(self):
            return False
    for leader, expected in ((Follower(), []), (AlwaysLeader(), ["fence"])):
        task = asyncio.create_task(run_data_guard_loop(svc, leader, 0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        assert pdb.calls == expected
