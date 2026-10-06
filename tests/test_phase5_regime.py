from datetime import datetime, timezone
from decimal import Decimal

from quant_phase5.contracts import BreadthState, ContextStatus, MarketBreadthSnapshot, MarketLeaderContext
from quant_phase5.regime import compute_market_regime
from quant_phase1.config import Settings


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


def leader(symbol: str, *, trend: str = "TREND_UP", volatility: str = "NORMAL", status: ContextStatus = ContextStatus.AVAILABLE):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketLeaderContext(
        symbol=symbol,
        timeframe="15m",
        context_timestamp=NOW,
        input_window_start=NOW,
        input_window_end=NOW,
        return_pct=None if unavailable else Decimal("1"),
        trend_state="NOT_AVAILABLE" if unavailable else trend,
        structure_state="NOT_AVAILABLE" if unavailable else "RANGE",
        volatility_state="NOT_AVAILABLE" if unavailable else volatility,
        volume_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volatility_value=None if unavailable else Decimal("0.003"),
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


def breadth(state: str = "BROAD_STRENGTH", status: ContextStatus = ContextStatus.AVAILABLE):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketBreadthSnapshot(
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=7,
        sample_size=20,
        available_count=20,
        missing_count=0,
        coverage_ratio=Decimal("1"),
        positive_ratio=None if unavailable else Decimal("0.7"),
        up_structure_ratio=None if unavailable else Decimal("0.7"),
        down_structure_ratio=None if unavailable else Decimal("0.1"),
        breadth_state="NOT_AVAILABLE" if unavailable else state,
        status=status,
        processed_at=NOW,
        missing_evidence=("missing",) if unavailable else (),
        reason_code="MISSING_INPUT" if unavailable else None,
    )


def test_both_leaders_and_breadth_produce_independent_regime_dimensions():
    row = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT", volatility="HIGH"), "ETHUSDT": leader("ETHUSDT")},
        breadth(),
        timeframe="15m",
        context_timestamp=NOW,
        settings=Settings.from_env({}),
        processed_at=NOW,
    )

    assert row.direction_regime.value == "TREND_UP"
    assert row.direction_status is ContextStatus.AVAILABLE
    assert row.volatility_regime.value == "HIGH"
    assert row.volatility_status is ContextStatus.AVAILABLE
    assert row.breadth_regime is BreadthState.BROAD_STRENGTH
    assert row.breadth_status is ContextStatus.AVAILABLE
    assert row.status is ContextStatus.AVAILABLE


def test_one_missing_leader_keeps_dimension_statuses_partial_and_no_score():
    row = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT"), "ETHUSDT": leader("ETHUSDT", status=ContextStatus.NOT_AVAILABLE)},
        breadth(),
        timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )

    assert row.direction_regime.value == "NOT_AVAILABLE"
    assert row.direction_status is ContextStatus.PARTIAL
    assert row.volatility_regime.value == "NORMAL"
    assert row.volatility_status is ContextStatus.PARTIAL
    assert row.status is ContextStatus.PARTIAL
    assert row.missing_evidence


def test_missing_breadth_keeps_leader_context_but_direction_is_unavailable():
    row = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT"), "ETHUSDT": leader("ETHUSDT")},
        breadth(status=ContextStatus.NOT_AVAILABLE),
        timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )

    assert row.direction_regime.value == "NOT_AVAILABLE"
    assert row.direction_status is ContextStatus.PARTIAL
    assert row.volatility_status is ContextStatus.AVAILABLE
    assert row.breadth_status is ContextStatus.NOT_AVAILABLE
    assert row.status is ContextStatus.PARTIAL


def test_partial_breadth_missing_evidence_is_preserved_in_regime():
    row = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT"), "ETHUSDT": leader("ETHUSDT")},
        breadth(status=ContextStatus.PARTIAL),
        timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )

    assert row.status is ContextStatus.PARTIAL
    assert "missing" in row.missing_evidence


def test_stale_and_error_precedence_is_explicit():
    stale = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT", status=ContextStatus.STALE), "ETHUSDT": leader("ETHUSDT")},
        breadth(), timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )
    error = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT", status=ContextStatus.ERROR), "ETHUSDT": leader("ETHUSDT")},
        breadth(), timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )

    assert stale.status is ContextStatus.STALE
    assert error.status is ContextStatus.ERROR
    assert error.direction_status is ContextStatus.ERROR

    breadth_stale = compute_market_regime(
        {"BTCUSDT": leader("BTCUSDT"), "ETHUSDT": leader("ETHUSDT", status=ContextStatus.NOT_AVAILABLE)},
        breadth(status=ContextStatus.STALE),
        timeframe="15m", context_timestamp=NOW, settings=Settings.from_env({}), processed_at=NOW,
    )
    assert breadth_stale.direction_status is ContextStatus.STALE
    assert breadth_stale.status is ContextStatus.STALE
