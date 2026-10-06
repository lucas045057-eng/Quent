from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncio
import json

import pytest

from quant_phase1.adapters.bitget_v3.websocket import (
    BitgetV3UtaWebSocket,
    TickerStreamWatchdog,
    parse_kline_message,
    parse_ticker_message,
)
from quant_phase1.contracts import DataStatus
from quant_phase1.adapters.bitget_v3.rest import BitgetV3UtaRestClient


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def test_v3_subscription_uses_topic_and_interval():
    assert BitgetV3UtaWebSocket.ticker_arg("BTCUSDT") == {
        "instType": "usdt-futures",
        "topic": "ticker",
        "symbol": "BTCUSDT",
    }
    assert BitgetV3UtaWebSocket.kline_arg("BTCUSDT", "5m") == {
        "instType": "usdt-futures",
        "topic": "kline",
        "symbol": "BTCUSDT",
        "interval": "5m",
    }


def test_v2_channel_shape_is_not_accepted():
    with pytest.raises(ValueError, match="v3 topic"):
        BitgetV3UtaWebSocket.validate_arg({"instType": "USDT-FUTURES", "channel": "candle5m", "instId": "BTCUSDT"})


def test_parse_v3_ticker_snapshot():
    message = {
        "action": "snapshot",
        "arg": {"instType": "usdt-futures", "topic": "ticker", "symbol": "BTCUSDT"},
        "ts": "1758364200000",
        "data": [
            {
                "symbol": "BTCUSDT",
                "lastPrice": "100",
                "bid1Price": "99.9",
                "ask1Price": "100.1",
                "bid1Size": "2",
                "ask1Size": "3",
                "volume24h": "1000",
                "turnover24h": "100000",
                "indexPrice": "100",
                "markPrice": "100",
            }
        ],
    }
    ticker = parse_ticker_message(message, fetched_at=NOW)
    assert ticker.status is DataStatus.AVAILABLE
    assert ticker.last_price == Decimal("100")


def test_parse_v3_kline_snapshot_and_only_return_closed_rows():
    message = {
        "action": "snapshot",
        "arg": {"instType": "usdt-futures", "topic": "kline", "symbol": "BTCUSDT", "interval": "5m"},
        "data": [
            {"start": "1758362400000", "open": "99", "close": "101", "high": "102", "low": "98", "volume": "10", "turnover": "1000"},
            {"start": str(int(datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc).timestamp() * 1000)), "open": "101", "close": "102", "high": "103", "low": "100", "volume": "2", "turnover": "200"},
        ],
    }
    candles = parse_kline_message(message, fetched_at=NOW, now=NOW)
    assert len(candles) == 1
    assert candles[0].is_closed is True


def test_receive_converts_text_pong_to_control_message():
    class FakeSocket:
        async def recv(self):
            return "pong"

    async def run():
        async def connector(*_args, **_kwargs):
            return FakeSocket()

        client = BitgetV3UtaWebSocket(connector=connector)
        return await client.receive()

    assert asyncio.run(run()) == {"event": "pong"}


def test_ping_and_receiver_reconnect_share_one_connection_attempt():
    class FakeSocket:
        async def close(self):
            return None

        async def send(self, _payload):
            return None

    async def run():
        attempt_two_started = asyncio.Event()
        release_attempt_two = asyncio.Event()
        attempts = 0
        active_attempts = 0
        max_active_attempts = 0

        async def connector(*_args, **_kwargs):
            nonlocal attempts, active_attempts, max_active_attempts
            attempts += 1
            active_attempts += 1
            max_active_attempts = max(max_active_attempts, active_attempts)
            try:
                if attempts == 2:
                    attempt_two_started.set()
                    await release_attempt_two.wait()
                return FakeSocket()
            finally:
                active_attempts -= 1

        client = BitgetV3UtaWebSocket(connector=connector)
        await client.connect()
        reconnect = asyncio.create_task(client.reconnect())
        await asyncio.wait_for(attempt_two_started.wait(), timeout=1)
        heartbeat = asyncio.create_task(client.ping())
        await asyncio.sleep(0.01)
        observed_attempts_during_race = attempts
        release_attempt_two.set()
        await asyncio.gather(reconnect, heartbeat)
        generation = client.connection_generation
        live_generations = client.live_connection_generations
        await client.close()
        return observed_attempts_during_race, attempts, max_active_attempts, generation, live_generations

    observed, total, maximum, generation, live_generations = asyncio.run(run())
    assert observed == 2
    assert total == 2
    assert maximum == 1
    assert generation == 2
    assert live_generations == 1


