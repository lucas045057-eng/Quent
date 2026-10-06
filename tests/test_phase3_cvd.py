from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase3.contracts import FlowStatus
from quant_phase3.cvd import BybitCVDBuilder
from quant_phase3.flow import TradeFlowWindow


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def window(index, *, delta="1", status=FlowStatus.AVAILABLE, exchange="bybit", reason=None):
    opened = BASE + timedelta(minutes=index)
    value = Decimal("10")
    return TradeFlowWindow(
        exchange=exchange,
        canonical_symbol="BTC-USDT-PERP",
        timeframe="1m",
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        total_trade_count=2,
        buy_trade_count=1,
        sell_trade_count=1,
        unknown_trade_count=0,
        total_volume_base=value,
        buy_volume_base=value / 2 + Decimal(delta) / 2,
        sell_volume_base=value / 2 - Decimal(delta) / 2,
        unknown_volume_base=Decimal("0"),
        total_notional_usd=value * Decimal("100"),
        average_trade_size=value / 2,
        trade_frequency=Decimal(2) / Decimal(60),
        delta_base=Decimal(delta) if exchange == "bybit" else None,
        delta_ratio=Decimal(delta) / value if exchange == "bybit" else None,
        first_trade_at=opened,
        last_trade_at=opened,
        freshness=status,
        status=status,
        status_reason=reason,
        processed_at=opened + timedelta(minutes=1),
    )


def test_bybit_cvd_is_positive_negative_and_zero_delta_sum_over_horizon():
    builder = BybitCVDBuilder()
    for index, delta in enumerate(("2", "-1", "0")):
        points = builder.ingest(window(index, delta=delta), processed_at=BASE + timedelta(minutes=index + 1))
    for index in range(3, 15):
        points = builder.ingest(window(index, delta="0"), processed_at=BASE + timedelta(minutes=index + 1))

    point = {row.timeframe: row for row in points}["15m"]
    assert point.value == Decimal("1")
    assert point.status is FlowStatus.AVAILABLE


def test_bybit_cvd_excludes_unknown_or_non_directional_sources():
    builder = BybitCVDBuilder()
    points = builder.ingest(window(0, delta="2"), processed_at=BASE + timedelta(minutes=1))
    assert all(point.value == Decimal("2") for point in points)

    unavailable = builder.ingest(
        window(1, delta="2", exchange="bitget"), processed_at=BASE + timedelta(minutes=2)
    )
    assert unavailable == ()


def test_bybit_cvd_blocks_partial_and_stale_source_windows():
    builder = BybitCVDBuilder()

    partial = builder.ingest(
        window(0, status=FlowStatus.PARTIAL, reason="TRADE_GAP"),
        processed_at=BASE + timedelta(minutes=1),
    )
    stale = builder.ingest(
        window(1, status=FlowStatus.STALE, reason="STALE_SOURCE"),
        processed_at=BASE + timedelta(minutes=2),
    )

    assert partial and all(point.status is FlowStatus.NOT_AVAILABLE for point in partial)
    assert all(point.reason in {"SOURCE_WINDOW_PARTIAL", "SOURCE_WINDOW_STALE"} for point in (*partial, *stale))


def test_bybit_cvd_horizon_expires_old_delta_and_restart_hydration_is_deterministic():
    initial = [window(index, delta="1") for index in range(16)]
    first = BybitCVDBuilder()
    for item in initial:
        first.ingest(item, processed_at=item.processed_at)
    continued = first.ingest(window(16, delta="5"), processed_at=BASE + timedelta(minutes=17))

    restarted = BybitCVDBuilder()
    restarted.hydrate(initial)
    recovered = restarted.ingest(window(16, delta="5"), processed_at=BASE + timedelta(minutes=17))

    assert {row.timeframe: row.value for row in continued} == {
        row.timeframe: row.value for row in recovered
    }
    assert {row.timeframe: row.value for row in continued}["15m"] == Decimal("19")

    for index in range(17, 31):
        first.ingest(window(index, delta="1"), processed_at=BASE + timedelta(minutes=index + 1))
    oldest = first.ingest(window(31, delta="1"), processed_at=BASE + timedelta(minutes=32))
    assert {row.timeframe: row.value for row in oldest}["15m"] == Decimal("15")
