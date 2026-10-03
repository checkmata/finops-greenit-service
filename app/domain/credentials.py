"""
Short-lived credential renewal policy (pure domain, V2.x hardening).

Incident 2026-10-01: the failover actuator used a STATIC Kubernetes token (fixed expiry,
renewed by a human) bound to its ServiceAccount UID. The ServiceAccount was recreated
during an outage → new UID → the token was rejected (401) → no actuation possible.

The policy below drives an automatic renewal (app/credential_service.py):
    renew when   no token · token unreadable · past `fraction` of its lifetime ·
                 rejected by the API server (401) · bound to another SA UID than the live one
    lifetime     short (actuator 1 h): a leaked token is useful for minutes, not days

Only the claims are read here (exp, iat, sub, SA uid). The SIGNATURE is verified by the
API server on every call — this module never decides that a token is valid, only when to
replace it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class TokenInfo:
    subject: str                  # system:serviceaccount:<ns>:<name>
    sa_uid: str | None
    issued_at: datetime
    expires_at: datetime

    @property
    def lifetime_seconds(self) -> float:
        return (self.expires_at - self.issued_at).total_seconds()


def parse_token(token: str) -> TokenInfo | None:
    """Claims of a Kubernetes service-account JWT; None if it is not one."""
    try:
        payload = token.strip().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        sa = (claims.get("kubernetes.io") or {}).get("serviceaccount") or {}
        return TokenInfo(str(claims["sub"]), sa.get("uid"),
                         datetime.fromtimestamp(int(claims["iat"]), timezone.utc),
                         datetime.fromtimestamp(int(claims["exp"]), timezone.utc))
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def renewal_reason(info: TokenInfo | None, now: datetime, *, fraction: float = 0.5,
                   rejected: bool = False, live_sa_uid: str | None = None) -> str | None:
    """Why the token must be replaced now (None = keep it)."""
    if info is None:
        return "no valid token"
    if rejected:
        return "rejected by the API server"
    if now >= info.expires_at:
        return "expired"
    if live_sa_uid is not None and info.sa_uid is not None and info.sa_uid != live_sa_uid:
        return "bound to a recreated ServiceAccount"
    if (now - info.issued_at).total_seconds() >= fraction * info.lifetime_seconds:
        return f"past {fraction:.0%} of its lifetime"
    return None


def remaining_seconds(info: TokenInfo | None, now: datetime) -> float | None:
    return None if info is None else max(0.0, (info.expires_at - now).total_seconds())
