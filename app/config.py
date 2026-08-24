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

    # --- API / Server ---
    API_HOST: str = Field(default="0.0.0.0", description="API bind address")
    API_PORT: int = Field(default=8080, ge=1, le=65535, description="API port")
    API_WORKERS: int = Field(default=1, ge=1, le=32, description="Number of worker processes")

    # --- Logging ---
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    LOG_FORMAT: str = Field(default="json", pattern="^(json|text)$", description="Log format")

    # --- Cache / Performance ---
    METRICS_CACHE_TTL_SECONDS: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="TTL for Prometheus query result cache",
    )


# Module-level instance for easy import: `from app.config import settings`
settings = Settings()