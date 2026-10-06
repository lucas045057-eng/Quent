-- Phase 4 public market-context metrics. Additive and safe to apply repeatedly.
CREATE TABLE IF NOT EXISTS liquidation_events (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    exchange_symbol TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    source_endpoint TEXT NOT NULL,
    source_channel TEXT,
    source_event_id TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('LIQUIDATED_LONG','LIQUIDATED_SHORT','UNKNOWN')),
    raw_side TEXT,
    raw_side_semantics TEXT,
    price NUMERIC,
    raw_quantity NUMERIC,
    quantity_unit TEXT NOT NULL CHECK (quantity_unit IN ('QUOTE_COIN','BASE_ASSET','CONTRACTS','UNKNOWN')),
    quantity_base NUMERIC,
    notional_usd NUMERIC,
    source_granularity TEXT NOT NULL,
    coverage_semantics TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    raw_reference TEXT,
    UNIQUE (exchange, source_endpoint, source_event_id)
);

CREATE INDEX IF NOT EXISTS liquidation_events_lookup_idx
    ON liquidation_events (canonical_symbol, event_timestamp DESC);
CREATE INDEX IF NOT EXISTS liquidation_events_retention_idx
    ON liquidation_events (processed_at);

CREATE TABLE IF NOT EXISTS liquidation_windows (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1H','4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    liquidated_long_count INTEGER NOT NULL DEFAULT 0,
    liquidated_short_count INTEGER NOT NULL DEFAULT 0,
    convertible_notional_usd NUMERIC,
    largest_source_notional_usd NUMERIC,
    source_exchange_count INTEGER NOT NULL DEFAULT 0,
    source_granularity TEXT NOT NULL,
    coverage_semantics TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (exchange, canonical_symbol, timeframe, window_open)
);

CREATE INDEX IF NOT EXISTS liquidation_windows_lookup_idx
    ON liquidation_windows (canonical_symbol, timeframe, window_open DESC);
CREATE INDEX IF NOT EXISTS liquidation_windows_retention_idx
    ON liquidation_windows (timeframe, window_open);

CREATE TABLE IF NOT EXISTS long_short_observations (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    exchange_symbol TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    period TEXT NOT NULL,
    long_value NUMERIC,
    short_value NUMERIC,
    ratio NUMERIC,
    exchange_timestamp TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    source_endpoint TEXT NOT NULL,
    population_semantics TEXT NOT NULL CHECK (population_semantics IN ('HOLDER_COUNT_RATIO','ALL_POSITION_HOLDER_ACCOUNT_RATIO','UNCONFIRMED_PUBLIC_SOURCE')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    raw_reference TEXT,
    UNIQUE (exchange, canonical_symbol, metric_type, period, exchange_timestamp)
);

ALTER TABLE long_short_observations
    ADD COLUMN IF NOT EXISTS population_semantics TEXT NOT NULL DEFAULT 'UNCONFIRMED_PUBLIC_SOURCE'
        CHECK (population_semantics IN ('HOLDER_COUNT_RATIO','ALL_POSITION_HOLDER_ACCOUNT_RATIO','UNCONFIRMED_PUBLIC_SOURCE'));

CREATE INDEX IF NOT EXISTS long_short_observations_lookup_idx
    ON long_short_observations (canonical_symbol, metric_type, period, exchange_timestamp DESC);
CREATE INDEX IF NOT EXISTS long_short_observations_retention_idx
    ON long_short_observations (processed_at);

CREATE TABLE IF NOT EXISTS basis_snapshots (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    exchange_symbol TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    basis_type TEXT NOT NULL CHECK (basis_type IN ('MARK_INDEX','MARK_ORACLE')),
    perpetual_price NUMERIC,
    reference_price NUMERIC,
    absolute_basis NUMERIC,
    basis_bps NUMERIC,
    basis_pct NUMERIC,
    exchange_timestamp TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    max_timestamp_skew INTERVAL NOT NULL,
    timestamp_skew INTERVAL,
    source_endpoint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    raw_reference TEXT,
    UNIQUE (exchange, canonical_symbol, basis_type, exchange_timestamp)
);

CREATE INDEX IF NOT EXISTS basis_snapshots_lookup_idx
    ON basis_snapshots (canonical_symbol, basis_type, exchange_timestamp DESC);
CREATE INDEX IF NOT EXISTS basis_snapshots_retention_idx
    ON basis_snapshots (processed_at);

CREATE TABLE IF NOT EXISTS cross_exchange_phase4_snapshots (
    id BIGSERIAL PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    metric TEXT NOT NULL,
    -- Empty string is the canonical identity value for metrics without a timeframe.
    timeframe TEXT NOT NULL DEFAULT '',
    snapshot_timestamp TIMESTAMPTZ NOT NULL,
    exchange_count INTEGER NOT NULL DEFAULT 0,
    comparable_exchange_count INTEGER NOT NULL DEFAULT 0,
    missing_sources TEXT[] NOT NULL DEFAULT '{}',
    stale_sources TEXT[] NOT NULL DEFAULT '{}',
    coverage_semantics TEXT,
    source_granularity TEXT,
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (canonical_symbol, metric, timeframe, snapshot_timestamp)
);

CREATE INDEX IF NOT EXISTS cross_exchange_phase4_snapshots_lookup_idx
    ON cross_exchange_phase4_snapshots (canonical_symbol, metric, snapshot_timestamp DESC);
CREATE INDEX IF NOT EXISTS cross_exchange_phase4_snapshots_retention_idx
    ON cross_exchange_phase4_snapshots (processed_at);

CREATE TABLE IF NOT EXISTS stage1_phase4_enrichment (
    id BIGSERIAL PRIMARY KEY,
    screening_run_id BIGINT NOT NULL REFERENCES screening_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    canonical_symbol TEXT,
    liquidation_status TEXT NOT NULL CHECK (liquidation_status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    long_short_status TEXT NOT NULL CHECK (long_short_status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    basis_status TEXT NOT NULL CHECK (basis_status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    context JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (screening_run_id, symbol)
);

CREATE INDEX IF NOT EXISTS stage1_phase4_enrichment_lookup_idx
    ON stage1_phase4_enrichment (symbol, processed_at DESC);
CREATE INDEX IF NOT EXISTS stage1_phase4_enrichment_retention_idx
    ON stage1_phase4_enrichment (processed_at);
