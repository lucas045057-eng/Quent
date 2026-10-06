from datetime import datetime, timezone
from decimal import Decimal

from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide
from quant_phase3.flow import TradeFlowWindowBuilder


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def trade(
    *,
    exchange="bybit",
    trade_id="1",
    side=TradeSide.BUY,
    raw_side="Buy",
    quantity="1",
    timestamp=BASE,
    notional=True,
):
    return CanonicalTrade(
        exchange=exchange,
        exchange_symbol="BTCUSDT" if exchange != "hyperliquid" else "BTC",
        canonical_symbol="BTC-USDT-PERP",
        trade_id=trade_id,
        price=Decimal("100"),
        quantity_base=Decimal(quantity),
        notional_usd=Decimal(quantity) * Decimal("100") if notional else None,
        aggressor_side=side,
        raw_side=raw_side,
        raw_side_semantics=(
            "EXCHANGE_PROVIDED_TAKER_SIDE"
            if side is not TradeSide.UNKNOWN
            else "TRADE_SIDE_UNCONFIRMED_AGGRESSOR"
        ),
        side_source=(SideSource.EXCHANGE_PROVIDED if side is not TradeSide.UNKNOWN else SideSource.UNKNOWN),
        exchange_timestamp=timestamp,
        received_at=timestamp,
        processed_at=timestamp,
        source_channel="test",
        status=FlowStatus.AVAILABLE,
    )


def test_one_minute_window_conserves_directional_and_unknown_volume():
    builder = TradeFlowWindowBuilder(timeframe="1m", window_seconds=60, allowed_lateness_seconds=5)

    assert builder.add(trade(trade_id="buy", quantity="2"), now=BASE) is True
    assert builder.add(
        trade(
            trade_id="sell",
            side=TradeSide.SELL,
            raw_side="Sell",
            quantity="3",
            timestamp=BASE.replace(second=20),
        ),
        now=BASE,
    ) is True
    assert builder.add(
        trade(
            trade_id="unknown",
            side=TradeSide.UNKNOWN,
            raw_side="buy",
            quantity="4",
            timestamp=BASE.replace(second=30),
        ),
        now=BASE,
    ) is True

    windows = builder.finalize(BASE.replace(minute=1, second=6), processed_at=BASE.replace(minute=1, second=7))
    window = windows[0]

    assert window.timeframe == "1m"
    assert window.total_trade_count == 3
    assert window.buy_trade_count == 1
    assert window.sell_trade_count == 1
    assert window.unknown_trade_count == 1
    assert window.total_volume_base == Decimal("9")
    assert window.buy_volume_base == Decimal("2")
    assert window.sell_volume_base == Decimal("3")
    assert window.unknown_volume_base == Decimal("4")
    assert window.delta_base == Decimal("-1")
    assert window.delta_ratio == Decimal("-0.1111111111111111111111111111")
    assert window.total_notional_usd == Decimal("900")
    assert window.average_trade_size == Decimal("3")
    assert window.trade_frequency == Decimal("0.05")
    assert window.status is FlowStatus.AVAILABLE


def test_non_directional_source_contributes_total_volume_only():
    builder = TradeFlowWindowBuilder(timeframe="1m", window_seconds=60, allowed_lateness_seconds=5)

    builder.add(
        trade(
            exchange="bitget",
            trade_id="bg-1",
            side=TradeSide.UNKNOWN,
            raw_side="buy",
            quantity="2",
        ),
        now=BASE,
    )
    window = builder.finalize(BASE.replace(minute=1, second=6), processed_at=BASE.replace(minute=1, second=7))[0]

    assert window.total_volume_base == Decimal("2")
    assert window.unknown_volume_base == Decimal("2")
    assert window.buy_volume_base == Decimal("0")
    assert window.sell_volume_base == Decimal("0")
    assert window.delta_base is None
    assert window.delta_ratio is None
    assert window.status is FlowStatus.AVAILABLE


def test_missing_notional_is_not_fabricated_and_partial_window_is_explicit():
    builder = TradeFlowWindowBuilder(timeframe="1m", window_seconds=60, allowed_lateness_seconds=5)
    builder.add(trade(trade_id="no-notional", notional=False), now=BASE)
    builder.mark_partial(
        exchange="bybit",
        canonical_symbol="BTC-USDT-PERP",
        window_open=BASE,
        reason="TRADE_GAP",
    )

    window = builder.finalize(BASE.replace(minute=1, second=6), processed_at=BASE.replace(minute=1, second=7))[0]

    assert window.total_notional_usd is None
    assert window.status is FlowStatus.PARTIAL
    assert window.status_reason == "TRADE_GAP"
