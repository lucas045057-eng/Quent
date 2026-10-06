import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from quant_phase8.config import Phase8Settings
from quant_phase8.adapters import deribit_rest


def _settings(**overrides):
    env = {
        "PHASE8_OPTIONS_REST_MIN_INTERVAL_SECONDS": "0",
        "PHASE8_OPTIONS_REST_TIMEOUT_SECONDS": "1",
    }
    env.update(overrides)
    return Phase8Settings.from_env(env)


async def _client_for(handler, **settings_overrides):
    sleep = settings_overrides.pop("_sleep", asyncio.sleep)
    monotonic = settings_overrides.pop("_monotonic", time.monotonic)
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    base_url = str(server.make_url("/api/v2")).rstrip("/")
    client = deribit_rest.DeribitPublicRestClient(
        _settings(**settings_overrides), _base_url=base_url, _sleep=sleep, _monotonic=monotonic
    )
    return server, client


@pytest.mark.asyncio
async def test_client_posts_jsonrpc_to_exact_public_v2_method_and_matches_id():
    requests = []

    async def handler(request):
        body = await request.json()
        requests.append((request.method, request.path, body))
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": ["btc_usd"]})

    server, client = await _client_for(handler)
    try:
        result = await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert result["result"] == ["btc_usd"]
    method, path, body = requests[0]
    assert method == "POST"
    assert path == "/api/v2/public/get_index_price_names"
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "public/get_index_price_names"
    assert body["params"] == {"extended": False}
    assert result["id"] == body["id"]


@pytest.mark.asyncio
async def test_client_bounds_one_inflight_request_and_minimum_start_interval():
    active = 0
    peak_active = 0
    starts = []

    async def handler(request):
        nonlocal active, peak_active
        body = await request.json()
        starts.append(time.monotonic())
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": []})

    server, client = await _client_for(
        handler, PHASE8_OPTIONS_REST_MIN_INTERVAL_SECONDS="0.04"
    )
    try:
        await asyncio.gather(
            client.call("public/get_index_price_names", {"extended": False}),
            client.call("public/get_index_price_names", {"extended": False}),
        )
    finally:
        await client.close()
        await server.close()

    assert peak_active == 1
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.035


@pytest.mark.asyncio
async def test_429_and_5xx_use_bounded_retry_and_retry_after():
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        body = await request.json()
        if requests == 1:
            return web.Response(status=429, headers={"Retry-After": "0.03"}, text="do not echo")
        if requests == 2:
            return web.Response(status=503, text="do not echo")
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": ["ok"]})

    server, client = await _client_for(handler)
    started = time.monotonic()
    try:
        result = await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert result["result"] == ["ok"]
    assert requests == 3
    assert time.monotonic() - started >= 0.025


@pytest.mark.asyncio
async def test_transient_failures_stop_at_three_attempts_and_cap_retry_after():
    requests = 0
    sleeps = []
    now = [0.0]

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    async def handler(request):
        nonlocal requests
        requests += 1
        return web.Response(status=429, headers={"Retry-After": "9999"}, text="not retained")

    server, client = await _client_for(
        handler,
        PHASE8_OPTIONS_REST_MAX_ATTEMPTS="3",
        PHASE8_OPTIONS_REST_MAX_COOLDOWN_SECONDS="2",
        _sleep=fake_sleep,
        _monotonic=lambda: now[0],
    )
    try:
        with pytest.raises(deribit_rest.DeribitRestError, match="bounded retries"):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert requests == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_retry_after_cooldown_is_carried_to_the_next_call():
    requests = 0
    now = [0.0]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    async def handler(request):
        nonlocal requests
        requests += 1
        body = await request.json()
        if requests == 1:
            return web.Response(status=429, headers={"Retry-After": "3"}, text="throttled")
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": ["ok"]})

    server, client = await _client_for(
        handler,
        PHASE8_OPTIONS_REST_MAX_ATTEMPTS="1",
        PHASE8_OPTIONS_REST_MAX_COOLDOWN_SECONDS="5",
        _sleep=fake_sleep,
        _monotonic=lambda: now[0],
    )
    try:
        with pytest.raises(deribit_rest.DeribitRestError):
            await client.call("public/get_index_price_names", {"extended": False})
        result = await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert requests == 2
    assert result["result"] == ["ok"]
    assert 3.0 in sleeps
    assert now[0] >= 3.0


