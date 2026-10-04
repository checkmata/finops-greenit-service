"""
Cluster configuration loading (V1 multicluster, V2 auth + actuation).

    clusters:
      - name: primary
        role: primary
        prometheus_url: ${PROMETHEUS_PRIMARY_URL}
      - name: secondary
        role: secondary
        prometheus_url: ${PROMETHEUS_SECONDARY_URL}
        prometheus_auth:                  # optional (V2)
          type: basic                     # basic | bearer
          username_env: PROM_SECONDARY_USER
          password_env: PROM_SECONDARY_PASSWORD
          # bearer: token_env: X  or  token_file: /run/secrets/x
        prometheus_tls:                   # optional
          ca_file: /run/secrets/ca.crt
          insecure_skip_verify: false
        kubernetes:                       # optional — only for failover actuation (V2)
          api_url: https://secondary-control-plane:6443
          token_file: /run/secrets/k8s_secondary_token
          ca_file: /run/secrets/k8s_secondary_ca.crt
        data_services:                    # optional (V2.x) — replicated databases
          - name: banking-ledger
            kind: postgres
            host: secondary-control-plane
            port: 30432
            database: ledger
            username: postgres
            password_file: /run/secrets/ledger_postgres_password
            app_role: ledger_app          # the role fenced on a stale copy

Credentials are NEVER in this file: only the NAME of an environment variable or
the PATH of a mounted secret (Docker secret / Kubernetes Secret). ${VAR} is
expanded in URLs. Without a file, one cluster is built from CLUSTER_NAME +
PROMETHEUS_URL (MVP behaviour).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import Settings
from app.domain.models.cluster import ClusterRole


class ClusterConfigError(ValueError):
    """Invalid clusters file (fail fast at startup rather than mis-attribute data)."""


@dataclass(frozen=True)
class Credentials:
    """Resolved at startup from env vars / secret files. repr never shows values."""

    kind: str  # "none" | "basic" | "bearer"
    username: str | None = None
    secret: str | None = None  # password or token

    def __repr__(self) -> str:  # never leak secrets in logs
        return f"Credentials(kind={self.kind!r}, username={self.username!r}, secret=***)"


NO_CREDENTIALS = Credentials("none")


@dataclass(frozen=True)
class TlsConfig:
    ca_file: str | None = None
    insecure_skip_verify: bool = False

    @property
    def verify(self) -> bool | str:
        if self.insecure_skip_verify:
            return False
        return self.ca_file or True


@dataclass(frozen=True)
class KubernetesAccess:
    api_url: str
    credentials: Credentials
    tls: TlsConfig
    # V2.x: kept so the actuator re-reads a ROTATED short-lived token without restart
    # (found in the real DR run: a 24 h token expired → 401 during the failover).
    token_file: str | None = None
    # V2.x hardening (docs/DR.md §6): automatic TokenRequest renewal. When set, the actuator
    # reads `renewal.store_dir/actuator.token` first (written by app/credential_service.py).
    renewal: "RenewalConfig | None" = None


@dataclass(frozen=True)
class RenewalConfig:
    store_dir: str
    actuator_namespace: str = "finops-system"
    actuator_service_account: str = "finops-failover-actuator"
    actuator_ttl_seconds: int = 3600
    issuer_namespace: str = "finops-credentials"
    issuer_service_account: str = "finops-actuator-issuer"
    issuer_ttl_seconds: int = 86400
    issuer_renew_every_seconds: int = 3600
    issuer_bootstrap_file: str | None = None

    @property
    def actuator_token_file(self) -> str:
        return f"{self.store_dir.rstrip('/')}/actuator.token"


def _renewal(spec: dict | None, where: str) -> RenewalConfig | None:
    if not spec:
        return None
    if any(k in spec for k in ("token", "issuer_token")):
        raise ClusterConfigError(f"{where}: inline tokens are forbidden — use issuer_bootstrap_file")
    try:
        a, i = spec.get("actuator", {}), spec.get("issuer", {})
        cfg = RenewalConfig(
            store_dir=str(spec["store_dir"]),
            actuator_namespace=a.get("namespace", "finops-system"),
            actuator_service_account=a.get("service_account", "finops-failover-actuator"),
            actuator_ttl_seconds=int(a.get("ttl_seconds", 3600)),
            issuer_namespace=i.get("namespace", "finops-credentials"),
            issuer_service_account=i.get("service_account", "finops-actuator-issuer"),
            issuer_ttl_seconds=int(i.get("ttl_seconds", 86400)),
            issuer_renew_every_seconds=int(i.get("renew_every_seconds", 3600)),
            issuer_bootstrap_file=i.get("bootstrap_token_file"))
    except (KeyError, ValueError, TypeError) as exc:
        raise ClusterConfigError(f"{where}: invalid renewal block: {exc}") from exc
    if not 600 <= cfg.actuator_ttl_seconds <= cfg.issuer_ttl_seconds:
        raise ClusterConfigError(f"{where}: need 600 ≤ actuator ttl ≤ issuer ttl (TokenRequest minimum is 600 s)")
    return cfg


@dataclass(frozen=True)
class DataServiceConfig:
    """One copy of a replicated database on one cluster (same `name` on every cluster)."""

    name: str
    kind: str
    host: str
    port: int
    database: str
    username: str
    password: str
    app_role: str

    def __repr__(self) -> str:  # never leak the password
        return f"DataServiceConfig({self.name!r}, {self.kind}, {self.host}:{self.port}, password=***)"


@dataclass(frozen=True)
class ClusterConfig:
    name: str
    role: ClusterRole
    prometheus_url: str
    prometheus_auth: Credentials = NO_CREDENTIALS
    prometheus_tls: TlsConfig = TlsConfig()
    kubernetes: KubernetesAccess | None = None
    data_services: tuple[DataServiceConfig, ...] = ()


def _expand(value: str) -> str:
    """${VAR} expansion so URLs come from the environment, never from Git."""
    expanded = os.path.expandvars(value)
    if "${" in expanded:
        raise ClusterConfigError(f"unset environment variable in cluster config: {value}")
    return expanded


_RESOLVE_SECRETS = True


def _read_secret(spec: dict, key: str, where: str) -> str:
    """Value from `<key>_env` (environment) or `<key>_file` (mounted secret)."""
    if not _RESOLVE_SECRETS and (spec.get(f"{key}_env") or spec.get(f"{key}_file")):
        return "<not resolved>"
    if spec.get(f"{key}_env"):
        value = os.getenv(spec[f"{key}_env"])
        if not value:
            raise ClusterConfigError(f"{where}: environment variable {spec[f'{key}_env']} is not set")
        return value
    if spec.get(f"{key}_file"):
        path = Path(spec[f"{key}_file"])
        if not path.is_file():
            raise ClusterConfigError(f"{where}: secret file {path} not found")
        return path.read_text().strip()
    raise ClusterConfigError(f"{where}: needs {key}_env or {key}_file (never an inline value)")


def _credentials(spec: dict | None, where: str) -> Credentials:
    if not spec:
        return NO_CREDENTIALS
    if any(k in spec for k in ("password", "token")):
        raise ClusterConfigError(f"{where}: inline credentials are forbidden — use *_env or *_file")
    kind = str(spec.get("type", "")).lower()
    if kind == "basic":
        user = spec.get("username") or (os.getenv(spec["username_env"]) if spec.get("username_env") else None)
        if not user:
            raise ClusterConfigError(f"{where}: basic auth needs username or username_env")
        return Credentials("basic", user, _read_secret(spec, "password", where))
    if kind == "bearer":
        return Credentials("bearer", None, _read_secret(spec, "token", where))
    raise ClusterConfigError(f"{where}: auth type must be basic or bearer")


def _tls(spec: dict | None) -> TlsConfig:
    spec = spec or {}
    return TlsConfig(ca_file=spec.get("ca_file"), insecure_skip_verify=bool(spec.get("insecure_skip_verify", False)))


def _data_services(items: list | None, where: str) -> tuple[DataServiceConfig, ...]:
    out = []
    for j, spec in enumerate(items or []):
        w = f"{where}.data_services[{j}]"
        if "password" in spec:
            raise ClusterConfigError(f"{w}: inline credentials are forbidden — use password_env or password_file")
        if spec.get("kind", "postgres") != "postgres":
            raise ClusterConfigError(f"{w}: only kind 'postgres' is supported")
        try:
            out.append(DataServiceConfig(
                name=str(spec["name"]), kind="postgres", host=_expand(str(spec["host"])),
                port=int(spec.get("port", 5432)), database=str(spec["database"]),
                username=str(spec["username"]), password=_read_secret(spec, "password", w),
                app_role=str(spec["app_role"])))
        except KeyError as exc:
            raise ClusterConfigError(f"{w}: missing {exc}") from exc
    return tuple(out)


def validate_structure(data: dict) -> list[ClusterConfig]:
    """Validate a clusters file WITHOUT reading secrets (CI / tests, where the
    Docker/Kubernetes secrets are not mounted). Inline credentials still fail."""
    global _RESOLVE_SECRETS
    _RESOLVE_SECRETS = False
    try:
        return parse_clusters(data)
    finally:
        _RESOLVE_SECRETS = True


def parse_clusters(data: dict) -> list[ClusterConfig]:
    items = (data or {}).get("clusters")
    if not isinstance(items, list) or not items:
        raise ClusterConfigError("'clusters' must be a non-empty list")
    out: list[ClusterConfig] = []
    for i, item in enumerate(items):
        where = f"clusters[{i}]"
        try:
            name = str(item["name"]).strip()
            role = ClusterRole(str(item.get("role", "secondary")).strip().lower())
            url = _expand(str(item["prometheus_url"]).strip())
        except (KeyError, TypeError, ValueError) as exc:
            raise ClusterConfigError(f"{where} invalid: {exc}") from exc
        if not name or not url:
            raise ClusterConfigError(f"{where} needs a name and a prometheus_url")
        k8s = None
        if item.get("kubernetes"):
            kspec = item["kubernetes"]
            renewal = _renewal(kspec.get("renewal"), f"{where}.kubernetes.renewal")
            has_static = any(k.startswith("token") for k in kspec)
            k8s = KubernetesAccess(
                api_url=_expand(str(kspec["api_url"])),
                # with automatic renewal a static token is optional (bootstrap/fallback only)
                credentials=(_credentials({"type": "bearer", **{k: v for k, v in kspec.items()
                                                                if k.startswith("token")}}, f"{where}.kubernetes")
                             if has_static or renewal is None else Credentials("bearer", None, "")),
                tls=_tls(kspec),
                token_file=kspec.get("token_file"),
                renewal=renewal,
            )
        out.append(ClusterConfig(
            name=name, role=role, prometheus_url=url,
            prometheus_auth=_credentials(item.get("prometheus_auth"), f"{where}.prometheus_auth"),
            prometheus_tls=_tls(item.get("prometheus_tls")),
            kubernetes=k8s,
            data_services=_data_services(item.get("data_services"), where),
        ))
    names = [c.name for c in out]
    if len(set(names)) != len(names):
        raise ClusterConfigError(f"duplicate cluster names: {names}")
    if sum(c.role is ClusterRole.PRIMARY for c in out) != 1:
        raise ClusterConfigError("exactly one cluster must have role: primary")
    return out


def load_clusters(settings: Settings) -> list[ClusterConfig]:
    if not settings.CLUSTERS_CONFIG_FILE:
        return [ClusterConfig(settings.CLUSTER_NAME, ClusterRole.PRIMARY, settings.PROMETHEUS_URL)]
    path = Path(settings.CLUSTERS_CONFIG_FILE)
    if not path.is_file():
        raise ClusterConfigError(f"CLUSTERS_CONFIG_FILE not found: {path}")
    return parse_clusters(yaml.safe_load(path.read_text()))
