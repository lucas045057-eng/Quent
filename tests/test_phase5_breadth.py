from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase5.breadth import compute_market_breadth
from quant_phase5.contracts import ContextStatus, DirectionState, MarketLeaderContext, StructureState
from quant_phase1.config import Settings


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


def leader(
    symbol: str,
    return_pct: str,
    *,
    structure: str = "RANGE",
    status: ContextStatus = ContextStatus.AVAILABLE,
    context_timestamp: datetime = NOW,
):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketLeaderContext(
        symbol=symbol,
        timeframe="15m",
        context_timestamp=context_timestamp,
        input_window_start=context_timestamp,
        input_window_end=context_timestamp,
        return_pct=None if unavailable else Decimal(return_pct),
        trend_state="NOT_AVAILABLE" if unavailable else DirectionState.RANGE,
        structure_state="NOT_AVAILABLE" if unavailable else structure,
        volatility_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volume_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volatility_value=None if unavailable else Decimal("0.001"),
        volume_ratio=None if unavailable else Decimal("1"),
        freshness_status=status,
        data_quality={"test": True},
        source_count=1 if not unavailable else 0,
        missing_count=0 if not unavailable else 1,
        status=status,
        processed_at=context_timestamp,
        reason_code="MISSING_INPUT" if unavailable else None,
        missing_evidence=("missing",) if unavailable else (),
    )


def symbols(count: int = 20) -> list[str]:
    return [f"S{index:03d}USDT" for index in range(count)]


def test_exact_coverage_boundary_is_partial_and_missing_is_not_zero():
    universe = symbols()
    contexts = {
        symbol: leader(symbol, "1" if index < 9 else "-1", structure="HIGHER_HIGH_HIGHER_LOW")
        for index, symbol in enumerate(universe[:16])
    }
    row = compute_market_breadth(
        universe,
        contexts,
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=7,
        settings=Settings.from_env({}),
        processed_at=NOW,
    )

    assert row.sample_size == 20
    assert row.available_count == 16
    assert row.missing_count == 4
    assert row.coverage_ratio == Decimal("0.8")
    assert row.status is ContextStatus.PARTIAL
    assert row.breadth_state.value == "NARROW_STRENGTH"
    assert row.positive_ratio == Decimal("0.5625")
    assert row.missing_evidence


def test_below_coverage_is_not_available_and_has_no_numeric_breadth_values():
    universe = symbols()
    contexts = {symbol: leader(symbol, "1") for symbol in universe[:15]}
    row = compute_market_breadth(
        universe,
        contexts,
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=7,
        settings=Settings.from_env({}),
        processed_at=NOW,
    )

    assert row.status is ContextStatus.NOT_AVAILABLE
    assert row.breadth_state.value == "NOT_AVAILABLE"
    assert row.coverage_ratio == Decimal("0.75")
    assert row.positive_ratio is None


def test_large_missing_universe_uses_bounded_evidence():
    universe = symbols(200)
    row = compute_market_breadth(
        universe,
        {},
        timeframe="15m",
        context_timestamp=NOW,
        universe_run_id=7,
        settings=Settings.from_env({}),
        processed_at=NOW,
    )

    assert row.status is ContextStatus.NOT_AVAILABLE
    assert row.missing_count == 200
    assert len(row.missing_evidence) == 64
    assert row.missing_evidence[-1] == "omitted:137"


def test_broad_and_narrow_boundaries_are_exact():
    universe = symbols()
    settings = Settings.from_env({})
    broad = compute_market_breadth(
        universe,
        {symbol: leader(symbol, "1" if index < 12 else "0") for index, symbol in enumerate(universe)},
        timeframe="15m", context_timestamp=NOW, universe_run_id=7, settings=settings, processed_at=NOW,
    )
    narrow = compute_market_breadth(
        universe,
        {symbol: leader(symbol, "1" if index < 11 else "0") for index, symbol in enumerate(universe)},
        timeframe="15m", context_timestamp=NOW, universe_run_id=8, settings=settings, processed_at=NOW,
    )

    assert broad.breadth_state.value == "BROAD_STRENGTH"
    assert narrow.breadth_state.value == "NARROW_STRENGTH"


def test_timestamp_or_survivorship_mismatch_is_rejected():
    universe = symbols()
    with pytest.raises(ValueError, match="context_timestamp"):
        compute_market_breadth(
            universe,
            {universe[0]: leader(universe[0], "1", context_timestamp=NOW), universe[1]: leader(universe[1], "1", context_timestamp=NOW.replace(minute=1))},
            timeframe="15m", context_timestamp=NOW, universe_run_id=7,
            settings=Settings.from_env({}), processed_at=NOW,
        )


def test_context_symbol_must_match_universe_key_and_error_input_is_unavailable():
    universe = symbols()
    with pytest.raises(ValueError, match="context symbol"):
        compute_market_breadth(
            universe,
            {universe[0]: leader("OTHERUSDT", "1")},
            timeframe="15m", context_timestamp=NOW, universe_run_id=7,
            settings=Settings.from_env({}), processed_at=NOW,
        )

    row = compute_market_breadth(
        universe,
        {universe[0]: leader(universe[0], "0", status=ContextStatus.ERROR)},
        timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert row.status is ContextStatus.NOT_AVAILABLE
    assert row.reason_code.value == "SOURCE_UNAVAILABLE"

    mixed = compute_market_breadth(
        universe,
        {
            universe[0]: leader(universe[0], "0", status=ContextStatus.ERROR),
            universe[1]: leader(universe[1], "0", status=ContextStatus.STALE),
        },
        timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        settings=Settings.from_env({}), processed_at=NOW,
    )
    assert mixed.status is ContextStatus.STALE
    assert mixed.reason_code.value == "STALE_INPUT"

    with pytest.raises(ValueError, match="universe"):
        compute_market_breadth(
            universe,
            {"OTHERUSDT": leader("OTHERUSDT", "1")},
            timeframe="15m", context_timestamp=NOW, universe_run_id=7,
            settings=Settings.from_env({}), processed_at=NOW,
        )
