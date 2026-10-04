"""
Kubernetes TokenRequest issuer + file credential store (V2.x hardening).

TokenRequest = POST /api/v1/namespaces/{ns}/serviceaccounts/{name}/token — the native,
GA (Kubernetes ≥ 1.22) API that `kubectl create token` uses. Tokens are bound to the
ServiceAccount (name + UID), time-limited (min 600 s) and never stored by the cluster.

The store is a directory on a WRITABLE volume (Docker named volume `actuator-credentials`):
the actuator reads its token file on every call (KubernetesActuator), so a renewed token is
used immediately, without restart. Files are written atomically (tmp + rename) with 0600.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import httpx

from app.ports.credential_ports import CredentialStorePort, TokenIssuerError, TokenIssuerPort


class KubernetesTokenIssuer(TokenIssuerPort):
    def __init__(self, api_url: str, verify: bool | str = True, timeout: float = 10) -> None:
        self._client = httpx.AsyncClient(base_url=api_url, verify=verify, timeout=timeout)

    async def request_token(self, namespace: str, service_account: str, expiration_seconds: int,
                            bearer: str) -> str:
        try:
            r = await self._client.post(
                f"/api/v1/namespaces/{namespace}/serviceaccounts/{service_account}/token",
                headers={"Authorization": f"Bearer {bearer}"},
                json={"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
                      "spec": {"expirationSeconds": int(expiration_seconds)}})
        except httpx.HTTPError as exc:
            raise TokenIssuerError(f"API server unreachable: {exc}") from exc
        if r.status_code != 201:
            # never include the response body: it could echo request details
            raise TokenIssuerError(f"TokenRequest for {namespace}/{service_account} refused: HTTP {r.status_code}",
                                   r.status_code)
        return r.json()["status"]["token"]

    async def close(self) -> None:
        await self._client.aclose()


class FileCredentialStore(CredentialStorePort):
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.token"

    def read(self, name: str) -> str | None:
        try:
            return self.path(name).read_text().strip() or None
        except OSError:
            return None

    def write(self, name: str, token: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{name}.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            os.replace(tmp, self.path(name))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
