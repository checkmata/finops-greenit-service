"""V2.x ports for the automatic renewal of short-lived Kubernetes credentials."""

from __future__ import annotations

from abc import ABC, abstractmethod


class TokenIssuerError(RuntimeError):
    """The API server refused or failed the TokenRequest. `status` = HTTP status if any."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TokenIssuerPort(ABC):
    @abstractmethod
    async def request_token(self, namespace: str, service_account: str, expiration_seconds: int,
                            bearer: str) -> str:
        """TokenRequest for `namespace/service_account`, authenticated with `bearer`."""


class CredentialStorePort(ABC):
    @abstractmethod
    def read(self, name: str) -> str | None: ...

    @abstractmethod
    def write(self, name: str, token: str) -> None:
        """Atomic replace (readers never see a half-written token), owner-only permissions."""
