-- Derivative context is persisted separately so Phase 1 classification stays
-- immutable and remains the only Stage1 decision input.
CREATE TABLE IF NOT EXISTS stage1_derivative_enrichment (
    id BIGSERIAL PRIMARY KEY,
    screening_run_id BIGINT NOT NULL REFERENCES screening_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    canonical_symbol TEXT,
    derivative_status TEXT NOT NULL CHECK (derivative_status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    derivative_snapshot_timestamp TIMESTAMPTZ,
    derivative_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (screening_run_id, symbol)
);

CREATE INDEX IF NOT EXISTS stage1_derivative_enrichment_lookup_idx
    ON stage1_derivative_enrichment (symbol, processed_at DESC);
