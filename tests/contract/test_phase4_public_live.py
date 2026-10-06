"""Opt-in official public Phase 4 basis schema probes."""

import asyncio
from datetime import datetime, timezone
import json
import os

import aiohttp
import pytest

from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3BasisAdapter
from quant_phase4.adapters.bitget_classic_v2 import BitgetClassicV2LongShortAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5BasisAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5LongShortAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5LiquidationAdapter
from quant_phase4.adapters.hyperliquid_public import HyperliquidPublicBasisAdapter
from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3LiquidationAdapter


LIVE = os.getenv("PHASE4_LIVE_CONTRACT") == "1"


def _require_live() -> None:
    if not LIVE:
        pytest.skip("set PHASE4_LIVE_CONTRACT=1 for official public API probes")


async def _json(method: str, url: str, **kwargs):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with getattr(session, method)(url, **kwargs) as response:
            assert response.status == 200, await response.text()
            return await response.json()


def test_official_public_basis_schemas():
    _require_live()

    async def run() -> None:
        now = datetime.now(timezone.utc)
        bitget = await _json("get", "https://api.bitget.com/api/v3/market/tickers", params={"category": "USDT-FUTURES"})
        bitget_rows = BitgetUTA3BasisAdapter().parse_tickers(bitget, now)
        assert bitget["code"] == "00000"
        assert bitget_rows and all(row.basis_type.value == "MARK_INDEX" for row in bitget_rows)

        bybit = await _json("get", "https://api.bybit.com/v5/market/tickers", params={"category": "linear", "symbol": "BTCUSDT"})
        bybit_rows = BybitV5BasisAdapter().parse_tickers(bybit, now)
        assert bybit["retCode"] == 0
        assert bybit_rows and bybit_rows[0].basis_type.value == "MARK_INDEX"

        hyperliquid = await _json("post", "https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"})
        hyperliquid_rows = HyperliquidPublicBasisAdapter().parse_meta_and_asset_contexts(hyperliquid, now)
        assert hyperliquid_rows and all(row.basis_type.value == "MARK_ORACLE" for row in hyperliquid_rows)

    asyncio.run(run())


def test_official_public_long_short_schemas():
    _require_live()

    async def run() -> None:
        now = datetime.now(timezone.utc)
        bitget = await _json(
            "get",
            "https://api.bitget.com/api/v2/mix/market/long-short",
            params={"symbol": "BTCUSDT", "period": "5m"},
        )
        bitget_row = BitgetClassicV2LongShortAdapter().parse_response(bitget, received_at=now)
        assert bitget["code"] == "00000"
        assert bitget_row.status.value == "AVAILABLE"
        assert bitget_row.period == "5m"
        assert bitget_row.long_value is not None and bitget_row.short_value is not None and bitget_row.ratio is not None
        assert bitget_row.exchange_timestamp.tzinfo is not None

        bybit = await _json(
            "get",
            "https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": "BTCUSDT", "period": "5min"},
        )
        bybit_row = BybitV5LongShortAdapter().parse_response(bybit, received_at=now)
        assert bybit["retCode"] == 0
        assert bybit_row.status.value == "AVAILABLE"
        assert bybit_row.period == "5min"
        assert bybit_row.long_value is not None and bybit_row.short_value is not None and bybit_row.ratio is not None
        assert bybit_row.exchange_timestamp.tzinfo is not None

    asyncio.run(run())


def test_official_public_liquidation_websocket_schemas():
    _require_live()

    async def receive_rows(ws, adapter, now):
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            message = await asyncio.wait_for(ws.receive(), timeout=5)
            if message.type is aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
                if isinstance(payload, dict) and payload.get("data"):
                    return adapter.parse_ws_message(payload, now)
        raise AssertionError("public liquidation WebSocket produced no data within timeout")

    async def run() -> None:
        now = datetime.now(timezone.utc)
        bitget_adapter = BitgetUTA3LiquidationAdapter()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.ws_connect(bitget_adapter.public_ws_url) as ws:
                await ws.send_str(json.dumps(bitget_adapter.subscription("BTCUSDT")))
                bitget_rows = await receive_rows(ws, bitget_adapter, now)
                assert bitget_rows and bitget_rows[0].source_channel == "liquidation"

            bybit_adapter = BybitV5LiquidationAdapter()
            async with session.ws_connect(bybit_adapter.public_ws_url) as ws:
                await ws.send_str(json.dumps(bybit_adapter.subscription("BTCUSDT")))
                bybit_rows = await receive_rows(ws, bybit_adapter, now)
                assert bybit_rows and bybit_rows[0].source_channel == "allLiquidation.BTCUSDT"

    asyncio.run(run())
