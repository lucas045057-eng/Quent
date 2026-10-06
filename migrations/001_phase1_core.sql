CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    base_coin TEXT NOT NULL,
    quote_coin TEXT NOT NULL,
    symbol_type TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    status TEXT NOT NULL,
    price_precision INTEGER NOT NULL,
    quantity_precision INTEGER NOT NULL,
    min_order_qty NUMERIC NOT NULL,
    max_order_qty NUMERIC,
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    raw_payload JSONB,
    raw_reference JSONB
);

CREATE TABLE IF NOT EXISTS market_observations (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    metric TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    raw_payload JSONB,
    raw_reference JSONB,
    UNIQUE (symbol, metric, exchange_timestamp, source)
);

CREATE TABLE IF NOT EXISTS klines (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    interval TEXT NOT NULL CHECK (interval IN ('5m', '15m', '1H', '4H')),
    bar_open_timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    turnover NUMERIC NOT NULL,
    exchange_timestamp TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    raw_payload JSONB,
    raw_reference JSONB,
    UNIQUE (symbol, interval, bar_open_timestamp)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    snapshot_timestamp TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    exchange_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    snapshot JSONB NOT NULL,
    UNIQUE (symbol, snapshot_timestamp, source)
);

CREATE TABLE IF NOT EXISTS universe_runs (
    id BIGSERIAL PRIMARY KEY,
    run_timestamp TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS universe_members (
    run_id BIGINT NOT NULL REFERENCES universe_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    rank INTEGER NOT NULL,
    turnover NUMERIC,
    PRIMARY KEY (run_id, symbol)
);

CREATE TABLE IF NOT EXISTS screening_runs (
    id BIGSERIAL PRIMARY KEY,
    run_timestamp TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    rule_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_results (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES screening_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    inputs_used JSONB NOT NULL DEFAULT '[]'::jsonb,
    indicators JSONB NOT NULL DEFAULT '{}'::jsonb,
    structure TEXT,
    UNIQUE (run_id, symbol)
);

CREATE TABLE IF NOT EXISTS system_health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE', 'STALE', 'NOT_AVAILABLE', 'ERROR')),
    checked_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);
