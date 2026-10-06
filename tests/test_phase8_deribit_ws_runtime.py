import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import time

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
import pytest

from quant_phase8 import contracts
from quant_phase8.config import Phase8Settings


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)

try:
    deribit_ws = importlib.import_module("quant_phase8.adapters.deribit_ws")
except ModuleNotFoundError:
    deribit_ws = None


def _api(name):
    function = getattr(deribit_ws, name, None) if deribit_ws is not None else None
    assert callable(function), f"WebSocket runtime must expose {name}"
    return function


def _instrument(symbol="BTC-30OCT26-100000-C", price_index="btc_usd"):
    return contracts.OptionInstrument(
        exchange="DERIBIT",
        source="deribit",
        symbol=symbol,
        underlying="BTC",
        option_type="put" if symbol.endswith("-P") else "call",
        strike=contracts.decimal_from_json(100000),
        expires_at=datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc),
        instrument_created_at=datetime(2026, 5, 28, 20, 26, 40, tzinfo=timezone.utc),
        instrument_state="open",
        is_active=True,
        price_index=price_index,
        base_currency="BTC",
        quote_currency="BTC",
        settlement_currency="BTC",
        exchange_timestamp=None,
        fetched_at=T0,
        processed_at=T0,
    )


def _catalog(count=1):
    return tuple(
        _instrument(
            f"BTC-30OCT26-{100000 + index}-C"
        )
        for index in range(count)
    )


def _settings(**overrides):
    env = {"PHASE8_OPTIONS_WS_SUBSCRIBE_BATCH_SIZE": "32"}
    env.update(overrides)
    return Phase8Settings.from_env(env)


async def _start_server(handler):
    app = web.Application()
    app.router.add_get("/ws/api/v2", handler)
    server = TestServer(app)
    await server.start_server()
    return server, str(server.make_url("/ws/api/v2")).replace("http://", "ws://", 1)


@pytest.mark.asyncio
async def test_runtime_subscribes_lifecycle_then_markprice_then_bounded_ticker_batches():
    requests = []
    request_times = []
    complete = asyncio.Event()

    async def handler(request):
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        async for message in socket:
            if message.type is WSMsgType.TEXT:
                payload = json.loads(message.data)
                requests.append(payload)
                request_times.append(time.monotonic())
                await socket.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": []})
                if len(requests) == 4:
                    complete.set()
            elif message.type is WSMsgType.ERROR:
                break
        return socket

    server, url = await _start_server(handler)
    client = _api("DeribitPublicWebSocketClient")(
        _settings(), instrument_catalog=_catalog(33), supported_index_names={"btc_usd"},
        ticker_symbols=[item.symbol for item in _catalog(33)], _url=url,
    )
    try:
        await client.start()
        await asyncio.wait_for(complete.wait(), timeout=4)
    finally:
        await client.close()
        await server.close()

    channel_groups = [request["params"]["channels"] for request in requests]
    assert requests[0]["method"] == "public/subscribe"
    assert channel_groups[0] == [
        "instrument.creation.option.BTC", "instrument.state.option.BTC",
        "instrument.creation.option.ETH", "instrument.state.option.ETH",
    ]
    assert channel_groups[1] == ["markprice.options.btc_usd"]
    assert [len(group) for group in channel_groups[2:]] == [32, 1]
    assert all(channel.startswith("incremental_ticker.") for group in channel_groups[2:] for channel in group)
    assert all(
        right - left >= 0.33 for left, right in zip(request_times, request_times[1:])
    )


@pytest.mark.asyncio
async def test_runtime_readiness_requires_markprice_seed_and_ticker_snapshot():
    subscribed = asyncio.Event()
    catalog = (_instrument(), _instrument("BTC-30OCT26-100000-P"))

    async def handler(request):
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        request_count = 0
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            request_count += 1
            await socket.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": []})
            if request_count == 2:
                subscribed.set()
                await asyncio.sleep(0.05)
                await socket.send_str((FIXTURES / "ws_markprice_seed.json").read_text(encoding="utf-8"))
                await socket.send_str((FIXTURES / "ws_ticker_snapshot.json").read_text(encoding="utf-8"))
        return socket

    server, url = await _start_server(handler)
    client = _api("DeribitPublicWebSocketClient")(
        _settings(), instrument_catalog=catalog, supported_index_names={"btc_usd"},
        ticker_symbols=[catalog[0].symbol], _url=url,
    )
    try:
        await client.start()
        await asyncio.wait_for(subscribed.wait(), timeout=2)
        await client.wait_ready(timeout=2)
        assert client.ready
        assert client.ready_markprice_indexes == {"btc_usd"}
        assert client.ready_ticker_symbols == {catalog[0].symbol}
        await client.close()
        assert not client.ready
        await client.start()
        await client.wait_ready(timeout=2)
        assert client.ready
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_websocket_ping_is_answered_and_owned_tasks_stop_on_close():
    ping_completed = asyncio.Event()

    async def handler(request):
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        await socket.ping(b"phase8-heartbeat")
        async for response in socket:
            if response.type is WSMsgType.PONG and response.data == b"phase8-heartbeat":
                ping_completed.set()
                break
        return socket

    server, url = await _start_server(handler)
    client = _api("DeribitPublicWebSocketClient")(
        _settings(), instrument_catalog=_catalog(), supported_index_names={"btc_usd"},
        ticker_symbols=[], _url=url,
    )
    await client.start()
    try:
        await asyncio.wait_for(ping_completed.wait(), timeout=2)
        owned_tasks = client.owned_tasks
        assert owned_tasks
    finally:
        await client.close()
        await server.close()

    assert client.owned_tasks == ()