def test_replaced_generation_and_shutdown_cannot_open_another_connection():
    class FakeSocket:
        async def close(self):
            return None

    async def run():
        attempts = 0

        async def connector(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            return FakeSocket()

        client = BitgetV3UtaWebSocket(connector=connector)
        first = await client.connect()
        second = await client.reconnect(expected_generation=first)
        stale_ping_generation = await client.ping(expected_generation=first)
        with pytest.raises(RuntimeError, match="generation was replaced"):
            await client.receive(expected_generation=first)
        assert attempts == 2
        assert second == stale_ping_generation == client.connection_generation
        await client.close()
        with pytest.raises(RuntimeError, match="stopping"):
            await client.connect()
        with pytest.raises(RuntimeError, match="stopping"):
            await client.reconnect(expected_generation=second)
        assert attempts == 2
        assert client.live_connection_generations == 0

    asyncio.run(run())


def test_rest_kline_recovery_uses_bounded_utc_start_and_end_range():
    class FakeResponse:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"code": "00000", "data": []}

    class FakeSession:
        def __init__(self):
            self.url = None
            self.params = None

        def get(self, url, *, params, timeout):
            self.url = url
            self.params = dict(params)
            assert timeout == 15
            return FakeResponse()

    async def run():
        session = FakeSession()
        client = BitgetV3UtaRestClient(session=session)
        start = NOW - timedelta(minutes=15)
        end = NOW - timedelta(minutes=5)
        rows = await client.get_candles(
            symbol="BTCUSDT",
            interval="5m",
            limit=3,
            start_time=start,
            end_time=end,
        )
        assert rows == []
        assert session.url.endswith("/api/v3/market/candles")
        assert session.params == {
            "category": "USDT-FUTURES",
            "symbol": "BTCUSDT",
            "interval": "5m",
            "limit": "3",
            "startTime": str(int(start.timestamp() * 1000) - 1),
            "endTime": str(int((end + timedelta(minutes=5)).timestamp() * 1000)),
        }
        with pytest.raises(ValueError, match="provided together"):
            await client.get_candles(symbol="BTCUSDT", interval="5m", start_time=start)

    asyncio.run(run())


