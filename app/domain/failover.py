"""
Failover decision engine — pure function, no I/O (V1).

What it does:
    Given what we observed on each cluster (ClusterState), decide for every
    workload which cluster SHOULD serve it: primary if it can, otherwise a
    healthy secondary that runs a standby copy, otherwise OUTAGE.

What it does NOT do (on purpose):
    - It does not move traffic (DNS/GSLB/ingress) nor scale anything.
    - In production that is the job of the fleet tooling (ACM policies,
      GitOps, global load balancer). This function produces the decision and
      its justification so it can be shown to a human and audited.

Fail-back is implicit: the decision is stateless, so as soon as the primary is
healthy and serving again, placement returns to NOMINAL. Hysteresis (avoid
flapping) is a V2 item — see docs/FAILOVER.md.
"""

from __future__ import annotations

from app.domain.models.cluster import (
    ClusterRole,
    ClusterState,
    FailoverPlan,
    PlacementStatus,
    WorkloadPlacement,
)


def _primary(clusters: tuple[ClusterState, ...]) -> ClusterState | None:
    return next((c for c in clusters if c.role is ClusterRole.PRIMARY), None)


def plan_failover(clusters: list[ClusterState] | tuple[ClusterState, ...]) -> FailoverPlan:
    clusters = tuple(clusters)
    primary = _primary(clusters)
    secondaries = tuple(c for c in clusters if c.role is ClusterRole.SECONDARY)

    if primary is not None and primary.is_healthy:
        active = primary.name
    else:
        active = next((c.name for c in secondaries if c.is_healthy), None)

    # Every workload seen on any cluster is a candidate.
    keys = sorted({(d.namespace, d.workload) for c in clusters for d in c.deployments})
    placements: list[WorkloadPlacement] = []

    for ns, wl in keys:
        p_dep = primary.deployment(ns, wl) if primary else None
        if primary is not None and primary.is_healthy and p_dep is not None and p_dep.is_serving:
            if p_dep.is_degraded:
                placements.append(WorkloadPlacement(
                    ns, wl, PlacementStatus.DEGRADED, primary.name,
                    f"Primary serves {p_dep.available}/{p_dep.desired} replicas.",
                ))
            else:
                placements.append(WorkloadPlacement(
                    ns, wl, PlacementStatus.NOMINAL, primary.name, "Primary healthy and serving.",
                ))
            continue

        why_primary = _why_primary_cannot_serve(primary, p_dep)
        standby = next(
            (
                c for c in secondaries
                if c.is_healthy and (d := c.deployment(ns, wl)) is not None and d.is_serving
            ),
            None,
        )
        if standby is not None:
            placements.append(WorkloadPlacement(
                ns, wl, PlacementStatus.FAILOVER, standby.name,
                f"{why_primary} Standby on '{standby.name}' is healthy and serving.",
            ))
        else:
            placements.append(WorkloadPlacement(
                ns, wl, PlacementStatus.OUTAGE, None,
                f"{why_primary} No healthy secondary runs this workload.",
            ))

    return FailoverPlan(active_cluster=active, clusters=clusters, placements=tuple(placements))


def _why_primary_cannot_serve(primary: ClusterState | None, dep) -> str:
    if primary is None:
        return "No primary cluster configured."
    if primary.declared_unavailable:
        return f"Primary '{primary.name}' declared unavailable ({primary.declared_reason or 'no reason'})."
    if not primary.reachable:
        return f"Primary '{primary.name}' Prometheus unreachable."
    if not primary.control_plane_metrics_ok:
        return f"Primary '{primary.name}' kube-state-metrics down (availability unknown)."
    if dep is None:
        return f"Workload not deployed on primary '{primary.name}'."
    return f"Workload has 0/{dep.desired} available replicas on primary '{primary.name}'."
