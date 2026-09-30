"""
Domain models for the multicluster view (V1).

IMPORTANT wording (see docs/MULTICLUSTER.md):
- Kubernetes/OpenShift orchestrates workloads INSIDE one cluster.
- A fleet manager (OpenShift ACM, GitOps/Argo CD) manages SEVERAL clusters.
- This service does neither: it OBSERVES each cluster through its own Prometheus
  and computes a failover *decision*. It never moves traffic by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ClusterRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


@dataclass(frozen=True)
class DeploymentAvailability:
    """
    Replica availability of one workload on one cluster.

    Source (kube-state-metrics, metrics-reference.md FinOps P1):
    - kube_deployment_spec_replicas            → desired
    - kube_deployment_status_replicas_available → available
    """

    namespace: str
    workload: str
    desired: int
    available: int

    @property
    def is_serving(self) -> bool:
        return self.available > 0

    @property
    def is_degraded(self) -> bool:
        return 0 < self.available < self.desired


@dataclass(frozen=True)
class ClusterState:
    """
    What the service observed about one cluster during a health check.

    - reachable: its Prometheus answered
    - control_plane_metrics_ok: kube-state-metrics `up == 1` (without it we
      cannot see replica availability, so the cluster is not trusted)
    - declared_unavailable: an operator declared it unavailable (maintenance,
      incident) through the API — takes precedence over metrics
    """

    name: str
    role: ClusterRole
    reachable: bool
    control_plane_metrics_ok: bool
    deployments: tuple[DeploymentAvailability, ...] = ()
    declared_unavailable: bool = False
    declared_reason: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.reachable and self.control_plane_metrics_ok and not self.declared_unavailable

    def deployment(self, namespace: str, workload: str) -> DeploymentAvailability | None:
        for d in self.deployments:
            if d.namespace == namespace and d.workload == workload:
                return d
        return None


class PlacementStatus(str, Enum):
    NOMINAL = "nominal"          # served by the primary, as intended
    DEGRADED = "degraded"        # served by the primary with fewer replicas than desired
    FAILOVER = "failover"        # primary cannot serve → secondary should serve
    OUTAGE = "outage"            # no healthy cluster can serve it


@dataclass(frozen=True)
class WorkloadPlacement:
    namespace: str
    workload: str
    status: PlacementStatus
    serving_cluster: str | None
    reason: str


@dataclass(frozen=True)
class FailoverPlan:
    """Result of plan_failover(): per-cluster health + per-workload decision."""

    active_cluster: str | None
    clusters: tuple[ClusterState, ...]
    placements: tuple[WorkloadPlacement, ...] = field(default_factory=tuple)

    @property
    def failover_active(self) -> bool:
        return any(p.status is PlacementStatus.FAILOVER for p in self.placements)
