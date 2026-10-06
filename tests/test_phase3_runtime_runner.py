import asyncio
import json
from datetime import datetime, timezone

import pytest

from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
from quant_phase3.runtime import Phase3PublicStreamRunner, Phase3TradeRuntime


NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def _runner():
    runtime = Phase3TradeRuntime(
        {
            "bybit": BybitPublicTradeAdapter(),
            "bitget": BitgetUTA3PublicTradeAdapter(),
            "hyperliquid": HyperliquidPublicTradeAdapter(),
        }
    )
    return runtime, Phase3PublicStreamRunner(runtime, reconnect_seconds=0)


def test_runner_uses_each_exchange_public_subscription_schema():
    runtime, runner = _runner()

    assert runner.subscription_payloads("bybit", ("BTCUSDT",)) == (
        {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]},
    )
    assert runner.subscription_payloads("bitget", ("BTCUSDT",)) == (
        {
            "op": "subscribe",
            "args": [{"instType": "usdt-futures", "topic": "publicTrade", "symbol": "BTCUSDT"}],
        },
    )
    assert runner.subscription_payloads("hyperliquid", ("BTC",)) == (
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
    )
    assert runtime.running is False


def test_runner_reconnects_and_resubscribes_after_disconnect():
    asyncio.run(_test_runner_reconnects_and_resubscribes_after_disconnect())


async def _test_runner_reconnects_and_resubscribes_after_disconnect():
    runtime, runner = _runner()
    await runtime.start()
    stop_event = asyncio.Event()
    sockets = []

    class FakeSocket:
        def __init__(self, messages):
            self.messages = iter(messages)
            self.sent = []

        async def __aenter__(self):
            sockets.append(self)
            return self

        async def __aexit__(self, *_):
            return False

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            try:
                return next(self.messages)
            except StopIteration:
                if len(sockets) >= 2:
                    stop_event.set()
                raise ConnectionError("test disconnect")

    payload = {
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "data": [{"T": 1789866123004, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "100", "i": "id-1", "seq": 1}],
    }
    queue = [FakeSocket([json.dumps(payload)]), FakeSocket([])]

    def fake_connect(*_args, **_kwargs):
        return queue.pop(0)

    runner.connect_factory = fake_connect
    await runner.run_exchange("bybit", ("BTCUSDT",), stop_event=stop_event)

    assert len(sockets) == 2
    assert all(socket.sent == [{"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}] for socket in sockets)
    assert runtime.health_snapshot("bybit").reconnect_count == 1


def test_runner_does_not_contain_private_or_order_capabilities():
    _, runner = _runner()
    assert all(not hasattr(adapter, "api_key") for adapter in runner.runtime.adapters.values())
    assert all("private" not in adapter.ws_url.lower() for adapter in runner.runtime.adapters.values())


def test_run_dynamic_cancels_blocked_receivers_when_shutdown_interrupts_refresh_wait():
    async def scenario():
        runtime, runner = _runner()
        await runtime.start()
        stop_event = asyncio.Event()
        all_receivers_blocked = asyncio.Event()
        release_receivers = asyncio.Event()
        receiver_count = 0

        class BlockingSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, _payload):
                return None

            async def recv(self):
                nonlocal receiver_count
                receiver_count += 1
                if receiver_count == 3:
                    all_receivers_blocked.set()
                await release_receivers.wait()
                return "{}"

        runner.connect_factory = lambda *_args, **_kwargs: BlockingSocket()
        task = asyncio.create_task(
            runner.run_dynamic(
                lambda: {"bitget": ("BTCUSDT",), "bybit": ("BTCUSDT",), "hyperliquid": ("BTC",)},
                stop_event=stop_event,
                refresh_seconds=3_600,
            )
        )
        await asyncio.wait_for(all_receivers_blocked.wait(), timeout=1)
        stop_event.set()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.2)
        stopped_within_bound = task in done

        # Release the fake transport after recording the red result so a
        # failing regression cannot strand tasks in asyncio.run teardown.
        release_receivers.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        await runtime.stop()
        return stopped_within_bound

    assert asyncio.run(scenario()), "run_dynamic left its nested recv workers alive on cancellation"
