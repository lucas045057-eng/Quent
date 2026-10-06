-- Phase 5 deterministic context persistence. Additive and idempotent.
-- This migration stores bounded derived context only; it never creates a raw-kline
-- warehouse and does not alter Phase 1-4 tables except for one retention lookup index.
CREATE TABLE IF NOT EXISTS phase5_market_leader_context (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('5m','15m','1H','4H')),
    context_timestamp TIMESTAMPTZ NOT NULL,
    input_window_start TIMESTAMPTZ,
    input_window_end TIMESTAMPTZ,
    return_pct NUMERIC,
    trend_state TEXT NOT NULL CHECK (trend_state IN ('TREND_UP','TREND_DOWN','RANGE','MIXED','NOT_AVAILABLE')),
    structure_state TEXT NOT NULL CHECK (structure_state IN ('HIGHER_HIGH_HIGHER_LOW','LOWER_HIGH_LOWER_LOW','RANGE','MIXED','NOT_AVAILABLE')),
    volatility_state TEXT NOT NULL CHECK (volatility_state IN ('LOW','NORMAL','HIGH','EXTREME','NOT_AVAILABLE')),
    volume_state TEXT NOT NULL CHECK (volume_state IN ('BELOW_BASELINE','NORMAL','ELEVATED','NOT_AVAILABLE')),
    volatility_value NUMERIC CHECK (volatility_value IS NULL OR volatility_value >= 0),
    volume_ratio NUMERIC CHECK (volume_ratio IS NULL OR volume_ratio >= 0),
    freshness_status TEXT NOT NULL CHECK (freshness_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    data_quality JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    support_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    calculation_version TEXT NOT NULL,
    input_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (symbol, timeframe, context_timestamp, calculation_version)
);

