-- =============================================================================
-- 003_v2_energy_provenance.sql — energy provenance observed on REAL Kepler (V2)
-- =============================================================================
-- energy_power_source: hardware (RAPL) | model (Kepler estimator) | unknown
-- energy_scope:        cpu_package+dram (RAPL-measured components) | platform
--                      (Kepler total, includes modelled "other") | node_share | cpu
-- Idempotent.
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS energy_power_source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE metric_snapshots ADD COLUMN IF NOT EXISTS energy_scope TEXT NOT NULL DEFAULT 'unknown';
