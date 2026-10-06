from datetime import datetime, timezone
from decimal import Decimal

from quant_data_layer.admission import ReplayClass
from quant_data_layer.backpressure import BackpressureAction
from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide
from quant_phase3.health import TradeHealthRegistry
from quant_phase3.queue import BoundedTradeQueue


NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def trade(trade_id="1"):
    return CanonicalTrade(
        exchange="bybit",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        trade_id=trade_id,
        price=Decimal("100"),
        quantity_base=Decimal("1"),
        notional_usd=Decimal("100"),
        aggressor_side=TradeSide.BUY,
        raw_side="Buy",
        raw_side_semantics="EXCHANGE_PROVIDED_TAKER_SIDE",
        side_source=SideSource.EXCHANGE_PROVIDED,
        exchange_timestamp=NOW,
        received_at=NOW,
        processed_at=NOW,
        source_channel="test",
        status=FlowStatus.AVAILABLE,
    )


def test_queue_is_bounded_and_emits_backpressure_event_before_drop():
    queue = BoundedTradeQueue(exchange="bybit", symbol="BTCUSDT", capacity=1)

    assert queue.put_nowait(trade("1"), now=NOW) is True
    assert queue.put_nowait(trade("2"), now=NOW) is False
    event = queue.backpressure_events[0]

    assert queue.depth == 1
    assert queue.dropped_count == 1
    assert event.exchange == "bybit"
    assert event.symbol == "BTCUSDT"
    assert event.queue_capacity == 1
    assert event.dropped_count == 1
    assert event.status is FlowStatus.PARTIAL
    assert event.stream_id == "phase3.bybit_trades"
    assert event.replay_class is ReplayClass.CANONICAL_UNRECOVERABLE
    assert event.overload_action is BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL
    assert queue.get_nowait().trade_id == "1"


def test_queue_rejects_invalid_capacity_and_does_not_grow_after_drops():
    try:
        BoundedTradeQueue(exchange="bybit", symbol="BTCUSDT", capacity=0)
    except ValueError:
        pass
    else:
        raise AssertionError("queue capacity must be positive")

    queue = BoundedTradeQueue(exchange="bybit", symbol="BTCUSDT", capacity=2)
    for index in range(10):
        queue.put_nowait(trade(str(index)), now=NOW)
    assert queue.depth == 2
    assert queue.dropped_count == 8


def test_health_isolated_per_exchange_and_records_reconnect_gap_and_backpressure():
    health = TradeHealthRegistry()

    health.mark_connected("bybit", NOW)
    health.record_subscription("bybit", "BTCUSDT")
    health.record_reconnect("bybit", NOW)
    health.record_gap("bybit", reason="TRADE_GAP", now=NOW)
    health.record_backpressure("bitget", dropped=3, now=NOW)

    bybit = health.snapshot("bybit")
    bitget = health.snapshot("bitget")

    assert bybit.connected is True
    assert bybit.subscribed_symbols == ("BTCUSDT",)
    assert bybit.reconnect_count == 1
    assert bybit.gap_count == 1
    assert bybit.status is FlowStatus.PARTIAL
    assert bitget.connected is False
    assert bitget.dropped_trades == 3
    assert bitget.status is FlowStatus.PARTIAL


def test_health_heartbeat_is_compact_and_utc():
    health = TradeHealthRegistry()
    health.mark_connected("hyperliquid", NOW)

    heartbeat = health.heartbeat("hyperliquid", NOW)

    assert heartbeat["exchange"] == "hyperliquid"
    assert heartbeat["status"] == "AVAILABLE"
    assert heartbeat["last_event_at"].endswith("+00:00")
