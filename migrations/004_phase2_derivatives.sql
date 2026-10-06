CREATE TABLE IF NOT EXISTS exchange_instruments (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    exchange_symbol TEXT NOT NULL,
    canonical_symbol TEXT,
    contract_type TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    settle_asset TEXT,
    margin_asset TEXT,
    contract_multiplier NUMERIC,
    contract_size NUMERIC,
    funding_interval_seconds INTEGER,
    mark_price NUMERIC,
    source_endpoint TEXT NOT NULL,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    raw_payload JSONB NOT NULL,
    UNIQUE (exchange, exchange_symbol)
);

CREATE TABLE IF NOT EXISTS open_interest (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    canonical_symbol TEXT,
    exchange TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    margin_asset TEXT,
    settle_asset TEXT,
    raw_open_interest NUMERIC,
    raw_unit TEXT NOT NULL,
    open_interest_base NUMERIC,
    open_interest_quote NUMERIC,
    open_interest_usd NUMERIC,
    mark_price NUMERIC,
    normalization_method TEXT,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    source_endpoint TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    raw_reference TEXT,
    UNIQUE (exchange, symbol, observation_key, source_endpoint)
);

CREATE INDEX IF NOT EXISTS open_interest_lookup_idx
    ON open_interest (canonical_symbol, exchange, exchange_timestamp DESC);

CREATE TABLE IF NOT EXISTS funding_rates (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    canonical_symbol TEXT,
    exchange TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    funding_rate NUMERIC,
    funding_interval_seconds INTEGER,
    normalized_8h_rate NUMERIC,
    predicted_funding_rate NUMERIC,
    realized_funding_rate NUMERIC,
    next_funding_time TIMESTAMPTZ,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    source_endpoint TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    raw_reference TEXT,
    classification TEXT,
    UNIQUE (exchange, symbol, observation_key, source_endpoint)
);

CREATE INDEX IF NOT EXISTS funding_rates_lookup_idx
    ON funding_rates (canonical_symbol, exchange, exchange_timestamp DESC);

CREATE TABLE IF NOT EXISTS cross_exchange_derivative_snapshots (
    id BIGSERIAL PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    snapshot_timestamp TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','NOT_AVAILABLE','ERROR')),
    oi_exchange_count INTEGER NOT NULL,
    funding_exchange_count INTEGER NOT NULL,
    oi_total_usd NUMERIC,
    oi_weighted_funding NUMERIC,
    median_funding NUMERIC,
    max_funding NUMERIC,
    min_funding NUMERIC,
    funding_dispersion NUMERIC,
    reason TEXT,
    snapshot JSONB NOT NULL,
    UNIQUE (canonical_symbol, snapshot_timestamp)
);

CREATE INDEX IF NOT EXISTS derivative_snapshot_lookup_idx
    ON cross_exchange_derivative_snapshots (canonical_symbol, snapshot_timestamp DESC);
