"""
ObservationPort on Prometheus / kube-state-metrics (V4) — the agent's READ path.

Reuses the cluster's PrometheusMetricsAdapter (same URL, auth, TLS): no new credential.
A Deployment's pods are matched by name (<deployment>-<replicaset hash>-<5 chars>), the
naming Kubernetes guarantees for ReplicaSet-managed pods.
"""

from __future__ import annotations

import re

from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.ports.agent_ports import ObservationPort


_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def _pods(deployment: str) -> str:
    """Regex of the Deployment's pods, as a PromQL string literal body.

    Found live: re.escape('txn-processor') → 'txn\\-processor', and '\\-' is an INVALID escape in
    a PromQL string (HTTP 400). Kubernetes names are DNS-1123 ([a-z0-9.-]): validate, then
    escape only '.' — doubled, because PromQL unescapes the string before the regex engine."""
    if not _DNS1123.match(deployment):
        raise ValueError(f"not a Kubernetes name: {deployment!r}")
    return deployment.replace(".", "\\\\.") + "-[a-z0-9]+-[a-z0-9]{5}"


def _name(value: str) -> str:
    if not _DNS1123.match(value):
        raise ValueError(f"not a Kubernetes name: {value!r}")
    return value


class PrometheusObservation(ObservationPort):
    def __init__(self, prom: PrometheusMetricsAdapter) -> None:
        self._prom = prom

    async def container_resources(self, namespace: str, deployment: str) -> dict[str, dict[str, float]]:
        sel = f'namespace="{_name(namespace)}", pod=~"{_pods(deployment)}"'
        out: dict[str, dict[str, float]] = {}
        for metric, kind in (("kube_pod_container_resource_requests", "request"),
                             ("kube_pod_container_resource_limits", "limit")):
            for e in await self._prom._query(f"min by (container, resource) ({metric}{{{sel}}})"):
                m = e["metric"]
                res = {"cpu": "cpu", "memory": "memory"}.get(m.get("resource", ""))
                if res:
                    out.setdefault(m["container"], {})[f"{res}_{kind}"] = float(e["value"][1])
        return out

    async def resource_spread(self, namespace, deployment, container, resource):
        if resource not in ("cpu", "memory"):
            raise ValueError(resource)
        sel = (f'namespace="{_name(namespace)}", pod=~"{_pods(deployment)}", container="{_name(container)}", '
               f'resource="{resource}"')
        lo = await self._prom._query(f"min(kube_pod_container_resource_requests{{{sel}}})")
        hi = await self._prom._query(f"max(kube_pod_container_resource_requests{{{sel}}})")
        if not lo or not hi:
            return None
        return float(lo[0]["value"][1]), float(hi[0]["value"][1])

    async def replicas(self, namespace, deployment):
        sel = f'namespace="{_name(namespace)}", deployment="{_name(deployment)}"'
        spec = await self._prom._query(f"kube_deployment_spec_replicas{{{sel}}}")
        avail = await self._prom._query(f"kube_deployment_status_replicas_available{{{sel}}}")
        if not spec:
            return None
        return int(float(spec[0]["value"][1])), int(float(avail[0]["value"][1])) if avail else 0
