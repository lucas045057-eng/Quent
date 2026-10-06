"""Bitget UTA v3 public REST adapter. No fallback endpoints are permitted."""

from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
from typing import Any

import aiohttp

from ...config import Settings
from ...contracts import Candle, Instrument, Ticker
from ...time import ensure_utc, utc_now
from .parsers import INTERVAL_SECONDS, parse_candles_response, parse_instruments_response, parse_tickers_response
from .rate_limit import TokenBucket, acquire_public_candle_slot


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(max(float(retry_after), 0.0), 8.0)
    return min(2**attempt, 8.0)


class BitgetV3UtaRestClient:
    INSTRUMENTS_PATH = "/api/v3/market/instruments"
    TICKERS_PATH = "/api/v3/market/tickers"
    CANDLES_PATH = "/api/v3/market/candles"

    def __init__(self, settings: Settings | None = None, *, session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings or Settings.from_env({})
        self._session = session
        self._owns_session = session is None
        self._bucket = TokenBucket(rate_per_second=self.settings.rest_requests_per_second, capacity=20)

    async def __aenter__(self) -> "BitgetV3UtaRestClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        for attempt in range(4):
            await self._bucket.acquire()
            try:
                async with self._session.get(self.settings.rest_base_url + path, params=params, timeout=15) as response:
                    if response.status == 429:
                        if attempt == 3:
                            raise RuntimeError("Bitget REST rate limit after retries")
                        retry_after = response.headers.get("Retry-After")
                        await asyncio.sleep(retry_delay(attempt, float(retry_after) if retry_after else None))
                        continue
                    if response.status >= 400:
                        raise RuntimeError(f"Bitget REST HTTP {response.status} for {path}")
                    payload = await response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Bitget REST response must be an object")
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 3:
                    raise
                await asyncio.sleep(retry_delay(attempt))
        raise RuntimeError("unreachable")

    async def get_instruments(self) -> list[Instrument]:
        fetched_at = utc_now()
        payload = await self._get_json(self.INSTRUMENTS_PATH, {"category": "USDT-FUTURES"})
        return parse_instruments_response(payload, fetched_at=fetched_at)

    async def get_spot_instruments(self) -> list[Instrument]:
        """Public SPOT capability discovery independently of on-chain providers."""
        payload = await self._get_json(self.INSTRUMENTS_PATH, {"category": "SPOT"})
        return parse_instruments_response(payload, fetched_at=utc_now(), allow_documented_spot=True)

    async def get_tickers(self, symbol: str | None = None) -> list[Ticker]:
        fetched_at = utc_now()
        params = {"category": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = symbol
        payload = await self._get_json(self.TICKERS_PATH, params)
        return parse_tickers_response(payload, fetched_at=fetched_at)

    async def get_candles(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int = 200,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[Candle]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if (start_time is None) != (end_time is None):
            raise ValueError("start_time and end_time must be provided together")
        fetched_at = utc_now()
        params = {"category": "USDT-FUTURES", "symbol": symbol, "interval": interval, "limit": str(limit)}
        if start_time is not None and end_time is not None:
            if interval not in INTERVAL_SECONDS:
                raise ValueError(f"unsupported interval {interval}")
            start = ensure_utc(start_time)
            end = ensure_utc(end_time)
            if start > end:
                raise ValueError("start_time must not follow end_time")
            params["startTime"] = str(max(0, int(start.timestamp() * 1000) - 1))
            params["endTime"] = str(
                int((end + timedelta(seconds=INTERVAL_SECONDS[interval])).timestamp() * 1000)
            )
        await acquire_public_candle_slot()
        payload = await self._get_json(
            self.CANDLES_PATH,
            params,
        )
        return parse_candles_response(payload, symbol=symbol, interval=interval, fetched_at=fetched_at, now=fetched_at)
