import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from datetime import timedelta

from quant_phase1.config import Settings
from quant_phase1.entrypoints import collector, engine
from quant_phase1.healthcheck import check
from quant_phase1.pipeline import MarketDataBatch, run_stage1
from quant_phase1.runtime import WebSocketCanonicalStore
from quant_phase1.contracts import Candle, DataStatus, Ticker


def test_stage1_batch_never_emits_more_than_five_a_candidates():
    # This checks the batch-level capacity rule independently from the market
    # data adapter and keeps the handoff deterministic.
    NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)

    def candles_for(interval: str, last_open: datetime, count: int = 20):
        seconds = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}[interval]
        first = last_open - timedelta(seconds=seconds * (count - 1))
        return [
            Candle(
                "BTCUSDT", interval, first + timedelta(seconds=seconds * index),
                Decimal(99 + index), Decimal(101 + index), Decimal(98 + index), Decimal(100 + index),
                Decimal("10"), Decimal(1000 + index), first + timedelta(seconds=seconds * index), NOW, NOW,
                DataStatus.AVAILABLE, True, [],
            )
            for index in range(count)
        ]

    def ticker_for(symbol: str) -> Ticker:
        return Ticker(symbol, Decimal("119"), Decimal("118.9"), Decimal("119.1"), Decimal("2"), Decimal("2"), Decimal("1000"), Decimal("100000"), Decimal("119"), Decimal("119"), NOW, NOW, NOW, DataStatus.AVAILABLE, {})

    candles = {
        interval: candles_for(interval, last)
        for interval, last in {
            "5m": datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc),
            "15m": datetime(2026, 9, 20, 10, 15, tzinfo=timezone.utc),
            "1H": datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
            "4H": datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc),
        }.items()
    }
    symbols = tuple(f"COIN{index}USDT" for index in range(10))
    tickers = [ticker_for(symbol) for symbol in symbols]
    batch = MarketDataBatch(NOW, [], tickers, symbols, {symbol: candles for symbol in symbols})
    results = run_stage1(batch, now=NOW)
    assert sum(result.category == "A" for result in results) <= 5


def test_ws_channels_are_partitioned_without_loss():
    args = [{"symbol": str(index)} for index in range(100)]
    parts = collector.partition_ws_args(args)
    assert len(parts) == 3
    assert max(len(part) for part in parts) == 40
    assert [item for part in parts for item in part] == args


def test_collector_partitions_each_topic_group_into_small_connections():
    service = collector.CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0"})
    )
    service.selected_symbols = tuple(f"COIN{index}USDT" for index in range(90))

    groups = service._ws_topic_groups()

    assert len(groups) == 15
    assert max(map(len, groups)) == collector.WS_CHANNELS_PER_CONNECTION
    assert all(len(group) <= 40 for group in groups)
    assert all(len({(arg["topic"], arg.get("interval")) for arg in group}) == 1 for group in groups)
    assert [arg for group in groups for arg in group] == (
        [collector.BitgetV3UtaWebSocket.ticker_arg(symbol) for symbol in service.selected_symbols]
        + [
            collector.BitgetV3UtaWebSocket.kline_arg(symbol, interval)
            for interval in collector.INTERVALS
            for symbol in service.selected_symbols
        ]
    )


def test_collector_ws_candle_store_is_bounded_by_kline_window():
    settings = Settings.from_env({"TRADING_MODE": "paper", "KLINE_FETCH_LIMIT": "37"})
    service = collector.CollectorService(settings)
    assert service.store.capacity == 4


def test_empty_universe_does_not_attempt_websocket_subscription():
    service = collector.CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0"})
    )
    asyncio.run(service._start_ws_connections(client=object()))
    assert service.websockets == []
    assert service.ws_tasks == []


