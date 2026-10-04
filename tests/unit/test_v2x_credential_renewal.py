"""V2.x hardening — automatic renewal of the actuator credential (incident 2026-10-01)."""

import base64
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.adapters.outbound.k8s_credentials import FileCredentialStore, KubernetesTokenIssuer
from app.adapters.outbound.kubernetes_actuator import KubernetesActuator
from app.cluster_config import ClusterConfigError, Credentials, KubernetesAccess, RenewalConfig, TlsConfig, parse_clusters
from app.credential_service import CredentialRenewer, RenewalSpec
from app.domain.credentials import parse_token, remaining_seconds, renewal_reason
from app.ports.credential_ports import TokenIssuerError, TokenIssuerPort
from app.ports.failover_ports import FailoverActuatorPort

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def jwt(sub="system:serviceaccount:finops-system:finops-failover-actuator", uid="uid-1", iat=T0, ttl=3600):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    claims = {"sub": sub, "iat": int(iat.timestamp()), "exp": int((iat + timedelta(seconds=ttl)).timestamp()),
              "kubernetes.io": {"serviceaccount": {"name": sub.split(":")[-1], "uid": uid}}}
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"


# ------------------------------------------------------------------ domain policy
def test_parse_and_renewal_policy():
    info = parse_token(jwt())
    assert info.sa_uid == "uid-1" and info.lifetime_seconds == 3600
    assert parse_token("not-a-jwt") is None
    assert renewal_reason(None, T0) == "no valid token"
    assert renewal_reason(info, T0 + timedelta(minutes=10)) is None                    # young: keep
    assert renewal_reason(info, T0 + timedelta(minutes=30)) == "past 50% of its lifetime"
    assert renewal_reason(info, T0 + timedelta(hours=2)) == "expired"
    assert renewal_reason(info, T0, rejected=True) == "rejected by the API server"       # e.g. SA recreated
    assert renewal_reason(info, T0, live_sa_uid="uid-2") == "bound to a recreated ServiceAccount"
    assert remaining_seconds(info, T0 + timedelta(minutes=45)) == 900


# ------------------------------------------------------------------ service
class Issuer(TokenIssuerPort):
    """Fake API server: issues JWTs; refuses expired bearers; simulates SA recreation (uid)."""

    def __init__(self, clock):
        self.clock, self.uid, self.calls, self.down = clock, {"finops-failover-actuator": "uid-1"}, [], False

    async def request_token(self, namespace, service_account, expiration_seconds, bearer):
        self.calls.append((namespace, service_account))
        if self.down:
            raise TokenIssuerError("API server unreachable")
        b = parse_token(bearer)
        if b is None or self.clock() >= b.expires_at:
            raise TokenIssuerError("refused: HTTP 401", 401)
        return jwt(f"system:serviceaccount:{namespace}:{service_account}",
                   self.uid.get(service_account, "uid-issuer"), self.clock(), expiration_seconds)


class Actuator(FailoverActuatorPort):
    """Answers 'unauthorized' when the stored token is expired or bound to an old SA uid."""

    def __init__(self, store, issuer, clock):
        self.store, self.issuer, self.clock = store, issuer, clock

    async def probe(self, namespace):
        info = parse_token(self.store.read("actuator") or "")
        ok = info and self.clock() < info.expires_at and info.sa_uid == self.issuer.uid["finops-failover-actuator"]
        return "ok" if ok else "unauthorized"

    async def get_replicas(self, namespace, deployment):
        return 1

    async def scale(self, namespace, deployment, replicas):
        return None


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def build(tmp_path, bootstrap_ttl=86400):
    clock = Clock()
    boot = tmp_path / "bootstrap"
    boot.write_text(jwt("system:serviceaccount:finops-credentials:finops-actuator-issuer", "uid-issuer", T0,
                        bootstrap_ttl))
    store = FileCredentialStore(str(tmp_path / "creds"))
    issuer = Issuer(clock)
    r = CredentialRenewer(RenewalSpec("secondary", actuator_ttl_seconds=3600, issuer_bootstrap_file=str(boot)),
                          issuer, store, Actuator(store, issuer, clock), clock)
    return r, clock, issuer, store


@pytest.mark.asyncio
async def test_first_tick_moves_off_the_bootstrap_and_issues_the_actuator_token(tmp_path):
    r, clock, issuer, store = build(tmp_path)
    await r.tick()
    assert store.read("issuer") and store.read("actuator")
    assert ("finops-credentials", "finops-actuator-issuer") in issuer.calls        # issuer renewed itself
    assert r.status()["actuator"]["state"] == "valid" and r.status()["actuator"]["lifetime_seconds"] == 3600


@pytest.mark.asyncio
async def test_actuator_token_is_renewed_before_expiry_for_days_without_a_human(tmp_path):
    r, clock, issuer, store = build(tmp_path)
    for _ in range(3 * 24 * 6):                        # 3 days, one tick every 10 min
        await r.tick()
        assert await r.actuator.probe("banking") == "ok", clock.t
        clock.t += timedelta(minutes=10)
    assert r.actuator_state.renewals >= 3 * 24 and r.issuer_state.renewals >= 3 * 24 - 1
    assert r.status()["issuer"]["state"] == "valid"     # the 24 h bootstrap expired long ago


