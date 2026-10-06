"""Opt-in official public Phase 3 REST/WS contract probes.

Run with ``PHASE3_LIVE_CONTRACT=1``. No credentials are read.
"""

import asyncio
from datetime import datetime, timezone
import json
import os

import aiohttp
import pytest
from websockets.asyncio.client import connect

from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter


LIVE = os.getenv("PHASE3_LIVE_CONTRACT") == "1"
BYBIT_SYMBOL = os.getenv("PHASE3_BYBIT_SYMBOL", "BTCUSDT")
BITGET_SYMBOL = os.getenv("PHASE3_BITGET_SYMBOL", "BTCUSDT")
HYPERLIQUID_COIN = os.getenv("PHASE3_HYPERLIQUID_COIN", "BTC")


def _require_live():
    if not LIVE:
        pytest.skip("set PHASE3_LIVE_CONTRACT=1 for official public API probes")


async def _rest_json(url, params):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            assert response.status == 200, await response.text()
            return await response.json()


async def _first_data_message(socket, *, channel: str | None = None):
    for _ in range(12):
        raw = await asyncio.wait_for(socket.recv(), timeout=15)
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if payload.get("event") == "error" or payload.get("channel") == "error":
            raise AssertionError(f"public WebSocket subscription failed: {payload}")
        if channel is None:
            if payload.get("data") and payload.get("channel") not in {"subscriptionResponse"}:
                return payload
        elif payload.get("channel") == channel and payload.get("data"):
            return payload
    raise AssertionError("public WebSocket did not return a data message")


def test_official_public_rest_contracts():
    _require_live()

    async def run():
        now = datetime.now(timezone.utc)
        bybit_payload = await _rest_json(
            "https://api.bybit.com/v5/market/recent-trade",
            {"category": "linear", "symbol": BYBIT_SYMBOL, "limit": "5"},
        )
        bybit_trades = BybitPublicTradeAdapter().parse_recent_trades(
            bybit_payload, now, exchange_symbol=BYBIT_SYMBOL
        )
        assert bybit_payload["retCode"] == 0
        assert bybit_trades
        assert all(trade.trade_id for trade in bybit_trades)

        bitget_payload = await _rest_json(
            "https://api.bitget.com/api/v3/market/fills",
            {"category": "USDT-FUTURES", "symbol": BITGET_SYMBOL, "limit": "5"},
        )
        bitget_trades = BitgetUTA3PublicTradeAdapter().parse_recent_trades(
            bitget_payload, now, exchange_symbol=BITGET_SYMBOL
        )
        assert bitget_payload["code"] == "00000"
        assert bitget_trades
        assert all(trade.aggressor_side.value == "UNKNOWN" for trade in bitget_trades)

    asyncio.run(run())


def test_official_public_websocket_subscriptions_and_payloads():
    _require_live()

    async def run():
        now = datetime.now(timezone.utc)
        bybit = BybitPublicTradeAdapter()
        async with connect(bybit.ws_url, open_timeout=15, ping_interval=20) as socket:
            await socket.send(json.dumps(bybit.subscription(BYBIT_SYMBOL)))
            payload = await _first_data_message(socket)
            trades = bybit.parse_ws_message(payload, now)
            assert trades
            assert all(trade.trade_id for trade in trades)

        bitget = BitgetUTA3PublicTradeAdapter()
        async with connect(bitget.ws_url, open_timeout=15, ping_interval=20) as socket:
            await socket.send(json.dumps(bitget.subscription(BITGET_SYMBOL)))
            payload = await _first_data_message(socket)
            trades = bitget.parse_ws_message(payload, now)
            assert trades
            assert all(trade.aggressor_side.value == "UNKNOWN" for trade in trades)

        hyperliquid = HyperliquidPublicTradeAdapter()
        async with connect(hyperliquid.ws_url, open_timeout=15, ping_interval=20) as socket:
            await socket.send(json.dumps(hyperliquid.subscription(HYPERLIQUID_COIN)))
            payload = await _first_data_message(socket, channel="trades")
            trades = hyperliquid.parse_ws_message(payload, now)
            assert trades
            assert all(trade.trade_id for trade in trades)
            assert all(trade.aggressor_side.value == "UNKNOWN" for trade in trades)

    asyncio.run(run())
