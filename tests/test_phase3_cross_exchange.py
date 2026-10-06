from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase3.contracts import FlowStatus
from quant_phase3.cross_exchange import CrossExchangeFlowSnapshot, build_cross_exchange_flow_snapshot
from quant_phase3.flow import TradeFlowWindow


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def row(exchange, *, volume="1", delta=None, status=FlowStatus.AVAILABLE):
    opened = BASE
    total = Decimal(volume)
    return TradeFlowWindow(
        exchange=exchange,
        canonical_symbol="BTC-USDT-PERP",
        timeframe="1m",
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        total_trade_count=1,
        buy_trade_count=1 if delta is not None else 0,
        sell_trade_count=0,
        unknown_trade_count=0 if delta is not None else 1,
        total_volume_base=total,
        buy_volume_base=total if delta is not None else Decimal("0"),
        sell_volume_base=Decimal("0"),
        unknown_volume_base=Decimal("0") if delta is not None else total,
        total_notional_usd=total * Decimal("100"),
        average_trade_size=total,
        trade_frequency=Decimal(1) / Decimal(60),
        delta_base=Decimal(delta) if delta is not None else None,
        delta_ratio=Decimal(delta) / total if delta is not None else None,
        first_trade_at=opened,
        last_trade_at=opened,
        freshness=status,
        status=status,
        status_reason=None,
        processed_at=opened + timedelta(minutes=1),
    )


def test_cross_exchange_counts_volume_and_directional_sources_separately():
    snapshot = build_cross_exchange_flow_snapshot(
        [row("bybit", volume="2", delta="2"), row("bitget", volume="3"), row("hyperliquid", volume="4")],
        snapshot_timestamp=BASE + timedelta(minutes=1),
        processed_at=BASE + timedelta(minutes=1),
        min_directional_sources=2,
    )

    assert isinstance(snapshot, CrossExchangeFlowSnapshot)
    assert snapshot.volume_exchange_count == 3
    assert snapshot.directional_exchange_count == 1
    assert snapshot.total_volume_base == Decimal("9")
    assert snapshot.directional_delta_base == Decimal("2")
    assert snapshot.directional_status == "INSUFFICIENT_DIRECTIONAL_SOURCES"
    assert snapshot.status is FlowStatus.AVAILABLE
    assert snapshot.reason == "INSUFFICIENT_DIRECTIONAL_SOURCES"


def test_cross_exchange_directional_context_is_available_only_when_minimum_is_met():
    snapshot = build_cross_exchange_flow_snapshot(
        [row("bybit", volume="2", delta="2"), row("other", volume="3", delta="-1")],
        snapshot_timestamp=BASE + timedelta(minutes=1),
        processed_at=BASE + timedelta(minutes=1),
        min_directional_sources=2,
    )

    assert snapshot.directional_exchange_count == 2
    assert snapshot.directional_status == "AVAILABLE"
    assert snapshot.directional_delta_base == Decimal("1")
    assert snapshot.directional_delta_ratio == Decimal("0.2")
    assert snapshot.reason is None


def test_cross_exchange_ignores_stale_partial_sources_without_fabricating_volume():
    snapshot = build_cross_exchange_flow_snapshot(
        [row("bybit", volume="2", delta="2"), row("bitget", volume="3", status=FlowStatus.STALE)],
        snapshot_timestamp=BASE + timedelta(minutes=1),
        processed_at=BASE + timedelta(minutes=1),
        min_directional_sources=1,
    )

    assert snapshot.volume_exchange_count == 1
    assert snapshot.total_volume_base == Decimal("2")
    assert snapshot.directional_exchange_count == 1