@pytest.mark.asyncio
async def test_recreated_service_account_is_recovered_automatically(tmp_path):
    r, clock, issuer, store = build(tmp_path)
    await r.tick()
    issuer.uid["finops-failover-actuator"] = "uid-2"   # GitOps cascade recreated the SA (2026-10-01)
    assert await r.actuator.probe("banking") == "unauthorized"
    await r.tick()
    assert await r.actuator.probe("banking") == "ok"
    assert r.actuator_state.last_reason == "rejected by the API server"


@pytest.mark.asyncio
async def test_expired_issuer_is_reported_not_hidden(tmp_path):
    r, clock, issuer, store = build(tmp_path, bootstrap_ttl=600)
    clock.t += timedelta(hours=2)                      # management plane down longer than the issuer lifetime
    await r.tick()
    st = r.status()
    assert st["issuer"]["state"] == "expired" and "make-actuator-issuer.sh" in st["actuator"]["last_error"]


@pytest.mark.asyncio
async def test_api_outage_keeps_the_current_token_and_retries(tmp_path):
    r, clock, issuer, store = build(tmp_path)
    await r.tick()
    first = store.read("actuator")
    issuer.down = True
    clock.t += timedelta(minutes=40)                   # renewal due, API server unreachable
    await r.tick()
    assert store.read("actuator") == first and "unreachable" in r.actuator_state.last_error
    issuer.down = False
    await r.tick()
    assert store.read("actuator") != first and r.actuator_state.last_error is None


# ------------------------------------------------------------------ adapters
def test_store_is_atomic_and_owner_only(tmp_path):
    s = FileCredentialStore(str(tmp_path / "d"))
    s.write("actuator", "a")
    s.write("actuator", "b")
    assert s.read("actuator") == "b" and s.read("missing") is None
    assert stat.S_IMODE(os.stat(s.path("actuator")).st_mode) == 0o600
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["actuator.token"]     # no temp leftovers


@pytest.mark.asyncio
async def test_token_request_call_shape_and_errors():
    seen = {}

    def handler(req):
        seen.update(path=req.url.path, auth=req.headers["Authorization"], body=json.loads(req.content))
        return httpx.Response(201, json={"status": {"token": "NEW"}})
    iss = KubernetesTokenIssuer("https://k8s:6443")
    iss._client._transport = httpx.MockTransport(handler)
    assert await iss.request_token("finops-system", "finops-failover-actuator", 3600, bearer="B") == "NEW"
    assert seen["path"] == "/api/v1/namespaces/finops-system/serviceaccounts/finops-failover-actuator/token"
    assert seen["auth"] == "Bearer B" and seen["body"]["spec"] == {"expirationSeconds": 3600}
    iss._client._transport = httpx.MockTransport(lambda r: httpx.Response(403, text="secret-ish body"))
    with pytest.raises(TokenIssuerError) as e:
        await iss.request_token("x", "y", 600, bearer="B")
    assert e.value.status == 403 and "secret-ish" not in str(e.value)


def test_actuator_prefers_the_renewed_token(tmp_path):
    store = tmp_path / "creds"
    store.mkdir()
    (store / "actuator.token").write_text("RENEWED")
    static = tmp_path / "static"
    static.write_text("STATIC")
    a = KubernetesActuator(KubernetesAccess("https://k8s:6443", Credentials("bearer", None, "INLINE"), TlsConfig(),
                                            token_file=str(static), renewal=RenewalConfig(store_dir=str(store))))
    assert a._headers()["Authorization"] == "Bearer RENEWED"
    (store / "actuator.token").unlink()
    assert a._headers()["Authorization"] == "Bearer STATIC"                       # bootstrap fallback


# ------------------------------------------------------------------ configuration
def _cfg(renewal):
    return {"clusters": [{"name": "primary", "role": "primary", "prometheus_url": "http://p"},
                         {"name": "secondary", "role": "secondary", "prometheus_url": "http://s",
                          "kubernetes": {"api_url": "https://k:6443", "renewal": renewal}}]}


def test_renewal_config_needs_no_static_token_and_refuses_inline_tokens():
    k = parse_clusters(_cfg({"store_dir": "/var/lib/finops/credentials",
                             "issuer": {"bootstrap_token_file": "/run/secrets/i"}}))[1].kubernetes
    assert k.renewal.actuator_ttl_seconds == 3600 and k.renewal.issuer_ttl_seconds == 86400
    assert k.renewal.actuator_token_file == "/var/lib/finops/credentials/actuator.token"
    with pytest.raises(ClusterConfigError, match="inline"):
        parse_clusters(_cfg({"store_dir": "/d", "token": "x"}))
    with pytest.raises(ClusterConfigError, match="600"):
        parse_clusters(_cfg({"store_dir": "/d", "actuator": {"ttl_seconds": 60}}))


def test_empty_ttl_override_from_compose_means_unset(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("ACTUATOR_TOKEN_TTL_SECONDS", "")
    assert Settings().ACTUATOR_TOKEN_TTL_SECONDS is None
    monkeypatch.setenv("ACTUATOR_TOKEN_TTL_SECONDS", "599")
    with pytest.raises(Exception):
        Settings()                                     # below the TokenRequest minimum
