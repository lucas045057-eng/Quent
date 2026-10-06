-- Phase 9 compatibility-closure storage. Additive only; legacy outbox rows
-- remain outside the Phase 9 state machine.

ALTER TABLE outbox_events
    ADD COLUMN IF NOT EXISTS event_id TEXT,
    ADD COLUMN IF NOT EXISTS phase9_state TEXT,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS attempt_limit INTEGER,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error_code TEXT,
    ADD COLUMN IF NOT EXISTS terminal_reason TEXT;

UPDATE outbox_events
SET next_attempt_at = created_at
WHERE next_attempt_at IS NULL;

ALTER TABLE outbox_events
    ALTER COLUMN next_attempt_at SET DEFAULT now(),
    ALTER COLUMN next_attempt_at SET NOT NULL;

ALTER TABLE outbox_events
    ADD CONSTRAINT outbox_events_phase9_identity_state_check CHECK (
        (event_id IS NULL AND phase9_state IS NULL)
        OR (event_id IS NOT NULL AND phase9_state IS NOT NULL)
    ),
    ADD CONSTRAINT outbox_events_phase9_event_id_check CHECK (
        event_id IS NULL OR event_id ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT outbox_events_phase9_state_check CHECK (
        phase9_state IS NULL OR phase9_state IN
            ('PENDING', 'LEASED', 'ACKNOWLEDGED', 'RETRY_WAIT', 'DEAD_LETTER')
    ),
    ADD CONSTRAINT outbox_events_phase9_attempt_count_check CHECK (attempt_count >= 0),
    ADD CONSTRAINT outbox_events_phase9_attempt_limit_check CHECK (
        attempt_limit IS NULL OR attempt_limit = 3
    ),
    ADD CONSTRAINT outbox_events_phase9_payload_size_check CHECK (
        phase9_state IS NULL OR octet_length(payload::text) <= 65536
    ),
    ADD CONSTRAINT outbox_events_phase9_state_shape_check CHECK (
        phase9_state IS NULL
        OR (phase9_state = 'PENDING' AND attempt_count = 0 AND attempt_limit IS NULL
            AND lease_owner IS NULL AND lease_expires_at IS NULL AND acknowledged_at IS NULL
            AND terminal_reason IS NULL)
        OR (phase9_state = 'LEASED' AND attempt_count > 0 AND attempt_limit = 3
            AND attempt_count <= attempt_limit AND lease_owner IS NOT NULL
            AND length(btrim(lease_owner)) > 0 AND lease_expires_at IS NOT NULL
            AND acknowledged_at IS NULL AND terminal_reason IS NULL)
        OR (phase9_state = 'RETRY_WAIT' AND attempt_count > 0 AND attempt_limit = 3
            AND attempt_count < attempt_limit AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND acknowledged_at IS NULL
            AND terminal_reason IS NULL)
        OR (phase9_state = 'ACKNOWLEDGED' AND attempt_count > 0 AND attempt_limit = 3
            AND acknowledged_at IS NOT NULL AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND terminal_reason IS NULL)
        OR (phase9_state = 'DEAD_LETTER' AND attempt_count > 0 AND attempt_limit = 3
            AND attempt_count >= attempt_limit AND acknowledged_at IS NULL
            AND lease_owner IS NULL AND lease_expires_at IS NULL
            AND terminal_reason IS NOT NULL AND length(btrim(terminal_reason)) > 0)
    );

CREATE UNIQUE INDEX IF NOT EXISTS outbox_events_phase9_identity_uq
    ON outbox_events (event_type, event_id)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_events_phase9_due_claim_idx
    ON outbox_events (phase9_state, next_attempt_at, created_at, event_id)
    WHERE phase9_state IN ('PENDING', 'RETRY_WAIT');
CREATE INDEX IF NOT EXISTS outbox_events_phase9_lease_recovery_idx
    ON outbox_events (lease_expires_at, event_id)
    WHERE phase9_state = 'LEASED';

CREATE TABLE IF NOT EXISTS phase9_evaluations (
    evaluation_id UUID PRIMARY KEY,
    stage1_candidate_id BIGINT NOT NULL CHECK (stage1_candidate_id > 0),
    symbol TEXT NOT NULL CHECK (length(btrim(symbol)) > 0),
    market TEXT NOT NULL CHECK (length(btrim(market)) > 0),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m', '1H', '4H')),
    evaluation_state TEXT NOT NULL CHECK (evaluation_state IN
        ('QUEUED', 'RUNNING', 'WAITING_JEV', 'COMPLETED', 'DEGRADED', 'FAILED', 'CANCELLED')),
    intake_disposition TEXT NOT NULL CHECK (intake_disposition IN
        ('ADMITTED', 'DEFERRED', 'EXPIRED', 'REJECTED', 'DEDUPLICATED', 'SUPERSEDED')),
    candidate_valid_until TIMESTAMPTZ,
    policy_generation TEXT NOT NULL CHECK (length(btrim(policy_generation)) > 0),
    material_change_generation TEXT NOT NULL CHECK (length(btrim(material_change_generation)) > 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    reason_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (stage1_candidate_id, timeframe, policy_generation, material_change_generation),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE TABLE IF NOT EXISTS phase9_evaluation_snapshots (
    evaluation_id UUID PRIMARY KEY REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    snapshot_digest TEXT NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
    as_of TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    code_version TEXT NOT NULL CHECK (code_version ~ '^[0-9a-f]{40}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (octet_length(payload::text) <= 262144)
);

CREATE TABLE IF NOT EXISTS phase9_evidence_items (
    evidence_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN
        ('PRICE_STRUCTURE', 'OPEN_INTEREST_STRUCTURE', 'TRADE_FLOW', 'LIQUIDATION_CONTEXT',
         'FUNDING_BASIS_POSITIONING', 'MARKET_REGIME', 'OPTIONS_CONTEXT', 'ONCHAIN_SPOT_MACRO')),
    semantic_code TEXT NOT NULL CHECK (length(btrim(semantic_code)) > 0),
    source_ref TEXT NOT NULL CHECK (length(btrim(source_ref)) > 0),
    observed_at TIMESTAMPTZ,
    availability_status TEXT NOT NULL CHECK (availability_status IN
        ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'PARTIAL', 'ERROR', 'NOT_CONFIGURED')),
    canonical_digest TEXT NOT NULL CHECK (canonical_digest ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (evaluation_id, evidence_id),
    UNIQUE (evaluation_id, evidence_type, semantic_code, source_ref),
    CHECK (octet_length(payload::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase9_evidence_chains (
    chain_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL UNIQUE REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    evaluation_snapshot_hash TEXT NOT NULL CHECK (evaluation_snapshot_hash ~ '^[0-9a-f]{64}$'),
    input_snapshot_hash TEXT NOT NULL CHECK (input_snapshot_hash ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (octet_length(payload::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase9_pattern_matches (
    pattern_match_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    pattern_type TEXT NOT NULL CHECK (length(btrim(pattern_type)) > 0),
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    status TEXT NOT NULL CHECK (status IN ('NOT_CONFIGURED', 'MATCHED', 'PARTIAL_MATCH', 'CONFLICTED', 'NOT_MATCHED')),
    pattern_policy_version TEXT NOT NULL CHECK (length(btrim(pattern_policy_version)) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (evaluation_id, pattern_type, direction),
    CHECK (octet_length(payload::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase9_jev_reviews (
    review_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    -- EvidenceChain is materialized in the final transaction after this review
    -- has been durably committed. Keep the typed reference without a premature FK.
    evidence_chain_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('COMPLETED', 'NOT_CONFIGURED', 'NOT_AVAILABLE', 'TIMEOUT', 'INVALID', 'FAILED')),
    reason_code TEXT,
    request_digest TEXT NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    response_digest TEXT CHECK (response_digest IS NULL OR response_digest ~ '^[0-9a-f]{64}$'),
    provider TEXT,
    model TEXT,
    model_version TEXT,
    prompt_version TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'COMPLETED' AND response_digest IS NOT NULL)
        OR (status <> 'COMPLETED' AND reason_code IS NOT NULL AND length(btrim(reason_code)) > 0)),
    CHECK (octet_length(payload::text) <= 32768)
);

CREATE TABLE IF NOT EXISTS phase9_decision_candidates (
    decision_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    stage1_candidate_id BIGINT NOT NULL CHECK (stage1_candidate_id > 0),
    symbol TEXT NOT NULL CHECK (length(btrim(symbol)) > 0),
    market TEXT NOT NULL CHECK (length(btrim(market)) > 0),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m', '1H', '4H')),
    valid_until TIMESTAMPTZ NOT NULL,
    eligible BOOLEAN NOT NULL,
    direction_bias TEXT NOT NULL CHECK (direction_bias IN ('BULLISH', 'BEARISH', 'NEUTRAL')),
    confidence_band TEXT NOT NULL CHECK (confidence_band IN ('HIGH', 'MEDIUM', 'LOW', 'INSUFFICIENT')),
    input_snapshot_hash TEXT NOT NULL CHECK (input_snapshot_hash ~ '^[0-9a-f]{64}$'),
    jev_review_id UUID REFERENCES phase9_jev_reviews(review_id) ON DELETE RESTRICT,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (octet_length(payload::text) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase9_decision_status_events (
    event_id UUID PRIMARY KEY,
    decision_id UUID NOT NULL REFERENCES phase9_decision_candidates(decision_id) ON DELETE RESTRICT,
    evaluation_id UUID NOT NULL REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'EXPIRED', 'SUPERSEDED', 'INVALIDATED')),
    event_time TIMESTAMPTZ NOT NULL,
    reason_code TEXT NOT NULL CHECK (length(btrim(reason_code)) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (octet_length(payload::text) <= 16384)
);

CREATE INDEX IF NOT EXISTS phase9_evaluations_stage1_latest_idx
    ON phase9_evaluations (stage1_candidate_id, timeframe, created_at DESC);
CREATE INDEX IF NOT EXISTS phase9_evaluations_symbol_latest_idx
    ON phase9_evaluations (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS phase9_evaluations_state_lease_idx
    ON phase9_evaluations (evaluation_state, lease_expires_at);
CREATE INDEX IF NOT EXISTS phase9_evaluation_snapshots_asof_idx
    ON phase9_evaluation_snapshots (as_of DESC);
CREATE INDEX IF NOT EXISTS phase9_evidence_items_type_idx
    ON phase9_evidence_items (evaluation_id, evidence_type, semantic_code);
CREATE INDEX IF NOT EXISTS phase9_evidence_chains_snapshot_idx
    ON phase9_evidence_chains (evaluation_snapshot_hash, evaluation_id);
CREATE INDEX IF NOT EXISTS phase9_pattern_matches_lookup_idx
    ON phase9_pattern_matches (evaluation_id, pattern_type, direction, status);
CREATE UNIQUE INDEX IF NOT EXISTS phase9_jev_reviews_request_uq
    ON phase9_jev_reviews (evaluation_id, request_digest);
CREATE UNIQUE INDEX IF NOT EXISTS phase9_decision_candidates_input_uq
    ON phase9_decision_candidates (evaluation_id, input_snapshot_hash);
CREATE INDEX IF NOT EXISTS phase9_decision_candidates_symbol_latest_idx
    ON phase9_decision_candidates (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS phase9_decision_status_events_decision_idx
    ON phase9_decision_status_events (decision_id, event_time, event_id);

CREATE OR REPLACE FUNCTION phase9_reject_immutable_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Phase 9 audit records are immutable'
        USING ERRCODE = '55000';
END;
$$;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase9_evaluation_snapshots',
        'phase9_evidence_items',
        'phase9_evidence_chains',
        'phase9_pattern_matches',
        'phase9_jev_reviews',
        'phase9_decision_candidates',
        'phase9_decision_status_events'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', table_name || '_immutable_trg', table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase9_reject_immutable_audit_mutation()',
            table_name || '_immutable_trg', table_name
        );
    END LOOP;
END;
$$;
