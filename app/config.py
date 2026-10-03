"""
Configuration management for finops-greenit-service.

Uses pydantic-settings for type-safe, validated configuration loading from
environment variables with sensible defaults. All settings are centralized here
to provide a single source of truth and enable testing via dependency injection.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    All fields are overridable via environment variables (e.g., PROMETHEUS_URL).
    Uses nested delimiter "__" for nested config if needed in future.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Prometheus / Metrics ---
    PROMETHEUS_URL: str = Field(
        default="http://prometheus-operated.monitoring.svc.cluster.local:9090",
        description="Base URL for Prometheus query API",
    )
    PROMETHEUS_QUERY_TIMEOUT_SECONDS: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Timeout for Prometheus query requests",
    )

    # --- Green IT Constants ---
    # PUE (Power Usage Effectiveness) - ratio of total facility power to IT equipment power
    # Typical values: 1.1-1.3 (efficient), 1.5-2.0 (average), >2.0 (inefficient)
    # Kept as constant here because: (a) changes infrequently, (b) requires facility-level measurement,
    # (c) avoids external dependency at runtime, (d) enables reproducible calculations.
    PUE_CONSTANT: float = Field(
        default=1.4,
        ge=1.0,
        le=3.0,
        description="Data center Power Usage Effectiveness (total facility power / IT equipment power)",
    )

    # Grid carbon intensity in gCO2/kWh - location-specific emission factor
    # Kept as constant because: (a) real-time APIs add latency/failure modes, (b) hourly averages
    # suffice for FinOps reporting granularity, (c) enables offline/air-gapped operation,
    # (d) value can be updated via ConfigMap/Secret rollout without code change.
    GRID_CARBON_INTENSITY_GCO2_PER_KWH: float = Field(
        default=475.0,
        ge=0.0,
        le=2000.0,
        description="Grid carbon intensity in grams CO2 per kWh (location-specific)",
    )

    # --- Cost Model ---
    # Baseline cost per vCPU-hour in USD - overridable per environment/cluster
    # Represents fully-loaded infrastructure cost (compute + memory + storage + network amortized)
    COST_PER_VCPU_HOUR_USD: float = Field(
        default=0.03,
        ge=0.0,
        le=10.0,
        description="Baseline cost per vCPU-hour in USD (fully-loaded)",
    )

    # Memory cost per GB-hour in USD
    COST_PER_GB_HOUR_USD: float = Field(
        default=0.004,
        ge=0.0,
        le=1.0,
        description="Baseline cost per GB-hour in USD",
    )

    # Storage cost per GB-month in USD
    COST_PER_GB_MONTH_USD: float = Field(
        default=0.10,
        ge=0.0,
        le=10.0,
        description="Baseline cost per GB-month in USD",
    )

    # --- API / Server (used by `python -m app`) ---
    # V1: API_WORKERS and METRICS_CACHE_TTL_SECONDS were removed — they were
    # declared but never read. The service must run with ONE worker because the
    # scheduler lives in the API process (see docs/LIMITATIONS.md).
    API_HOST: str = Field(default="0.0.0.0", description="API bind address")
    API_PORT: int = Field(default=8080, ge=1, le=65535, description="API port")

    # --- Logging (wired in app/logging_config.py since V1) ---
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    LOG_FORMAT: str = Field(default="json", pattern="^(json|text)$", description="Log format")

    # --- Database (TimescaleDB) ---
    # V1: no password in the default. Local dev sets it in .env (git-ignored);
    # Kubernetes/OpenShift injects it from a Secret.
    DATABASE_URL: str = Field(
        default="postgresql://finops@localhost:5432/finops_greenit",
        description="PostgreSQL connection string (TimescaleDB-compatible)",
    )

    # --- Multicluster (V1) ---
    # Without CLUSTERS_CONFIG_FILE the service observes ONE cluster named
    # CLUSTER_NAME through PROMETHEUS_URL (MVP behaviour).
    CLUSTER_NAME: str = Field(default="local", description="Name of the single cluster when no clusters file")
    CLUSTERS_CONFIG_FILE: str | None = Field(
        default=None,
        description="YAML file listing clusters (name, role, prometheus_url). See config/clusters.*.yaml",
    )

    # --- Scheduler (V1) ---
    SCHEDULER_ENABLED: bool = Field(default=True, description="Start the snapshot scheduler with the API")
    SCHEDULER_INTERVAL_SECONDS: int = Field(default=300, ge=10, le=86400)
    SCHEDULER_NAMESPACES: str = Field(
        default="",
        description="Comma-separated explicit namespaces. Empty = discover from Prometheus",
    )
    NAMESPACE_INCLUDE_REGEX: str = Field(default=".*", description="Full-match regex applied to discovered namespaces")
    NAMESPACE_EXCLUDE_REGEX: str = Field(
        default=r"^(kube-.*|openshift.*|kepler)$",
        description="Full-match regex of namespaces to skip (platform namespaces)",
    )
    MIN_OBSERVATION_HOURS: float = Field(
        default=24.0, ge=0.0, le=24 * 90,
        description="Hours of CPU history required before a right-sizing recommendation (7 days advised in prod)",
    )
    WORKLOAD_LABEL: str = Field(
        default="app",
        description="Pod label identifying a workload (must be allow-listed in kube-state-metrics)",
    )

    # --- Failover (V2) ---
    FAILOVER_ENABLED: bool = Field(default=True, description="Run the failover controller (needs >= 2 clusters)")
    FAILOVER_MODE: str = Field(
        default="manual", pattern="^(decision_only|manual|auto)$",
        description="decision_only: record only | manual: human approves via API | auto: execute",
    )
    FAILOVER_FAILURE_THRESHOLD_SECONDS: float = Field(default=60, ge=0, description="Primary unhealthy this long → failover")
    FAILOVER_RECOVERY_THRESHOLD_SECONDS: float = Field(default=120, ge=0, description="Primary healthy this long → failback")
    FAILOVER_TICK_SECONDS: float = Field(default=10, gt=0, le=3600)
    # --- V3 ML (app.ml_service) ---
    ML_ENABLED: bool = Field(default=True, description="Hourly ML cycle (anomaly, forecast, right-sizing) on the leader")
    ML_INTERVAL_SECONDS: float = Field(default=3600, gt=0)
    ML_HISTORY_HOURS: float = Field(default=48, gt=0, le=24 * 7, description="History read (retention is 7 days)")
    ML_ANOMALY_THRESHOLD: float = Field(default=4.0, gt=0, description="Robust z-score threshold")
    ML_MIN_CONFIDENCE: float = Field(default=0.3, ge=0, le=1, description="Below: no ML right-sizing advice")
    # --- V4 agent (app.agent_service) ---
    AGENT_ENABLED: bool = Field(default=True)
    AGENT_GITOPS_URL: str = Field(default="", description="Gitea base URL, e.g. http://gitea:3000 (empty = actions disabled)")
    AGENT_GITOPS_REPO: str = Field(default="finops/platform-gitops")
    AGENT_GITOPS_TOKEN_FILE: str = Field(default="", description="agent's Gitea token (Docker/K8s secret file)")
    AGENT_APPROVAL_TOKEN_FILE: str = Field(default="", description="human approval token file (empty = approvals disabled)")
    AGENT_VERIFY_TIMEOUT_SECONDS: int = Field(default=300, gt=0, le=3600)
    AGENT_ACTIVE_CLUSTERS: str = Field(default="", description="clusters the agent may act on (default: the primary)")
    DATA_GUARD_PERIOD_SECONDS: float = Field(default=1.0, gt=0, le=60,
                                             description="V2.x split-brain guard period (fencing of stale DB copies)")
    FAILOVER_NAMESPACES: str = Field(default="banking,trading,ecommerce", description="Namespaces the actuator may scale")
    FAILOVER_MAX_REPLICAS: int = Field(default=5, ge=1, le=100)
    FAILOVER_EXCLUDE_DEPLOYMENTS: str = Field(default="loadgenerator",
                                              description="Deployments never promoted (not application workloads)")

    # --- High availability (V2) ---
    LEADER_ELECTION: str = Field(
        default="auto", pattern="^(auto|postgres|none)$",
        description="auto: PostgreSQL advisory lock when DATABASE_URL is PostgreSQL, else single process",
    )

    @property
    def explicit_namespaces(self) -> list[str]:
        return [n.strip() for n in self.SCHEDULER_NAMESPACES.split(",") if n.strip()]


# Module-level instance for easy import: `from app.config import settings`
settings = Settings()