"""Shared public HTTP adapter primitives."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import random
from typing import Any, Mapping

import aiohttp


class AdapterSchemaError(ValueError):
    pass


def decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdapterSchemaError(f"{field} must be Decimal-compatible") from exc


def timestamp_ms(value: Any, field: str) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdapterSchemaError(f"{field} must be epoch milliseconds") from exc


def success_payload(payload: Mapping[str, Any], *, code_key: str = "code", success: str = "00000") -> Any:
    if payload.get(code_key) != success:
        raise AdapterSchemaError(f"unexpected response code: {payload.get(code_key)!r}")
    return payload.get("data")


class PublicHTTPAdapter:
    def __init__(
        self,
        base_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        rate_limit_per_second: float | None = None,
        jitter_seconds: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self._owns_session = session is None
        self._min_request_interval = 1.0 / rate_limit_per_second if rate_limit_per_second else 0.0
        self._jitter_seconds = max(0.0, jitter_seconds)
        self._schedule_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> "PublicHTTPAdapter":
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self.session is not None:
            await self.session.close()
            self.session = None

    @staticmethod
    def retry_delay(attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(float(retry_after), 0.0), 8.0)
        return min(2**attempt, 8.0)

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        for attempt in range(4):
            try:
                await self._wait_for_request_slot()
                request = getattr(self.session, method.lower())
                async with request(self.base_url + path, timeout=15, **kwargs) as response:
                    if response.status == 429:
                        if attempt == 3:
                            raise AdapterSchemaError(f"HTTP 429 for {path} after retries")
                        retry_after = response.headers.get("Retry-After")
                        await asyncio.sleep(self.retry_delay(attempt, float(retry_after) if retry_after else None))
                        continue
                    if response.status >= 400:
                        raise AdapterSchemaError(f"HTTP {response.status} for {path}")
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 3:
                    raise
                await asyncio.sleep(self.retry_delay(attempt))
        raise AdapterSchemaError(f"request failed for {path}")

    async def _wait_for_request_slot(self) -> None:
        if self._min_request_interval <= 0:
            return
        async with self._schedule_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self._min_request_interval
            if self._jitter_seconds:
                self._next_request_at += random.uniform(0.0, self._jitter_seconds)
        if wait:
            await asyncio.sleep(wait)

    async def get_json(self, path: str, params: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        payload = await self._request_json("GET", path, params=params)
        if not isinstance(payload, Mapping):
            raise AdapterSchemaError(f"{path} response must be an object")
        return payload

    async def post_json(self, path: str, body: Mapping[str, Any]) -> Any:
        return await self._request_json("POST", path, json=dict(body))
