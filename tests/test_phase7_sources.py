from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from quant_phase7.sources import (
    AuthMode,
    BoundedPublicTransport,
    HTTPStatusError,
    ResponseTooLarge,
    RetryExhausted,
    SourceStatus,
    SourceRegistry,
    SourceUnavailable,
    default_source_definitions,
)


class Response:
    def __init__(self, status: int, body: bytes = b"{}", headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self) -> bytes:
        return self._body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_default_source_matrix_is_explicit_and_bounded():
    definitions = {item.source_id: item for item in default_source_definitions()}
    assert set(definitions) == {
        "btc_core_rpc", "ethereum_rpc", "binance_spot", "bitget_spot_uta_v3",
    }
    assert definitions["btc_core_rpc"].status is SourceStatus.NOT_CONFIGURED
    assert definitions["ethereum_rpc"].status is SourceStatus.NOT_CONFIGURED
    assert definitions["binance_spot"].auth_mode is AuthMode.NONE
    bitget = definitions["bitget_spot_uta_v3"]
    assert bitget.status is SourceStatus.PENDING_CONTRACT
    assert bitget.fallback_source_id is None
    assert bitget.ws_schema == {
        "op": "subscribe",
        "args": [{"instType": "spot", "topic": "publicTrade", "symbol": "BTCUSDT"}],
    }
    assert "channel=trade" not in json.dumps(bitget.ws_schema)
    assert bitget.max_bytes == 4 * 1024 * 1024


def test_disabled_and_unconfigured_sources_do_not_make_network_calls():
    session = Session([])
    transport = BoundedPublicTransport(session)

    with pytest.raises(SourceUnavailable):
        asyncio.run(transport.request("bitget_spot_uta_v3", "/api/v3/market/fills"))
    with pytest.raises(SourceUnavailable):
        asyncio.run(transport.request("btc_core_rpc", "/anything"))
    assert session.calls == []


def test_binance_path_is_allowlisted_and_response_is_bounded():
    session = Session([Response(200, b'{"ok":true}')])
    transport = BoundedPublicTransport(session)

    result = asyncio.run(transport.request(
        "binance_spot", "/api/v3/aggTrades", params={"symbol": "BTCUSDT"},
    ))
    assert result.json() == {"ok": True}
    assert result.attempts == 1
    assert session.calls[0][0] == "https://api.binance.com/api/v3/aggTrades"
    with pytest.raises(ValueError):
        asyncio.run(transport.request("binance_spot", "/api/v3/exchangeInfo"))


def test_retryable_429_is_finite_and_honors_retry_after(monkeypatch):
    session = Session([
        Response(429, headers={"Retry-After": "0"}),
        Response(200, b'{"ok":true}'),
    ])
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    result = asyncio.run(BoundedPublicTransport(session).request(
        "binance_spot", "/api/v3/aggTrades",
    ))
    assert result.attempts == 2
    assert sleeps[0] == 0.0
    assert sleeps[1] >= 0.19


def test_transport_errors_retry_then_report_exhaustion(monkeypatch):
    session = Session([ConnectionError("reset"), ConnectionError("reset"), ConnectionError("reset")])
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(RetryExhausted):
        asyncio.run(BoundedPublicTransport(session).request(
            "binance_spot", "/api/v3/aggTrades",
        ))
    assert len(session.calls) == 3
    assert sleeps[0] == pytest.approx(0.25)
    assert sleeps[1] >= 0.19
    assert sleeps[2] == pytest.approx(1.0)
    assert sleeps[3] >= 0.19


def test_non_retryable_http_status_is_not_retried():
    session = Session([Response(403, b"blocked"), Response(200)])
    with pytest.raises(HTTPStatusError):
        asyncio.run(BoundedPublicTransport(session).request(
            "binance_spot", "/api/v3/aggTrades",
        ))
    assert len(session.calls) == 1


def test_content_length_and_body_caps_are_enforced():
    too_large_header = Session([Response(200, headers={"Content-Length": str(9 * 1024 * 1024)})])
    with pytest.raises(ResponseTooLarge):
        asyncio.run(BoundedPublicTransport(too_large_header).request(
            "binance_spot", "/api/v3/aggTrades",
        ))


def test_request_bounds_connect_budget_and_fixed_bitget_params():
    definitions = default_source_definitions()
    binance = next(item for item in definitions if item.source_id == "binance_spot")
    session = Session([Response(200)])
    transport = BoundedPublicTransport(session, SourceRegistry((binance,)))
    with pytest.raises(ValueError):
        asyncio.run(transport.request("binance_spot", "/api/v3/aggTrades", page_number=4))
    with pytest.raises(ValueError):
        asyncio.run(transport.request("binance_spot", "/api/v3/aggTrades", event_count=3_001))
    with pytest.raises(ValueError):
        asyncio.run(transport.request("binance_spot", "/api/v3/aggTrades", queue_size=2_049))
    asyncio.run(transport.request("binance_spot", "/api/v3/aggTrades"))
    timeout = session.calls[0][2]
    assert getattr(timeout, "connect", 1.0) == pytest.approx(1.0)

    bitget = replace(
        next(item for item in definitions if item.source_id == "bitget_spot_uta_v3"),
        status=SourceStatus.ENABLED,
    )
    bitget_session = Session([Response(200)])
    asyncio.run(BoundedPublicTransport(bitget_session, SourceRegistry((bitget,))).request(
        "bitget_spot_uta_v3", "/api/v3/market/fills",
    ))
    assert bitget_session.calls[0][1]["category"] == "SPOT"
    with pytest.raises(ValueError):
        asyncio.run(BoundedPublicTransport(bitget_session, SourceRegistry((bitget,))).request(
            "bitget_spot_uta_v3", "/api/v3/market/fills", params={"category": "USDT-FUTURES"},
        ))


def test_ethereum_log_range_cap_is_enforced():
    ethereum = replace(
        next(item for item in default_source_definitions() if item.source_id == "ethereum_rpc"),
        status=SourceStatus.ENABLED,
        rest_base_url="https://rpc.example.invalid",
        rest_paths=("/rpc",),
    )
    session = Session([Response(200)])
    transport = BoundedPublicTransport(session, SourceRegistry((ethereum,)))
    asyncio.run(transport.request("ethereum_rpc", "/rpc", block_range=1_000))
    with pytest.raises(ValueError):
        asyncio.run(transport.request("ethereum_rpc", "/rpc", block_range=1_001))

    too_large_body = Session([Response(200, b"x" * (8 * 1024 * 1024 + 1))])
    with pytest.raises(ResponseTooLarge):
        asyncio.run(BoundedPublicTransport(too_large_body).request(
            "binance_spot", "/api/v3/aggTrades",
        ))


def test_source_limits_are_not_unbounded():
    for definition in default_source_definitions():
        assert definition.max_retries <= 4
        assert definition.max_pages <= 1_000
        assert definition.max_events <= 20_000
        assert definition.queue_capacity <= 2_048
        assert definition.max_concurrency == 1
