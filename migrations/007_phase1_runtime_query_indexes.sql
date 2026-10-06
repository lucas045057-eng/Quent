-- Keep the existing Phase 1 retention model, but make the latest canonical
-- batch lookup bounded on the low-resource PostgreSQL deployment.
CREATE INDEX IF NOT EXISTS market_snapshots_available_latest_idx
    ON market_snapshots (symbol, snapshot_timestamp DESC)
    WHERE status = 'AVAILABLE';

CREATE INDEX IF NOT EXISTS klines_available_symbol_interval_open_idx
    ON klines (symbol, interval, bar_open_timestamp DESC)
    WHERE status = 'AVAILABLE';