def test_initial_websocket_reset_is_degraded_and_retried(monkeypatch):
    class FlakyWebSocket:
        attempts = 0
        ticker_arg = staticmethod(collector.BitgetV3UtaWebSocket.ticker_arg)
        kline_arg = staticmethod(collector.BitgetV3UtaWebSocket.kline_arg)

        def __init__(self, _settings):
            self.subscriptions = []

        async def connect(self):
            type(self).attempts += 1
            if type(self).attempts <= 6:
                raise ConnectionResetError("fixture reset")

        async def subscribe_many(self, _args, *, batch_size):
            assert batch_size == 1
            self.subscriptions.extend(_args)

        async def receive(self):
            await asyncio.Future()

        async def ping(self):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(collector, "BitgetV3UtaWebSocket", FlakyWebSocket)
    service = collector.CollectorService(
        Settings.from_env({
            "TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0",
            "WS_RECONNECT_SECONDS": "0.01",
        })
    )
    service.selected_symbols = ("BTCUSDT",)

    async def exercise():
        await service._start_ws_connections(client=object())
        retry_task = service.ws_startup_retry_task
        assert retry_task is not None
        deadline = asyncio.get_running_loop().time() + 1
        while FlakyWebSocket.attempts < 6:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("initial retry attempt did not reach backoff")
            await asyncio.sleep(0.005)
        attempts_during_backoff = FlakyWebSocket.attempts
        service.selected_symbols = ("ETHUSDT",)
        await service._restart_ws_connections(client=object())
        assert service.ws_startup_retry_task is retry_task
        assert FlakyWebSocket.attempts == attempts_during_backoff
        deadline = asyncio.get_running_loop().time() + 2
        while len(service.websockets) != 5:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("initial WebSocket retry did not restore all topic groups")
            await asyncio.sleep(0.005)
        assert all(
            arg["symbol"] == "ETHUSDT"
            for websocket in service.websockets
            for arg in websocket.subscriptions
        )
        service.stop_event.set()
        tasks = tuple(service.ws_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert all(task.done() for task in tasks)

    asyncio.run(exercise())


def test_empty_universe_during_websocket_retry_remains_not_available(monkeypatch, caplog):
    caplog.set_level(20, logger="quant_phase1")

    class UnavailableWebSocket:
        attempts = 0
        ticker_arg = staticmethod(collector.BitgetV3UtaWebSocket.ticker_arg)
        kline_arg = staticmethod(collector.BitgetV3UtaWebSocket.kline_arg)

        def __init__(self, _settings):
            pass

        async def connect(self):
            type(self).attempts += 1
            raise ConnectionResetError("fixture reset")

        async def subscribe_many(self, _args, *, batch_size):
            raise AssertionError("unavailable socket cannot subscribe")

        async def close(self):
            return None

    monkeypatch.setattr(collector, "BitgetV3UtaWebSocket", UnavailableWebSocket)
    service = collector.CollectorService(
        Settings.from_env({
            "TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0",
            "WS_RECONNECT_SECONDS": "0.01",
        })
    )
    service.selected_symbols = ("BTCUSDT",)

    async def exercise():
        await service._start_ws_connections(client=object())
        retry_task = service.ws_startup_retry_task
        assert retry_task is not None
        deadline = asyncio.get_running_loop().time() + 1
        while UnavailableWebSocket.attempts < 6:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("retry did not enter its backoff interval")
            await asyncio.sleep(0.005)
        service.selected_symbols = ()
        await service._restart_ws_connections(client=object())
        deadline = asyncio.get_running_loop().time() + 2
        while service.ws_startup_retry_task is not None:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("empty-universe retry did not stop cleanly")
            await asyncio.sleep(0.005)
        assert service.websockets == []
        assert all(task.done() for task in service.ws_tasks)

    asyncio.run(exercise())
    recovered = [record for record in caplog.records if record.getMessage() == "ws_startup_retry_recovered"]
    assert recovered
    assert getattr(recovered[-1], "status", None) == "NOT_AVAILABLE"


def test_universe_refresh_does_not_reset_websocket_startup_backoff(monkeypatch):
    class UnavailableWebSocket:
        attempts = 0
        ticker_arg = staticmethod(collector.BitgetV3UtaWebSocket.ticker_arg)
        kline_arg = staticmethod(collector.BitgetV3UtaWebSocket.kline_arg)

        def __init__(self, _settings):
            pass

        async def connect(self):
            type(self).attempts += 1
            raise ConnectionResetError("fixture reset")

        async def subscribe_many(self, _args, *, batch_size):
            raise AssertionError("unavailable socket cannot subscribe")

        async def close(self):
            return None

    monkeypatch.setattr(collector, "BitgetV3UtaWebSocket", UnavailableWebSocket)
    service = collector.CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0"})
    )
    service.selected_symbols = ("BTCUSDT",)

    async def exercise():
        await service._start_ws_connections(client=object())
        retry_task = service.ws_startup_retry_task
        assert retry_task is not None
        await asyncio.sleep(0.02)
        attempts_before_refresh = UnavailableWebSocket.attempts
        assert attempts_before_refresh >= 6
        service.selected_symbols = ("ETHUSDT",)
        await service._restart_ws_connections(client=object())
        assert service.ws_startup_retry_task is retry_task
        assert UnavailableWebSocket.attempts == attempts_before_refresh
        service.stop_event.set()
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)
        assert retry_task.done()
        assert service.ws_startup_retry_task is None

    asyncio.run(exercise())


