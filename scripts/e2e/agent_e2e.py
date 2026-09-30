#!/usr/bin/env python3
"""
V4 — REAL end-to-end run of the whole chain, with evidence:

  workload → Prometheus → TimescaleDB → ML (V3) → recommendation → agent: explain + prioritize
  → propose (policy + offline dry-run) → HUMAN approval (token) → Git commit (agent identity)
  → Argo CD → Kubernetes → verification (Prometheus) → metrics change in TimescaleDB
  → new ML observation → rollback (inverse commit) → verified.

Usage: scripts/e2e/agent_e2e.py [namespace/workload] [resource]   (default banking/txn-processor cpu)
The approval token is read from .secrets/agent_approval_token and never printed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "http://127.0.0.1:8080"
NS, WL = (sys.argv[1] if len(sys.argv) > 1 else "banking/txn-processor").split("/")
RES = sys.argv[2] if len(sys.argv) > 2 else "cpu"
OUT = ROOT / f"docs/evidence/agent-e2e-{datetime.now():%Y-%m-%d}.log"
TOKEN = (ROOT / ".secrets/agent_approval_token").read_text().strip()


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
    print(line, flush=True)
    with OUT.open("a") as f:
        f.write(line + "\n")


def call(method: str, path: str, body: dict | None = None, token: bool = False, timeout: int = 180):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", **({"X-Approval-Token": TOKEN} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def sh(*a: str) -> str:
    return subprocess.run(a, capture_output=True, text=True, timeout=120).stdout.strip()


def live_request() -> str:
    return sh("kubectl", "--context", "kind-primary", "-n", NS, "get", "deploy", WL, "-o",
              f"jsonpath={{.spec.template.spec.containers[0].resources.requests.{RES}}}")


def sql(q: str) -> str:
    return sh("docker", "exec", "finops-greenit-service-timescaledb-1", "sh", "-c",
              f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "{q}"')


def snapshot_request() -> str:
    col = "cpu_cores_requested" if RES == "cpu" else "memory_gb_requested"
    return sql(f"SELECT to_char(time,'HH24:MI:SS')||' '||round({col}::numeric,4)||' requested_cost='||"
               f"round(requested_cost_usd::numeric,2) FROM workload_snapshots WHERE cluster='primary' AND "
               f"namespace='{NS}' AND workload='{WL}' ORDER BY time DESC LIMIT 1")


def wait_action(aid: str, statuses: set[str], timeout: int = 600) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        _, a = call("GET", f"/api/agent/actions/{aid}")
        if a.get("status") in statuses:
            return a
        time.sleep(5)
    raise TimeoutError(statuses)


def main() -> int:
    OUT.write_text("")
    log(f"== V4 agent E2E — REAL — target primary/{NS}/{WL} {RES}")
    log(f"1. BEFORE  live request (kubectl) = {live_request()} | last snapshot: {snapshot_request()}")

    _, b = call("GET", "/api/agent/briefing?cluster=primary&limit=100")
    item = next(i for i in b["items"] if i["scope"] == f"primary/{NS}/{WL}" and i["category"] == "cost_right-sizing_ml"
                and i["explanation"]["why"].startswith(RES))
    rec_id = item["recommendation_ids"][0]
    log(f"2. AGENT briefing: rank {item['rank']}/{b['count']} score {item['score']} — {item['priority']['rationale']}")
    for k in ("why", "metrics", "evolution", "cost_impact", "energy_impact", "co2_impact", "confidence"):
        log(f"   {k}: {item['explanation'][k]}")
    log(f"   factors: {item['priority']['factors']}")

    code, a = call("POST", "/api/agent/actions", {"recommendation_id": rec_id, "requested_by": "e2e-script"})
    log(f"3. PROPOSE → HTTP {code} status={a['status']} proposal: {a['proposal']['current']} → {a['proposal']['proposed']}"
        f" (staged={a['proposal']['staged']}, final target {a['proposal']['final_target']})")
    log(f"   policy: {a['policy']}")
    log("   offline dry-run diff:\n" + "\n".join("      " + l for l in a["dry_run"].get("diff", "").splitlines()))
    code2, a2 = call("POST", "/api/agent/actions", {"recommendation_id": rec_id, "requested_by": "e2e-script"})
    log(f"   idempotence: second proposal → same action id: {a2.get('id') == a['id']} (HTTP {code2})")

    code, r = call("POST", f"/api/agent/actions/{a['id']}/approve", {"approver": "e2e-human"})
    log(f"4. APPROVE without token → HTTP {code} ({r.get('detail')})")
    t0 = time.time()
    code, r = call("POST", f"/api/agent/actions/{a['id']}/approve", {"approver": "e2e-human"}, token=True)
    log(f"   APPROVE with token → HTTP {code} status={r['status']}")
    done = wait_action(a["id"], {"executed", "failed", "rolled_back"})
    log(f"5. EXECUTION → {done['status']} in {time.time() - t0:.0f}s | commit {done['execution'].get('commit', '')[:10]}"
        f" | {done['execution'].get('verification')}")
    log(f"   live request (kubectl) = {live_request()}")
    log("   audit: " + " → ".join(f"{e['event']}({e['actor']})" for e in done["audit"]))
    rev = sh("kubectl", "--context", "kind-secondary", "-n", "argocd", "get", "app", f"{NS}-primary", "-o",
             "jsonpath={.status.sync.revision} {.status.sync.status}")
    log(f"   Argo CD {NS}-primary revision {rev[:10]} {rev.split()[-1] if rev else ''}")
    if done["status"] != "executed":
        return 1

    # metrics change → new observation (next scheduler cycle writes the new request)
    before = snapshot_request()
    end = time.time() + 240
    while time.time() < end and snapshot_request() == before:
        time.sleep(10)
    log(f"6. METRICS CHANGE  TimescaleDB snapshot after the action: {snapshot_request()}")
    code, w = call("GET", f"/api/ml/workloads/primary/{NS}/{WL}")
    rs = w["rightsizing"][RES]
    log(f"7. NEW OBSERVATION  ML now sees request {rs['current_request']:.4g} {rs['unit']} (workload total) → "
        f"{rs['action']} to {rs['recommended_request']:.4g} (confidence {rs['confidence']})")

    code, rb = call("POST", f"/api/agent/actions/{a['id']}/rollback", {"approver": "e2e-human"}, token=True, timeout=900)
    log(f"8. ROLLBACK → HTTP {code} status={rb.get('status')} | {rb.get('execution', {}).get('rollback', {}).get('verification')}")
    log(f"   live request (kubectl) = {live_request()}")
    log("RESULT " + json.dumps({"action": a["id"], "executed": done["status"] == "executed",
                                "rolled_back": rb.get("status") == "rolled_back"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
