"""Shared strict helpers for Phase 4 public basis adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
import re
from typing import Any, Mapping

import aiohttp

from quant_phase1.adapters.bitget_v3.rate_limit import TokenBucket


class AdapterError(Exception):
    """Safe-to-classify adapter failure; never requires retaining response text."""

    category = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        http_status: int | None = None,
        provider_code: str | None = None,
        schema_stage: str | None = None,
        schema_field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.http_status = http_status
        self.provider_code = _safe_code(provider_code)
        self.schema_stage = schema_stage
        self.schema_field = schema_field


def _safe_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,48}", text) else None


class AdapterNetworkError(AdapterError):
    category = "NETWORK_ERROR"


class AdapterTimeoutError(AdapterError):
    category = "TIMEOUT"


class AdapterHTTPError(AdapterError):
    category = "HTTP_ERROR"


class AdapterRateLimitError(AdapterHTTPError):
    category = "RATE_LIMIT"


class AdapterAuthError(AdapterHTTPError):
    category = "AUTH_ERROR"


class AdapterProviderError(AdapterError):
    category = "PROVIDER_ERROR"


class AdapterParseError(AdapterError):
    category = "PARSE_ERROR"


class AdapterSchemaError(AdapterError, ValueError):
    """The response cannot satisfy the single documented source schema."""

    category = "SCHEMA_ERROR"

    def __init__(self, message: str, *, field: str | None = None, stage: str = "response_validation") -> None:
        super().__init__(message, schema_field=field, schema_stage=stage)


def _raise_http_error(status: int, endpoint: str) -> None:
    message = f"HTTP {status} for {endpoint}"
    if status == 429:
        raise AdapterRateLimitError(message, http_status=status, endpoint=endpoint)
    if status in {401, 403}:
        raise AdapterAuthError(message, http_status=status, endpoint=endpoint)
    raise AdapterHTTPError(message, http_status=status, endpoint=endpoint)


async def _response_json(response: Any, *, endpoint: str) -> Mapping[str, Any]:
    if response.status >= 400:
        _raise_http_error(int(response.status), endpoint)
    try:
        payload = await response.json()
    except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise AdapterParseError(
            f"{endpoint} response is not valid JSON", endpoint=endpoint
        ) from exc
    if not isinstance(payload, Mapping):
        raise AdapterSchemaError(f"{endpoint} response must be an object", field="response")
    return payload


async def _request_json(session: aiohttp.ClientSession, base_url: str, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
    endpoint = path
    try:
        async with session.get(base_url.rstrip("/") + path, params=dict(params), timeout=15) as response:
            return await _response_json(response, endpoint=endpoint)
    except AdapterError:
        raise
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise AdapterTimeoutError(f"request timed out for {endpoint}", endpoint=endpoint) from exc
    except (aiohttp.ClientError, OSError) as exc:
        raise AdapterNetworkError(f"network request failed for {endpoint}", endpoint=endpoint) from exc


class SharedPublicRESTTransport:
    """Settings-driven session and shared limiter owned by a runtime."""

    def __init__(self, base_url: str, *, session: aiohttp.ClientSession, rate_limiter: TokenBucket) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.rate_limiter = rate_limiter

    async def get_json(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        await self.rate_limiter.acquire()
        return await _request_json(self.session, self.base_url, path, params)


def mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterSchemaError(f"{field} must be an object", field=field)
    return value


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterSchemaError(f"{field} must be a non-empty string", field=field)
    return value


def decimal_field(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdapterSchemaError(f"{field} must be Decimal-compatible", field=field, stage="field_parse") from exc
    if not result.is_finite() or result <= 0:
        raise AdapterSchemaError(f"{field} must be positive and finite", field=field, stage="field_validation")
    return result


def utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise AdapterSchemaError(f"{field} must be UTC-aware", field=field)
    return value


def epoch_milliseconds(value: Any, field: str) -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float):
            if not isfinite(value) or not value.is_integer():
                raise ValueError
        elif isinstance(value, Decimal):
            if not value.is_finite() or value != value.to_integral_value():
                raise ValueError
        elif not isinstance(value, (int, str)):
            raise ValueError
        milliseconds = int(value)
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AdapterSchemaError(f"{field} must be epoch milliseconds", field=field, stage="timestamp_parse") from exc


def canonical_perpetual(exchange_symbol: str) -> str:
    symbol = exchange_symbol.strip().upper()
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT-PERP"
    return f"{symbol}-USDT-PERP"