def test_cancellation_during_websocket_startup_closes_prior_sockets(monkeypatch):
    class BlockingWebSocket:
        instances = []
        second_started = None
        ticker_arg = staticmethod(collector.BitgetV3UtaWebSocket.ticker_arg)
        kline_arg = staticmethod(collector.BitgetV3UtaWebSocket.kline_arg)

        def __init__(self, _settings):
            self.index = len(type(self).instances) + 1
            self.closed = False
            type(self).instances.append(self)

        async def connect(self):
            if self.index == 2:
                type(self).second_started.set()
                await asyncio.Future()

        async def subscribe_many(self, _args, *, batch_size):
            assert batch_size == 1

        async def close(self):
            self.closed = True

    BlockingWebSocket.second_started = asyncio.Event()
    monkeypatch.setattr(collector, "BitgetV3UtaWebSocket", BlockingWebSocket)
    service = collector.CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0"})
    )
    service.selected_symbols = ("BTCUSDT",)

    async def exercise():
        startup = asyncio.create_task(service._start_ws_connections(client=object()))
        await asyncio.wait_for(BlockingWebSocket.second_started.wait(), timeout=1)
        startup.cancel()
        result = await asyncio.gather(startup, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert BlockingWebSocket.instances
        assert all(websocket.closed for websocket in BlockingWebSocket.instances)
        assert service.websockets == []

    asyncio.run(exercise())


def test_snapshot_retention_is_configurable():
    settings = Settings.from_env({"TRADING_MODE": "paper", "MARKET_SNAPSHOT_RETENTION_DAYS": "3"})
    assert settings.market_snapshot_retention_days == 3


def test_engine_service_runs_scheduler_and_stops_cleanly(monkeypatch):
    settings = Settings.from_env({"TRADING_MODE": "paper", "STAGE1_INTERVAL_SECONDS": "0.001"})
    stop_event = asyncio.Event()
    calls = {"bootstrap": 0, "cycles": 0}

    async def fake_bootstrap(_: Settings, *, stage1_candidate_ttl_seconds=None) -> dict[str, int]:
        calls["bootstrap"] += 1
        return {"symbols": 0, "available": 0, "persisted": 0}

    def fake_cycle(_: Settings, __: object, *, stage1_candidate_ttl_seconds=None) -> dict[str, int]:
        calls["cycles"] += 1
        stop_event.set()
        return {"symbols": 0, "available": 0, "persisted": 0}

    monkeypatch.setattr(engine, "run_once", fake_bootstrap)
    monkeypatch.setattr(engine, "run_database_cycle", fake_cycle)
    asyncio.run(engine.run_service(settings, stop_event=stop_event))
    assert calls == {"bootstrap": 1, "cycles": 1}


def test_healthcheck_fails_closed_and_passes_only_running(monkeypatch):
    from pathlib import Path as RealPath

    path = RealPath.cwd() / ".healthcheck-test.tmp"
    monkeypatch.setattr("quant_phase1.healthcheck.Path", lambda _: path)
    try:
        path.write_text("DEGRADED\n", encoding="utf-8")
        assert check("quant-engine") == 1
        path.write_text("RUNNING\n", encoding="utf-8")
        assert check("quant-engine") == 0
    finally:
        path.unlink(missing_ok=True)


def test_required_readiness_tickers_use_a_dedicated_small_ws_group(monkeypatch):
    monkeypatch.delenv("QUANT_COLLECTOR_READINESS_SYMBOLS", raising=False)
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", *(f"COIN{index}USDT" for index in range(97)))
    monkeypatch.setenv("QUANT_REALTIME_PAPER_SYMBOLS", ",".join(symbols))
    service = collector.CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "PHASE3_ENABLED": "0", "PHASE4_ENABLED": "0"})
    )
    service.selected_symbols = ("SOLUSDT", "BTCUSDT", "ETHUSDT", *(f"COIN{index}USDT" for index in range(97)))

    groups = service._ws_topic_groups()
    ticker_groups = [group for group in groups if group and group[0]["topic"] == "ticker"]

    assert ticker_groups[0] == [
        collector.BitgetV3UtaWebSocket.ticker_arg("BTCUSDT"),
        collector.BitgetV3UtaWebSocket.ticker_arg("ETHUSDT"),
    ]
    assert all(
        arg["symbol"] not in {"BTCUSDT", "ETHUSDT"}
        for group in ticker_groups[1:] for arg in group
    )
    flattened = [arg for group in ticker_groups for arg in group]
    assert len(flattened) == len({arg["symbol"] for arg in flattened}) == len(service.selected_symbols)
    assert max(map(len, ticker_groups)) <= collector.WS_CHANNELS_PER_CONNECTION

    monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS", "SOLUSDT")
    configured_groups = service._ws_topic_groups()
    configured_ticker_groups = [group for group in configured_groups if group and group[0]["topic"] == "ticker"]
    assert configured_ticker_groups[0] == [collector.BitgetV3UtaWebSocket.ticker_arg("SOLUSDT")]