def test_failed_reconnect_does_not_let_receive_open_an_unsubscribed_socket():
    class FakeSocket:
        async def close(self):
            return None

    async def run():
        attempts = 0

        async def connector(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("deterministic connector failure")
            return FakeSocket()

        client = BitgetV3UtaWebSocket(connector=connector)
        generation = await client.connect()
        with pytest.raises(OSError, match="deterministic"):
            await client.reconnect(expected_generation=generation)
        with pytest.raises(RuntimeError, match="reconnect is required"):
            await client.receive(expected_generation=generation)
        assert attempts == 2
        assert client.connection_generation == generation
        recovered_generation = await client.reconnect(expected_generation=generation)
        assert recovered_generation == generation + 1
        assert attempts == 3
        await client.close()

    asyncio.run(run())


def test_failed_resubscription_closes_partial_generation_and_can_retry():
    class FakeSocket:
        def __init__(self, fail_send=False):
            self.fail_send = fail_send
            self.closed = False

        async def close(self):
            self.closed = True

        async def send(self, _payload):
            if self.fail_send:
                raise OSError("deterministic subscribe send failure")

    async def run():
        sockets = []

        async def connector(*_args, **_kwargs):
            socket = FakeSocket(fail_send=len(sockets) == 1)
            sockets.append(socket)
            return socket

        client = BitgetV3UtaWebSocket(connector=connector)
        await client.subscribe_ticker("BTCUSDT")
        original_generation = client.connection_generation
        with pytest.raises(OSError, match="subscribe send failure"):
            await client.reconnect(expected_generation=original_generation)
        assert sockets[1].closed
        assert client.live_connection_generations == 0
        generation = await client.reconnect(expected_generation=original_generation)
        assert generation == original_generation + 2
        assert client.live_connection_generations == 1
        assert len(sockets) == 3
        await client.close()

    asyncio.run(run())


def test_reconnect_resubscribes_in_bounded_batches(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("quant_phase1.adapters.bitget_v3.websocket.asyncio.sleep", no_sleep)

    async def run():
        sockets = []

        class FakeSocket:
            def __init__(self):
                self.sent = []

            async def send(self, payload):
                self.sent.append(payload)

            async def close(self):
                return None

        async def connector(*_args, **_kwargs):
            socket = FakeSocket()
            sockets.append(socket)
            return socket

        client = BitgetV3UtaWebSocket(connector=connector)
        args = [client.ticker_arg(f"COIN{index}USDT") for index in range(100)]
        await client.subscribe_many(args, batch_size=40)
        initial_messages = len(sockets[0].sent)
        await client.reconnect(expected_generation=client.connection_generation)
        restored_messages = [json.loads(payload) for payload in sockets[1].sent]
        await client.close()
        return initial_messages, restored_messages, args

    initial_messages, restored_messages, expected_args = asyncio.run(run())

    assert initial_messages == 3
    assert len(restored_messages) == 3
    assert all(0 < len(message["args"]) <= 40 for message in restored_messages)
    assert [arg for message in restored_messages for arg in message["args"]] == expected_args


def test_fifteen_injected_resets_have_single_attempts_and_generation_trace():
    async def run():
        traces = []

        class FakeSocket:
            def __init__(self):
                self.reset = False

            async def recv(self):
                if self.reset:
                    raise ConnectionResetError("injected reset")
                return "pong"

            async def close(self):
                return None

        clients = []
        for client_index in range(5):
            sockets = []
            active_attempts = 0
            max_active_attempts = 0

            async def connector(*_args, **_kwargs):
                nonlocal active_attempts, max_active_attempts
                active_attempts += 1
                max_active_attempts = max(max_active_attempts, active_attempts)
                try:
                    await asyncio.sleep(0)
                    socket = FakeSocket()
                    sockets.append(socket)
                    return socket
                finally:
                    active_attempts -= 1

            client = BitgetV3UtaWebSocket(connector=connector)
            await client.connect()
            clients.append(client)
            generation = client.connection_generation
            for wave in range(3):
                reset_id = f"R{client_index * 3 + wave + 1:02d}"
                sockets[-1].reset = True
                with pytest.raises(ConnectionResetError):
                    await client.receive(expected_generation=generation)
                attempts_before = len(sockets)
                generation = await client.reconnect(expected_generation=generation)
                trace = {
                    "reset_id": reset_id,
                    "receive_exception": "ConnectionResetError",
                    "connection_attempts": len(sockets) - attempts_before,
                    "successful_generation": generation,
                    "max_active_attempts": max_active_attempts,
                    "live_generations": client.live_connection_generations,
                }
                traces.append(trace)
                assert trace["connection_attempts"] == 1
                assert trace["max_active_attempts"] <= 1
                assert trace["live_generations"] <= 1
        assert len(traces) == 15
        assert [trace["reset_id"] for trace in traces] == [f"R{i:02d}" for i in range(1, 16)]
        for client in clients:
            await client.close()

    asyncio.run(run())
def test_ticker_stream_watchdog_tracks_only_fresh_ticker_data_per_symbol():
    watchdog = TickerStreamWatchdog(
        ("BTCUSDT", "ETHUSDT"), idle_timeout_seconds=30, started_at=0.0,
    )

    assert watchdog.seconds_until_expiry(now=0.0) == 30
    watchdog.observe({"event": "pong"}, now=20.0)
    watchdog.observe(
        {"arg": {"topic": "ticker", "symbol": "SOLUSDT"}, "data": [{}]},
        now=20.0,
    )
    assert watchdog.seconds_until_expiry(now=29.0) == 1
    assert watchdog.expired_symbols(now=30.0) == ("BTCUSDT", "ETHUSDT")

    watchdog.observe(
        {"arg": {"topic": "ticker", "symbol": "BTCUSDT"}, "data": [{}]},
        now=31.0,
    )
    assert watchdog.expired_symbols(now=31.0) == ("ETHUSDT",)
    assert watchdog.seconds_until_expiry(now=31.0) == 0
    watchdog.observe(
        {"arg": {"topic": "ticker", "symbol": "ETHUSDT"}, "data": [{}]},
        now=31.0,
    )
    assert watchdog.expired_symbols(now=31.0) == ()
    assert watchdog.seconds_until_expiry(now=31.0) == 30


def test_ticker_stream_watchdog_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="positive"):
        TickerStreamWatchdog(("BTCUSDT",), idle_timeout_seconds=0, started_at=0.0)

def test_ticker_stream_watchdog_does_not_treat_stale_exchange_timestamps_as_live():
    watchdog = TickerStreamWatchdog(
        ("BTCUSDT",),
        idle_timeout_seconds=30,
        max_source_age_seconds=60,
        started_at=0.0,
    )
    stale_ms = int((NOW - timedelta(seconds=61)).timestamp() * 1000)
    stale_message = {
        "arg": {"topic": "ticker", "symbol": "BTCUSDT"},
        "ts": str(stale_ms),
        "data": [{"ts": str(stale_ms)}],
    }

    assert watchdog.observe(stale_message, now=20.0, wall_now=NOW) is False
    assert watchdog.seconds_until_expiry(now=20.0) == 10
    assert watchdog.expired_symbols(now=30.0) == ("BTCUSDT",)

    fresh_ms = int(NOW.timestamp() * 1000)
    fresh_message = {
        "arg": {"topic": "ticker", "symbol": "BTCUSDT"},
        "ts": str(fresh_ms),
        "data": [{"ts": str(fresh_ms)}],
    }
    assert watchdog.observe(fresh_message, now=31.0, wall_now=NOW) is True
    assert watchdog.expired_symbols(now=31.0) == ()


def test_stream_deadline_does_not_change_per_coin_expiry():
    watchdog=TickerStreamWatchdog(("BTCUSDT","ETHUSDT"),idle_timeout_seconds=30,started_at=0)
    watchdog.observe({"arg":{"topic":"ticker","symbol":"BTCUSDT"},"data":[{}]},now=29)
    assert watchdog.expired_symbols(now=31)==("ETHUSDT",)
    assert watchdog.seconds_until_expiry(now=31)==0
    assert watchdog.seconds_until_stream_expiry(now=31)==28
    assert watchdog.seconds_until_stream_expiry(now=59)==0
    assert watchdog.expired_symbols(now=59)==("BTCUSDT","ETHUSDT")
