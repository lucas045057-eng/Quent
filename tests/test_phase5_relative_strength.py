from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase1.config import Settings
from quant_phase5.contracts import ContextStatus, DirectionState, MarketLeaderContext, RelativeStrengthClass
from quant_phase5.relative_strength import compute_relative_strength


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


def context(symbol: str, return_pct: str | None, status: ContextStatus = ContextStatus.AVAILABLE, timeframe: str = "15m"):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketLeaderContext(
        symbol=symbol,
        timeframe=timeframe,
        context_timestamp=NOW,
        input_window_start=NOW,
        input_window_end=NOW,
        return_pct=None if unavailable else Decimal(return_pct or "0"),
        trend_state="NOT_AVAILABLE" if unavailable else DirectionState.RANGE,
        structure_state="NOT_AVAILABLE" if unavailable else "RANGE",
        volatility_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volume_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volatility_value=None if unavailable else Decimal("0.001"),
        volume_ratio=None if unavailable else Decimal("1"),
        freshness_status=status,
        data_quality={"test": True},
        source_count=1 if not unavailable else 0,
        missing_count=0 if not unavailable else 1,
        status=status,
        processed_at=NOW,
        reason_code="MISSING_INPUT" if unavailable else None,
        missing_evidence=("missing",) if unavailable else (),
    )


def test_self_comparison_is_not_available():
    row = compute_relative_strength(
        "BTCUSDT", context("BTCUSDT", "1"), "BTCUSDT", context("BTCUSDT", "1"),
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
    )
    assert row.status is ContextStatus.NOT_AVAILABLE
    assert row.reason_code.value == "MISSING_INPUT"
    assert row.relative_class is RelativeStrengthClass.NOT_AVAILABLE
    assert row.relative_return_pct is None


def test_btc_eth_relative_return_and_threshold_boundaries():
    settings = Settings.from_env({})
    strong = compute_relative_strength(
        "ETHUSDT", context("ETHUSDT", "1"), "BTCUSDT", context("BTCUSDT", "0.8"),
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=settings,
    )
    weak = compute_relative_strength(
        "ETHUSDT", context("ETHUSDT", "0"), "BTCUSDT", context("BTCUSDT", "0.2"),
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=settings,
    )
    assert strong.relative_return_pct == Decimal("0.2")
    assert strong.relative_class is RelativeStrengthClass.STRONG
    assert weak.relative_class is RelativeStrengthClass.WEAK


def test_market_benchmark_excludes_candidate_and_preserves_universe_identity():
    symbols = [f"S{index:03d}USDT" for index in range(21)]
    universe = {symbol: context(symbol, "0") for symbol in symbols}
    universe["S000USDT"] = context("S000USDT", "1")
    row = compute_relative_strength(
        "S000USDT", universe["S000USDT"], "MARKET_UNIVERSE_EQUAL_WEIGHT", None,
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
        universe_run_id=9, universe_contexts=universe,
    )
    assert row.universe_run_id == 9
    assert row.sample_size == 20
    assert row.available_count == 20
    assert row.benchmark_return_pct == Decimal("0")
    assert row.relative_return_pct == Decimal("1")


def test_market_benchmark_coverage_shortfall_is_not_available():
    symbols = [f"S{index:03d}USDT" for index in range(20)]
    universe = {symbol: context(symbol, "0") for symbol in symbols[:16]}
    universe["S000USDT"] = context("S000USDT", "1")
    row = compute_relative_strength(
        "S000USDT", universe["S000USDT"], "MARKET_UNIVERSE_EQUAL_WEIGHT", None,
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
        universe_run_id=9, universe_contexts=universe,
    )
    assert row.status is ContextStatus.NOT_AVAILABLE
    assert row.reason_code.value == "INSUFFICIENT_COVERAGE"
    assert row.benchmark_return_pct is None


def test_market_benchmark_with_accepted_missing_member_is_partial():
    symbols = [f"S{index:03d}USDT" for index in range(21)]
    universe = {symbol: context(symbol, "0") for symbol in symbols}
    universe["S000USDT"] = context("S000USDT", "1")
    universe["S020USDT"] = None
    row = compute_relative_strength(
        "S000USDT", universe["S000USDT"], "MARKET_UNIVERSE_EQUAL_WEIGHT", None,
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
        universe_run_id=9, universe_contexts=universe,
    )
    assert row.status is ContextStatus.PARTIAL
    assert row.missing_count == 1
    assert row.benchmark_return_pct == Decimal("0")
    assert row.missing_evidence == ("missing:S020USDT",)


def test_unavailable_member_with_wrong_identity_is_rejected():
    symbols = [f"S{index:03d}USDT" for index in range(21)]
    universe = {symbol: context(symbol, "0") for symbol in symbols}
    universe["S000USDT"] = context("S000USDT", "1")
    universe["S020USDT"] = context("S020USDT", None, ContextStatus.NOT_AVAILABLE, timeframe="1H")
    with pytest.raises(ValueError, match="universe context identity"):
        compute_relative_strength(
            "S000USDT", universe["S000USDT"], "MARKET_UNIVERSE_EQUAL_WEIGHT", None,
            timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
            universe_run_id=9, universe_contexts=universe,
        )


def test_stale_and_5m_inputs_are_explicitly_blocked():
    stale = compute_relative_strength(
        "ETHUSDT", context("ETHUSDT", "1", ContextStatus.STALE), "BTCUSDT", context("BTCUSDT", "0"),
        timeframe="15m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
    )
    assert stale.status is ContextStatus.STALE
    with pytest.raises(ValueError, match="15m, 1H, and 4H"):
        compute_relative_strength(
            "ETHUSDT", context("ETHUSDT", "1", timeframe="5m"), "BTCUSDT", context("BTCUSDT", "0", timeframe="5m"),
            timeframe="5m", context_timestamp=NOW, processed_at=NOW, settings=Settings.from_env({}),
        )
