-- Mutable scheduling cursors only. Phase9 audit rows remain immutable.
CREATE TABLE phase9_revalidation_cursors (
    stage1_candidate_id BIGINT NOT NULL,
    timeframe TEXT NOT NULL CHECK(timeframe IN ('15m','1H','4H')),
    policy_generation TEXT NOT NULL,
    event_payload JSONB NOT NULL CHECK(octet_length(event_payload::text)<=65536),
    next_due TIMESTAMPTZ NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
    decision_id UUID NOT NULL REFERENCES phase9_decision_candidates(decision_id) ON DELETE RESTRICT,
    semantic_digest TEXT NOT NULL CHECK(semantic_digest ~ '^[0-9a-f]{64}$'),
    current_evaluation_id UUID REFERENCES phase9_evaluations(evaluation_id) ON DELETE RESTRICT,
    current_identity JSONB,
    requested BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY(stage1_candidate_id,timeframe,policy_generation)
);
CREATE INDEX phase9_revalidation_due_idx ON phase9_revalidation_cursors(next_due)
    WHERE active;
CREATE TABLE phase9_revalidation_budget (
    minute TIMESTAMPTZ PRIMARY KEY,
    runs INTEGER NOT NULL CHECK(runs BETWEEN 0 AND 16)
);
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='quant_runtime') THEN
        GRANT SELECT,INSERT,UPDATE ON phase9_revalidation_cursors TO quant_runtime;
        GRANT SELECT,INSERT,UPDATE,DELETE ON phase9_revalidation_budget TO quant_runtime;
    END IF;
END $$;
