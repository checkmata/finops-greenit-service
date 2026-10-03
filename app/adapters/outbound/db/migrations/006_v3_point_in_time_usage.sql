-- V3: point-in-time CPU per workload (5-min rate), next to the long-window average
-- kept in cpu_cores_used (V1 right-sizing). ML features need the instantaneous value.
-- Past rows can be backfilled from the REAL Prometheus history: scripts/ml/backfill_cpu_now.py
ALTER TABLE workload_snapshots ADD COLUMN IF NOT EXISTS cpu_cores_used_now DOUBLE PRECISION;
