-- =============================================================================
-- finops-greenit-service — V1 schema evolution (002_v1_multicluster.sql)
-- =============================================================================
-- Runs after 001_init.sql (docker-entrypoint-initdb.d executes files in order).
-- Idempotent: safe to re-run on an existing database.
--
-- Changes:
--   1. recommendations: cluster, workload, fingerprint (de-duplication),
--      estimation_method (provenance); status vocabulary V1
--      (pending/approved/rejected/executed/failed + system-only 'obsolete';
--      legacy 'dismissed' → 'rejected').
--   2. metric_snapshots: cluster + real cost history (requested/actual/waste/
--      savings), storage status, idle energy, energy attribution. energy_kwh and
--      co2e_kg become NULLable: "not measurable" is stored as NULL, never 0.
--   3. workload_snapshots: new hypertable, per-workload cost history.
-- =============================================================================

-- 1. RECOMMENDATIONS ----------------------------------------------------------
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS cluster TEXT NOT NULL DEFAULT 'default';
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS workload TEXT NOT NULL DEFAULT '*';
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS fingerprint TEXT;
ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS estimation_method TEXT;

ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS recommendations_status_check;
UPDATE recommendations SET status = 'rejected' WHERE status = 'dismissed';
ALTER TABLE recommendations ADD CONSTRAINT recommendations_status_check
    CHECK (status IN ('pending', 'approved', 'rejected', 'executed', 'failed', 'obsolete'));

UPDATE recommendations
   SET fingerprint = cluster || '|' || namespace || '|' || workload || '|' || category || '|' || resource
 WHERE fingerprint IS NULL;

CREATE INDEX IF NOT EXISTS idx_recommendations_fingerprint ON recommendations (fingerprint, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_cluster_ns ON recommendations (cluster, namespace, status);

-- 2. METRIC_SNAPSHOTS ---------------------------------------------------------
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS cluster TEXT NOT NULL DEFAULT 'default';
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS requested_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS waste_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS potential_savings_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS storage_cost_usd DOUBLE PRECISION;
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS storage_status TEXT NOT NULL DEFAULT 'unavailable';
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS idle_energy_kwh DOUBLE PRECISION;
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS energy_attribution TEXT NOT NULL DEFAULT 'unavailable';
ALTER TABLE metric_snapshots ALTER COLUMN energy_kwh DROP NOT NULL;
ALTER TABLE metric_snapshots ALTER COLUMN energy_kwh DROP DEFAULT;
ALTER TABLE metric_snapshots ALTER COLUMN co2e_kg DROP NOT NULL;
ALTER TABLE metric_snapshots ALTER COLUMN co2e_kg DROP DEFAULT;

CREATE INDEX IF NOT EXISTS idx_metric_snapshots_cluster_ns_time
    ON metric_snapshots (cluster, namespace, time DESC);

-- 3. WORKLOAD_SNAPSHOTS -------------------------------------------------------
CREATE TABLE IF NOT EXISTS workload_snapshots (
    time                 TIMESTAMPTZ NOT NULL,
    cluster              TEXT NOT NULL,
    namespace            TEXT NOT NULL,
    workload             TEXT NOT NULL,
    team                 TEXT NOT NULL DEFAULT 'unknown',
    cost_center          TEXT NOT NULL DEFAULT 'unknown',
    environment          TEXT NOT NULL DEFAULT 'unknown',
    cpu_cores_used       DOUBLE PRECISION NOT NULL DEFAULT 0,
    cpu_cores_requested  DOUBLE PRECISION NOT NULL DEFAULT 0,
    memory_gb_used       DOUBLE PRECISION NOT NULL DEFAULT 0,
    memory_gb_requested  DOUBLE PRECISION NOT NULL DEFAULT 0,
    requested_cost_usd   DOUBLE PRECISION NOT NULL DEFAULT 0,
    actual_cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
    waste_cost_usd       DOUBLE PRECISION NOT NULL DEFAULT 0,
    potential_savings_usd DOUBLE PRECISION NOT NULL DEFAULT 0
);

SELECT create_hypertable('workload_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_workload_snapshots_scope_time
    ON workload_snapshots (cluster, namespace, workload, time DESC);

SELECT add_retention_policy('workload_snapshots', INTERVAL '7 days', if_not_exists => TRUE);
