"""
Automatic renewal of the failover actuator's Kubernetes credential (V2.x hardening).

Root cause fixed (incident 2026-10-01, docs/DR.md §6): the actuator held a STATIC token
(fixed 24 h expiry, issued by a human) bound to its ServiceAccount UID; the SA was
recreated by a GitOps cascade → 401 → no actuation until a human re-issued a token.

Mechanism — Kubernetes TokenRequest, two identities, leader only:

    issuer   finops-credentials/finops-actuator-issuer — NOT GitOps-managed, can ONLY
             TokenRequest for the actuator and for itself. Bootstrapped once by a human
             (scripts/k8s/make-actuator-issuer.sh), then renews ITSELF every hour (24 h tokens).
    actuator finops-system/finops-failover-actuator — scale rights only, no token rights.
             Its 1 h token is re-issued BY THE ISSUER at 50 % of its lifetime, and at once
             when the API server rejects it (expired, or SA recreated with a new UID: a token
             requested by NAME is bound to the new SA → recovery without a human).

Tokens live in a writable volume (FileCredentialStore); KubernetesActuator re-reads its
file on every call, so a renewal takes effect immediately. Every renewal is recorded
(status for /api/failover, /health/ready, metrics) — never the token values.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.domain.credentials import parse_token, remaining_seconds, renewal_reason
from app.ports.credential_ports import CredentialStorePort, TokenIssuerError, TokenIssuerPort
from app.ports.failover_ports import FailoverActuatorPort, LeaderElectionPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenewalSpec:
    cluster: str
    actuator_namespace: str = "finops-system"
    actuator_service_account: str = "finops-failover-actuator"
    actuator_ttl_seconds: int = 3600
    issuer_namespace: str = "finops-credentials"
    issuer_service_account: str = "finops-actuator-issuer"
    issuer_ttl_seconds: int = 86400
    issuer_renew_every_seconds: int = 3600
    issuer_bootstrap_file: str | None = None
    fraction: float = 0.5
    probe_namespace: str = "banking"


@dataclass
class _State:
    last_renewal: datetime | None = None
    last_reason: str | None = None
    last_error: str | None = None
    renewals: int = 0


@dataclass
class CredentialRenewer:
    spec: RenewalSpec
    issuer: TokenIssuerPort
    store: CredentialStorePort
    actuator: FailoverActuatorPort
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    issuer_state: _State = field(default_factory=_State)
    actuator_state: _State = field(default_factory=_State)

    # ------------------------------------------------------------------ issuer
    def _issuer_token(self) -> str | None:
        tok = self.store.read("issuer")
        if tok is None and self.spec.issuer_bootstrap_file:
            try:
                tok = Path(self.spec.issuer_bootstrap_file).read_text().strip() or None
            except OSError:
                tok = None
        return tok

    async def _renew_issuer(self, now: datetime) -> str | None:
        tok = self._issuer_token()
        info = parse_token(tok) if tok else None
        if info is None or now >= info.expires_at:
            self.issuer_state.last_error = "issuer token missing or expired: run scripts/k8s/make-actuator-issuer.sh"
            return None
        age = (now - info.issued_at).total_seconds()
        if age < self.spec.issuer_renew_every_seconds and self.store.read("issuer") is not None:
            return tok
        try:
            new = await self.issuer.request_token(self.spec.issuer_namespace, self.spec.issuer_service_account,
                                                  self.spec.issuer_ttl_seconds, bearer=tok)
        except TokenIssuerError as exc:
            self.issuer_state.last_error = str(exc)
            logger.error("issuer self-renewal failed: %s", exc)
            return tok                                    # the current one is still valid
        self.store.write("issuer", new)
        s = self.issuer_state
        s.last_renewal, s.last_reason, s.last_error, s.renewals = now, "scheduled self-renewal", None, s.renewals + 1
        logger.info("credential: issuer token renewed (cluster %s)", self.spec.cluster)
        return new

    # ------------------------------------------------------------------ actuator
    async def tick(self) -> None:
        now = self.clock()
        issuer_tok = await self._renew_issuer(now)
        current = self.store.read("actuator")
        info = parse_token(current) if current else None
        rejected = False
        if info is not None and now < info.expires_at:
            rejected = (await self.actuator.probe(self.spec.probe_namespace)) == "unauthorized"
        reason = renewal_reason(info, now, fraction=self.spec.fraction, rejected=rejected)
        if reason is None:
            return
        s = self.actuator_state
        if issuer_tok is None:
            s.last_error = f"cannot renew ({reason}): {self.issuer_state.last_error}"
            logger.error("credential: actuator token NOT renewed — %s", s.last_error)
            return
        try:
            new = await self.issuer.request_token(self.spec.actuator_namespace, self.spec.actuator_service_account,
                                                  self.spec.actuator_ttl_seconds, bearer=issuer_tok)
        except TokenIssuerError as exc:
            s.last_error = str(exc)
            logger.error("credential: actuator renewal failed (%s): %s", reason, exc)
            return
        self.store.write("actuator", new)
        verdict = await self.actuator.probe(self.spec.probe_namespace)
        s.last_renewal, s.last_reason, s.renewals = now, reason, s.renewals + 1
        s.last_error = None if verdict == "ok" else f"renewed token answers '{verdict}'"
        logger.warning("credential: actuator token renewed (%s) → probe %s", reason, verdict)

    def status(self) -> dict:
        now = self.clock()
        a, i = parse_token(self.store.read("actuator") or ""), parse_token(self._issuer_token() or "")
        try:
            from app import observability as obs
            for ident, info, st in (("actuator", a, self.actuator_state), ("issuer", i, self.issuer_state)):
                left = remaining_seconds(info, now)
                obs.ACTUATOR_TOKEN_EXPIRES_IN.labels(self.spec.cluster, ident).set(left or 0)
                obs.ACTUATOR_TOKEN_RENEWALS.labels(self.spec.cluster, ident).set(st.renewals)
        except Exception:                                   # metrics never break the status
            pass

        def block(info, st: _State) -> dict:
            left = remaining_seconds(info, now)
            return {"state": "missing" if info is None else ("expired" if left == 0 else "valid"),
                    "expires_in_seconds": None if left is None else round(left),
                    "lifetime_seconds": None if info is None else round(info.lifetime_seconds),
                    "renewals": st.renewals, "last_renewal": st.last_renewal and st.last_renewal.isoformat(),
                    "last_reason": st.last_reason, "last_error": st.last_error}
        return {"actuator": block(a, self.actuator_state), "issuer": block(i, self.issuer_state)}


async def run_credential_loop(renewers: Sequence[CredentialRenewer], leader: LeaderElectionPort,
                              period_seconds: float = 30) -> None:
    """Leader only (one writer of the shared credential volume)."""
    while True:
        for r in renewers:
            try:
                if leader.is_leader:
                    await r.tick()
            except Exception as exc:                          # never kill the loop
                logger.error("credential loop error (%s): %s", r.spec.cluster, exc, exc_info=True)
        await asyncio.sleep(period_seconds)