@pytest.mark.asyncio
async def test_disconnect_invalidates_state_and_reconnect_resubscribes():
    connections = 0
    second_connection = asyncio.Event()
    invalidations = []

    async def handler(request):
        nonlocal connections
        connections += 1
        connection_id = connections
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        received_subscriptions = 0
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            received_subscriptions += 1
            await socket.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": []})
            if connection_id == 1 and received_subscriptions == 2:
                await socket.send_str((FIXTURES / "ws_markprice_seed.json").read_text(encoding="utf-8"))
                await socket.send_str((FIXTURES / "ws_ticker_snapshot.json").read_text(encoding="utf-8"))
                await socket.close()
            elif connection_id == 2 and received_subscriptions == 2:
                second_connection.set()
        return socket

    async def no_delay(_seconds):
        await asyncio.sleep(0)

    server, url = await _start_server(handler)
    client = _api("DeribitPublicWebSocketClient")(
        _settings(PHASE8_OPTIONS_WS_SUBSCRIBE_MIN_INTERVAL_SECONDS="0.35"),
        instrument_catalog=_catalog(2), supported_index_names={"btc_usd"},
        ticker_symbols=[_catalog()[0].symbol], _url=url,
        on_invalidate=lambda reason: invalidations.append(reason), _sleep=no_delay,
    )
    try:
        await client.start()
        await asyncio.wait_for(second_connection.wait(), timeout=3)
        assert connections >= 2
        assert invalidations
        assert not client.ready
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_reconnect_attempts_are_bounded_until_a_trusted_seed_arrives():
    connections = 0
    completed = asyncio.Event()

    async def handler(request):
        nonlocal connections
        connections += 1
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        requests = 0
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            requests += 1
            await socket.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": []})
            if requests == 2:
                await socket.close()
                if connections == 2:
                    completed.set()
                return socket
        return socket

    async def no_delay(_seconds):
        await asyncio.sleep(0)

    server, url = await _start_server(handler)
    client = _api("DeribitPublicWebSocketClient")(
        _settings(PHASE8_OPTIONS_WS_MAX_RECONNECT_ATTEMPTS="1"),
        instrument_catalog=_catalog(), supported_index_names={"btc_usd"},
        ticker_symbols=[], _url=url, _sleep=no_delay,
    )
    try:
        await client.start()
        await asyncio.wait_for(completed.wait(), timeout=2)
        await asyncio.wait_for(client._main_task, timeout=2)
        assert connections == 2
        assert client.last_error
        assert not client.ready
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_oversized_frame_is_rejected_and_reconnect_budget_remains_bounded():
    connections = 0

    async def handler(request):
        nonlocal connections
        connections += 1
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        requests = 0
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            requests += 1
            if requests == 2:
                await socket.send_str("x" * 9)
                return socket
        return socket

    async def no_delay(_seconds):
        await asyncio.sleep(0)

    server, url = await _start_server(handler)
    settings = replace(
        _settings(PHASE8_OPTIONS_WS_MAX_RECONNECT_ATTEMPTS="1"),
        ws_max_message_bytes=8,
        ws_max_queued_bytes=16,
    )
    client = _api("DeribitPublicWebSocketClient")(
        settings, instrument_catalog=_catalog(), supported_index_names={"btc_usd"},
        ticker_symbols=[], _url=url, _sleep=no_delay,
    )
    try:
        await client.start()
        await asyncio.wait_for(client._main_task, timeout=2)
        assert connections == 2
        assert client.last_error
        assert not client.ready
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_aggregate_payload_queue_fails_closed_and_releases_byte_budget():
    queue = _api("BoundedPayloadQueue")(max_messages=2, max_bytes=10)
    await queue.put("12345")
    assert queue.queued_bytes == 5
    with pytest.raises(_api("WebSocketQueueOverflow")):
        await queue.put("678901")

    payload = await queue.get()
    assert payload == "12345"
    assert queue.queued_bytes == 0

    await queue.put(b"12345678")
    with pytest.raises(_api("WebSocketQueueOverflow")):
        await queue.put(b"123")


def test_reconnect_delay_is_exponential_and_respects_configured_ceiling():
    delay = _api("reconnect_delay")

    assert delay(1, 60) == 1
    assert delay(2, 60) == 2
    assert delay(8, 60) == 60
    assert delay(100, 60) == 60
