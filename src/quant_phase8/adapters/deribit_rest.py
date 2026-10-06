"""Public Deribit REST response parsers for Phase 8.

These functions consume already-decoded JSON-RPC objects. They deliberately
have no network, retry, or persistence behavior; provider fields terminate at
this boundary and are translated into canonical contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence, Set
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from itertools import count
import math
import time
from typing import Any, Awaitable, Callable

import aiohttp

from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    ObservationKind,
    OptionInstrument,
    OptionMarketObservation,
    OptionMetricValue,
    OptionType,
    Provenance,
    TimestampSemantics,
    UnitStatus,
    decimal_from_json,
    loads_decimal_json,
)


_SOURCE = "deribit"
_EXCHANGE = "DERIBIT"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
REST_BASE_URL = "https://www.deribit.com/api/v2"
_PUBLIC_METHODS = frozenset(
    {
        "public/get_index_price_names",
        "public/get_instruments",
        "public/get_book_summary_by_currency",
    }
)
_SUMMARY_METRICS = (
    ("volume", "volume_24h"),
    ("open_interest", "open_interest"),
    ("underlying_price", "underlying_price"),
    ("mark_price", "mark_price"),
    ("mark_iv", "mark_iv"),
    ("bid_price", "bid_price"),
    ("ask_price", "ask_price"),
    ("mid_price", "mid_price"),
    ("last", "last_price"),
)


class DeribitRestError(RuntimeError):
    """Sanitized transport or response-contract error."""


class DeribitRestResponseTooLarge(DeribitRestError):
    """Response exceeded the configured bounded-read budget."""


class DeribitRestSchemaError(DeribitRestError):
    """Response could not be trusted as the expected JSON-RPC contract."""


class DeribitPublicRestClient:
    """Serialized, bounded, public-only JSON-RPC client for Deribit REST."""

    def __init__(
        self,
        settings: Phase8Settings | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
        _base_url: str = REST_BASE_URL,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        _monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Phase8Settings.from_env({})
        self._session = session
        self._owns_session = session is None
        self._base_url = _base_url.rstrip("/")
        self._sleep = _sleep
        self._monotonic = _monotonic
        self._request_ids = count(1)
        self._request_lock = asyncio.Lock()
        self._last_request_started: float | None = None
        self._cooldown_until: float | None = None

    async def __aenter__(self) -> "DeribitPublicRestClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def _pace(self) -> None:
        now = self._monotonic()
        due_at = now
        if self._last_request_started is not None:
            due_at = max(due_at, self._last_request_started + self.settings.rest_min_interval_seconds)
        if self._cooldown_until is not None:
            due_at = max(due_at, self._cooldown_until)
        delay = due_at - now
        if delay > 0:
            await self._sleep(delay)

    def _retry_after(self, value: str | None, attempt: int) -> float:
        delay: float | None = None
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None:
            delay = float(2 ** (attempt - 1))
        if not math.isfinite(delay):
            delay = float(2 ** (attempt - 1))
        return min(max(delay, 0.0), float(self.settings.rest_max_cooldown_seconds))

    async def _read_bounded(self, response: aiohttp.ClientResponse) -> bytes:
        limit = self.settings.rest_max_response_bytes
        content_length = response.content_length
        if content_length is not None and content_length > limit:
            raise DeribitRestResponseTooLarge("Deribit REST response exceeded configured byte limit")
        body = bytearray()
        async for chunk in response.content.iter_chunked(min(64 * 1024, limit + 1)):
            if len(body) + len(chunk) > limit:
                raise DeribitRestResponseTooLarge("Deribit REST response exceeded configured byte limit")
            body.extend(chunk)
        return bytes(body)

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """POST a single allowlisted public JSON-RPC method; never falls back."""
        if method not in _PUBLIC_METHODS:
            raise ValueError("method is not in the Phase 8 public allowlist")
        if not isinstance(params, Mapping):
            raise TypeError("public REST params must be a mapping")

        request_id = next(self._request_ids)
        request_body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        url = f"{self._base_url}/{method}"
        timeout = aiohttp.ClientTimeout(total=self.settings.rest_timeout_seconds)

        async with self._request_lock:
            for attempt in range(1, self.settings.rest_max_attempts + 1):
                await self._pace()
                self._last_request_started = self._monotonic()
                retry_delay: float | None = None
                try:
                    session = await self._ensure_session()
                    async with session.post(
                        url, json=request_body, timeout=timeout, allow_redirects=False
                    ) as response:
                        if response.status == 429 or response.status >= 500:
                            retry_after = response.headers.get("Retry-After") if response.status == 429 else None
                            retry_delay = self._retry_after(retry_after, attempt)
                            self._cooldown_until = self._monotonic() + retry_delay
                            if attempt >= self.settings.rest_max_attempts:
                                raise DeribitRestError(
                                    f"Deribit public REST transient HTTP failure ({response.status}) after bounded retries"
                                )
                        elif response.status < 200 or response.status >= 300:
                            raise DeribitRestError(f"Deribit public REST HTTP {response.status}")
                        else:
                            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                            if content_type != "application/json" and not content_type.endswith("+json"):
                                raise DeribitRestSchemaError("Deribit public REST response content type is not JSON")
                            raw = await self._read_bounded(response)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    retry_delay = self._retry_after(None, attempt)
                    self._cooldown_until = self._monotonic() + retry_delay
                    if attempt >= self.settings.rest_max_attempts:
                        raise DeribitRestError("Deribit public REST transport failed after bounded retries") from None
                    await self._sleep(retry_delay)
                    continue
                if retry_delay is not None:
                    await self._sleep(retry_delay)
                    continue

                try:
                    payload = loads_decimal_json(raw)
                except (UnicodeDecodeError, ValueError, TypeError):
                    raise DeribitRestSchemaError("Deribit public REST response is not valid JSON") from None
                if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
                    raise DeribitRestSchemaError("Deribit public REST response JSON-RPC version mismatch")
                response_id = payload.get("id")
                if isinstance(response_id, bool) or not isinstance(response_id, int) or response_id != request_id:
                    raise DeribitRestSchemaError("Deribit public REST response id mismatch")
                if "error" in payload:
                    raise DeribitRestSchemaError("Deribit public REST JSON-RPC method returned an error")
                if "result" not in payload:
                    raise DeribitRestSchemaError("Deribit public REST response is missing result")
                return dict(payload)
        raise DeribitRestError("Deribit public REST call ended without a response")


def _jsonrpc_result(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        raise ValueError("JSON-RPC response must be an object")
    if payload.get("jsonrpc") != "2.0":
        raise ValueError("JSON-RPC version mismatch")
    request_id = payload.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
        raise ValueError("JSON-RPC response id is invalid")
    if "error" in payload:
        raise ValueError("Deribit public REST request returned an error")
    if "result" not in payload:
        raise ValueError("JSON-RPC response is missing result")
    return payload["result"]


def _rows(payload: Any, *, max_records: int) -> list[Mapping[str, Any]]:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise ValueError("record cap must be a positive integer")
    result = _jsonrpc_result(payload)
    if not isinstance(result, list):
        raise ValueError("JSON-RPC result must be a list")
    if len(result) > max_records:
        raise ValueError("response exceeds configured record cap")
    if any(not isinstance(row, Mapping) for row in result):
        raise ValueError("response row schema mismatch")
    return result


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required by the source contract")
    return value


def _decimal_field(row: Mapping[str, Any], field: str, *, nullable: bool = False) -> Decimal | None:
    if field not in row:
        if nullable:
            return None
        raise ValueError(f"{field} is required by the source contract")
    value = row[field]
    if value is None and nullable:
        return None
    try:
        return decimal_from_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite JSON number") from exc


def _epoch_milliseconds(value: Any, field: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} timestamp must be an integer in Unix milliseconds")
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError as exc:
        raise ValueError(f"{field} timestamp is outside the supported range") from exc


def _currency(value: str) -> str:
    if value not in {"BTC", "ETH"}:
        raise ValueError("requested currency must be BTC or ETH")
    return value


def parse_index_price_names_response(payload: Any) -> frozenset[str]:
    """Parse the `extended=false` supported-index response (a string list)."""
    result = _jsonrpc_result(payload)
    if not isinstance(result, list) or not result:
        raise ValueError("supported index result must be a non-empty string list")
    if any(not isinstance(name, str) or not name.strip() for name in result):
        raise ValueError("supported index result must be a string list without empty names")
    if len(set(result)) != len(result):
        raise ValueError("supported index result contains duplicate names")
    return frozenset(result)


def parse_instruments_response(
    payload: Any,
    *,
    requested_currency: str,
    supported_index_names: Set[str] | frozenset[str],
    fetched_at: datetime,
    processed_at: datetime,
    max_records: int = 2048,
) -> tuple[OptionInstrument, ...]:
    """Validate and canonicalize a public/get_instruments option response."""
    currency = _currency(requested_currency)
    if not isinstance(supported_index_names, (set, frozenset)) or not supported_index_names:
        raise ValueError("supported index names are required for instrument validation")
    rows = _rows(payload, max_records=max_records)
    result: list[OptionInstrument] = []
    symbols: set[str] = set()
    for row in rows:
        symbol = _required_text(row, "instrument_name")
        if symbol in symbols:
            raise ValueError("duplicate instrument_name in response")
        symbols.add(symbol)

        base_currency = _required_text(row, "base_currency")
        quote_currency = _required_text(row, "quote_currency")
        settlement_currency = _required_text(row, "settlement_currency")
        if base_currency != currency or settlement_currency != currency:
            raise ValueError("instrument currency does not match requested currency")
        if row.get("kind") != "option":
            raise ValueError("instrument kind must be option")
        option_type = row.get("option_type")
        if option_type not in {OptionType.CALL.value, OptionType.PUT.value}:
            raise ValueError("instrument option_type must be call or put")
        is_active = row.get("is_active")
        if not isinstance(is_active, bool):
            raise ValueError("instrument is_active must be boolean")
        state = _required_text(row, "state")
        price_index = _required_text(row, "price_index")
        if price_index not in supported_index_names:
            raise ValueError("instrument price_index is not in supported index names")

        strike = _decimal_field(row, "strike")
        if strike is None or strike <= 0:
            raise ValueError("instrument strike must be positive")
        expires_at = _epoch_milliseconds(row.get("expiration_timestamp"), "expiration_timestamp")
        assert expires_at is not None
        created_at = _epoch_milliseconds(row.get("creation_timestamp"), "creation_timestamp", nullable=True)
        provider_id = row.get("instrument_id")
        if provider_id is not None and (
            isinstance(provider_id, bool) or not isinstance(provider_id, int) or provider_id <= 0
        ):
            raise ValueError("instrument_id must be a positive integer")

        result.append(
            OptionInstrument(
                exchange=_EXCHANGE,
                source=_SOURCE,
                symbol=symbol,
                underlying=currency,
                option_type=option_type,
                strike=strike,
                expires_at=expires_at,
                instrument_created_at=created_at,
                instrument_state=state,
                is_active=is_active,
                price_index=price_index,
                base_currency=base_currency,
                quote_currency=quote_currency,
                settlement_currency=settlement_currency,
                exchange_timestamp=None,
                fetched_at=fetched_at,
                processed_at=processed_at,
                status=DataStatus.AVAILABLE,
                provider_instrument_id=provider_id,
                source_field="result[]",
                timestamp_semantics=TimestampSemantics.NOT_PROVIDED,
            )
        )
    return tuple(result)


def _summary_metric(
    row: Mapping[str, Any],
    *,
    source_field: str,
    metric: str,
    fetched_at: datetime,
    unit_code: str | None,
    unit_status: UnitStatus,
) -> OptionMetricValue:
    present = source_field in row
    raw_value = row.get(source_field)
    if not present:
        value, status, reason = None, DataStatus.NOT_AVAILABLE, "MISSING_FIELD"
    elif raw_value is None:
        value, status, reason = None, DataStatus.NOT_AVAILABLE, "EXPLICIT_NULL"
    else:
        value = _decimal_field(row, source_field)
        status, reason = DataStatus.AVAILABLE, None
    return OptionMetricValue(
        metric=metric,
        value=value,
        source=_SOURCE,
        exchange=_EXCHANGE,
        source_field=source_field,
        source_method_or_channel="public/get_book_summary_by_currency",
        exchange_timestamp=None,
        field_last_updated_at=None,
        fetched_at=fetched_at,
        received_at=None,
        timestamp_semantics=TimestampSemantics.NOT_PROVIDED,
        unit_code=unit_code,
        unit_status=unit_status,
        status=status,
        provenance=Provenance.SOURCE_PROVIDED,
        quality_reason=reason,
    )


def parse_book_summary_response(
    payload: Any,
    *,
    requested_currency: str,
    instrument_catalog: Sequence[OptionInstrument],
    fetched_at: datetime,
    processed_at: datetime,
    max_records: int = 2048,
) -> tuple[OptionMarketObservation, ...]:
    """Parse one all-or-nothing public/get_book_summary_by_currency cycle."""
    currency = _currency(requested_currency)
    rows = _rows(payload, max_records=max_records)
    catalog: dict[str, OptionInstrument] = {}
    for instrument in instrument_catalog:
        if instrument.symbol in catalog:
            raise ValueError("duplicate instrument in canonical catalog")
        catalog[instrument.symbol] = instrument

    result: list[OptionMarketObservation] = []
    seen: set[str] = set()
    for row in rows:
        symbol = _required_text(row, "instrument_name")
        if symbol in seen:
            raise ValueError("duplicate instrument_name in summary response")
        seen.add(symbol)
        instrument = catalog.get(symbol)
        if instrument is None or instrument.underlying != currency:
            raise ValueError("summary contains unknown instrument for requested currency")

        summary_base = row.get("base_currency")
        if summary_base is not None and summary_base != currency:
            raise ValueError("summary base currency does not match requested currency")
        summary_quote = row.get("quote_currency")
        if summary_quote is not None:
            if not isinstance(summary_quote, str) or not summary_quote.strip():
                raise ValueError("summary quote_currency is invalid")
            if summary_quote != instrument.quote_currency:
                raise ValueError("summary quote currency does not match instrument catalog")
        else:
            summary_quote = instrument.quote_currency
        summary_index = row.get("underlying_index")
        if summary_index is not None and (not isinstance(summary_index, str) or not summary_index.strip()):
            raise ValueError("summary underlying_index is invalid")

        metrics: dict[str, OptionMetricValue] = {}
        for field_name, metric_name in _SUMMARY_METRICS:
            if field_name == "volume":
                unit_code, unit_status = currency, UnitStatus.VERIFIED
            elif field_name == "open_interest":
                unit_code, unit_status = currency, UnitStatus.VERIFIED
            elif field_name in {"mark_price", "bid_price", "ask_price", "mid_price", "last"}:
                unit_code, unit_status = summary_quote, UnitStatus.VERIFIED
            elif field_name == "underlying_price":
                unit_code, unit_status = None, UnitStatus.SOURCE_NATIVE_UNVERIFIED
            else:
                unit_code, unit_status = None, UnitStatus.SOURCE_NATIVE_UNVERIFIED
            metrics[metric_name] = _summary_metric(
                row,
                source_field=field_name,
                metric=metric_name,
                fetched_at=fetched_at,
                unit_code=unit_code,
                unit_status=unit_status,
            )

        if "creation_timestamp" in row:
            creation_timestamp = row["creation_timestamp"]
            _epoch_milliseconds(creation_timestamp, "creation_timestamp")
            metrics["source_creation_timestamp_ms"] = OptionMetricValue(
                metric="source_creation_timestamp_ms",
                value=Decimal(creation_timestamp),
                source=_SOURCE,
                exchange=_EXCHANGE,
                source_field="creation_timestamp",
                source_method_or_channel="public/get_book_summary_by_currency",
                fetched_at=fetched_at,
                timestamp_semantics=TimestampSemantics.UNVERIFIED,
                unit_code="unix_ms",
                unit_status=UnitStatus.VERIFIED,
                provenance=Provenance.SOURCE_PROVIDED,
                quality_reason="SOURCE_ATTRIBUTE_NOT_EVENT_TIME",
            )

        required_summary_metrics = ("volume_24h", "open_interest")
        status = (
            DataStatus.AVAILABLE
            if all(metrics[name].status is DataStatus.AVAILABLE for name in required_summary_metrics)
            else DataStatus.PARTIAL
        )
        result.append(
            OptionMarketObservation(
                exchange=_EXCHANGE,
                source=_SOURCE,
                symbol=symbol,
                underlying=currency,
                observation_kind=ObservationKind.REST_CHAIN_SUMMARY,
                metrics=metrics,
                exchange_timestamp=None,
                fetched_at=fetched_at,
                received_at=None,
                processed_at=processed_at,
                status=status,
                price_index=instrument.price_index,
                underlying_index=summary_index,
                quote_currency=summary_quote,
            )
        )
    return tuple(result)
