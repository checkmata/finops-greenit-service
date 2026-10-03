-- V2.x: data-layer events in the failover log.
--   fence : a stale writable database (older timeline than the promoted one) was
--           fenced (application role NOLOGIN + its sessions terminated) to prevent
--           split-brain writes. Idempotent.
ALTER TABLE failover_events DROP CONSTRAINT IF EXISTS failover_events_kind_check;
ALTER TABLE failover_events ADD CONSTRAINT failover_events_kind_check
    CHECK (kind IN ('failover', 'failback', 'fence'));
