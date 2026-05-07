-- Migration 001 — Post-Call Processing v2
--
-- Rationale:
--   The v1 design relied on Celery + Redis as the source of truth for in-flight
--   work, with results written into the JSONB `interaction_metadata` column.
--   At 100K calls/campaign that produced silent drops on broker restarts and
--   no auditability. v2 makes Postgres the system of record:
--
--     - `customers` formalises per-tenant configuration (budget, priority).
--     - `interaction_jobs` is a durable outbox replacing implicit Celery state.
--     - `interaction_audit_log` gives an append-only trail per interaction.
--     - `recording_jobs` decouples recording polling from LLM analysis.
--     - `analysis_results` separates analysis output from the dashboard cache,
--       so a retry never overwrites a previously good result.
--     - `dlq_jobs` is the explicit dead letter store — nothing is dropped.
--
-- All migrations are idempotent (IF NOT EXISTS) so they can be re-applied
-- safely in dev / CI.

BEGIN;

-- ── 1. Customers ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    -- Pre-allocated guaranteed share of the global TPM budget. The sum of
    -- reservations across customers must be <= LLM_TOKENS_PER_MINUTE * RESERVED_FRACTION.
    -- Anything above the reservation comes from the shared headroom pool.
    tokens_per_minute_reservation INTEGER NOT NULL DEFAULT 0,
    -- Soft priority used for tie-breaking in the shared pool (higher = served first).
    priority SMALLINT NOT NULL DEFAULT 100,
    -- Per-customer config: which call_stages bypass the cheap triage and always
    -- go to the hot lane, custom token limits, etc. Editable at runtime.
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 2. Interaction jobs (durable outbox) ──────────────────────────────────────
-- One row per processing job. Replaces opaque Celery task IDs with a queryable
-- table that survives broker restarts. State machine:
--   PENDING -> CLAIMED -> SUCCEEDED
--                      -> FAILED (retried) -> CLAIMED ...
--                      -> DEAD_LETTERED
CREATE TABLE IF NOT EXISTS interaction_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    interaction_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    campaign_id UUID NOT NULL,
    job_type VARCHAR(40) NOT NULL,            -- 'postcall_analysis' | 'recording_upload' | 'signal_dispatch' | 'lead_stage_update' | 'crm_push'
    lane VARCHAR(10) NOT NULL DEFAULT 'cold', -- 'hot' | 'cold'
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    -- Idempotency key — typically interaction_id:job_type. Prevents two
    -- producers from creating duplicate jobs for the same logical step.
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    -- Correlation ID is shared across all jobs and audit entries for the
    -- same interaction so the on-call engineer can trace a single call end-to-end.
    correlation_id UUID NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Visibility timeout for the worker that claimed this row. After this
    -- expires, another worker may re-claim and continue (at-least-once).
    claimed_until TIMESTAMPTZ,
    claimed_by VARCHAR(128),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_interaction ON interaction_jobs(interaction_id);
CREATE INDEX IF NOT EXISTS idx_jobs_customer ON interaction_jobs(customer_id);
CREATE INDEX IF NOT EXISTS idx_jobs_correlation ON interaction_jobs(correlation_id);
-- Hot path: dispatcher picks the next runnable job for a lane.
CREATE INDEX IF NOT EXISTS idx_jobs_dispatch
    ON interaction_jobs(state, lane, next_run_at)
    WHERE state IN ('PENDING', 'FAILED');

