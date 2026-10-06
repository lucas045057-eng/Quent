"""Opt-in public official Phase 2 contract probes.

Run with ``PHASE2_LIVE_CONTRACT=1``. No credentials are read.
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone

import pytest

from quant_phase2.adapters.bitget import BitgetUTAAdapter
from quant_phase2.adapters.bybit import BybitV5Adapter
from quant_phase2.adapters.hyperliquid import HyperliquidAdapter


def _enabled() -> None:
    if os.getenv("PHASE2_LIVE_CONTRACT") != "1":
        pytest.skip("set PHASE2_LIVE_CONTRACT=1 for official public API probes")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _bitget_contract() -> None:
    _enabled()
    async with BitgetUTAAdapter() as adapter:
        instruments = await adapter.fetch_instruments(_now())
        oi_rows, funding_rows = await adapter.fetch_tickers(_now(), "BTCUSDT")
        current = await adapter.fetch_current_funding("BTCUSDT", _now())
    btc = next(item for item in instruments if item.exchange_symbol == "BTCUSDT")
    assert btc.funding_interval_seconds is not None
    assert btc.contract_size is not None
    assert oi_rows[0].raw_open_interest is not None
    assert funding_rows[0].funding_rate is not None
    assert current.funding_rate is not None
    assert current.funding_interval_seconds is not None


async def _bybit_contract() -> None:
    _enabled()
    async with BybitV5Adapter() as adapter:
        instruments = await adapter.fetch_instruments(_now())
        btc = next(item for item in instruments if item.exchange_symbol == "BTCUSDT")
        oi, funding = await adapter.fetch_ticker("BTCUSDT", _now(), btc.funding_interval_seconds)
        oi_history = await adapter.fetch_oi_history("BTCUSDT", limit=2)
        funding_history = await adapter.fetch_funding_history("BTCUSDT", limit=2)
    assert btc.funding_interval_seconds is not None
    assert oi.raw_unit == "BASE_ASSET"
    assert oi.open_interest_usd is not None
    assert funding.funding_rate is not None
    assert oi_history["retCode"] == 0
    assert funding_history["retCode"] == 0


async def _hyperliquid_contract() -> None:
    _enabled()
    async with HyperliquidAdapter() as adapter:
        instruments, oi_rows, funding_rows = await adapter.fetch_meta_and_contexts(_now())
        history = await adapter.fetch_funding_history("BTC", start_time_ms=int(_now().timestamp() * 1000) - 7_200_000)
    assert instruments
    assert oi_rows
    assert funding_rows
    assert oi_rows[0].open_interest_usd is not None
    assert isinstance(history, list)


def test_bitget_uta_v3_public_oi_and_funding_contract() -> None:
    asyncio.run(_bitget_contract())


def test_bybit_v5_public_oi_funding_and_instrument_contract() -> None:
    asyncio.run(_bybit_contract())


def test_hyperliquid_public_info_contract() -> None:
    asyncio.run(_hyperliquid_contract())
