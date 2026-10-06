from quant_phase2.symbols import registry_from_phase1_symbols


def test_phase2_registry_is_derived_from_phase1_universe() -> None:
    registry = registry_from_phase1_symbols([
        {"symbol": "BTCUSDT", "base_coin": "BTC", "quote_coin": "USDT", "contract_type": "perpetual", "status": "online"},
        {"symbol": "BADUSDC", "base_coin": "BAD", "quote_coin": "USDC", "contract_type": "perpetual", "status": "online"},
    ])
    symbol = registry.resolve("hyperliquid", "BTC")
    assert symbol.canonical_symbol == "BTC-USDT-PERP"
    assert registry.canonical_for("bitget", "BADUSDC") is None
