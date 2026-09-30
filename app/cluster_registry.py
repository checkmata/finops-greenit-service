"""
ClusterRegistry — the set of observed clusters and their MetricsPort (V1).

WHY a registry:
- One PrometheusMetricsAdapter per cluster, created ONCE at startup and shared
  by the API and the scheduler. The MVP created a new HTTP client and a new DB
  pool on every request and never closed them.
- Holds operator declarations ("cluster X is unavailable: maintenance").
  V1 keeps them in memory (lost on restart) — persisted in V2.

Framework-free: it only knows ports and domain types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.domain.models.cluster import ClusterRole, DeploymentAvailability
from app.ports.metrics_port import MetricsPort


class UnknownClusterError(KeyError):
    pass


@dataclass(frozen=True)
class Declaration:
    available: bool
    reason: str | None


class ClusterRegistry:
    def __init__(self, roles: Mapping[str, ClusterRole], ports: Mapping[str, MetricsPort]) -> None:
        if set(roles) != set(ports):
            raise ValueError("roles and ports must describe the same clusters")
        self._roles = dict(roles)
        self._ports = dict(ports)
        self._declarations: dict[str, Declaration] = {}
        # Last deployments successfully observed per cluster. WHY: when a
        # cluster becomes unreachable we can no longer list its workloads; without
        # memory, workloads that ran ONLY there would vanish from the failover
        # plan instead of being reported as OUTAGE.
        self._last_seen: dict[str, tuple[DeploymentAvailability, ...]] = {}

    @property
    def names(self) -> list[str]:
        # Primary first, then alphabetical — stable ordering for APIs/dashboards.
        return sorted(self._roles, key=lambda n: (self._roles[n] is not ClusterRole.PRIMARY, n))

    @property
    def primary(self) -> str:
        return self.names[0]

    def role(self, name: str) -> ClusterRole:
        self._check(name)
        return self._roles[name]

    def port(self, name: str | None = None) -> MetricsPort:
        """Port of `name`, or of the primary cluster when name is None."""
        name = name or self.primary
        self._check(name)
        return self._ports[name]

    def ports(self) -> dict[str, MetricsPort]:
        return {n: self._ports[n] for n in self.names}

    def declare(self, name: str, available: bool, reason: str | None = None) -> None:
        self._check(name)
        if available:
            self._declarations.pop(name, None)
        else:
            self._declarations[name] = Declaration(False, reason)

    def declaration(self, name: str) -> Declaration | None:
        return self._declarations.get(name)

    def remember_deployments(self, name: str, deployments: tuple[DeploymentAvailability, ...]) -> None:
        self._check(name)
        self._last_seen[name] = tuple(deployments)

    def last_seen_deployments(self, name: str) -> tuple[DeploymentAvailability, ...]:
        return self._last_seen.get(name, ())

    def _check(self, name: str) -> None:
        if name not in self._roles:
            raise UnknownClusterError(name)