CREATE INDEX IF NOT EXISTS phase5_market_leader_context_lookup_idx
    ON phase5_market_leader_context (symbol, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_market_leader_context_retention_idx
    ON phase5_market_leader_context (processed_at);

CREATE TABLE IF NOT EXISTS phase5_market_regime_snapshots (
    id BIGSERIAL PRIMARY KEY,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('5m','15m','1H','4H')),
    context_timestamp TIMESTAMPTZ NOT NULL,
    direction_regime TEXT NOT NULL CHECK (direction_regime IN ('TREND_UP','TREND_DOWN','RANGE','MIXED','NOT_AVAILABLE')),
    volatility_regime TEXT NOT NULL CHECK (volatility_regime IN ('LOW','NORMAL','HIGH','EXTREME','NOT_AVAILABLE')),
    breadth_regime TEXT NOT NULL CHECK (breadth_regime IN ('BROAD_STRENGTH','NARROW_STRENGTH','BROAD_WEAKNESS','NARROW_WEAKNESS','MIXED','NOT_AVAILABLE')),
    direction_status TEXT NOT NULL CHECK (direction_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    volatility_status TEXT NOT NULL CHECK (volatility_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    breadth_status TEXT NOT NULL CHECK (breadth_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    universe_run_id BIGINT REFERENCES universe_runs(id) CHECK (universe_run_id IS NULL OR universe_run_id > 0),
    sample_size INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0 AND available_count + missing_count = sample_size),
    coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1),
    support_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflict_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    calculation_version TEXT NOT NULL,
    input_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS phase5_regime_replay_uq
    ON phase5_market_regime_snapshots
    (timeframe, context_timestamp, (COALESCE(universe_run_id, 0)), calculation_version);
CREATE INDEX IF NOT EXISTS phase5_market_regime_snapshots_lookup_idx
    ON phase5_market_regime_snapshots (timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_market_regime_snapshots_universe_lookup_idx
    ON phase5_market_regime_snapshots (universe_run_id, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_market_regime_snapshots_retention_idx
    ON phase5_market_regime_snapshots (processed_at);

CREATE TABLE IF NOT EXISTS phase5_relative_strength_snapshots (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    benchmark TEXT NOT NULL CHECK (benchmark IN ('BTCUSDT','ETHUSDT','MARKET_UNIVERSE_EQUAL_WEIGHT')),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m','1H','4H')),
    context_timestamp TIMESTAMPTZ NOT NULL,
    candidate_return_pct NUMERIC,
    benchmark_return_pct NUMERIC,
    relative_return_pct NUMERIC,
    relative_class TEXT NOT NULL CHECK (relative_class IN ('STRONG','WEAK','NEUTRAL','NOT_AVAILABLE')),
    universe_run_id BIGINT REFERENCES universe_runs(id) CHECK (universe_run_id IS NULL OR universe_run_id > 0),
    sample_size INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0 AND available_count + missing_count = sample_size),
    coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    calculation_version TEXT NOT NULL,
    input_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (benchmark = 'MARKET_UNIVERSE_EQUAL_WEIGHT' AND universe_run_id IS NOT NULL)
        OR (benchmark IN ('BTCUSDT','ETHUSDT') AND universe_run_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS phase5_rs_leader_replay_uq
    ON phase5_relative_strength_snapshots
    (symbol, benchmark, timeframe, context_timestamp, calculation_version)
    WHERE benchmark IN ('BTCUSDT','ETHUSDT');
CREATE UNIQUE INDEX IF NOT EXISTS phase5_rs_market_replay_uq
    ON phase5_relative_strength_snapshots
    (symbol, benchmark, timeframe, context_timestamp, universe_run_id, calculation_version)
    WHERE benchmark = 'MARKET_UNIVERSE_EQUAL_WEIGHT';
CREATE INDEX IF NOT EXISTS phase5_relative_strength_snapshots_lookup_idx
    ON phase5_relative_strength_snapshots (symbol, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_relative_strength_snapshots_benchmark_idx
    ON phase5_relative_strength_snapshots (benchmark, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_relative_strength_snapshots_universe_idx
    ON phase5_relative_strength_snapshots (universe_run_id, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_relative_strength_snapshots_retention_idx
    ON phase5_relative_strength_snapshots (processed_at);

CREATE TABLE IF NOT EXISTS phase5_sector_membership (
    id BIGSERIAL PRIMARY KEY,
    mapping_version TEXT NOT NULL,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    sector TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','NOT_AVAILABLE','ERROR')),
    processed_at TIMESTAMPTZ NOT NULL,
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    UNIQUE (mapping_version, symbol, effective_from)
);

CREATE INDEX IF NOT EXISTS phase5_sector_membership_lookup_idx
    ON phase5_sector_membership (mapping_version, symbol, effective_from DESC);
CREATE INDEX IF NOT EXISTS phase5_sector_membership_symbol_idx
    ON phase5_sector_membership (symbol, effective_from DESC);
CREATE INDEX IF NOT EXISTS phase5_sector_membership_retention_idx
    ON phase5_sector_membership (processed_at);

CREATE TABLE IF NOT EXISTS phase5_sector_context_snapshots (
    id BIGSERIAL PRIMARY KEY,
    sector TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m','1H','4H')),
    context_timestamp TIMESTAMPTZ NOT NULL,
    universe_run_id BIGINT REFERENCES universe_runs(id) CHECK (universe_run_id IS NULL OR universe_run_id > 0),
    mapping_version TEXT NOT NULL,
    sector_return_pct NUMERIC,
    sector_positive_ratio NUMERIC CHECK (sector_positive_ratio IS NULL OR sector_positive_ratio BETWEEN 0 AND 1),
    member_count INTEGER NOT NULL CHECK (member_count >= 0),
    sample_size INTEGER NOT NULL CHECK (sample_size >= 0),
    available_count INTEGER NOT NULL CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL CHECK (missing_count >= 0 AND available_count + missing_count = sample_size),
    coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason_code TEXT,
    missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    calculation_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    CHECK (
        universe_run_id IS NOT NULL
        OR (
            status = 'NOT_AVAILABLE'
            AND reason_code IN ('MISSING_INPUT','INSUFFICIENT_COVERAGE','NO_ELIGIBLE_MEMBERS')
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS phase5_sector_context_replay_uq
    ON phase5_sector_context_snapshots
    (sector, timeframe, context_timestamp, (COALESCE(universe_run_id, 0)), mapping_version, calculation_version);
CREATE INDEX IF NOT EXISTS phase5_sector_context_snapshots_lookup_idx
    ON phase5_sector_context_snapshots (sector, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_sector_context_snapshots_universe_idx
    ON phase5_sector_context_snapshots (universe_run_id, timeframe, context_timestamp DESC);
CREATE INDEX IF NOT EXISTS phase5_sector_context_snapshots_retention_idx
    ON phase5_sector_context_snapshots (processed_at);

CREATE TABLE IF NOT EXISTS stage1_phase5_context_enrichment (
    id BIGSERIAL PRIMARY KEY,
    screening_run_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    universe_run_id BIGINT REFERENCES universe_runs(id) CHECK (universe_run_id IS NULL OR universe_run_id > 0),
    sector TEXT NOT NULL DEFAULT 'UNKNOWN',
    mapping_version TEXT,
    candidate_return_pct NUMERIC,
    sector_return_pct NUMERIC,
    candidate_vs_sector_pct NUMERIC,
    sector_relation TEXT NOT NULL CHECK (sector_relation IN ('OUTPERFORMING','UNDERPERFORMING','IN_LINE','NOT_AVAILABLE')),
    context_status TEXT NOT NULL CHECK (context_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    leader_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    regime_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    relative_strength_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    sector_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason_code TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    context_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (context_only IS TRUE),
    UNIQUE (screening_run_id, symbol),
    FOREIGN KEY (screening_run_id, symbol)
        REFERENCES screening_results(run_id, symbol) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS stage1_phase5_context_enrichment_symbol_idx
    ON stage1_phase5_context_enrichment (symbol, processed_at DESC);
CREATE INDEX IF NOT EXISTS stage1_phase5_context_enrichment_run_idx
    ON stage1_phase5_context_enrichment (screening_run_id, processed_at DESC);
CREATE INDEX IF NOT EXISTS stage1_phase5_context_enrichment_retention_idx
    ON stage1_phase5_context_enrichment (processed_at);

CREATE INDEX IF NOT EXISTS screening_runs_phase5_retention_idx
    ON screening_runs (run_timestamp);
