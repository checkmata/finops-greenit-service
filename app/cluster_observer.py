"""
Observe one cluster (reachability, telemetry health, Deployment availability).

Shared by GET /api/clusters and the V2 failover service. Remembers the last
TRUSTED Deployment list (kube-state-metrics up) so that a cluster going dark does
not make its workloads disappear from the plan (V1 finding, real demo).
"""

from __future__ import annotations

import dataclasses

from app.cluster_registry import ClusterRegistry
from app.domain.models.cluster import ClusterState
from app.domain.models.data_quality import TelemetrySource


async def observe_cluster(registry: ClusterRegistry, name: str) -> tuple[ClusterState, dict]:
    """Probe one cluster: reachability, telemetry health, deployment availability."""
    port = registry.port(name)
    details: dict = {"healthy_sources": [], "errors": []}
    try:
        reachable = bool(await port.ping())
    except Exception as exc:
        reachable = False
        details["errors"].append(f"ping: {exc}")

    ksm_ok = False
    deployments = ()
    if reachable:
        try:
            quality = await port.check_data_quality()
            details["healthy_sources"] = sorted(s.value for s in quality.healthy_sources)
            ksm_ok = quality.is_healthy(TelemetrySource.KUBE_STATE_METRICS)
        except Exception as exc:
            details["errors"].append(f"data_quality: {exc}")
        try:
            deployments = tuple(await port.get_deployment_availability())
            if ksm_ok:  # only a trusted observation replaces the remembered list
                registry.remember_deployments(name, deployments)
        except Exception as exc:
            details["errors"].append(f"deployments: {exc}")

    if not deployments and not (reachable and ksm_ok):
        # Prometheus unreachable OR kube-state-metrics down (its series go stale
        # → empty result): the workload list cannot be trusted. Reuse the last
        # observed list; availability is unknown, so they count as NOT serving
        # → failover or outage. (Found with scripts/simulate_failover.sh: with
        # only the "unreachable" case handled, primary-only workloads vanished.)
        # If KSM is up and reports nothing, the workloads really are gone.
        deployments = tuple(
            dataclasses.replace(d, available=0) for d in registry.last_seen_deployments(name)
        )
        details["workloads_source"] = "last_seen" if deployments else "unknown"

    decl = registry.declaration(name)
    state = ClusterState(
        name=name,
        role=registry.role(name),
        reachable=reachable,
        control_plane_metrics_ok=ksm_ok,
        deployments=deployments,
        declared_unavailable=decl is not None and not decl.available,
        declared_reason=decl.reason if decl else None,
    )
    return state, details
