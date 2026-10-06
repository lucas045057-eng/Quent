import asyncio

from quant_phase1.adapters.bitget_v3.websocket import BitgetV3UtaWebSocket
from quant_phase1.config import Settings
from quant_phase1.entrypoints import collector as collector_entrypoint
from quant_phase1.entrypoints.collector import CollectorService


def test_silent_required_ticker_reconnects_while_socket_is_open(monkeypatch):
    async def run():
        service = CollectorService(
            Settings.from_env({"TRADING_MODE": "paper", "WS_RECONNECT_SECONDS": "0.001"})
        )
        service.selected_symbols = ("BTCUSDT", "ETHUSDT")

        class SilentTickerSocket:
            ticker_symbols = ("BTCUSDT", "ETHUSDT")
            connection_generation = 1

            def __init__(self):
                self.reconnect_count = 0

            async def receive(self, expected_generation=None):
                await asyncio.Future()

            async def reconnect(self, expected_generation=None):
                self.reconnect_count += 1
                self.connection_generation += 1
                service.stop_event.set()
                return self.connection_generation

        recovery_calls = []

        async def no_recovery(_service, _client):
            recovery_calls.append(_client)
            return None

        socket = SilentTickerSocket()
        monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS", "BTCUSDT,ETHUSDT")
        monkeypatch.setattr(
            collector_entrypoint, "TICKER_STREAM_IDLE_RECONNECT_SECONDS", 0.01,
        )
        monkeypatch.setattr(CollectorService, "_recover_gaps", no_recovery)
        await service._receive_loop(socket, object())
        assert socket.reconnect_count == 1
        assert service.ws_reconnect_count == 1
        assert recovery_calls == []

    asyncio.run(run())

def test_receive_loop_yields_to_other_websocket_tasks(monkeypatch):
    async def run():
        service = CollectorService(
            Settings.from_env({"TRADING_MODE": "paper", "WS_RECONNECT_SECONDS": "0.001"})
        )
        seen = 0
        scheduled_at = []

        class BusySocket:
            ticker_symbols = ()

            async def receive(self):
                nonlocal seen
                if seen < 100:
                    seen += 1
                    return {"event": "pong"}
                await service.stop_event.wait()
                return {"event": "pong"}

        socket = BusySocket()

        async def competing_task():
            await asyncio.sleep(0)
            scheduled_at.append(seen)
            service.stop_event.set()

        receive_task = asyncio.create_task(service._receive_loop(socket, object()))
        competitor = asyncio.create_task(competing_task())
        await asyncio.gather(receive_task, competitor)
        assert scheduled_at and scheduled_at[0] < 100

    asyncio.run(run())


def test_stale_required_ticker_is_rejected_before_store(monkeypatch):
    async def run():
        service = CollectorService(
            Settings.from_env({"TRADING_MODE": "paper", "WS_RECONNECT_SECONDS": "0.001"})
        )
        service.selected_symbols = ("BTCUSDT", "ETHUSDT")
        monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS", "BTCUSDT,ETHUSDT")
        monkeypatch.setattr(collector_entrypoint, "TICKER_STREAM_IDLE_RECONNECT_SECONDS", 0.01)
        ingested = []
        service.store.ingest = lambda message, *, now: ingested.append(message)
        recovery_calls = []

        async def no_recovery(_service, _client):
            recovery_calls.append(_client)

        monkeypatch.setattr(CollectorService, "_recover_gaps", no_recovery)

        class StaleTickerSocket:
            ticker_symbols = ("BTCUSDT", "ETHUSDT")
            connection_generation = 1

            def __init__(self):
                self.reconnect_count = 0
                self.sent_stale = False

            async def receive(self, expected_generation=None):
                if not self.sent_stale:
                    self.sent_stale = True
                    from datetime import datetime, timedelta, timezone
                    source_ts = int((datetime.now(timezone.utc) - timedelta(seconds=120)).timestamp() * 1000)
                    return {
                        "arg": {"topic": "ticker", "symbol": "BTCUSDT"},
                        "data": [{"ts": source_ts}],
                    }
                await asyncio.Future()

            async def reconnect(self, expected_generation=None):
                self.reconnect_count += 1
                service.stop_event.set()
                return self.connection_generation + self.reconnect_count

        socket = StaleTickerSocket()
        await service._receive_loop(socket, object())
        assert ingested == []
        assert socket.reconnect_count == 1
        assert recovery_calls == []

    asyncio.run(run())