@pytest.mark.asyncio
async def test_http_client_error_is_not_retried_and_body_is_never_exposed():
    requests = 0
    sensitive_body = "provider error with private-looking payload"

    async def handler(request):
        nonlocal requests
        requests += 1
        return web.Response(status=400, text=sensitive_body)

    server, client = await _client_for(handler)
    try:
        with pytest.raises(deribit_rest.DeribitRestError) as caught:
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert requests == 1
    assert "400" in str(caught.value)
    assert sensitive_body not in str(caught.value)


@pytest.mark.asyncio
async def test_response_body_size_is_bounded_before_json_parsing():
    body = "{" + " " * 40 + "}"

    async def handler(request):
        return web.Response(text=body, content_type="application/json")

    server, client = await _client_for(
        handler, PHASE8_OPTIONS_MAX_REST_RESPONSE_BYTES="16"
    )
    try:
        with pytest.raises(deribit_rest.DeribitRestResponseTooLarge):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_chunked_response_without_content_length_is_still_bounded():
    content_lengths = []

    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        content_lengths.append(response.content_length)
        await response.write(b"{" + b" " * 40 + b"}")
        await response.write_eof()
        return response

    server, client = await _client_for(
        handler, PHASE8_OPTIONS_MAX_REST_RESPONSE_BYTES="16"
    )
    try:
        with pytest.raises(deribit_rest.DeribitRestResponseTooLarge):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert content_lengths == [None]


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_never_falls_back_to_another_endpoint():
    requests = []
    received = asyncio.Event()

    async def handler(request):
        body = await request.json()
        requests.append(request.path)
        received.set()
        await asyncio.sleep(1.0)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": []})

    server, client = await _client_for(
        handler,
        PHASE8_OPTIONS_REST_TIMEOUT_SECONDS="0.5",
        PHASE8_OPTIONS_REST_MAX_ATTEMPTS="1",
    )
    try:
        with pytest.raises(deribit_rest.DeribitRestError):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert received.is_set()
    assert requests == ["/api/v2/public/get_index_price_names"]


@pytest.mark.asyncio
async def test_jsonrpc_id_mismatch_fails_without_retry_or_fallback():
    requests = []

    async def handler(request):
        body = await request.json()
        requests.append(request.path)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"] + 1, "result": []})

    server, client = await _client_for(handler)
    try:
        with pytest.raises(deribit_rest.DeribitRestError, match="id mismatch"):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert requests == ["/api/v2/public/get_index_price_names"]


@pytest.mark.asyncio
async def test_boolean_jsonrpc_id_does_not_equal_integer_request_id():
    async def handler(request):
        return web.json_response({"jsonrpc": "2.0", "id": True, "result": []})

    server, client = await _client_for(handler)
    try:
        with pytest.raises(deribit_rest.DeribitRestError, match="id mismatch"):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_redirect_is_not_followed_to_another_endpoint():
    requests = []

    async def handler(request):
        requests.append(request.path)
        raise web.HTTPFound(location="/redirect-target")

    server, client = await _client_for(handler)
    try:
        with pytest.raises(deribit_rest.DeribitRestError, match="HTTP 302"):
            await client.call("public/get_index_price_names", {"extended": False})
    finally:
        await client.close()
        await server.close()

    assert requests == ["/api/v2/public/get_index_price_names"]


@pytest.mark.asyncio
async def test_client_rejects_non_public_methods_before_network():
    async def handler(request):
        pytest.fail("private method must not make a request")

    server, client = await _client_for(handler)
    try:
        with pytest.raises(ValueError, match="public allowlist"):
            await client.call("private/buy", {"instrument_name": "BTC-..."})
    finally:
        await client.close()
        await server.close()
