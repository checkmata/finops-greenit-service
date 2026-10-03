-- V4: controlled actions proposed by the agent, and their append-only audit log.
-- Lifecycle (app/domain/agent/lifecycle.py):
--   proposed → rejected_by_policy | pending_approval → rejected | approved → executing
--   → executed | failed ; executed → rolled_back. failed actions are rolled back automatically.
CREATE TABLE IF NOT EXISTS agent_actions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key  TEXT NOT NULL,
    recommendation_id UUID,
    status           TEXT NOT NULL CHECK (status IN ('pending_approval', 'rejected_by_policy', 'rejected',
                     'approved', 'executing', 'executed', 'failed', 'rolled_back')),
    proposal         JSONB NOT NULL,
    policy           JSONB NOT NULL DEFAULT '{}'::jsonb,
    dry_run          JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution        JSONB NOT NULL DEFAULT '{}'::jsonb,
    approver         TEXT,
    timeout_seconds  INTEGER NOT NULL DEFAULT 300
);
-- One OPEN action per identical change (idempotence); terminal ones are history.
CREATE UNIQUE INDEX IF NOT EXISTS agent_actions_open_idem ON agent_actions (idempotency_key)
    WHERE status IN ('pending_approval', 'approved', 'executing');
CREATE INDEX IF NOT EXISTS agent_actions_created ON agent_actions (created_at DESC);

CREATE TABLE IF NOT EXISTS agent_audit (
    id        BIGSERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    action_id UUID NOT NULL REFERENCES agent_actions(id),
    actor     TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS agent_audit_action ON agent_audit (action_id, at);