def test_subscribed_socket_reader_starts_before_later_handshakes(monkeypatch):
    async def run():
        first_read=asyncio.Event()
        release=asyncio.Event()
        calls=[]
        class Socket:
            def __init__(self,settings):self.index=len(calls);calls.append(self)
            async def connect(self):
                if self.index==1:
                    # Old startup deadlocks here because readers were only
                    # scheduled after all groups had connected/subscribed.
                    await asyncio.wait_for(first_read.wait(),timeout=0.1)
            async def subscribe_many(self,args,*,batch_size):pass
            async def close(self):pass
        service=collector.CollectorService(Settings.from_env({"TRADING_MODE":"paper"}))
        async def reader(socket,client,*,group_context):
            if socket.index==0:first_read.set()
            await release.wait()
        async def ping(socket,client,*,group_context):await release.wait()
        monkeypatch.setattr(collector,'BitgetV3UtaWebSocket',Socket)
        monkeypatch.setattr(service,'_ws_topic_groups',lambda:[
            [{"topic":"ticker","symbol":"BTCUSDT"}],
            [{"topic":"ticker","symbol":"ETHUSDT"}],
        ])
        monkeypatch.setattr(service,'_receive_loop',reader)
        monkeypatch.setattr(service,'_ping_loop',ping)
        await service._start_ws_connections(object())
        assert len(service.websockets)==2
        assert len(service.ws_tasks)==4
        assert service.ws_startup_retry_task is None
        release.set()
        await asyncio.gather(*service.ws_tasks)
    asyncio.run(run())
