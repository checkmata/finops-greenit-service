-- =============================================================================
-- 004_v2_history_failover.sql — V2: long-term history, CO2 per workload, failover
-- =============================================================================
-- 1. Per-CYCLE energy/CO2 (energy_interval_kwh, co2_interval_kg): energy measured
--    over the scheduler interval. Summing these gives exact totals per hour/day/
--    month (the rolling 24 h value cannot be summed).
-- 2. Energy + CO2 per WORKLOAD (workload_snapshots).
-- 3. Continuous aggregates (hourly, daily) with long retention; raw data keeps 7 d.
--    Weeks and months are derived from the daily aggregate at query time.
-- 4. Failover controller state (one row) + transition/event log.
-- Idempotent where TimescaleDB allows it.

-- 1-2. New columns ------------------------------------------------------------
ALTER TABLE metric_snapshots   ADD COLUMN IF NOT EXISTS energy_interval_kwh DOUBLE PRECISION;
ALTER TABLE metric_snapshots   ADD COLUMN IF NOT EXISTS co2_interval_kg     DOUBLE PRECISION;
ALTER TABLE metric_snapshots   ADD COLUMN IF NOT EXISTS interval_seconds    DOUBLE PRECISION;
ALTER TABLE workload_snapshots ADD COLUMN IF NOT EXISTS energy_interval_kwh DOUBLE PRECISION;
ALTER TABLE workload_snapshots ADD COLUMN IF NOT EXISTS co2_interval_kg     DOUBLE PRECISION;
ALTER TABLE workload_snapshots ADD COLUMN IF NOT EXISTS energy_precision    TEXT NOT NULL DEFAULT 'unavailable';
ALTER TABLE workload_snapshots ADD COLUMN IF NOT EXISTS energy_power_source TEXT NOT NULL DEFAULT 'unknown';

-- 3. Continuous aggregates -----------------------------------------------------
-- Rule (same as app/domain/history.py): a cost is a monthly-rate SAMPLE → AVG
-- inside a bucket; energy per cycle is an AMOUNT → SUM inside a bucket.
CREATE MATERIALIZED VIEW IF NOT EXISTS metric_snapshots_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket, cluster, namespace,
       avg(cost_usd) AS cost_usd, avg(requested_cost_usd) AS requested_cost_usd,
       avg(waste_cost_usd) AS waste_cost_usd, avg(potential_savings_usd) AS savings_usd,
       avg(cpu_core_hours) AS cpu_cores, avg(memory_gb_hours) AS memory_gb,
       avg(storage_cost_usd) AS storage_cost_usd,
       sum(energy_interval_kwh) AS energy_kwh, sum(co2_interval_kg) AS co2_kg,
       count(*) AS samples
FROM metric_snapshots GROUP BY 1, 2, 3
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS metric_snapshots_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', time) AS bucket, cluster, namespace,
       avg(cost_usd) AS cost_usd, avg(requested_cost_usd) AS requested_cost_usd,
       avg(waste_cost_usd) AS waste_cost_usd, avg(potential_savings_usd) AS savings_usd,
       avg(cpu_core_hours) AS cpu_cores, avg(memory_gb_hours) AS memory_gb,
       avg(storage_cost_usd) AS storage_cost_usd,
       sum(energy_interval_kwh) AS energy_kwh, sum(co2_interval_kg) AS co2_kg,
       count(*) AS samples
FROM metric_snapshots GROUP BY 1, 2, 3
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS workload_snapshots_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket, cluster, namespace, workload,
       avg(actual_cost_usd) AS cost_usd, avg(requested_cost_usd) AS requested_cost_usd,
       avg(waste_cost_usd) AS waste_cost_usd, avg(potential_savings_usd) AS savings_usd,
       avg(cpu_cores_used) AS cpu_cores, avg(memory_gb_used) AS memory_gb,
       sum(energy_interval_kwh) AS energy_kwh, sum(co2_interval_kg) AS co2_kg,
       count(*) AS samples
FROM workload_snapshots GROUP BY 1, 2, 3, 4
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS workload_snapshots_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', time) AS bucket, cluster, namespace, workload,
       avg(actual_cost_usd) AS cost_usd, avg(requested_cost_usd) AS requested_cost_usd,
       avg(waste_cost_usd) AS waste_cost_usd, avg(potential_savings_usd) AS savings_usd,
       avg(cpu_cores_used) AS cpu_cores, avg(memory_gb_used) AS memory_gb,
       sum(energy_interval_kwh) AS energy_kwh, sum(co2_interval_kg) AS co2_kg,
       count(*) AS samples
FROM workload_snapshots GROUP BY 1, 2, 3, 4
WITH NO DATA;

-- Real-time aggregation: queries also see the not-yet-materialized recent data.
ALTER MATERIALIZED VIEW metric_snapshots_hourly   SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW metric_snapshots_daily    SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW workload_snapshots_hourly SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW workload_snapshots_daily  SET (timescaledb.materialized_only = false);

-- Refresh BEFORE the raw 7-day retention drops the chunks.
SELECT add_continuous_aggregate_policy('metric_snapshots_hourly', start_offset => INTERVAL '3 days',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('metric_snapshots_daily', start_offset => INTERVAL '6 days',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('workload_snapshots_hourly', start_offset => INTERVAL '3 days',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('workload_snapshots_daily', start_offset => INTERVAL '6 days',
       end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);

SELECT add_retention_policy('metric_snapshots_hourly',   INTERVAL '90 days',  if_not_exists => TRUE);
SELECT add_retention_policy('workload_snapshots_hourly', INTERVAL '90 days',  if_not_exists => TRUE);
SELECT add_retention_policy('metric_snapshots_daily',    INTERVAL '730 days', if_not_exists => TRUE);
SELECT add_retention_policy('workload_snapshots_daily',  INTERVAL '730 days', if_not_exists => TRUE);

-- 4. Failover ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS failover_state (
    id                       SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- singleton
    mode                     TEXT NOT NULL,
    active_cluster           TEXT NOT NULL,
    primary_unhealthy_since  TIMESTAMPTZ,
    primary_healthy_since    TIMESTAMPTZ,
    last_transition_at       TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS failover_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT NOT NULL CHECK (kind IN ('failover', 'failback')),
    from_cluster  TEXT NOT NULL,
    to_cluster    TEXT NOT NULL,
    reason        TEXT NOT NULL,
    -- awaiting_approval (manual mode) → executed | failed ; or decision_only
    action_status TEXT NOT NULL CHECK (action_status IN
                  ('awaiting_approval', 'executed', 'failed', 'decision_only', 'rejected')),
    action_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_by    TEXT NOT NULL DEFAULT 'controller'
);
CREATE INDEX IF NOT EXISTS idx_failover_events_at ON failover_events (at DESC);
