import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import quant_phase1.entrypoints.collector as collector_entrypoint
import quant_phase1.entrypoints.engine as engine_entrypoint
from quant_phase1.adapters.bitget_v3.websocket import BitgetV3UtaWebSocket
from quant_phase1.config import Settings
from quant_phase1.entrypoints.collector import CollectorService
from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
from quant_phase3.runtime import Phase3PublicStreamRunner, Phase3TradeRuntime
from quant_phase4.runtime import Phase4PublicLiquidationRunner


def test_collector_shutdown_closes_all_uta_sockets_concurrently(monkeypatch):
    asyncio.run(_test_collector_shutdown_closes_all_uta_sockets_concurrently(monkeypatch))


async def _test_collector_shutdown_closes_all_uta_sockets_concurrently(monkeypatch):
    active = 0
    maximum_active = 0
    closed = 0

    class FakeWebSocket:
        async def close(self):
            nonlocal active, maximum_active, closed
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.25)
            finally:
                active -= 1
                closed += 1

    class FakeRestClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeMarketDataCollector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect_once(self):
            return SimpleNamespace(selected_symbols=(), tickers=(), candles_by_symbol={})

    async def noop(*_args, **_kwargs):
        return None

    async def idle(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def start_sockets(service, _client):
        service.websockets.extend(FakeWebSocket() for _ in range(5))
        service.stop_event.set()

    monkeypatch.setattr(collector_entrypoint, "BitgetV3UtaRestClient", FakeRestClient)
    monkeypatch.setattr(collector_entrypoint, "MarketDataCollector", FakeMarketDataCollector)
    monkeypatch.setattr(collector_entrypoint, "write_health_file", lambda *_a, **_kw: None)
    monkeypatch.setattr(CollectorService, "_persist_batch", noop)
    monkeypatch.setattr(CollectorService, "_load_phase3_stage1_ab_symbols", noop)
    monkeypatch.setattr(CollectorService, "_hydrate_phase3_state", noop)
    monkeypatch.setattr(CollectorService, "_start_gap_recovery", noop)
    monkeypatch.setattr(CollectorService, "_start_ws_connections", start_sockets)
    monkeypatch.setattr(CollectorService, "_persist_loop", idle)
    monkeypatch.setattr(CollectorService, "_universe_loop", idle)

    service = CollectorService(Settings.from_env({}), stop_event=asyncio.Event())
    started = time.monotonic()
    await asyncio.wait_for(service.run(), timeout=1.0)
    elapsed = time.monotonic() - started

    assert closed == 5
    assert maximum_active == 5
    assert elapsed < 0.75


def test_collector_shutdown_cancels_rest_bootstrap(monkeypatch):
    asyncio.run(_test_collector_shutdown_cancels_rest_bootstrap(monkeypatch))


async def _test_collector_shutdown_cancels_rest_bootstrap(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeRestClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class BlockingCollector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect_once(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(collector_entrypoint, "BitgetV3UtaRestClient", FakeRestClient)
    monkeypatch.setattr(collector_entrypoint, "MarketDataCollector", BlockingCollector)
    monkeypatch.setattr(collector_entrypoint, "write_health_file", lambda *_a, **_kw: None)
    settings = Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "PHASE2_ENABLED": "0",
            "PHASE3_ENABLED": "0",
            "PHASE4_ENABLED": "0",
            "PHASE5_ENABLED": "0",
            "PHASE6_ENABLED": "0",
        }
    )
    stop_event = asyncio.Event()
    service = CollectorService(settings, stop_event=stop_event)
    task = asyncio.create_task(service.run())
    await asyncio.wait_for(started.wait(), timeout=0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)

    assert cancelled.is_set()


def test_collector_shutdown_cancels_websocket_startup(monkeypatch):
    asyncio.run(_test_collector_shutdown_cancels_websocket_startup(monkeypatch))


async def _test_collector_shutdown_cancels_websocket_startup(monkeypatch):
    websocket_starting = asyncio.Event()
    websocket_cancelled = asyncio.Event()

    class FakeRestClient:
        def __init__(self, _settings):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeMarketDataCollector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect_once(self):
            return SimpleNamespace(selected_symbols=(), tickers=(), candles_by_symbol={})

    async def noop(*_args, **_kwargs):
        return None

    async def blocked_websocket_startup(*_args, **_kwargs):
        websocket_starting.set()
        try:
            await asyncio.Event().wait()
        finally:
            websocket_cancelled.set()

    monkeypatch.setattr(collector_entrypoint, "BitgetV3UtaRestClient", FakeRestClient)
    monkeypatch.setattr(collector_entrypoint, "MarketDataCollector", FakeMarketDataCollector)
    monkeypatch.setattr(collector_entrypoint, "write_health_file", lambda *_a, **_kw: None)
    monkeypatch.setattr(CollectorService, "_persist_batch", noop)
    monkeypatch.setattr(CollectorService, "_load_phase3_stage1_ab_symbols", noop)
    monkeypatch.setattr(CollectorService, "_hydrate_phase3_state", noop)
    monkeypatch.setattr(CollectorService, "_start_gap_recovery", noop)
    monkeypatch.setattr(CollectorService, "_start_ws_connections", blocked_websocket_startup)
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE2_ENABLED": "0",
        "PHASE3_ENABLED": "0",
        "PHASE4_ENABLED": "0",
        "PHASE5_ENABLED": "0",
        "PHASE6_ENABLED": "0",
        "PHASE7_ENABLED": "0",
    })
    stop_event = asyncio.Event()
    service = CollectorService(settings, stop_event=stop_event)
    task = asyncio.create_task(service.run())
    await asyncio.wait_for(websocket_starting.wait(), timeout=0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)

    assert websocket_cancelled.is_set()


