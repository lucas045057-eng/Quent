from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase3.contracts import FlowStatus
from quant_phase3.flow import TradeFlowWindow
from quant_phase3.rollup import FlowRollupBuilder


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def minute_window(index, *, base=BASE, status=FlowStatus.AVAILABLE, reason=None, quantity="1"):
    opened = base + timedelta(minutes=index)
    total = Decimal(quantity)
    return TradeFlowWindow(
        exchange="bybit",
        canonical_symbol="BTC-USDT-PERP",
        timeframe="1m",
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        total_trade_count=1,
        buy_trade_count=1,
        sell_trade_count=0,
        unknown_trade_count=0,
        total_volume_base=total,
        buy_volume_base=total,
        sell_volume_base=Decimal("0"),
        unknown_volume_base=Decimal("0"),
        total_notional_usd=total * Decimal("100"),
        average_trade_size=total,
        trade_frequency=Decimal(1) / Decimal(60),
        delta_base=total,
        delta_ratio=Decimal("1"),
        first_trade_at=opened,
        last_trade_at=opened,
        freshness=status,
        status=status,
        status_reason=reason,
        processed_at=opened + timedelta(minutes=1),
    )


def test_rollup_combines_complete_minutes_without_double_counting():
    rows = [minute_window(index, quantity=str(index + 1)) for index in range(5)]
    rows.append(minute_window(0, quantity="999"))

    result = FlowRollupBuilder().build(rows, timeframe="5m", processed_at=BASE + timedelta(minutes=5))

    assert len(result) == 1
    window = result[0]
    assert window.window_open == BASE
    assert window.window_close == BASE + timedelta(minutes=5)
    assert window.total_trade_count == 5
    assert window.total_volume_base == Decimal("15")
    assert window.buy_volume_base == Decimal("15")
    assert window.delta_base == Decimal("15")
    assert window.average_trade_size == Decimal("3")
    assert window.trade_frequency == Decimal("0.01666666666666666666666666667")
    assert window.status is FlowStatus.AVAILABLE


def test_rollup_marks_missing_minute_partial_and_does_not_fabricate_values():
    rows = [minute_window(index) for index in (0, 1, 3, 4)]

    window = FlowRollupBuilder().build(
        rows, timeframe="5m", processed_at=BASE + timedelta(minutes=5)
    )[0]

    assert window.status is FlowStatus.PARTIAL
    assert window.status_reason == "MISSING_MINUTE_WINDOWS"
    assert window.total_trade_count == 4
    assert window.total_volume_base == Decimal("4")


def test_rollup_preserves_partial_source_and_supports_all_phase3_timeframes():
    aligned = BASE.replace(hour=0, minute=0)
    for timeframe, seconds in (("5m", 300), ("15m", 900), ("1H", 3600), ("4H", 14400)):
        rows = [minute_window(index, base=aligned) for index in range(seconds // 60)]
        rows[0] = minute_window(0, base=aligned, status=FlowStatus.PARTIAL, reason="TRADE_GAP")
        result = FlowRollupBuilder().build(
            rows, timeframe=timeframe, processed_at=BASE + timedelta(seconds=seconds)
        )
        assert len(result) == 1
        assert result[0].status is FlowStatus.PARTIAL
        assert result[0].status_reason == "TRADE_GAP"
