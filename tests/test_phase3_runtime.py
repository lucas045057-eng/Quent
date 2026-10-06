from datetime import datetime, timedelta, timezone
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json

from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide
from quant_phase3.cross_exchange import build_cross_exchange_flow_snapshot
from quant_phase3.queue import BoundedTradeQueue
from quant_phase3.runtime import Phase3PublicStreamRunner, Phase3TradeRuntime


NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def bybit_message(trade_id="id-1"):
    return {
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "ts": 1789866123004,
        "data": [{"T": 1789866123004, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "100", "i": trade_id, "seq": 1}],
    }


def bitget_message(trade_id="id-1"):
    return {
        "arg": {"instType": "usdt-futures", "topic": "publicTrade", "symbol": "BTCUSDT"},
        "action": "snapshot",
        "data": [{"i": trade_id, "p": "100", "v": "1", "S": "buy", "T": "1789866123004", "isRPI": "no"}],
    }


def test_runtime_start_stop_ingest_and_processes_bounded_trade_flow():
    asyncio.run(_test_runtime_start_stop_ingest_and_processes_bounded_trade_flow())


def test_process_all_pending_yields_to_other_tasks_between_symbols():
    asyncio.run(_test_process_all_pending_yields_to_other_tasks_between_symbols())


async def _test_process_all_pending_yields_to_other_tasks_between_symbols():
    runtime = Phase3TradeRuntime({"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")})
    symbols = tuple(f"S{index}USDT" for index in range(12))
    for symbol in symbols:
        runtime.register_active_symbol("bybit", symbol)
    processed = []

    def process_pending(exchange, symbol, **kwargs):
        processed.append(symbol)
        return ()

    runtime.process_pending = process_pending
    watermark = NOW + timedelta(minutes=3)
    drain = asyncio.create_task(runtime.process_all_pending_async(
        watermark=watermark, processed_at=watermark,
    ))
    observer = asyncio.create_task(asyncio.sleep(0))
    await observer
    processed_when_observer_ran = len(processed)
    await drain

    assert 0 < processed_when_observer_ran < len(symbols)
    assert len(processed) == len(symbols)


def test_process_pending_budget_defers_finalization_until_queue_drained():
    asyncio.run(_test_process_pending_budget_defers_finalization_until_queue_drained())


async def _test_process_pending_budget_defers_finalization_until_queue_drained():
    runtime = Phase3TradeRuntime({"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")})
    await runtime.start()
    event_time = datetime.fromtimestamp(1789866123004 / 1000, tz=timezone.utc)
    runtime.ingest_ws_message("bybit", bybit_message("first"), received_at=event_time)
    runtime.ingest_ws_message("bybit", bybit_message("second"), received_at=event_time)
    watermark = event_time + timedelta(minutes=3)

    first = runtime.process_pending(
        "bybit", "BTCUSDT", watermark=watermark, processed_at=watermark, max_trades=1,
    )
    assert first == ()
    assert runtime._queues[("bybit", "BTCUSDT")].depth == 1

    second = runtime.process_pending(
        "bybit", "BTCUSDT", watermark=watermark, processed_at=watermark, max_trades=1,
    )
    assert len(second) == 1
    assert second[0].total_trade_count == 2
    await runtime.stop()


def test_process_all_pending_async_caps_work_and_rotates_pending_symbols():
    asyncio.run(_test_process_all_pending_async_caps_work_and_rotates_pending_symbols())


async def _test_process_all_pending_async_caps_work_and_rotates_pending_symbols():
    runtime = Phase3TradeRuntime(
        {"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")}, queue_capacity=4,
    )
    symbols = tuple(f"S{index}USDT" for index in range(9))
    for symbol in symbols:
        canonical_symbol = f"{symbol[:-4]}-USDT-PERP"
        runtime.register_active_symbol("bybit", symbol)
        queue = BoundedTradeQueue(exchange="bybit", symbol=symbol, capacity=4)
        for index in range(4):
            queue.put_nowait(CanonicalTrade(
                exchange="bybit", exchange_symbol=symbol, canonical_symbol=canonical_symbol,
                trade_id=f"{symbol}-{index}", price=Decimal("100"), quantity_base=Decimal("1"),
                notional_usd=Decimal("100"), aggressor_side=TradeSide.BUY, raw_side="Buy",
                raw_side_semantics="EXCHANGE_PROVIDED_TAKER_SIDE", side_source=SideSource.EXCHANGE_PROVIDED,
                exchange_timestamp=NOW, received_at=NOW, processed_at=NOW, source_channel="test",
                status=FlowStatus.AVAILABLE,
            ), now=NOW)
        runtime._queues[("bybit", symbol)] = queue
        runtime._queue_builder_keys[("bybit", symbol)] = ("bybit", canonical_symbol)
    runtime._pending_backpressure_builders = set()
    watermark = NOW + timedelta(minutes=3)

    first_cycle = await runtime.process_all_pending_async(watermark=watermark, processed_at=watermark)
    assert len(first_cycle) == 8
    assert runtime._queues[("bybit", symbols[-1])].depth == 4

    second_cycle = await runtime.process_all_pending_async(watermark=watermark, processed_at=watermark)
    assert len(second_cycle) == 1
    assert all(queue.depth == 0 for queue in runtime._queues.values())


def test_queue_overflow_marks_flow_partial_and_excludes_it_from_directional_aggregation():
    asyncio.run(_test_queue_overflow_marks_flow_partial_and_excludes_it_from_directional_aggregation())


