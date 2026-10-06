-- Compatibility migration for databases that applied an early 004 revision.
-- New rows are keyed by a deterministic provider-event key, including when a
-- public endpoint does not expose an exchange timestamp.
ALTER TABLE open_interest ADD COLUMN IF NOT EXISTS observation_key TEXT;
ALTER TABLE funding_rates ADD COLUMN IF NOT EXISTS observation_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS open_interest_observation_key_idx
    ON open_interest (exchange, symbol, observation_key, source_endpoint)
    WHERE observation_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS funding_rates_observation_key_idx
    ON funding_rates (exchange, symbol, observation_key, source_endpoint)
    WHERE observation_key IS NOT NULL;
