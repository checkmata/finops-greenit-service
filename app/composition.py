"""
Composition root (V1): the ONE place that instantiates concrete adapters.

WHY: the MVP instantiated PrometheusMetricsAdapter / TimescaleAdapter inside
every router dependency, i.e. a new HTTP client and a new DB pool per request,
never closed. Adapters are now created once at startup and shared.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.outbound.agent_store import SqlActionStore
from app.adapters.outbound.failover_store import SqlFailoverStore
from app.adapters.outbound.gitea_writer import GiteaWriter
from app.adapters.outbound.prometheus_observation import PrometheusObservation
from app.agent_service import AgentService
from app.domain.agent.policy import ActionPolicy
from app.adapters.outbound.kubernetes_actuator import KubernetesActuator
from app.adapters.outbound.postgres_leader import AlwaysLeader, PostgresAdvisoryLeader
from app.adapters.outbound.postgres_replication import PostgresReplicationAdapter
from app.adapters.outbound.prometheus_adapter import PrometheusMetricsAdapter
from app.adapters.outbound.timescale_adapter import TimescaleAdapter
from app.cluster_config import load_clusters
from app.cluster_registry import ClusterRegistry
from app.config import Settings
from app.domain.models.cluster import ClusterRole
from app.domain.failover_controller import FailoverPolicy
from app.failover_service import FailoverService, FailoverSettings, parse_namespaces
from app.ml_service import MLService, MLSettings
from app.adapters.outbound.k8s_credentials import FileCredentialStore, KubernetesTokenIssuer
from app.credential_service import CredentialRenewer, RenewalSpec
from app.ports.failover_ports import LeaderElectionPort
from app.ports.storage_port import StoragePort


@dataclass
class Container:
    registry: ClusterRegistry
    storage: StoragePort
    leader: LeaderElectionPort | None = None
    failover: FailoverService | None = None
    ml: MLService | None = None
    agent: AgentService | None = None
    credentials: list = None           # CredentialRenewer per cluster with automatic renewal

    async def close(self) -> None:
        for port in self.registry.ports().values():
            close = getattr(port, "close", None)
            if close:
                await close()
        if self.failover is not None:
            for actuator in self.failover.actuators.values():
                await actuator.close()
        if self.agent is not None and self.agent.writer is not None:
            await self.agent.writer.close()
        if self.leader is not None:
            await self.leader.release()
        await self.storage.close()


def build_container(settings: Settings) -> Container:
    clusters = load_clusters(settings)
    ports = {
        c.name: PrometheusMetricsAdapter(
            prometheus_url=c.prometheus_url,
            timeout_seconds=settings.PROMETHEUS_QUERY_TIMEOUT_SECONDS,
            cluster_name=c.name,
            workload_label=settings.WORKLOAD_LABEL,
            credentials=c.prometheus_auth,
            verify=c.prometheus_tls.verify,
        )
        for c in clusters
    }
    registry = ClusterRegistry({c.name: c.role for c in clusters}, ports)
    storage = TimescaleAdapter(database_url=settings.DATABASE_URL)

    # V2 HA: one leader among the replicas runs the scheduler + failover loop.
    use_pg = settings.LEADER_ELECTION == "postgres" or (
        settings.LEADER_ELECTION == "auto" and settings.DATABASE_URL.startswith("postgresql"))
    leader: LeaderElectionPort = PostgresAdvisoryLeader(settings.DATABASE_URL) if use_pg else AlwaysLeader()

    failover = None
    if settings.FAILOVER_ENABLED and len(clusters) > 1:
        actuators = {c.name: KubernetesActuator(c.kubernetes) for c in clusters if c.kubernetes}
        data = {c.name: [PostgresReplicationAdapter(d.name, d.host, d.port, d.database, d.username,
                                                    d.password, d.app_role) for d in c.data_services]
                for c in clusters if c.data_services}
        failover = FailoverService(
            registry, SqlFailoverStore(storage._session_factory), actuators,
            FailoverSettings(
                mode=settings.FAILOVER_MODE,
                policy=FailoverPolicy(settings.FAILOVER_FAILURE_THRESHOLD_SECONDS,
                                      settings.FAILOVER_RECOVERY_THRESHOLD_SECONDS),
                namespaces=parse_namespaces(settings.FAILOVER_NAMESPACES),
                max_replicas=settings.FAILOVER_MAX_REPLICAS,
                exclude_deployments=parse_namespaces(settings.FAILOVER_EXCLUDE_DEPLOYMENTS),
            ),
            data=data,
        )
    ml = MLService(storage, MLSettings(
        history_hours=settings.ML_HISTORY_HOURS, anomaly_threshold=settings.ML_ANOMALY_THRESHOLD,
        cpu_price_hour=settings.COST_PER_VCPU_HOUR_USD, memory_price_hour=settings.COST_PER_GB_HOUR_USD,
        min_hours=settings.MIN_OBSERVATION_HOURS, min_confidence=settings.ML_MIN_CONFIDENCE,
        standby_clusters=frozenset(c.name for c in clusters if c.role is not ClusterRole.PRIMARY)))
    agent = None
    if settings.AGENT_ENABLED:
        writer = (GiteaWriter(settings.AGENT_GITOPS_URL, settings.AGENT_GITOPS_REPO, settings.AGENT_GITOPS_TOKEN_FILE)
                  if settings.AGENT_GITOPS_URL and settings.AGENT_GITOPS_TOKEN_FILE else None)
        active = parse_namespaces(settings.AGENT_ACTIVE_CLUSTERS) or frozenset(
            c.name for c in clusters if c.role is ClusterRole.PRIMARY)
        agent = AgentService(
            storage, ml, SqlActionStore(storage._session_factory), writer,
            {name: PrometheusObservation(port) for name, port in ports.items()},
            ActionPolicy(active_clusters=active, namespaces=parse_namespaces(settings.FAILOVER_NAMESPACES)),
            co2_kg_per_kwh=settings.PUE_CONSTANT * settings.GRID_CARBON_INTENSITY_GCO2_PER_KWH / 1000,
            approval_token_file=settings.AGENT_APPROVAL_TOKEN_FILE or None,
            verify_timeout_seconds=settings.AGENT_VERIFY_TIMEOUT_SECONDS)
    renewers = []
    if failover is not None:
        for c in clusters:
            k = c.kubernetes
            if k is None or k.renewal is None or c.name not in failover.actuators:
                continue
            r = k.renewal
            renewers.append(CredentialRenewer(
                RenewalSpec(cluster=c.name, actuator_namespace=r.actuator_namespace,
                            actuator_service_account=r.actuator_service_account,
                            actuator_ttl_seconds=settings.ACTUATOR_TOKEN_TTL_SECONDS or r.actuator_ttl_seconds,
                            issuer_namespace=r.issuer_namespace, issuer_service_account=r.issuer_service_account,
                            issuer_ttl_seconds=r.issuer_ttl_seconds,
                            issuer_renew_every_seconds=r.issuer_renew_every_seconds,
                            issuer_bootstrap_file=r.issuer_bootstrap_file,
                            probe_namespace=sorted(failover.settings.namespaces)[0]),
                KubernetesTokenIssuer(k.api_url, verify=k.tls.verify), FileCredentialStore(r.store_dir),
                failover.actuators[c.name]))
    return Container(registry=registry, storage=storage, leader=leader, failover=failover, ml=ml, agent=agent,
                     credentials=renewers)