async def _test_queue_overflow_marks_flow_partial_and_excludes_it_from_directional_aggregation():
    runtime = Phase3TradeRuntime(
        {"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")},
        queue_capacity=1,
    )
    await runtime.start()
    event_time = datetime.fromtimestamp(1789866123004 / 1000, tz=timezone.utc)
    received_at = event_time
    runtime.ingest_ws_message("bybit", bybit_message("kept"), received_at=received_at)
    runtime.ingest_ws_message("bybit", bybit_message("dropped"), received_at=received_at)

    window = runtime.process_pending(
        "bybit", "BTCUSDT",
        watermark=event_time + timedelta(minutes=3),
        processed_at=event_time + timedelta(minutes=3),
    )[0]
    assert window.status is FlowStatus.PARTIAL
    assert window.freshness is FlowStatus.PARTIAL
    assert window.status_reason == "BACKPRESSURE_EVENT"

    snapshot = build_cross_exchange_flow_snapshot(
        (window,),
        snapshot_timestamp=window.window_close,
        processed_at=window.processed_at,
        min_directional_sources=1,
    )
    assert snapshot.directional_exchange_count == 0
    assert snapshot.directional_delta_base is None
    assert snapshot.directional_delta_ratio is None

    health = runtime.health.snapshot("bybit")
    assert health.gap_count == 1
    assert health.dropped_trades == 1
    await runtime.stop()


async def _test_runtime_start_stop_ingest_and_processes_bounded_trade_flow():
    runtime = Phase3TradeRuntime(
        {"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")},
        queue_capacity=4,
    )
    await runtime.start()

    assert runtime.ingest_ws_message("bybit", bybit_message(), received_at=NOW) == 1
    windows = runtime.process_pending(
        "bybit", "BTCUSDT", watermark=NOW.replace(minute=3, second=6), processed_at=NOW.replace(minute=3, second=7)
    )

    assert len(windows) == 1
    assert windows[0].total_volume_base == 1
    await runtime.stop()
    assert runtime.running is False


def test_runtime_uses_same_dedup_path_for_ws_and_rest_recovery():
    asyncio.run(_test_runtime_uses_same_dedup_path_for_ws_and_rest_recovery())


async def _test_runtime_uses_same_dedup_path_for_ws_and_rest_recovery():
    runtime = Phase3TradeRuntime(
        {"bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")},
        queue_capacity=4,
    )
    await runtime.start()
    assert runtime.ingest_ws_message("bybit", bybit_message(), received_at=NOW) == 1
    rest_payload = {
        "retCode": 0,
        "result": {"category": "linear", "list": [{"execId": "id-1", "symbol": "BTCUSDT", "price": "100", "size": "1", "side": "Buy", "time": "1789866123004"}]},
    }

    assert runtime.ingest_recent_trades("bybit", rest_payload, received_at=NOW, exchange_symbol="BTCUSDT") == 0
    assert runtime.dedup("bybit").duplicate_count == 1


def test_runtime_exchange_isolation_and_backpressure_are_observable():
    asyncio.run(_test_runtime_exchange_isolation_and_backpressure_are_observable())


async def _test_runtime_exchange_isolation_and_backpressure_are_observable():
    runtime = Phase3TradeRuntime(
        {
            "bybit": BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP"),
            "bitget": BitgetUTA3PublicTradeAdapter(canonical_symbol="BTC-USDT-PERP"),
        },
        queue_capacity=1,
    )
    await runtime.start()
    assert runtime.ingest_ws_message("bybit", bybit_message("one"), received_at=NOW) == 1
    assert runtime.ingest_ws_message("bybit", bybit_message("two"), received_at=NOW) == 0
    assert runtime.ingest_ws_message("bitget", bitget_message(), received_at=NOW) == 1

    assert runtime.health.snapshot("bybit").dropped_trades == 1
    assert runtime.health.snapshot("bitget").connected is True
    assert runtime.health.snapshot("bitget").dropped_trades == 0


def test_runtime_reconnect_restores_health_and_returns_public_resubscriptions():
    asyncio.run(_test_runtime_reconnect_restores_health_and_returns_public_resubscriptions())


def test_dynamic_runner_keeps_unchanged_trade_subscriptions_connected():
    asyncio.run(_test_dynamic_runner_keeps_unchanged_trade_subscriptions_connected())


async def _test_dynamic_runner_keeps_unchanged_trade_subscriptions_connected():
    runtime = Phase3TradeRuntime({"bitget": BitgetUTA3PublicTradeAdapter()})
    runner = Phase3PublicStreamRunner(runtime, reconnect_seconds=0)
    stop_event = asyncio.Event()
    third_refresh = asyncio.Event()
    refresh_count = 0
    opened = 0

    class Socket:
        async def send(self, payload):
            return None

        async def recv(self):
            await asyncio.sleep(0)
            return json.dumps({"op": "pong"})

    @asynccontextmanager
    async def connect_factory(*args, **kwargs):
        nonlocal opened
        opened += 1
        yield Socket()

    runner.connect_factory = connect_factory

    def symbols_provider():
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count >= 3:
            third_refresh.set()
        return {"bitget": ("BTCUSDT",)}

    worker = asyncio.create_task(
        runner.run_dynamic(symbols_provider, stop_event=stop_event, refresh_seconds=0.01)
    )
    await asyncio.wait_for(third_refresh.wait(), timeout=1)
    assert opened == 1

    stop_event.set()
    await asyncio.wait_for(worker, timeout=1)


async def _test_runtime_reconnect_restores_health_and_returns_public_resubscriptions():
    adapter = BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    runtime = Phase3TradeRuntime({"bybit": adapter}, queue_capacity=2)
    await runtime.start()
    runtime.register_active_symbol("bybit", "BTCUSDT")

    subscriptions = runtime.reconnect_exchange("bybit", now=NOW)

    assert subscriptions == (adapter.subscription("BTCUSDT"),)
    assert runtime.health.snapshot("bybit").reconnect_count == 1
    assert runtime.health.snapshot("bybit").connected is True