def test_engine_shutdown_cancels_rest_bootstrap(monkeypatch):
    asyncio.run(_test_engine_shutdown_cancels_rest_bootstrap(monkeypatch))


async def _test_engine_shutdown_cancels_rest_bootstrap(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_bootstrap(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(engine_entrypoint, "run_once", blocking_bootstrap)
    monkeypatch.setattr(engine_entrypoint, "write_health_file", lambda *_a, **_kw: None)
    settings = Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "PHASE2_ENABLED": "0",
            "PHASE3_ENABLED": "0",
            "PHASE4_ENABLED": "0",
            "PHASE5_ENABLED": "0",
            "PHASE6_ENABLED": "0",
        }
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(engine_entrypoint.run_service(settings, stop_event=stop_event))
    await asyncio.wait_for(started.wait(), timeout=0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.2)

    assert cancelled.is_set()


def test_bitget_uta_socket_connector_has_bounded_close_timeout():
    async def scenario():
        observed = {}

        class FakeSocket:
            async def close(self):
                return None

        async def connector(_url, **kwargs):
            observed.update(kwargs)
            return FakeSocket()

        client = BitgetV3UtaWebSocket(connector=connector)
        await client.connect()
        await client.close()
        assert observed["close_timeout"] == 2.0

    asyncio.run(scenario())


def test_phase3_public_stream_connector_has_bounded_close_timeout():
    async def scenario():
        runtime = Phase3TradeRuntime(
            {
                "bybit": BybitPublicTradeAdapter(),
                "bitget": BitgetUTA3PublicTradeAdapter(),
                "hyperliquid": HyperliquidPublicTradeAdapter(),
            }
        )
        await runtime.start()
        stop_event = asyncio.Event()
        observed = {}

        class FakeSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, _payload):
                return None

            async def recv(self):
                stop_event.set()
                return json.dumps({"event": "subscribe"})

        def connector(_url, **kwargs):
            observed.update(kwargs)
            return FakeSocket()

        runner = Phase3PublicStreamRunner(runtime, reconnect_seconds=0)
        runner.connect_factory = connector
        try:
            await runner.run_exchange("bybit", ("BTCUSDT",), stop_event=stop_event)
        finally:
            await runtime.stop()

        assert observed["close_timeout"] == 2.0

    asyncio.run(scenario())


def test_phase4_public_liquidation_connector_has_bounded_close_timeout():
    async def scenario():
        stop_event = asyncio.Event()
        observed = {}

        class FakeAdapter:
            public_ws_url = "wss://example.invalid/public"

            @staticmethod
            def subscription(_symbol):
                return {"op": "subscribe"}

        class RuntimeStub:
            liquidation_adapters = {"fake": FakeAdapter()}

            def ingest_liquidation(self, *_args):
                return None

        class FakeSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def send(self, _payload):
                return None

            async def recv(self):
                stop_event.set()
                return json.dumps({"event": "subscribe"})

        def connector(_url, **kwargs):
            observed.update(kwargs)
            return FakeSocket()

        runner = Phase4PublicLiquidationRunner(RuntimeStub(), connect_factory=connector)
        await runner.run_exchange("fake", ("BTCUSDT",), stop_event=stop_event)

        assert observed["close_timeout"] == 2.0

    asyncio.run(scenario())


def test_engine_sigterm_wakes_event_loop_promptly():
    script = """
import asyncio
import os
import signal
import threading
import time
os.environ["TRADING_MODE"] = "paper"

import quant_phase1.entrypoints.engine as entrypoint

async def fake_run_service(_settings, *, stop_event):
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    def send_sigterm():
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=send_sigterm, daemon=True).start()
    loop.call_later(1.5, stop_event.set)
    await stop_event.wait()
    elapsed = time.monotonic() - started
    print(f"STOP_WAKE_SECONDS={elapsed:.3f}", flush=True)
    if elapsed >= 1.0:
        raise SystemExit(17)

entrypoint.run_service = fake_run_service
entrypoint.main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=4.0,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STOP_WAKE_SECONDS=" in result.stdout
