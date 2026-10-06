-- Phase 3 public-trade flow context. Additive and safe to apply repeatedly.
CREATE TABLE IF NOT EXISTS trade_flow_windows (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m', '5m', '15m', '1H', '4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    total_trade_count INTEGER NOT NULL,
    buy_trade_count INTEGER NOT NULL,
    sell_trade_count INTEGER NOT NULL,
    unknown_trade_count INTEGER NOT NULL,
    total_volume_base NUMERIC NOT NULL,
    buy_volume_base NUMERIC NOT NULL,
    sell_volume_base NUMERIC NOT NULL,
    unknown_volume_base NUMERIC NOT NULL,
    total_notional_usd NUMERIC,
    average_trade_size NUMERIC NOT NULL,
    trade_frequency NUMERIC NOT NULL,
    delta_base NUMERIC,
    delta_ratio NUMERIC,
    first_trade_at TIMESTAMPTZ NOT NULL,
    last_trade_at TIMESTAMPTZ NOT NULL,
    freshness TEXT NOT NULL CHECK (freshness IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    status_reason TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (exchange, canonical_symbol, timeframe, window_open)
);

CREATE INDEX IF NOT EXISTS trade_flow_windows_lookup_idx
    ON trade_flow_windows (canonical_symbol, timeframe, window_open DESC);
CREATE INDEX IF NOT EXISTS trade_flow_windows_retention_idx
    ON trade_flow_windows (timeframe, window_open);

CREATE TABLE IF NOT EXISTS cvd_snapshots (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m', '1H', '4H', '24H')),
    window_end TIMESTAMPTZ NOT NULL,
    value NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (exchange, canonical_symbol, timeframe, window_end)
);

CREATE INDEX IF NOT EXISTS cvd_snapshots_retention_idx
    ON cvd_snapshots (processed_at);
CREATE INDEX IF NOT EXISTS cvd_snapshots_lookup_idx
    ON cvd_snapshots (canonical_symbol, timeframe, window_end DESC);

CREATE TABLE IF NOT EXISTS cross_exchange_flow_snapshots (
    id BIGSERIAL PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    snapshot_timestamp TIMESTAMPTZ NOT NULL,
    volume_exchange_count INTEGER NOT NULL,
    directional_exchange_count INTEGER NOT NULL,
    total_volume_base NUMERIC,
    total_notional_usd NUMERIC,
    directional_delta_base NUMERIC,
    directional_delta_ratio NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (canonical_symbol, timeframe, snapshot_timestamp)
);

CREATE INDEX IF NOT EXISTS cross_exchange_flow_lookup_idx
    ON cross_exchange_flow_snapshots (canonical_symbol, timeframe, snapshot_timestamp DESC);

CREATE TABLE IF NOT EXISTS trade_gap_events (
    id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    gap_start TIMESTAMPTZ NOT NULL,
    gap_end TIMESTAMPTZ NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    affected_timeframe TEXT,
    affected_window_open TIMESTAMPTZ,
    missing_count INTEGER NOT NULL DEFAULT 0,
    dropped_count INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (exchange, canonical_symbol, gap_start, gap_end, reason)
);

CREATE INDEX IF NOT EXISTS trade_gap_events_retention_idx
    ON trade_gap_events (detected_at);
CREATE INDEX IF NOT EXISTS trade_gap_events_lookup_idx
    ON trade_gap_events (canonical_symbol, detected_at DESC);

CREATE TABLE IF NOT EXISTS stage1_flow_enrichment (
    id BIGSERIAL PRIMARY KEY,
    screening_run_id BIGINT NOT NULL REFERENCES screening_runs(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL REFERENCES symbols(symbol),
    canonical_symbol TEXT,
    flow_status TEXT NOT NULL CHECK (flow_status IN ('AVAILABLE','STALE','PARTIAL','NOT_AVAILABLE','ERROR')),
    flow_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (screening_run_id, symbol)
);

CREATE INDEX IF NOT EXISTS stage1_flow_enrichment_lookup_idx
    ON stage1_flow_enrichment (symbol, processed_at DESC);
