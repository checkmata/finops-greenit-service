#!/usr/bin/env python3
"""
V2.x — REAL disaster-recovery scenario on the two kind clusters, with measurements.

    NORMAL → primary cluster frozen (docker pause: API server, kubelet, Prometheus,
    workloads AND the primary ledger database stop at once) → detection → hysteresis
    → failover (ledger promoted, workloads scaled on the standby) → service checks
    → primary back (docker unpause) → stale ledger fenced → RPO / consistency measured.

The failback (data resync + controlled switchover + workload failback) is a
separate, explicit step: scripts/dr/ledger-failback.sh.

Measured, never assumed:
    detection time   pause → controller's primary_unhealthy_since
    failover time    pause → failover event executed
    recovery time    pause → all standby Deployments Available
    RTO (banking)    pause → first ledger commit accepted on the secondary
    RTO (ecommerce)  pause → secondary frontend answers HTTP 200
    replication lag  standby replay lag right before the pause and at promotion
    RPO              ledger rows committed on the primary before the freeze that are
                     missing on the promoted copy (count + time window)
    consistency      checksum of the rows both copies have in common
    split-brain      rows accepted by the old primary after the freeze (before fencing)

Usage:  scripts/dr/dr_scenario.py [evidence.log]      (requires FAILOVER_MODE=auto)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "http://127.0.0.1:8080"          # IPv4: Docker's IPv6 port forwarding hangs on this host
ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / f"docs/evidence/dr-scenario-{datetime.now():%Y-%m-%d}.log"
T0: float = 0.0
RESULTS: dict[str, object] = {}


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
    print(line, flush=True)
    with OUT.open("a") as f:
        f.write(line + "\n")


def since_t0() -> float:
    return round(time.time() - T0, 1)


def sh(*args: str, check: bool = True, timeout: int = 60) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout).stdout.strip()


def kubectl(cluster: str, *args: str, check: bool = True, timeout: int = 60) -> str:
    return sh("kubectl", "--context", f"kind-{cluster}", *args, check=check, timeout=timeout)


def sql(cluster: str, query: str) -> str:
    return kubectl(cluster, "-n", "banking", "exec", "ledger-db-0", "--", "psql", "-U", "postgres",
                   "-d", "ledger", "-Atc", query, timeout=30)


def api() -> dict:
    with urllib.request.urlopen(f"{API}/api/failover", timeout=30) as r:
        return json.load(r)


def wait(label: str, cond, timeout: float, every: float = 1.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    raise TimeoutError(label)


def standby_deployments() -> list[tuple[str, str, int, int]]:
    out = []
    for ns in ("banking", "ecommerce", "trading"):
        for line in kubectl("secondary", "-n", ns, "get", "deploy", "--no-headers", "-o",
                            "custom-columns=N:.metadata.name,R:.spec.replicas,A:.status.availableReplicas").splitlines():
            n, r, a = line.split()
            out.append((ns, n, int(r), 0 if a == "<none>" else int(a)))
    return out


def iso(ts: str | None) -> float | None:
    return datetime.fromisoformat(ts).timestamp() if ts else None


def main() -> int:
    global T0
    OUT.write_text("")
    log("== DR scenario — REAL (kind primary/secondary, ledger streaming replication, GitOps-managed)")
    st = api()
    assert st["mode"] == "auto", "requires FAILOVER_MODE=auto"
    assert st["state"]["active_cluster"] == "primary", "start from the nominal state"
    log(f"policy: failure {st['policy']['failure_threshold_seconds']} s / recovery {st['policy']['recovery_threshold_seconds']} s")

    # ---------------- 1. NORMAL
    rep = sql("primary", "SELECT state||' '||sync_state||' replay_lag='||coalesce(replay_lag::text,'0') FROM pg_stat_replication")
    lag_before = sql("secondary", "SELECT coalesce(EXTRACT(EPOCH FROM now()-pg_last_xact_replay_timestamp()),0)::numeric(10,3)")
    counts = {c: sql(c, "SELECT count(*)||' rows, max id '||max(id) FROM ledger") for c in ("primary", "secondary")}
    log(f"1. NORMAL  replication: {rep} | standby replay lag {lag_before}s | primary {counts['primary']} | standby {counts['secondary']}")
    log("   standby deployments (ns/name replicas/available): "
        + ", ".join(f"{ns}/{n} {r}/{a}" for ns, n, r, a in standby_deployments()))
    RESULTS["replication_lag_before_s"] = float(lag_before)

    # ---------------- 2. OUTAGE
    T0 = time.time()
    sh("docker", "pause", "primary-control-plane")
    log(f"2. PRIMARY FROZEN (docker pause primary-control-plane) at t0={datetime.fromtimestamp(T0, timezone.utc):%H:%M:%S.%f}"[:-3])

    try:
        st = wait("detection", lambda: (s := api())["state"]["primary_unhealthy_since"] and s, 180)
        det = iso(st["state"]["primary_unhealthy_since"]) - T0
        RESULTS["detection_s"] = round(det, 1)
        log(f"3. DETECTION  controller marks primary unhealthy {det:.1f}s after t0 → hysteresis starts")

        st = wait("failover", lambda: (s := api())["state"]["active_cluster"] == "secondary" and s, 240)
        ev = next(e for e in st["events"] if e["kind"] == "failover")
        RESULTS["failover_s"] = round(iso(ev["at"]) - T0, 1)
        log(f"4. FAILOVER  event {ev['action_status']} at t0+{RESULTS['failover_s']}s | {ev['reason']}")
        RESULTS["failover_event_status"] = ev["action_status"]
        for d in ev["action_detail"].get("data", []):
            # replay_lag_seconds_before = now() - last replayed commit: with the primary frozen,
            # this is the AGE of the last replicated transaction (≈ time since the freeze),
            # not a replication delay. The real lag is measured in step 1 (and RPO in step 9).
            log(f"   data: {d['service']} promoted={d['promoted']} last replicated commit {d['replay_lag_seconds_before']}s old "
                f"(lsn {d['lsn_before']}) → timeline {d['timeline_after']} writable={d['writable_after']}")
        log(f"   scaled: {len(ev['action_detail'].get('scaled', []))} deployments "
            f"{[c['namespace'] + '/' + c['deployment'] for c in ev['action_detail'].get('scaled', [])]}")
        log(f"   not_replicated: {ev['action_detail'].get('not_replicated')} errors: {ev['action_detail'].get('errors')}")

        if ev["action_status"] != "executed":
            log("   FAIL: the failover action did not fully execute — see errors above")
        # ---------------- 5. RECOVERY — only the Deployments this failover actually scaled count
        targets = {(c["namespace"], c["deployment"]): c["to"] for c in ev["action_detail"].get("scaled", [])}
        def recovered() -> bool:
            deps = {(ns, n): (r, a) for ns, n, r, a in standby_deployments()}
            return bool(targets) and all(deps.get(k, (0, 0))[1] >= to for k, to in targets.items())
        wait("scaled deployments available", recovered, 420, 2)
        RESULTS["recovery_s"] = since_t0()
        RESULTS["recovered_workloads"] = len(targets)
        deps = standby_deployments()
        log(f"5. WORKLOAD RECOVERY  the {len(targets)} Deployments scaled by the failover are Available at "
            f"t0+{RESULTS['recovery_s']}s: " + ", ".join(f"{ns}/{n} {a}/{r}" for ns, n, r, a in deps if (ns, n) in targets))

        first = wait("ledger write on secondary", lambda: sql(
            "secondary", f"SELECT EXTRACT(EPOCH FROM min(created_at)) FROM ledger WHERE origin='secondary' "
            f"AND created_at > to_timestamp({T0})") or None, 300, 2)
        RESULTS["rto_banking_s"] = round(float(first) - T0, 1)
        log(f"6. SERVICE  banking: first ledger commit on the promoted secondary at t0+{RESULTS['rto_banking_s']}s")
        code = wait("frontend", lambda: (c := kubectl("secondary", "-n", "ecommerce", "run", f"probe-{int(time.time())}",
                                                      "--rm", "-i", "--restart=Never", "--image=curlimages/curl:8.10.1",
                                                      "-q", "--", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                                      "http://frontend", check=False, timeout=90)) == "200" and c, 300, 3)
        RESULTS["rto_ecommerce_s"] = since_t0()
        log(f"   ecommerce: frontend on secondary HTTP {code} (checked at t0+{RESULTS['rto_ecommerce_s']}s)")
        n_sec = sql("secondary", f"SELECT count(*) FROM ledger WHERE origin='secondary' AND created_at > to_timestamp({T0})")
        log(f"   trading: {[f'{n} {a}/{r}' for ns, n, r, a in deps if ns == 'trading']} (manifests replicated by GitOps; "
            f"order book in memory, NOT replicated)")
        log(f"   ledger rows written by the secondary so far: {n_sec}")
        time.sleep(20)
    finally:
        # ---------------- 7. PRIMARY BACK
        sh("docker", "unpause", "primary-control-plane")
        t_back = time.time()
        log(f"7. PRIMARY BACK (docker unpause) at t0+{round(t_back - T0, 1)}s")

    # stale ledger must be fenced by the controller (split-brain prevention)
    ev = wait("fence", lambda: next((e for e in api()["events"] if e["kind"] == "fence"
                                     and iso(e["at"]) > T0), None), 180, 1)
    fenced_at = time.time()
    RESULTS["fence_within_s"] = round(fenced_at - t_back, 1)   # upper bound (poll every 1 s)
    log(f"8. FENCE  {ev['action_status']} {ev['reason']} (observed ≤ {RESULTS['fence_within_s']}s after the unpause)")
    login = sql("primary", "SELECT rolcanlogin FROM pg_roles WHERE rolname='ledger_app'")
    log(f"   old primary: ledger_app can login = {login}")

    # ---------------- 9. RPO / consistency (old primary is intact: frozen, not destroyed)
    t0 = f"to_timestamp({T0})"
    last_rep = sql("secondary", f"SELECT max(id)||'|'||EXTRACT(EPOCH FROM max(created_at)) FROM ledger WHERE origin='primary'")
    last_id, last_ts = last_rep.split("|")
    before = int(sql("primary", f"SELECT count(*) FROM ledger WHERE origin='primary' AND created_at <= {t0}"))
    on_sec = int(sql("secondary", f"SELECT count(*) FROM ledger WHERE origin='primary' AND created_at <= {t0}"))
    lost = before - on_sec
    zombie = int(sql("primary", f"SELECT count(*) FROM ledger WHERE origin='primary' AND created_at > {t0}"))
    last_primary_ts = float(sql("primary", f"SELECT EXTRACT(EPOCH FROM max(created_at)) FROM ledger "
                                           f"WHERE origin='primary' AND created_at <= {t0}"))
    RESULTS.update(rpo_rows_lost=lost, rpo_window_s=round(max(0.0, last_primary_ts - float(last_ts)), 3),
                   split_brain_rows_on_old_primary=zombie)
    log(f"9. RPO  rows committed on primary before the freeze: {before}; present on promoted copy: {on_sec}; "
        f"LOST = {lost} (last replicated commit id {last_id}, "
        f"{RESULTS['rpo_window_s']}s before the last primary commit)")
    log(f"   rows accepted by the OLD primary after the freeze and before fencing (discarded at resync): {zombie}")
    q = (f"SELECT md5(string_agg(id||':'||origin||':'||amount||':'||src||':'||dst, ',' ORDER BY id)) "
         f"FROM ledger WHERE origin='primary' AND id <= {last_id}")
    h1, h2 = sql("primary", q), sql("secondary", q)
    RESULTS["consistency"] = "PASS" if h1 == h2 else "FAIL"
    log(f"   CONSISTENCY  checksum of the {on_sec} common rows: primary {h1[:12]}… secondary {h2[:12]}… → {RESULTS['consistency']}")

    st = api()
    log(f"10. STATE  active={st['state']['active_cluster']} | leader status: {st['this_replica']['last_status']}")
    log("    failback is gated on data: run scripts/dr/ledger-failback.sh (resync + controlled switchover)")
    log("RESULTS " + json.dumps(RESULTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
