-- Phase 7 public on-chain and spot context persistence.
-- Additive and idempotent.  No raw source warehouse and no trading tables.

CREATE TABLE IF NOT EXISTS phase7_asset_registry (
    asset_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL CHECK (chain IN ('BITCOIN', 'ETHEREUM')),
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('NATIVE', 'ERC20')),
    contract_address TEXT,
    symbol TEXT NOT NULL,
    decimals SMALLINT NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    registry_version TEXT NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (chain, asset_id),
    UNIQUE (asset_id, asset_kind, contract_address),
    UNIQUE (asset_id, asset_kind, contract_address, decimals),
    UNIQUE (asset_id, asset_kind, decimals),
    UNIQUE (asset_id, contract_address),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK ((asset_kind = 'NATIVE' AND contract_address IS NULL)
        OR (asset_kind = 'ERC20' AND contract_address IS NOT NULL)),
    CHECK (chain <> 'BITCOIN' OR (asset_kind = 'NATIVE' AND decimals = 8)),
    CHECK (status <> 'AVAILABLE' OR decimals <= 36),
    CHECK ((chain = 'BITCOIN' AND asset_kind = 'NATIVE' AND symbol = 'BTC' AND decimals = 8)
        OR (chain = 'ETHEREUM' AND asset_kind = 'NATIVE' AND symbol = 'ETH' AND decimals = 18)
        OR (chain = 'ETHEREUM' AND asset_kind = 'ERC20' AND (
            (symbol = 'USDT' AND decimals = 6 AND lower(contract_address) = '0xdac17f958d2ee523a2206206994597c13d831ec7')
            OR (symbol = 'USDC' AND decimals = 6 AND lower(contract_address) = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48')
        ))),
    CHECK (asset_kind <> 'ERC20' OR chain = 'ETHEREUM'),
    CHECK (contract_address IS NULL OR contract_address ~ '^0x[0-9a-f]{40}$'),
    CHECK (octet_length(source_reference) <= 2048)
);

