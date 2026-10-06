-- Keep legacy spot scope; admit only explicitly versioned Bitget SBE windows.
-- Application validation still requires the exact approved same-symbol spot
-- catalog. Canonical consumers independently verify the immutable SBE receipt.
ALTER TABLE phase7_spot_flow_windows
    DROP CONSTRAINT phase7_spot_flow_windows_symbol_check;
ALTER TABLE phase7_spot_flow_windows
    ADD CONSTRAINT phase7_spot_flow_windows_symbol_check CHECK (
        symbol IN ('BTCUSDT','ETHUSDT') OR (
            exchange = 'bitget'
            AND aggregation_version = 'bitget-sbe-candle-reconciled-v1'
            AND normalization_version = 'bitget-sbe-candle-reconciled-v1'
            AND source_reference IS NOT NULL
            AND source_reference ~ '^[0-9a-f]{64}$'
            AND length(symbol) <= 24
            AND symbol ~ '^[A-Z0-9]+USDT$'
        )
    );
