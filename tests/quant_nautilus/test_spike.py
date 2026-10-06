from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from quant_nautilus.spike import FixtureFeatureV1, bind_fixture_instrument, run_fixture_spike


T0 = 1_700_000_000_000_000_000


def test_explicit_instrument_binding_rejects_wrong_core_or_venue():
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    binding = bind_fixture_instrument(
        core_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        venue="BINANCE",
        venue_symbol="BTCUSDT",
        instrument=btc,
    )
    assert binding.instrument_id == "BTCUSDT-PERP.BINANCE"
    assert binding.core_symbol == "BTCUSDT"
    with pytest.raises(ValueError):
        bind_fixture_instrument(
            core_symbol="ETHUSDT", canonical_symbol="ETH-USDT-PERP",
            venue="BINANCE", venue_symbol="ETHUSDT", instrument=btc,
        )
    with pytest.raises(ValueError):
        bind_fixture_instrument(
            core_symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP",
            venue="BITGET", venue_symbol="BTCUSDT", instrument=btc,
        )


def test_feature_rejects_unknown_quality_and_future_availability():
    with pytest.raises(ValueError):
        FixtureFeatureV1(
            core_symbol="BTCUSDT", pattern="TREND_CONTINUATION", direction="LONG",
            observed_at_ns=T0, known_at_ns=T0 + 2_000_000_000,
            valid_until_ns=T0 + 1_000_000_000, quality="AVAILABLE",
            source_ref="fixture:1", oi_usd=Decimal("1000"),
        )
    with pytest.raises(ValueError):
        FixtureFeatureV1(
            core_symbol="BTCUSDT", pattern="TREND_CONTINUATION", direction="LONG",
            observed_at_ns=T0, known_at_ns=T0,
            valid_until_ns=T0 + 2_000_000_000, quality="UNKNOWN",
            source_ref="fixture:1", oi_usd=Decimal("1000"),
        )


def test_pinned_engine_runs_btc_long_and_eth_short_with_real_fills():
    result = run_fixture_spike()
    assert result.nautilus_version == "1.231.0"
    assert result.acceptance_kind == "FIXTURE_DRIVEN_ACCEPTANCE"
    assert {(fill.core_symbol, fill.direction, fill.pattern) for fill in result.fills} == {
        ("BTCUSDT", "LONG", "TREND_CONTINUATION"),
        ("ETHUSDT", "SHORT", "BREAKOUT_CONFIRMATION"),
    }
    assert len(result.fills) == 2
    assert all(fill.order_id and fill.execution_id for fill in result.fills)
    assert all(fill.quantity == Decimal("0.010") for fill in result.fills)
    assert all(fill.commission_usdt > 0 for fill in result.fills)
    assert all(fill.filled_at_ns >= fill.feature_known_at_ns for fill in result.fills)
    assert result.positions == {"BTCUSDT": "LONG", "ETHUSDT": "SHORT"}
    assert result.total_commission_usdt == sum(fill.commission_usdt for fill in result.fills)