CREATE TABLE IF NOT EXISTS phase7_address_labels (
    chain TEXT NOT NULL CHECK (chain IN ('BITCOIN', 'ETHEREUM')),
    address TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'KNOWN_EXCHANGE','KNOWN_PROTOCOL','KNOWN_TREASURY','KNOWN_BRIDGE',
        'KNOWN_BURN','KNOWN_EXTERNAL','UNKNOWN'
    )),
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    label_version TEXT NOT NULL,
    confidence NUMERIC(5,4) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    snapshot_hash TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    PRIMARY KEY (chain, address, label_version),
    UNIQUE (chain, address, source_id, source_version, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK (address = btrim(address) AND address <> ''),
    CHECK (
        (chain = 'ETHEREUM' AND address = lower(address) AND address ~ '^0x[0-9a-f]{40}$')
        OR (chain = 'BITCOIN' AND address !~ '[[:space:]]')
    ),
    CHECK (octet_length(source_reference) <= 2048),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS phase7_onchain_transfer_events (
    event_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL CHECK (chain IN ('BITCOIN', 'ETHEREUM')),
    block_number BIGINT NOT NULL CHECK (block_number >= 0),
    block_hash TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    tx_index INTEGER,
    event_index BIGINT,
    event_index_kind TEXT NOT NULL CHECK (event_index_kind IN ('LOG_INDEX','VOUT_INDEX','TX_VALUE')),
    asset_id TEXT NOT NULL REFERENCES phase7_asset_registry(asset_id),
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('NATIVE','ERC20')),
    contract_address TEXT,
    from_address TEXT,
    to_address TEXT,
    from_address_set_ref TEXT,
    amount_raw TEXT NOT NULL CHECK (amount_raw ~ '^[0-9]+$'),
    decimals SMALLINT NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    amount_normalized NUMERIC(120,36) NOT NULL CHECK (amount_normalized >= 0),
    amount_usd NUMERIC(120,36),
    valuation_price NUMERIC(120,36),
    valuation_exchange TEXT,
    valuation_source TEXT,
    valuation_reason TEXT,
    valuation_status TEXT NOT NULL CHECK (valuation_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    valuation_exchange_timestamp TIMESTAMPTZ,
    valuation_fetched_at TIMESTAMPTZ,
    valuation_skew_seconds INTEGER CHECK (valuation_skew_seconds IS NULL OR valuation_skew_seconds >= 0),
    event_time TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    finality_status TEXT NOT NULL CHECK (finality_status IN ('OBSERVED','PENDING','CONFIRMED','FINALIZED','REORGED')),
    source_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    raw_reference TEXT,
    details JSONB,
    FOREIGN KEY (chain, asset_id) REFERENCES phase7_asset_registry(chain, asset_id),
    FOREIGN KEY (asset_id, asset_kind, decimals)
        REFERENCES phase7_asset_registry(asset_id, asset_kind, decimals),
    CHECK (status <> 'AVAILABLE' OR reason = 'COMPLETE'),
    CHECK (status = 'AVAILABLE' OR reason <> 'COMPLETE'),
    CHECK (status = 'AVAILABLE' OR (amount_usd IS NULL AND valuation_status <> 'AVAILABLE')),
    CHECK (status <> 'AVAILABLE' OR finality_status <> 'REORGED'),
    CHECK (finality_status <> 'REORGED' OR (status = 'STALE' AND reason = 'REORGED_EVENT')),
    CHECK (event_index_kind <> 'TX_VALUE' OR (chain = 'ETHEREUM' AND tx_index IS NOT NULL
        AND event_index IS NULL AND contract_address IS NULL AND asset_kind = 'NATIVE')),
    CHECK (event_index_kind <> 'LOG_INDEX' OR (chain = 'ETHEREUM' AND tx_index IS NOT NULL
        AND event_index IS NOT NULL AND contract_address IS NOT NULL AND asset_kind = 'ERC20')),
    CHECK (event_index_kind <> 'VOUT_INDEX' OR (chain = 'BITCOIN' AND tx_index IS NULL
        AND event_index IS NOT NULL AND contract_address IS NULL AND asset_kind = 'NATIVE')),
    FOREIGN KEY (asset_id, asset_kind, contract_address, decimals)
        REFERENCES phase7_asset_registry(asset_id, asset_kind, contract_address, decimals),
    CHECK (amount_usd IS NULL OR (valuation_status = 'AVAILABLE' AND valuation_price IS NOT NULL
        AND valuation_exchange IS NOT NULL AND valuation_source IS NOT NULL
        AND valuation_exchange_timestamp IS NOT NULL AND valuation_fetched_at IS NOT NULL
        AND valuation_skew_seconds IS NOT NULL)),
    CHECK (amount_usd IS NOT NULL OR (valuation_status <> 'AVAILABLE' AND valuation_price IS NULL
        AND valuation_exchange IS NULL AND valuation_source IS NULL
        AND valuation_exchange_timestamp IS NULL AND valuation_fetched_at IS NULL)),
    CHECK (amount_usd IS NULL OR amount_usd = amount_normalized * valuation_price),
    CHECK (amount_usd IS NULL OR amount_usd >= 0),
    CHECK (valuation_price IS NULL OR valuation_price > 0),
    CHECK (amount_normalized = amount_raw::numeric / power(10::numeric, decimals)),
    CHECK (valuation_status = 'AVAILABLE' OR valuation_reason IS NOT NULL),
    CHECK (valuation_fetched_at IS NULL OR valuation_exchange_timestamp IS NULL
        OR valuation_fetched_at >= valuation_exchange_timestamp),
    CHECK (valuation_skew_seconds IS NULL OR (
        valuation_exchange_timestamp IS NOT NULL
        AND valuation_skew_seconds = FLOOR(EXTRACT(EPOCH FROM (event_time - valuation_exchange_timestamp)))::INTEGER
        AND valuation_skew_seconds BETWEEN 0 AND 86400
    )),
    CHECK (valuation_exchange_timestamp IS NULL OR valuation_exchange_timestamp <= event_time),
    CHECK (valuation_fetched_at IS NULL OR valuation_fetched_at <= event_time),
    CHECK (octet_length(source_reference) <= 2048),
    CHECK (octet_length(from_address_set_ref) <= 65536),
    CHECK (octet_length(details::text) <= 8192),
    CHECK (octet_length(raw_reference) <= 65536)
);

CREATE TABLE IF NOT EXISTS phase7_onchain_flow_windows (
    window_id BIGSERIAL PRIMARY KEY,
    chain TEXT NOT NULL CHECK (chain IN ('BITCOIN', 'ETHEREUM')),
    asset_id TEXT NOT NULL REFERENCES phase7_asset_registry(asset_id),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1H','4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    context_version TEXT NOT NULL,
    flow_domain TEXT NOT NULL CHECK (flow_domain IN ('GENERIC_ONCHAIN','STABLECOIN','BRIDGE')),
    aggregation_scope TEXT NOT NULL CHECK (aggregation_scope IN ('GENERIC','STABLECOIN','BRIDGE')),
    aggregation_eligible BOOLEAN NOT NULL,
    bridge_leg_id TEXT,
    inbound_amount NUMERIC(120,36) NOT NULL CHECK (inbound_amount >= 0),
    outbound_amount NUMERIC(120,36) NOT NULL CHECK (outbound_amount >= 0),
    net_amount NUMERIC(120,36) NOT NULL,
    inbound_amount_usd NUMERIC(120,36) CHECK (inbound_amount_usd IS NULL OR inbound_amount_usd >= 0),
    outbound_amount_usd NUMERIC(120,36) CHECK (outbound_amount_usd IS NULL OR outbound_amount_usd >= 0),
    net_amount_usd NUMERIC(120,36),
    exchange_inflow_amount NUMERIC(120,36) CHECK (exchange_inflow_amount IS NULL OR exchange_inflow_amount >= 0),
    exchange_outflow_amount NUMERIC(120,36) CHECK (exchange_outflow_amount IS NULL OR exchange_outflow_amount >= 0),
    unknown_transfer_count INTEGER NOT NULL DEFAULT 0 CHECK (unknown_transfer_count >= 0),
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    known_address_count INTEGER NOT NULL DEFAULT 0 CHECK (known_address_count >= 0),
    labeled_address_count INTEGER NOT NULL DEFAULT 0 CHECK (labeled_address_count >= 0
        AND labeled_address_count <= known_address_count),
    labeled_count INTEGER NOT NULL DEFAULT 0 CHECK (labeled_count >= 0),
    source_quality TEXT NOT NULL DEFAULT 'UNSPECIFIED' CHECK (octet_length(source_quality) BETWEEN 1 AND 128),
    coverage_status TEXT NOT NULL DEFAULT 'NOT_AVAILABLE'
        CHECK (coverage_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    coverage_ratio NUMERIC(5,4) NOT NULL CHECK (coverage_ratio BETWEEN 0 AND 1),
    label_coverage_ratio NUMERIC(5,4) NOT NULL CHECK (label_coverage_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    source_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    source_reference TEXT,
    processed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (chain, asset_id, timeframe, window_open, context_version, aggregation_scope),
    CHECK (window_close > window_open),
    CHECK (net_amount = inbound_amount - outbound_amount),
    CHECK (flow_domain <> 'GENERIC_ONCHAIN' OR aggregation_scope = 'GENERIC'),
    CHECK (flow_domain <> 'BRIDGE' OR aggregation_scope = 'BRIDGE'),
    CHECK (flow_domain <> 'STABLECOIN' OR aggregation_scope = 'STABLECOIN'),
    CHECK (aggregation_scope NOT IN ('BRIDGE','STABLECOIN') OR NOT aggregation_eligible),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS phase7_whale_flow_windows (
    window_id BIGSERIAL PRIMARY KEY,
    chain TEXT NOT NULL CHECK (chain IN ('BITCOIN', 'ETHEREUM')),
    asset_id TEXT NOT NULL REFERENCES phase7_asset_registry(asset_id),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1H','4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    aggregation_version TEXT NOT NULL,
    flow_domain TEXT NOT NULL CHECK (flow_domain IN ('GENERIC_ONCHAIN','STABLECOIN','BRIDGE')),
    aggregation_scope TEXT NOT NULL CHECK (aggregation_scope IN ('GENERIC','STABLECOIN','BRIDGE')),
    aggregation_eligible BOOLEAN NOT NULL,
    bridge_leg_id TEXT,
    threshold_version TEXT NOT NULL,
    threshold_tier TEXT,
    large_inflow_count INTEGER NOT NULL DEFAULT 0 CHECK (large_inflow_count >= 0),
    large_outflow_count INTEGER NOT NULL DEFAULT 0 CHECK (large_outflow_count >= 0),
    large_inflow_amount NUMERIC(120,36) NOT NULL CHECK (large_inflow_amount >= 0),
    large_outflow_amount NUMERIC(120,36) NOT NULL CHECK (large_outflow_amount >= 0),
    large_inflow_usd NUMERIC(120,36) CHECK (large_inflow_usd IS NULL OR large_inflow_usd >= 0),
    large_outflow_usd NUMERIC(120,36) CHECK (large_outflow_usd IS NULL OR large_outflow_usd >= 0),
    threshold_not_evaluable_count INTEGER NOT NULL DEFAULT 0 CHECK (threshold_not_evaluable_count >= 0),
    unknown_transfer_count INTEGER NOT NULL DEFAULT 0 CHECK (unknown_transfer_count >= 0),
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    known_address_count INTEGER NOT NULL DEFAULT 0 CHECK (known_address_count >= 0),
    labeled_address_count INTEGER NOT NULL DEFAULT 0 CHECK (labeled_address_count >= 0
        AND labeled_address_count <= known_address_count),
    labeled_count INTEGER NOT NULL DEFAULT 0 CHECK (labeled_count >= 0),
    source_quality TEXT NOT NULL DEFAULT 'UNSPECIFIED' CHECK (octet_length(source_quality) BETWEEN 1 AND 128),
    coverage_status TEXT NOT NULL DEFAULT 'NOT_AVAILABLE'
        CHECK (coverage_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    coverage_ratio NUMERIC(5,4) NOT NULL CHECK (coverage_ratio BETWEEN 0 AND 1),
    label_coverage_ratio NUMERIC(5,4) NOT NULL CHECK (label_coverage_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    source_reference TEXT,
    normalization_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (chain, asset_id, threshold_version, timeframe, window_open, aggregation_version, aggregation_scope),
    CHECK (window_close > window_open),
    CHECK (flow_domain <> 'GENERIC_ONCHAIN' OR aggregation_scope = 'GENERIC'),
    CHECK (flow_domain <> 'BRIDGE' OR aggregation_scope = 'BRIDGE'),
    CHECK (flow_domain <> 'STABLECOIN' OR aggregation_scope = 'STABLECOIN'),
    CHECK (aggregation_scope NOT IN ('BRIDGE','STABLECOIN') OR NOT aggregation_eligible),
    CHECK (flow_domain <> 'STABLECOIN' OR NOT aggregation_eligible),
    CHECK (aggregation_eligible OR (
        large_inflow_count = 0 AND large_outflow_count = 0
        AND large_inflow_amount = 0 AND large_outflow_amount = 0
        AND COALESCE(large_inflow_usd, 0) = 0 AND COALESCE(large_outflow_usd, 0) = 0
        AND threshold_not_evaluable_count = 0
    )),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS phase7_spot_flow_windows (
    window_id BIGSERIAL PRIMARY KEY,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market_kind TEXT NOT NULL CHECK (market_kind = 'SPOT'),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1H','4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    aggregation_version TEXT NOT NULL,
    base_volume NUMERIC(120,36) NOT NULL CHECK (base_volume >= 0),
    quote_volume NUMERIC(120,36) NOT NULL CHECK (quote_volume >= 0),
    buy_volume NUMERIC(120,36) CHECK (buy_volume IS NULL OR buy_volume >= 0),
    sell_volume NUMERIC(120,36) CHECK (sell_volume IS NULL OR sell_volume >= 0),
    unknown_volume NUMERIC(120,36) NOT NULL CHECK (unknown_volume >= 0),
    delta NUMERIC(120,36),
    cvd NUMERIC(120,36),
    trade_count INTEGER NOT NULL DEFAULT 0 CHECK (trade_count >= 0),
    directional_trade_count INTEGER NOT NULL DEFAULT 0 CHECK (directional_trade_count >= 0),
    event_time_first TIMESTAMPTZ,
    event_time_last TIMESTAMPTZ,
    cursor_first TEXT,
    cursor_last TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    coverage_ratio NUMERIC(5,4) NOT NULL CHECK (coverage_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    source_reference TEXT,
    normalization_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (exchange, symbol, market_kind, timeframe, window_open, aggregation_version),
    CHECK (window_close > window_open),
    CHECK (symbol IN ('BTCUSDT','ETHUSDT')),
    CHECK (unknown_volume <= base_volume),
    CHECK (buy_volume IS NULL OR sell_volume IS NOT NULL),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS phase7_stablecoin_context (
    context_id BIGSERIAL PRIMARY KEY,
    chain TEXT NOT NULL CHECK (chain = 'ETHEREUM'),
    asset_id TEXT NOT NULL REFERENCES phase7_asset_registry(asset_id),
    contract_address TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'ORDINARY_TRANSFER','MINT','BURN','EXCHANGE_DEPOSIT','EXCHANGE_WITHDRAWAL',
        'BRIDGE_TRANSFER','UNKNOWN'
    )),
    timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','5m','15m','1H','4H')),
    window_open TIMESTAMPTZ NOT NULL,
    window_close TIMESTAMPTZ NOT NULL,
    aggregation_version TEXT NOT NULL,
    flow_domain TEXT NOT NULL CHECK (flow_domain = 'STABLECOIN'),
    aggregation_scope TEXT NOT NULL CHECK (aggregation_scope = 'STABLECOIN'),
    bridge_leg_id TEXT,
    aggregation_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    transfer_count INTEGER NOT NULL DEFAULT 0 CHECK (transfer_count >= 0),
    amount_normalized NUMERIC(120,36) NOT NULL CHECK (amount_normalized >= 0),
    amount_usd NUMERIC(120,36) CHECK (amount_usd IS NULL OR amount_usd >= 0),
    mint_count INTEGER NOT NULL DEFAULT 0 CHECK (mint_count >= 0),
    burn_count INTEGER NOT NULL DEFAULT 0 CHECK (burn_count >= 0),
    exchange_deposit_count INTEGER NOT NULL DEFAULT 0 CHECK (exchange_deposit_count >= 0),
    exchange_withdrawal_count INTEGER NOT NULL DEFAULT 0 CHECK (exchange_withdrawal_count >= 0),
    sample_count INTEGER NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
    source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0),
    missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    coverage_ratio NUMERIC(5,4) NOT NULL CHECK (coverage_ratio BETWEEN 0 AND 1),
    finality_status TEXT NOT NULL CHECK (finality_status IN ('OBSERVED','PENDING','CONFIRMED','FINALIZED','REORGED')),
    freshness_status TEXT NOT NULL CHECK (freshness_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    source_reference TEXT,
    normalization_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (chain, asset_id, category, timeframe, window_open, aggregation_version),
    FOREIGN KEY (asset_id, contract_address)
        REFERENCES phase7_asset_registry(asset_id, contract_address),
    CHECK (lower(contract_address) IN (
        '0xdac17f958d2ee523a2206206994597c13d831ec7',
        '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
    )),
    CHECK (asset_id LIKE 'ETHEREUM:ERC20:%'),
    CHECK (window_close > window_open),
    CHECK (category <> 'BRIDGE_TRANSFER' OR NOT aggregation_eligible),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS phase7_ingestion_checkpoints (
    checkpoint_id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('CHAIN','EXCHANGE')),
    scope_key TEXT NOT NULL,
    cursor_kind TEXT NOT NULL,
    cursor_value TEXT NOT NULL,
    last_observed_cursor TEXT,
    last_finalized_cursor TEXT,
    last_block_hash TEXT,
    parser_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_id, scope_kind, scope_key),
    CHECK (cursor_kind IN ('BLOCK','LOG_INDEX','TRADE_ID','TRADE_SEQUENCE')),
    CHECK (scope_kind <> 'CHAIN' OR last_block_hash IS NOT NULL),
    CHECK (last_block_hash IS NULL OR last_block_hash ~ '^(0x[0-9a-f]{64}|[0-9a-f]{64})$'),
    CHECK (octet_length(source_id) <= 256 AND octet_length(scope_key) <= 256),
    CHECK (octet_length(cursor_kind) <= 64 AND octet_length(cursor_value) <= 512),
    CHECK (last_observed_cursor IS NULL OR octet_length(last_observed_cursor) <= 512),
    CHECK (last_finalized_cursor IS NULL OR octet_length(last_finalized_cursor) <= 512),
    CHECK (reason IS NULL OR octet_length(reason) <= 2048),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE OR REPLACE FUNCTION phase7_checkpoint_monotonicity_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.last_block_hash IS NOT NULL
       AND NEW.last_block_hash !~ '^(0x[0-9a-f]{64}|[0-9a-f]{64})$' THEN
        RAISE EXCEPTION 'Phase 7 checkpoint block hash is not canonical lowercase hex';
    END IF;
    IF NEW.scope_kind = 'CHAIN' AND NEW.last_block_hash IS NULL THEN
        RAISE EXCEPTION 'Phase 7 chain checkpoint requires last_block_hash';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.cursor_value !~ '^[0-9]+$'
           OR (
               NEW.last_observed_cursor IS NOT NULL
               AND (
                   NEW.last_observed_cursor !~ '^[0-9]+$'
                   OR NEW.last_observed_cursor <> NEW.cursor_value
               )
           )
           OR NEW.last_finalized_cursor IS NOT NULL
              AND (
                  NEW.last_observed_cursor IS NULL
                  OR NEW.last_observed_cursor !~ '^[0-9]+$'
                  OR NEW.last_finalized_cursor !~ '^[0-9]+$'
                  OR NEW.last_finalized_cursor::numeric > NEW.cursor_value::numeric
              ) THEN
            RAISE EXCEPTION 'Phase 7 checkpoint initial cursor is not self-consistent';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.cursor_kind <> OLD.cursor_kind THEN
        RAISE EXCEPTION 'Phase 7 checkpoint cursor_kind cannot change';
    END IF;
    IF NEW.cursor_value !~ '^[0-9]+$'
       OR OLD.cursor_value !~ '^[0-9]+$'
       OR NEW.cursor_value::numeric < OLD.cursor_value::numeric THEN
        RAISE EXCEPTION 'Phase 7 checkpoint cursor cannot move backwards';
    END IF;
    IF NEW.last_observed_cursor IS NULL THEN
        NEW.last_observed_cursor := OLD.last_observed_cursor;
    END IF;
    IF NEW.last_observed_cursor IS NULL
       OR NEW.last_observed_cursor !~ '^[0-9]+$'
       OR OLD.last_observed_cursor IS NOT NULL
          AND (
              OLD.last_observed_cursor !~ '^[0-9]+$'
              OR NEW.last_observed_cursor::numeric < OLD.last_observed_cursor::numeric
          ) THEN
        RAISE EXCEPTION 'Phase 7 observed cursor cannot move backwards';
    END IF;
    IF NEW.last_observed_cursor <> NEW.cursor_value THEN
        RAISE EXCEPTION 'Phase 7 observed cursor must equal cursor_value';
    END IF;
    IF NEW.last_finalized_cursor IS NULL THEN
        NEW.last_finalized_cursor := OLD.last_finalized_cursor;
    END IF;
    IF OLD.last_finalized_cursor IS NOT NULL
       AND (
           NEW.last_finalized_cursor IS NULL
           OR NEW.last_finalized_cursor !~ '^[0-9]+$'
           OR OLD.last_finalized_cursor !~ '^[0-9]+$'
           OR NEW.last_finalized_cursor::numeric < OLD.last_finalized_cursor::numeric
       ) THEN
        RAISE EXCEPTION 'Phase 7 finalized cursor cannot move backwards';
    END IF;
    IF NEW.last_finalized_cursor IS NOT NULL
       AND (
           NEW.last_finalized_cursor !~ '^[0-9]+$'
           OR NEW.last_finalized_cursor::numeric > NEW.cursor_value::numeric
       ) THEN
        RAISE EXCEPTION 'Phase 7 finalized cursor cannot exceed observed cursor';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'phase7_checkpoint_monotonicity_trigger'
          AND tgrelid = 'phase7_ingestion_checkpoints'::regclass
    ) THEN
        CREATE TRIGGER phase7_checkpoint_monotonicity_trigger
        BEFORE INSERT OR UPDATE ON phase7_ingestion_checkpoints
        FOR EACH ROW EXECUTE FUNCTION phase7_checkpoint_monotonicity_guard();
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS stage1_phase7_context_enrichment (
    enrichment_id BIGSERIAL PRIMARY KEY,
    screening_run_id BIGINT NOT NULL REFERENCES screening_runs(id),
    symbol TEXT NOT NULL,
    context_reference JSONB,
    coverage JSONB,
    status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR')),
    reason TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    UNIQUE (screening_run_id, symbol),
    FOREIGN KEY (screening_run_id, symbol)
        REFERENCES screening_results(run_id, symbol) ON DELETE CASCADE,
    CHECK (octet_length(context_reference::text) <= 65536),
    CHECK (octet_length(coverage::text) <= 65536),
    CHECK (status = 'AVAILABLE' OR reason IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS phase7_asset_native_uq
    ON phase7_asset_registry (chain, asset_kind, registry_version)
    WHERE contract_address IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS phase7_asset_erc20_uq
    ON phase7_asset_registry (chain, asset_kind, contract_address, registry_version)
    WHERE contract_address IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS phase7_transfer_log_identity_uq
    ON phase7_onchain_transfer_events (chain, block_hash, tx_hash, event_index, contract_address)
    WHERE event_index_kind = 'LOG_INDEX';
CREATE UNIQUE INDEX IF NOT EXISTS phase7_transfer_tx_value_identity_uq
    ON phase7_onchain_transfer_events (chain, block_hash, tx_hash, tx_index)
    WHERE event_index_kind = 'TX_VALUE';
CREATE UNIQUE INDEX IF NOT EXISTS phase7_transfer_vout_identity_uq
    ON phase7_onchain_transfer_events (chain, block_hash, tx_hash, event_index)
    WHERE event_index_kind = 'VOUT_INDEX';
CREATE INDEX IF NOT EXISTS phase7_transfer_block_idx
    ON phase7_onchain_transfer_events (chain, block_number DESC);
CREATE INDEX IF NOT EXISTS phase7_address_labels_retention_idx
    ON phase7_address_labels (updated_at);
CREATE INDEX IF NOT EXISTS phase7_asset_lookup_idx
    ON phase7_asset_registry (chain, symbol, effective_from DESC);
CREATE INDEX IF NOT EXISTS phase7_transfer_asset_time_idx
    ON phase7_onchain_transfer_events (asset_id, event_time DESC);
CREATE INDEX IF NOT EXISTS phase7_transfer_retention_idx
    ON phase7_onchain_transfer_events (processed_at);
CREATE INDEX IF NOT EXISTS phase7_flow_asset_time_idx
    ON phase7_onchain_flow_windows (asset_id, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_flow_status_time_idx
    ON phase7_onchain_flow_windows (status, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_whale_asset_time_idx
    ON phase7_whale_flow_windows (asset_id, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_whale_retention_idx
    ON phase7_whale_flow_windows (processed_at);
CREATE INDEX IF NOT EXISTS phase7_whale_threshold_lookup_idx
    ON phase7_whale_flow_windows (threshold_version, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_spot_symbol_time_idx
    ON phase7_spot_flow_windows (symbol, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_spot_status_time_idx
    ON phase7_spot_flow_windows (status, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_spot_retention_idx
    ON phase7_spot_flow_windows (processed_at);
CREATE INDEX IF NOT EXISTS phase7_stablecoin_contract_time_idx
    ON phase7_stablecoin_context (contract_address, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_stablecoin_category_time_idx
    ON phase7_stablecoin_context (category, window_open DESC);
CREATE INDEX IF NOT EXISTS phase7_stablecoin_retention_idx
    ON phase7_stablecoin_context (processed_at);
CREATE INDEX IF NOT EXISTS phase7_checkpoint_status_idx
    ON phase7_ingestion_checkpoints (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS phase7_enrichment_symbol_idx
    ON stage1_phase7_context_enrichment (symbol, processed_at DESC);
CREATE INDEX IF NOT EXISTS phase7_enrichment_retention_idx
    ON stage1_phase7_context_enrichment (processed_at);
