from datetime import datetime, timezone

from quant_phase1.config import Settings
from quant_phase2.runtime import Phase2CycleResult, Phase2DerivativeRuntime, default_registry


def test_runtime_maps_configured_uta_symbols_to_hyperliquid_symbols() -> None:
    runtime = Phase2DerivativeRuntime(Settings.from_env({"TRADING_MODE": "paper"}))

    assert runtime._hyperliquid_symbols(("BTCUSDT", "ETHUSDT")) == {"BTC", "ETH"}


def test_phase2_defaults_are_opt_in_and_paper_only() -> None:
    settings = Settings.from_env({"TRADING_MODE": "paper"})

    assert settings.phase2_enabled is False
    assert settings.phase2_symbols == ()
    assert settings.phase2_min_oi_sources == 2
    assert settings.phase2_min_funding_sources == 2


def test_cycle_result_is_utc_and_reports_isolated_errors() -> None:
    now = datetime.now(timezone.utc)
    result = Phase2CycleResult(now, now, 2, 4, 4, 2, ("bitget:BTCUSDT:TimeoutError",))

    assert result.completed_at.tzinfo is not None
    assert result.errors == ("bitget:BTCUSDT:TimeoutError",)


def test_default_registry_has_explicit_exchange_mappings() -> None:
    registry = default_registry()
    symbol = registry.resolve("hyperliquid", "BTC")

    assert symbol.canonical_symbol == "BTC-USDT-PERP"
    assert symbol.exchange_symbols == {"bitget": "BTCUSDT", "bybit": "BTCUSDT", "hyperliquid": "BTC"}
