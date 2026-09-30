"""
Workload profiles of the SIMULATED clusters (local demo only).

Values are illustrative, chosen to exercise every code path of the service:
over-provisioned (banking/txn-processor), well-sized (banking/fraud-scorer),
under-provisioned (trading/order-matcher → throttling), memory pressure
(ecommerce/recommendationservice → OOM risk), unlabelled pod (_unattributed),
PVC with and without kubelet stats, and three Kepler exposure modes.

Service names in `ecommerce` mirror Google's Online Boutique so the simulation
and the real Kubernetes deployment (workloads/ecommerce) share workload names.
"""

MI = 1024 ** 2
GI = 1024 ** 3

LABELS = {
    "banking": {"team": "payments", "cost_center": "cc-banking", "environment": "prod"},
    "trading": {"team": "markets", "cost_center": "cc-trading", "environment": "prod"},
    "ecommerce": {"team": "digital", "cost_center": "cc-ecommerce", "environment": "prod"},
}


def wl(ns, name, replicas, cpu_req, cpu_base, mem_req, mem_base, *, diurnal=0.3, trend_per_day=0.0, labelled=True):
    return dict(namespace=ns, name=name, replicas=replicas, cpu_request=cpu_req, cpu_base=cpu_base,
                mem_request=mem_req, mem_base=mem_base, diurnal=diurnal, trend_per_day=trend_per_day,
                labelled=labelled)


PROFILES = {
    "primary": {
        "kepler_mode": "container_legacy",   # per-container joules WITH idle/dynamic split
        "nodes": {"node-a1": 16, "node-a2": 16},
        "workloads": [
            wl("banking", "txn-processor", 3, 1.0, 0.22, 1 * GI, 300 * MI),
            wl("banking", "fraud-scorer", 2, 0.5, 0.40, 512 * MI, 380 * MI, diurnal=0.15),
            wl("trading", "order-matcher", 2, 0.5, 0.52, 512 * MI, 350 * MI, diurnal=0.4, trend_per_day=0.05),
            wl("trading", "market-feed", 1, 0.25, 0.12, 256 * MI, 120 * MI),
            wl("trading", "trading-debug", 1, 0.25, 0.01, 256 * MI, 40 * MI, labelled=False),
            wl("ecommerce", "frontend", 2, 0.1, 0.07, 64 * MI, 50 * MI, diurnal=0.5),
            wl("ecommerce", "cartservice", 1, 0.2, 0.05, 64 * MI, 40 * MI),
            wl("ecommerce", "checkoutservice", 1, 0.1, 0.03, 64 * MI, 30 * MI),
            wl("ecommerce", "productcatalogservice", 1, 0.1, 0.04, 64 * MI, 30 * MI),
            wl("ecommerce", "recommendationservice", 1, 0.1, 0.06, 220 * MI, 260 * MI),
        ],
        "pvcs": [
            {"namespace": "banking", "name": "ledger-db", "capacity": 20 * GI, "used": 6 * GI, "stats": True},
            {"namespace": "trading", "name": "tick-archive", "capacity": 10 * GI, "used": 7 * GI, "stats": True},
        ],
    },
    "secondary": {
        "kepler_mode": "node_only",          # only node-level joules → estimated attribution
        "nodes": {"node-b1": 8},
        "workloads": [
            wl("banking", "txn-processor", 1, 1.0, 0.03, 1 * GI, 150 * MI, diurnal=0.05),
            wl("ecommerce", "frontend", 1, 0.1, 0.01, 64 * MI, 30 * MI, diurnal=0.05),
            wl("ecommerce", "cartservice", 1, 0.2, 0.01, 64 * MI, 25 * MI, diurnal=0.05),
        ],
        "pvcs": [
            # Replica of the ledger: KSM knows the claim but kubelet stats are missing
            {"namespace": "banking", "name": "ledger-db-replica", "capacity": 20 * GI, "used": 0, "stats": False},
        ],
    },
}
