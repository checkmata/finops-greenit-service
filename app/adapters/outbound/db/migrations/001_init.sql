-- =============================================================================
-- finops-greenit-service — initial schema (001_init.sql)
-- =============================================================================
-- Two tables only:
--   1. recommendations  — relational, status lifecycle (pending/approved/executed/dismissed)
--   2. metric_snapshots — TimescaleDB hypertable, one row per namespace per scheduler run
--
-- WHY two tables (not one):
--   - recommendations have a lifecycle (status changes over time) → relational
--   - metric_snapshots are append-only time-series → hypertable
--   - They serve different query patterns (point lookup vs time-range aggregation)
--   - TimescaleDB hypertables have special indexing/retention rules that would hurt
--     the recommendations table (which is small and needs UPDATE/DELETE)
--
-- Runs automatically via docker-entrypoint-initdb.d on first `docker compose up`.
-- =============================================================================

-- Enable TimescaleDB extension (idempotent)
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =============================================================================
-- 1. RECOMMENDATIONS — relational table with status lifecycle
-- =============================================================================
-- WHY UUID primary key:
--   - No sequence contention across multiple workers/schedulers
--   - Safe to generate client-side (no DB round-trip needed)
--   - Standard for distributed systems (Kubernetes UIDs, etc.)
--
-- Status lifecycle: pending → approved → executed → dismissed
--   - pending:   engine generated it, awaiting human review
--   - approved:  human reviewed, ready to execute
--   - executed:  action taken (e.g., HPA patched)
--   - dismissed: human rejected it (no action needed)
-- =============================================================================

CREATE TABLE IF NOT EXISTS recommendations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace       TEXT NOT NULL,
    team            TEXT NOT NULL DEFAULT 'unknown',
    cost_center     TEXT NOT NULL DEFAULT 'unknown',
    environment     TEXT NOT NULL DEFAULT 'unknown',
    category        TEXT NOT NULL,       -- cost_right-sizing, energy_waste, data_quality, etc.
    severity        TEXT NOT NULL,       -- critical, warning, info
    resource        TEXT NOT NULL,       -- cpu, memory, energy, carbon, telemetry
    message         TEXT NOT NULL,
    current_value   TEXT NOT NULL,
    recommended_value TEXT NOT NULL,
    estimated_savings_usd   DOUBLE PRECISION,
    estimated_savings_co2_kg DOUBLE PRECISION,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'executed', 'dismissed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for common queries: "show me pending recommendations for namespace X"
CREATE INDEX IF NOT EXISTS idx_recommendations_namespace_status
    ON recommendations (namespace, status);

-- Index for chargeback: "show me all recommendations for team Y"
CREATE INDEX IF NOT EXISTS idx_recommendations_team
    ON recommendations (team);

-- =============================================================================
-- 2. METRIC_SNAPSHOTS — TimescaleDB hypertable (append-only time-series)
-- =============================================================================
-- WHY a hypertable (not a normal Postgres table):
--   - Hypertables auto-partition by time (chunks) → fast time-range queries
--   - Enable compression (old data compressed automatically)
--   - Enable retention policies (old data auto-deleted)
--   - Normal Postgres table would require manual partitioning + VACUUM
--
-- One row per namespace per scheduler run (every 5 minutes typically).
-- 7-day retention policy → old data auto-deleted to control disk usage.
-- =============================================================================

CREATE TABLE IF NOT EXISTS metric_snapshots (
    time            TIMESTAMPTZ NOT NULL DEFAULT now(),
    namespace       TEXT NOT NULL,
    team            TEXT NOT NULL DEFAULT 'unknown',
    cost_center     TEXT NOT NULL DEFAULT 'unknown',
    environment     TEXT NOT NULL DEFAULT 'unknown',
    cpu_core_hours  DOUBLE PRECISION NOT NULL DEFAULT 0,
    memory_gb_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    energy_kwh      DOUBLE PRECISION NOT NULL DEFAULT 0,
    co2e_kg         DOUBLE PRECISION NOT NULL DEFAULT 0,
    waste_pct       DOUBLE PRECISION NOT NULL DEFAULT 0
);

-- Convert to hypertable (must be done AFTER table creation)
-- chunk_time_interval: 1 day (each chunk holds 1 day of data)
SELECT create_hypertable('metric_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Index for time-range queries: "show me namespace X over the last 7 days"
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_namespace_time
    ON metric_snapshots (namespace, time DESC);

-- =============================================================================
-- 3. RETENTION POLICY — auto-delete data older than 7 days
-- =============================================================================
-- WHY 7 days:
--   - Grafana dashboards typically show last 7 days (rolling window)
--   - Older data can be re-computed from Prometheus if needed (source of truth)
--   - Prevents unbounded disk growth on OpenShift PVCs
--   - 7 days = ~20,160 rows per namespace (at 5-min intervals) = manageable
-- =============================================================================

SELECT add_retention_policy('metric_snapshots', INTERVAL '7 days',
    if_not_exists => TRUE
);