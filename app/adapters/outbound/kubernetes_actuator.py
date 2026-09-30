"""
Kubernetes failover actuator (V2) — scale Deployments through the Kubernetes API.

WHY httpx and not the `kubernetes` client: httpx is already a dependency and
only two calls are needed (read the scale, patch the scale). No new dependency.

Least privilege: the token belongs to a ServiceAccount that may only get/patch
deployments/scale in the failover namespaces (manifests/failover-actuator-rbac.yaml).

V2.x: the token is short-lived. When configured with token_file, the file is
re-read on every call, so a rotated token (scripts/k8s/make-actuator-secrets.sh)
is used without restarting the service. probe() reports an expired/invalid
identity ahead of an incident (GET /api/failover → "actuators").
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.cluster_config import KubernetesAccess
from app.ports.failover_ports import FailoverActuatorPort


class KubernetesActuator(FailoverActuatorPort):
    def __init__(self, access: KubernetesAccess, timeout_seconds: float = 10) -> None:
        self._static_token = access.credentials.secret
        self._token_file = Path(access.token_file) if access.token_file else None
        self._client = httpx.AsyncClient(
            base_url=access.api_url,
            verify=access.tls.verify,
            timeout=timeout_seconds,
        )

    def _headers(self, extra: dict | None = None) -> dict:
        token = self._static_token
        if self._token_file is not None:
            try:
                token = self._token_file.read_text().strip() or token
            except OSError:
                pass
        return {"Authorization": f"Bearer {token}", **(extra or {})}

    async def probe(self, namespace: str) -> str:
        try:
            r = await self._client.get(f"/apis/apps/v1/namespaces/{namespace}/deployments",
                                       params={"limit": 1}, headers=self._headers())
        except httpx.HTTPError:
            return "unreachable"
        return {200: "ok", 401: "unauthorized", 403: "forbidden"}.get(r.status_code, f"http_{r.status_code}")

    def _path(self, namespace: str, deployment: str) -> str:
        return f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}/scale"

    async def get_replicas(self, namespace: str, deployment: str) -> int | None:
        r = await self._client.get(self._path(namespace, deployment), headers=self._headers())
        # 404: no such Deployment. 403: the namespace is outside the actuator's
        # RBAC (Kubernetes authorizes BEFORE checking existence, so a namespace
        # that does not exist on this cluster answers 403 — observed in V2).
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return int(r.json().get("spec", {}).get("replicas", 0))

    async def scale(self, namespace: str, deployment: str, replicas: int) -> None:
        r = await self._client.patch(
            self._path(namespace, deployment),
            json={"spec": {"replicas": int(replicas)}},
            headers=self._headers({"Content-Type": "application/merge-patch+json"}),
        )
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