-- ── 3. Audit log (append-only) ────────────────────────────────────────────────
-- Every state transition / external call writes a row here. Three days from now,
-- the on-call engineer queries WHERE interaction_id = ? ORDER BY created_at and
-- sees exactly what happened, when, and why.
CREATE TABLE IF NOT EXISTS interaction_audit_log (
    id BIGSERIAL PRIMARY KEY,
    interaction_id UUID NOT NULL,
    correlation_id UUID NOT NULL,
    customer_id UUID,
    campaign_id UUID,
    event_type VARCHAR(64) NOT NULL,       -- 'call_ended' | 'triaged' | 'budget_denied' | 'llm_started' | ...
    severity VARCHAR(10) NOT NULL DEFAULT 'INFO',
    message TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_interaction ON interaction_audit_log(interaction_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON interaction_audit_log(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_event ON interaction_audit_log(event_type, created_at);

-- ── 4. Recording jobs (decoupled from LLM) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS recording_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    interaction_id UUID NOT NULL UNIQUE,
    call_sid VARCHAR(255) NOT NULL,
    exotel_account_id VARCHAR(255) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',  -- PENDING | UPLOADED | DEAD_LETTERED
    attempt INTEGER NOT NULL DEFAULT 0,
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    s3_key VARCHAR(512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recording_state ON recording_jobs(state, next_poll_at);

-- ── 5. Analysis results (separate from interaction_metadata) ─────────────────
-- interaction_metadata remains the dashboard hot cache, but the canonical
-- analysis result lives here so retries / replays don't overwrite history.
CREATE TABLE IF NOT EXISTS analysis_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    interaction_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    campaign_id UUID NOT NULL,
    -- The pre-LLM triage label is stored separately so we can audit/recover
    -- if the LLM was skipped (deferred / cold lane completed via heuristic only).
    triage_label VARCHAR(64),
    triage_confidence NUMERIC(4, 3),
    call_stage VARCHAR(64),
    entities JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary TEXT,
    raw_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    latency_ms NUMERIC(10, 2),
    provider VARCHAR(50),
    model VARCHAR(100),
    -- 'heuristic' for triage-only, 'llm' for LLM-classified, 'manual' for ops override.
    source VARCHAR(20) NOT NULL DEFAULT 'llm',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_results_interaction ON analysis_results(interaction_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_results_customer ON analysis_results(customer_id, created_at DESC);

-- ── 6. Token usage ledger ────────────────────────────────────────────────────
-- Append-only record of every LLM call's token spend. Source of truth for
-- billing and for "how many tokens did Customer X use today?". The Redis
-- counters are an ephemeral acceleration of this table.
CREATE TABLE IF NOT EXISTS token_usage (
    id BIGSERIAL PRIMARY KEY,
    interaction_id UUID NOT NULL,
    customer_id UUID NOT NULL,
    campaign_id UUID,
    tokens INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 1,
    provider VARCHAR(50) NOT NULL,
    model VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_token_customer_time ON token_usage(customer_id, created_at);

-- ── 7. Dead letter queue ──────────────────────────────────────────────────────
-- Anything that exhausts retries lands here. Includes the FULL payload so it
-- can be replayed by an operator with a single SQL UPDATE.
CREATE TABLE IF NOT EXISTS dlq_jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    original_job_id UUID,
    interaction_id UUID,
    customer_id UUID,
    job_type VARCHAR(40) NOT NULL,
    payload JSONB NOT NULL,
    last_error TEXT,
    attempts INTEGER NOT NULL,
    correlation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    replayed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dlq_unreplayed ON dlq_jobs(created_at) WHERE replayed_at IS NULL;

-- ── 8. interactions table additions ───────────────────────────────────────────
-- Extend the existing table — keep the JSONB hot cache but add explicit columns
-- for the things the dashboard now needs to filter on.
ALTER TABLE interactions
    ADD COLUMN IF NOT EXISTS lane VARCHAR(10),
    ADD COLUMN IF NOT EXISTS triage_label VARCHAR(64),
    ADD COLUMN IF NOT EXISTS triage_confidence NUMERIC(4, 3),
    ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(20) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS recording_status VARCHAR(20) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS correlation_id UUID;

CREATE INDEX IF NOT EXISTS idx_interactions_analysis_status ON interactions(analysis_status);
CREATE INDEX IF NOT EXISTS idx_interactions_correlation ON interactions(correlation_id);

-- Seed the customers we already reference in seed data so foreign-key-style
-- joins work even though we keep the column type-loose for now.
INSERT INTO customers (id, name, tokens_per_minute_reservation, priority)
VALUES
    ('d0000000-0000-0000-0000-000000000001', 'Cashify',  20000, 200),
    ('d0000000-0000-0000-0000-000000000002', 'Acme CRM', 10000, 100)
ON CONFLICT (id) DO NOTHING;

COMMIT;
