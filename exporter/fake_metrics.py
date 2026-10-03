"""
Entry point of one SIMULATED cluster (local demo).

    CLUSTER_NAME=primary   CLUSTER_PROFILE=primary   python -m exporter.fake_metrics

Ports (override with *_PORT env vars):
    8000  admin API (JSON)       GET /state · POST /components/<c>/<up|down>
                                 POST /cluster/<up|down>
                                 POST /workloads/<ns>/<name>/<up|down>
                                 POST /workloads/<ns>/<name>/replicas/<n>
                                 POST /workloads/<ns>/<name>/load/<factor>
    9101  kube-state-metrics /metrics
    9102  node-exporter      /metrics
    9103  kepler             /metrics
    9104  kubelet (cAdvisor) /metrics

A component that is "down" answers 503, so Prometheus records up{job=...}=0 —
exactly what the service's DataQuality check reads on a real cluster.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from exporter.profiles import PROFILES
from exporter.simulator import ClusterSimulator

PORTS = {
    "kube-state-metrics": int(os.getenv("KSM_PORT", "9101")),
    "node-exporter": int(os.getenv("NODE_EXPORTER_PORT", "9102")),
    "kepler": int(os.getenv("KEPLER_PORT", "9103")),
    "kubelet": int(os.getenv("KUBELET_PORT", "9104")),
}
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8000"))


def metrics_handler(sim: ClusterSimulator, component: str):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not sim.serving(component):
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"component down (simulated)\n")
                return
            with sim.lock:
                body = generate_latest(sim.registries[component])
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return H


def admin_handler(sim: ClusterSimulator):
    class H(BaseHTTPRequestHandler):
        def _json(self, code: int, obj) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._json(200, sim.state()) if self.path.rstrip("/") in ("", "/state") else self._json(404, {})

        def do_POST(self):  # noqa: N802
            parts = [p for p in self.path.split("/") if p]
            try:
                if parts[:1] == ["cluster"] and len(parts) == 2:
                    sim.cluster_up = parts[1] == "up"
                elif parts[:1] == ["components"] and len(parts) == 3:
                    sim.set_component(parts[1], parts[2] == "up")
                elif parts[:1] == ["workloads"] and len(parts) == 4:
                    sim.set_workload(parts[1], parts[2], up=parts[3] == "up")
                elif parts[:1] == ["workloads"] and len(parts) == 5 and parts[3] == "replicas":
                    sim.set_workload(parts[1], parts[2], replicas=int(parts[4]))
                elif parts[:1] == ["workloads"] and len(parts) == 5 and parts[3] == "load":
                    sim.set_workload(parts[1], parts[2], load=float(parts[4]))
                else:
                    return self._json(404, {"error": "unknown admin path"})
            except (KeyError, ValueError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, sim.state())

        def log_message(self, *args):
            pass
    return H


def serve(port: int, handler) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    name = os.getenv("CLUSTER_NAME", "primary")
    profile = PROFILES[os.getenv("CLUSTER_PROFILE", name)]
    sim = ClusterSimulator(name, profile, day_seconds=float(os.getenv("SIM_DAY_SECONDS", "3600")),
                           seed=int(os.getenv("SIM_SEED", "42")))
    for comp, port in PORTS.items():
        serve(port, metrics_handler(sim, comp))
    serve(ADMIN_PORT, admin_handler(sim))
    print(f"[{name}] simulated cluster up — admin :{ADMIN_PORT}, components {PORTS}", flush=True)
    tick = float(os.getenv("SIM_TICK_SECONDS", "1"))
    while True:
        sim.tick(tick)
        time.sleep(tick)


if __name__ == "__main__":
    main()