def test_quiet_coin_does_not_disconnect_live_coin_or_refresh_its_clock(monkeypatch):
    async def run():
        from datetime import datetime, timezone
        service = CollectorService(Settings.from_env({"TRADING_MODE": "paper", "WS_RECONNECT_SECONDS": "0.001"}))
        monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS", "BTCUSDT,ETHUSDT")
        monkeypatch.setattr(collector_entrypoint, "TICKER_STREAM_IDLE_RECONNECT_SECONDS", 0.01)
        seen = []
        service.store.ingest = lambda event, *, now: seen.append((event['arg']['symbol'],now))

        class UpdatingSocket:
            ticker_symbols = ("BTCUSDT", "ETHUSDT")
            connection_generation = 1
            reconnect_count = 0
            count = 0
            async def receive(self, expected_generation=None):
                await asyncio.sleep(0.002)
                self.count += 1
                if self.count == 20:
                    service.stop_event.set()
                return {"arg":{"topic":"ticker","symbol":"BTCUSDT"},
                        "ts":int(datetime.now(timezone.utc).timestamp()*1000), "data":[{}]}
            async def reconnect(self, expected_generation=None):
                self.reconnect_count += 1
                service.stop_event.set()
                return self.connection_generation + 1

        socket = UpdatingSocket()
        await service._receive_loop(socket,object())
        assert len(seen) == 20
        assert socket.reconnect_count == 0
        assert set(symbol for symbol,_ in seen) == {"BTCUSDT"}
        assert "ETHUSDT" not in service.store.tickers
        assert service._ticker_idle_symbols_last_group == 1
    asyncio.run(run())


def test_required_ticker_receive_is_owned_by_reader_and_cancelled_without_orphan(monkeypatch):
    async def run():
        service=CollectorService(Settings.from_env({"TRADING_MODE":"paper"}))
        monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS","BTCUSDT")
        entered=asyncio.Event()
        cancelled=asyncio.Event()
        receive_tasks=[]
        class Socket:
            ticker_symbols=("BTCUSDT",)
            connection_generation=1
            async def receive(self,expected_generation=None):
                receive_tasks.append(asyncio.current_task())
                entered.set()
                try:await asyncio.Future()
                finally:cancelled.set()
        reader=asyncio.create_task(service._receive_loop(Socket(),object()))
        await entered.wait()
        assert receive_tasks==[reader]
        reader.cancel()
        result=await asyncio.gather(reader,return_exceptions=True)
        assert isinstance(result[0],asyncio.CancelledError)
        assert cancelled.is_set()
        assert service.ws_reconnect_count==0
        assert not service.store.tickers
    asyncio.run(run())


def test_receive_loop_uses_dedicated_readiness_symbols(monkeypatch):
    async def run():
        service = CollectorService(Settings.from_env({"TRADING_MODE": "paper"}))
        monkeypatch.setenv("QUANT_COLLECTOR_READINESS_SYMBOLS", "BTCUSDT")
        monkeypatch.setenv("QUANT_REALTIME_PAPER_SYMBOLS", "ETHUSDT")
        watched = []
        real_watchdog = collector_entrypoint.TickerStreamWatchdog

        class RecordingWatchdog(real_watchdog):
            def __init__(self, symbols, **kwargs):
                watched.append(set(symbols))
                super().__init__(symbols, **kwargs)

        monkeypatch.setattr(collector_entrypoint, "TickerStreamWatchdog", RecordingWatchdog)

        class Socket:
            ticker_symbols = ("BTCUSDT", "ETHUSDT")
            async def receive(self, expected_generation=None):
                service.stop_event.set()
                return {"event": "pong"}

        await service._receive_loop(Socket(), object())
        assert watched == [{"BTCUSDT"}]

    asyncio.run(run())
