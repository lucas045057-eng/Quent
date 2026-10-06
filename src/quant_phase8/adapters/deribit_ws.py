"""Public Deribit WebSocket contracts and bounded session runtime for Phase 8."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from itertools import count
from types import MappingProxyType
from typing import Any

import aiohttp
from aiohttp import WSMsgType

from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    InstrumentEventType,
    ObservationKind,
    OptionInstrument,
    OptionInstrumentEvent,
    OptionMarketObservation,
    OptionMetricValue,
    OptionType,
    Provenance,
    TimestampSemantics,
    UnitStatus,
    decimal_from_json,
    loads_decimal_json,
)


WS_URL = "wss://www.deribit.com/ws/api/v2"
_SOURCE = "deribit"
_EXCHANGE = "DERIBIT"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LIFECYCLE_STATES = frozenset(
    {"open", "settlement", "delivered", "inactive", "locked", "halted", "archivized"}
)
_TICKER_FIELDS = {
    "mark_price": ("data.mark_price", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "mark_iv": ("data.mark_iv", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "bid_iv": ("data.bid_iv", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "ask_iv": ("data.ask_iv", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "underlying_price": (
        "data.underlying_price", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None
    ),
    "open_interest": ("data.open_interest", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "volume_24h": ("data.stats.volume", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "delta": ("data.greeks.delta", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "gamma": ("data.greeks.gamma", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "theta": ("data.greeks.theta", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "vega": ("data.greeks.vega", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
    "rho": ("data.greeks.rho", UnitStatus.SOURCE_NATIVE_UNVERIFIED, None),
}


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required by the WebSocket contract")
    return value


def _epoch_ms(value: Any, name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} timestamp must be an integer in Unix milliseconds")
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError as exc:
        raise ValueError(f"{name} timestamp is outside the supported range") from exc


def _subscription_data(payload: Any, channel_prefix: str) -> tuple[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
        raise ValueError("WebSocket notification JSON-RPC contract mismatch")
    if payload.get("method") != "subscription":
        raise ValueError("WebSocket message is not a subscription notification")
    params = payload.get("params")
    if not isinstance(params, Mapping):
        raise ValueError("WebSocket notification params must be an object")
    channel = _required_text(params.get("channel"), "channel")
    if not channel.startswith(channel_prefix):
        raise ValueError("WebSocket channel does not match the expected contract")
    if "data" not in params:
        raise ValueError("WebSocket notification is missing data")
    return channel, params["data"]


def _rpc_envelope(payload: Mapping[str, Any], request_id: int) -> tuple[str, Any]:
    if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
        raise ValueError("WebSocket JSON-RPC response id/version mismatch")
    if isinstance(payload.get("id"), bool):
        raise ValueError("WebSocket JSON-RPC response id is invalid")
    if "error" in payload:
        raise ValueError("WebSocket public subscription returned an error")
    if "result" not in payload:
        raise ValueError("WebSocket JSON-RPC response is missing result")
    return "result", payload["result"]


def _metric(
    name: str,
    raw_value: Any,
    *,
    source_field: str,
    channel: str,
    source_time: datetime,
    received_at: datetime,
    processed_at: datetime,
    unit_status: UnitStatus,
    unit_code: str | None = None,
    absent: bool = False,
) -> OptionMetricValue:
    if absent:
        value, status, reason = None, DataStatus.NOT_AVAILABLE, "MISSING_FIELD"
    elif raw_value is None:
        value, status, reason = None, DataStatus.NOT_AVAILABLE, "EXPLICIT_NULL"
    else:
        try:
            value = decimal_from_json(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source_field} must be a finite JSON number") from exc
        status, reason = DataStatus.AVAILABLE, None
    return OptionMetricValue(
        metric=name,
        value=value,
        source=_SOURCE,
        exchange=_EXCHANGE,
        source_field=source_field,
        source_method_or_channel=channel,
        exchange_timestamp=source_time,
        field_last_updated_at=source_time,
        received_at=received_at,
        processed_at=processed_at,
        timestamp_semantics=TimestampSemantics.VERIFIED,
        unit_code=unit_code,
        unit_status=unit_status,
        status=status,
        provenance=Provenance.SOURCE_PROVIDED,
        quality_reason=reason,
    )


def _instrument_catalog(
    instruments: Sequence[OptionInstrument] | Mapping[str, OptionInstrument],
) -> dict[str, OptionInstrument]:
    values = instruments.values() if isinstance(instruments, Mapping) else instruments
    result: dict[str, OptionInstrument] = {}
    for instrument in values:
        if not isinstance(instrument, OptionInstrument):
            raise TypeError("instrument catalog must contain canonical OptionInstrument objects")
        if instrument.symbol in result:
            raise ValueError("instrument catalog contains duplicate symbols")
        result[instrument.symbol] = instrument
    return result


def build_lifecycle_channels(currencies: Sequence[str] = ("BTC", "ETH")) -> tuple[str, ...]:
    if len(set(currencies)) != len(currencies) or any(c not in {"BTC", "ETH"} for c in currencies):
        raise ValueError("lifecycle currencies must be unique BTC/ETH values")
    return tuple(
        channel
        for currency in currencies
        for channel in (
            f"instrument.creation.option.{currency}",
            f"instrument.state.option.{currency}",
        )
    )


def derive_markprice_channels(
    instruments: Sequence[OptionInstrument],
    supported_index_names: set[str] | frozenset[str],
    *,
    max_channels: int,
) -> tuple[str, ...]:
    if isinstance(max_channels, bool) or not isinstance(max_channels, int) or max_channels <= 0:
        raise ValueError("markprice channel cap must be positive")
    if not isinstance(supported_index_names, (set, frozenset)) or not supported_index_names:
        raise ValueError("official supported index names are required")
    active = [
        item for item in instruments
        if item.status is DataStatus.AVAILABLE and item.is_active and item.instrument_state == "open"
    ]
    indexes = sorted({item.price_index for item in active})
    unsupported = set(indexes).difference(supported_index_names)
    if unsupported:
        raise ValueError("instrument price_index is not in the supported index set")
    if len(indexes) > max_channels:
        raise ValueError("derived markprice channels exceed configured cap")
    return tuple(f"markprice.options.{name}" for name in indexes)


def derive_ticker_channels(symbols: Sequence[str], *, max_subscriptions: int) -> tuple[str, ...]:
    if isinstance(max_subscriptions, bool) or not isinstance(max_subscriptions, int) or max_subscriptions <= 0:
        raise ValueError("ticker subscription cap must be positive")
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        raise ValueError("ticker symbols must be non-empty strings")
    if len(set(symbols)) != len(symbols):
        raise ValueError("ticker symbols must be unique")
    if len(symbols) > max_subscriptions:
        raise ValueError("ticker channels exceed configured cap")
    return tuple(f"incremental_ticker.{symbol}" for symbol in sorted(symbols))


def build_subscribe_request(channels: Sequence[str], *, request_id: int) -> dict[str, Any]:
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
        raise ValueError("request id must be a positive integer")
    if not channels or any(not isinstance(channel, str) or not channel for channel in channels):
        raise ValueError("subscribe channels must be a non-empty string list")
    if len(set(channels)) != len(channels):
        raise ValueError("subscribe channels must be unique")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "public/subscribe",
        "params": {"channels": list(channels)},
    }


def parse_instrument_creation_notification(
    payload: Any,
    *,
    supported_index_names: set[str] | frozenset[str],
    received_at: datetime,
    processed_at: datetime,
) -> tuple[OptionInstrument, OptionInstrumentEvent]:
    channel, data = _subscription_data(payload, "instrument.creation.option.")
    currency = channel.removeprefix("instrument.creation.option.")
    if currency not in {"BTC", "ETH"} or not isinstance(data, Mapping):
        raise ValueError("instrument creation notification schema mismatch")
    if data.get("kind") != "option" or data.get("base_currency") != currency:
        raise ValueError("instrument creation currency/kind mismatch")
    if data.get("settlement_currency") != currency:
        raise ValueError("instrument creation settlement currency mismatch")
    option_type = data.get("option_type")
    if option_type not in {OptionType.CALL.value, OptionType.PUT.value}:
        raise ValueError("instrument creation option_type is invalid")
    state = _required_text(data.get("state"), "state")
    if state not in _LIFECYCLE_STATES:
        raise ValueError("instrument creation state is unknown")
    if not isinstance(data.get("is_active"), bool):
        raise ValueError("instrument creation is_active must be boolean")
    symbol = _required_text(data.get("instrument_name"), "instrument_name")
    price_index = _required_text(data.get("price_index"), "price_index")
    if price_index not in supported_index_names:
        raise ValueError("instrument creation price_index is unsupported")
    try:
        strike = decimal_from_json(data.get("strike"))
    except (TypeError, ValueError) as exc:
        raise ValueError("instrument creation strike is invalid") from exc
    if strike <= 0:
        raise ValueError("instrument creation strike must be positive")
    expires_at = _epoch_ms(data.get("expiration_timestamp"), "expiration_timestamp")
    created_at = _epoch_ms(data.get("creation_timestamp"), "creation_timestamp")
    exchange_timestamp = _epoch_ms(data.get("timestamp"), "timestamp")
    provider_id = data.get("instrument_id")
    if provider_id is not None and (
        isinstance(provider_id, bool) or not isinstance(provider_id, int) or provider_id <= 0
    ):
        raise ValueError("instrument_id must be a positive integer")
    instrument = OptionInstrument(
        exchange=_EXCHANGE,
        source=_SOURCE,
        symbol=symbol,
        underlying=currency,
        option_type=option_type,
        strike=strike,
        expires_at=expires_at,
        instrument_created_at=created_at,
        instrument_state=state,
        is_active=data["is_active"],
        price_index=price_index,
        base_currency=currency,
        quote_currency=_required_text(data.get("quote_currency"), "quote_currency"),
        settlement_currency=currency,
        exchange_timestamp=exchange_timestamp,
        fetched_at=received_at,
        processed_at=processed_at,
        status=DataStatus.AVAILABLE,
        provider_instrument_id=provider_id,
        source_field="params.data",
        timestamp_semantics=TimestampSemantics.VERIFIED,
    )
    event = OptionInstrumentEvent(
        exchange=_EXCHANGE,
        source=_SOURCE,
        symbol=symbol,
        event_type=InstrumentEventType.CREATION,
        instrument_state=state,
        exchange_timestamp=exchange_timestamp,
        received_at=received_at,
        processed_at=processed_at,
        status=DataStatus.AVAILABLE,
        event_identity=_event_identity(payload),
    )
    return instrument, event


def parse_instrument_state_notification(
    payload: Any, *, received_at: datetime, processed_at: datetime
) -> OptionInstrumentEvent:
    channel, data = _subscription_data(payload, "instrument.state.option.")
    currency = channel.removeprefix("instrument.state.option.")
    if currency not in {"BTC", "ETH"} or not isinstance(data, Mapping):
        raise ValueError("instrument state notification schema mismatch")
    symbol = _required_text(data.get("instrument_name"), "instrument_name")
    if not symbol.startswith(f"{currency}-"):
        raise ValueError("instrument state currency does not match channel")
    state = _required_text(data.get("state"), "state")
    if state not in _LIFECYCLE_STATES:
        raise ValueError("instrument state is unknown")
    return OptionInstrumentEvent(
        exchange=_EXCHANGE,
        source=_SOURCE,
        symbol=symbol,
        event_type=InstrumentEventType.STATE,
        instrument_state=state,
        exchange_timestamp=_epoch_ms(data.get("timestamp"), "timestamp"),
        received_at=received_at,
        processed_at=processed_at,
        status=DataStatus.AVAILABLE,
        event_identity=_event_identity(payload),
    )


def _event_identity(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _instrument_identity(
    catalog: Mapping[str, OptionInstrument], symbol: str, index_name: str
) -> OptionInstrument:
    instrument = catalog.get(symbol)
    if instrument is None:
        raise ValueError("WebSocket message contains an unknown instrument")
    if instrument.price_index != index_name:
        raise ValueError("WebSocket channel index does not match instrument price_index")
    return instrument


def parse_markprice_notification(
    payload: Any,
    *,
    instrument_catalog: Mapping[str, OptionInstrument] | Sequence[OptionInstrument],
    received_at: datetime,
    processed_at: datetime,
    is_seed: bool,
) -> tuple[OptionMarketObservation, ...]:
    channel, data = _subscription_data(payload, "markprice.options.")
    index_name = channel.removeprefix("markprice.options.")
    if not index_name or not isinstance(data, list):
        raise ValueError("markprice notification schema mismatch")
    catalog = _instrument_catalog(instrument_catalog)
    observations: list[OptionMarketObservation] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, Mapping):
            raise ValueError("markprice data rows must be objects")
        symbol = _required_text(entry.get("instrument_name"), "instrument_name")
        if symbol in seen:
            raise ValueError("markprice notification contains duplicate instrument")
        seen.add(symbol)
        instrument = _instrument_identity(catalog, symbol, index_name)
        source_time = _epoch_ms(entry.get("timestamp"), "timestamp")
        metric_values: dict[str, OptionMetricValue] = {}
        for field_name, metric_name, unit_code, unit_status in (
            ("mark_price", "mark_price", instrument.quote_currency, UnitStatus.VERIFIED),
            ("iv", "iv", None, UnitStatus.SOURCE_NATIVE_UNVERIFIED),
        ):
            if is_seed or field_name in entry:
                metric_values[metric_name] = _metric(
                    metric_name,
                    entry.get(field_name),
                    source_field=f"params.data[].{field_name}",
                    channel=channel,
                    source_time=source_time,
                    received_at=received_at,
                    processed_at=processed_at,
                    unit_status=unit_status,
                    unit_code=unit_code,
                    absent=field_name not in entry,
                )
        status = (
            DataStatus.AVAILABLE
            if metric_values and all(item.status is DataStatus.AVAILABLE for item in metric_values.values())
            else DataStatus.PARTIAL
        )
        observations.append(
            OptionMarketObservation(
                exchange=_EXCHANGE,
                source=_SOURCE,
                symbol=symbol,
                underlying=instrument.underlying,
                observation_kind=(
                    ObservationKind.WS_MARKPRICE_SNAPSHOT if is_seed else ObservationKind.WS_MARKPRICE_CHANGE
                ),
                metrics=metric_values,
                exchange_timestamp=source_time,
                fetched_at=None,
                received_at=received_at,
                processed_at=processed_at,
                status=status,
                price_index=index_name,
                quote_currency=instrument.quote_currency,
            )
        )
    return tuple(observations)


def _newer_metric(incoming: OptionMetricValue, current: OptionMetricValue | None) -> bool:
    if current is None or current.exchange_timestamp is None:
        return True
    if incoming.exchange_timestamp is None:
        return False
    return incoming.exchange_timestamp > current.exchange_timestamp


def merge_markprice_state(
    previous: Mapping[str, OptionMarketObservation],
    updates: Sequence[OptionMarketObservation],
    *,
    is_seed: bool,
) -> dict[str, OptionMarketObservation]:
    """Merge one index's markprice state; seed replaces, changes are sparse."""
    state = {} if is_seed else dict(previous)
    for update in updates:
        current = state.get(update.symbol)
        if current is None:
            state[update.symbol] = update
            continue
        metrics = dict(current.metrics)
        for name, incoming in update.metrics.items():
            if _newer_metric(incoming, metrics.get(name)):
                metrics[name] = incoming
        latest_time = max(
            (metric.exchange_timestamp for metric in metrics.values() if metric.exchange_timestamp is not None),
            default=update.exchange_timestamp,
        )
        status = (
            DataStatus.AVAILABLE
            if metrics and all(metric.status is DataStatus.AVAILABLE for metric in metrics.values())
            else DataStatus.PARTIAL
        )
        state[update.symbol] = OptionMarketObservation(
            exchange=update.exchange,
            source=update.source,
            symbol=update.symbol,
            underlying=update.underlying,
            observation_kind=update.observation_kind,
            metrics=metrics,
            exchange_timestamp=latest_time,
            fetched_at=None,
            received_at=update.received_at,
            processed_at=update.processed_at,
            status=status,
            price_index=update.price_index,
            underlying_index=update.underlying_index,
            quote_currency=update.quote_currency,
            observation_id=update.observation_id,
            raw_reference=update.raw_reference,
        )
    return state


@dataclass(frozen=True, slots=True)
class ParsedTickerUpdate:
    symbol: str
    is_snapshot: bool
    metrics: Mapping[str, OptionMetricValue]
    exchange_timestamp: datetime
    received_at: datetime
    processed_at: datetime


def _nested(data: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    field_path = path.split(".")
    node: Any = data
    for part in field_path:
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def parse_incremental_ticker_notification(
    payload: Any, *, received_at: datetime, processed_at: datetime
) -> ParsedTickerUpdate:
    channel, data = _subscription_data(payload, "incremental_ticker.")
    symbol = channel.removeprefix("incremental_ticker.")
    if not isinstance(data, Mapping) or data.get("instrument_name") != symbol:
        raise ValueError("incremental ticker instrument identity mismatch")
    update_type = data.get("type")
    if update_type not in {"snapshot", "change"}:
        raise ValueError("incremental ticker type must be snapshot or change")
    source_time = _epoch_ms(data.get("timestamp"), "timestamp")
    metrics: dict[str, OptionMetricValue] = {}
    for metric_name, (source_field, unit_status, unit_code) in _TICKER_FIELDS.items():
        path = source_field.removeprefix("data.")
        present, value = _nested(data, path)
        if update_type == "snapshot" or present:
            metrics[metric_name] = _metric(
                metric_name,
                value,
                source_field=source_field,
                channel=channel,
                source_time=source_time,
                received_at=received_at,
                processed_at=processed_at,
                unit_status=unit_status,
                unit_code=unit_code,
                absent=not present,
            )
    return ParsedTickerUpdate(
        symbol=symbol,
        is_snapshot=update_type == "snapshot",
        metrics=metrics,
        exchange_timestamp=source_time,
        received_at=received_at,
        processed_at=processed_at,
    )


def apply_incremental_ticker(
    previous: Mapping[str, OptionMetricValue], update: ParsedTickerUpdate
) -> dict[str, OptionMetricValue]:
    state = {} if update.is_snapshot else dict(previous)
    for name, incoming in update.metrics.items():
        if _newer_metric(incoming, state.get(name)):
            state[name] = incoming
    return state


class WebSocketQueueOverflow(RuntimeError):
    """The local receive queue exceeded its configured memory budget."""


class BoundedPayloadQueue:
    """A non-blocking bounded queue that accounts for queued payload bytes."""

    def __init__(self, *, max_messages: int, max_bytes: int) -> None:
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages <= 0:
            raise ValueError("max_messages must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_messages = max_messages
        self._max_bytes = max_bytes
        self._queue: asyncio.Queue[tuple[str | bytes, int]] = asyncio.Queue(maxsize=max_messages)
        self._queued_bytes = 0

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    async def put(self, payload: str | bytes) -> None:
        if not isinstance(payload, (str, bytes)):
            raise TypeError("WebSocket payload must be text or bytes")
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        if size > self._max_bytes or self._queue.full() or self._queued_bytes + size > self._max_bytes:
            raise WebSocketQueueOverflow("WebSocket receive queue exceeded configured capacity")
        self._queued_bytes += size
        try:
            self._queue.put_nowait((payload, size))
        except asyncio.QueueFull:
            self._queued_bytes -= size
            raise WebSocketQueueOverflow("WebSocket receive queue exceeded configured capacity") from None

    async def get(self) -> str | bytes:
        payload, size = await self._queue.get()
        self._queued_bytes -= size
        self._queue.task_done()
        return payload


def reconnect_delay(attempt: int, maximum_seconds: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("reconnect attempt must be a positive integer")
    if isinstance(maximum_seconds, bool) or not isinstance(maximum_seconds, int) or maximum_seconds <= 0:
        raise ValueError("maximum reconnect delay must be a positive integer")
    return min(2 ** min(attempt - 1, 30), maximum_seconds)


class DeribitWebSocketError(RuntimeError):
    """Sanitized public WebSocket transport or contract error."""


class DeribitPublicWebSocketClient:
    """Bounded public-only Deribit WebSocket client for Phase 8 option data."""

    def __init__(
        self,
        settings: Phase8Settings | None = None,
        *,
        instrument_catalog: Sequence[OptionInstrument] | Mapping[str, OptionInstrument],
        supported_index_names: set[str] | frozenset[str],
        ticker_symbols: Sequence[str],
        on_invalidate: Callable[[str], Any] | None = None,
        on_lifecycle: Callable[[OptionInstrument | None, OptionInstrumentEvent], Any] | None = None,
        on_markprice_batch: Callable[[Sequence[OptionMarketObservation]], Any] | None = None,
        on_ticker: Callable[[str, Mapping[str, OptionMetricValue]], Any] | None = None,
        defer_market_subscriptions: bool = False,
        _url: str = WS_URL,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        _monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or Phase8Settings.from_env({})
        self._catalog = _instrument_catalog(instrument_catalog)
        self._supported_indexes = frozenset(supported_index_names)
        self._ticker_symbols = tuple(ticker_symbols)
        self._url = _url
        self._sleep = _sleep
        self._monotonic = _monotonic
        self._on_invalidate = on_invalidate
        self._on_lifecycle = on_lifecycle
        self._on_markprice_batch = on_markprice_batch
        self._on_ticker = on_ticker
        self._market_subscriptions_configured = not defer_market_subscriptions
        self._deferred_lifecycle: list[Mapping[str, Any]] = []
        self._deferred_lifecycle_bytes = 0
        self._markprice_channels = (
            derive_markprice_channels(
                tuple(self._catalog.values()), self._supported_indexes,
                max_channels=self.settings.max_markprice_channels_total,
            )
            if self._market_subscriptions_configured or self._supported_indexes
            else ()
        )
        self._ticker_channels = derive_ticker_channels(
            self._ticker_symbols,
            max_subscriptions=self.settings.max_ticker_subscriptions_total,
        ) if self._ticker_symbols else ()
        self._validate_catalog_caps()
        self._ticker_counts_by_currency: dict[str, int] = {"BTC": 0, "ETH": 0}
        for symbol in self._ticker_symbols:
            instrument = self._catalog.get(symbol)
            if instrument is None:
                raise ValueError("ticker selection contains an unknown instrument")
            self._ticker_counts_by_currency[instrument.underlying] += 1
        if any(
            value > self.settings.max_ticker_instruments_per_currency
            for value in self._ticker_counts_by_currency.values()
        ):
            raise ValueError("ticker selection exceeds per-underlying subscription cap")

        self._markprice_state: dict[str, dict[str, OptionMarketObservation]] = {}
        self._ticker_state: dict[str, dict[str, OptionMetricValue]] = {}
        self._ready_markprice_indexes: set[str] = set()
        self._ready_ticker_symbols: set[str] = set()
        self._pending_requests: set[int] = set()
        self._request_ids = count(1)
        self._last_subscribe_at: float | None = None
        self._active_socket: aiohttp.ClientWebSocketResponse | None = None
        self._stopping = False
        self._main_task: asyncio.Task[None] | None = None
        self._child_tasks: set[asyncio.Task[Any]] = set()
        self._initial_subscriptions_sent = asyncio.Event()
        self._ready_changed = asyncio.Event()
        self._reconnect_attempts = 0
        self.last_error: str | None = None

    def _validate_catalog_caps(self) -> None:
        counts = {"BTC": 0, "ETH": 0}
        for instrument in self._catalog.values():
            counts[instrument.underlying] += 1
        if any(
            value > self.settings.max_full_chain_records_per_underlying
            for value in counts.values()
        ) or sum(counts.values()) > self.settings.max_full_chain_records_total:
            raise ValueError("instrument catalog exceeds configured full-chain record cap")

    @property
    def ready(self) -> bool:
        return (
            self._market_subscriptions_configured
            and
            set(self._markprice_state) == self._ready_markprice_indexes
            and set(self._ticker_symbols) == self._ready_ticker_symbols
            and self._ready_markprice_indexes == {
                channel.removeprefix("markprice.options.") for channel in self._markprice_channels
            }
        )

    @property
    def ready_markprice_indexes(self) -> set[str]:
        return set(self._ready_markprice_indexes)

    @property
    def ready_ticker_symbols(self) -> set[str]:
        return set(self._ready_ticker_symbols)

    @property
    def markprice_state(self) -> Mapping[str, Mapping[str, OptionMarketObservation]]:
        return MappingProxyType({
            index: MappingProxyType(rows) for index, rows in self._markprice_state.items()
        })

    @property
    def ticker_state(self) -> Mapping[str, Mapping[str, OptionMetricValue]]:
        return MappingProxyType({
            symbol: MappingProxyType(metrics) for symbol, metrics in self._ticker_state.items()
        })

    @property
    def owned_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        tasks = set(self._child_tasks)
        if self._main_task is not None and not self._main_task.done():
            tasks.add(self._main_task)
        return tuple(task for task in tasks if not task.done())

    async def start(self) -> None:
        if self._main_task is not None and not self._main_task.done():
            return
        self._main_task = None
        self._stopping = False
        self._initial_subscriptions_sent.clear()
        await self._invalidate("WEBSOCKET_STARTUP")
        self._main_task = asyncio.create_task(self._run(), name="phase8-deribit-public-ws")
        signal = asyncio.create_task(self._initial_subscriptions_sent.wait())
        try:
            done, _ = await asyncio.wait(
                (signal, self._main_task), return_when=asyncio.FIRST_COMPLETED
            )
            if signal not in done:
                await self._main_task
                raise DeribitWebSocketError("Deribit public WebSocket stopped before initial subscriptions")
        finally:
            if not signal.done():
                signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)

    async def wait_ready(self, *, timeout: float) -> None:
        if timeout <= 0:
            raise ValueError("readiness timeout must be positive")
        async def wait() -> None:
            while not self.ready:
                if self._main_task is not None and self._main_task.done():
                    await self._main_task
                    raise DeribitWebSocketError("Deribit public WebSocket stopped before readiness")
                self._ready_changed.clear()
                if self.ready:
                    return
                await self._ready_changed.wait()
        try:
            await asyncio.wait_for(wait(), timeout=timeout)
        except TimeoutError:
            raise DeribitWebSocketError("Deribit public WebSocket readiness deadline expired") from None

    async def configure_market_subscriptions(
        self,
        instrument_catalog: Sequence[OptionInstrument] | Mapping[str, OptionInstrument],
        supported_index_names: set[str] | frozenset[str],
        *,
        ticker_symbols: Sequence[str],
        force_reseed: bool = False,
    ) -> None:
        """Install validated market subscriptions after lifecycle-first startup.

        Reconfiguration is additive at the wire boundary: obsolete channels are
        unsubscribed and new channels subscribed, while unchanged channel state
        remains available. A reconnect always resubscribes the configured set.
        """
        catalog = _instrument_catalog(instrument_catalog)
        indexes = frozenset(supported_index_names)
        if not indexes or any(not isinstance(name, str) or not name.strip() for name in indexes):
            raise ValueError("supported index names are required before market subscriptions")
        ticker_values = tuple(ticker_symbols)
        mark_channels = derive_markprice_channels(
            tuple(catalog.values()), indexes,
            max_channels=self.settings.max_markprice_channels_total,
        )
        ticker_channels = derive_ticker_channels(
            ticker_values,
            max_subscriptions=self.settings.max_ticker_subscriptions_total,
        ) if ticker_values else ()
        counts = {"BTC": 0, "ETH": 0}
        for symbol in ticker_values:
            instrument = catalog.get(symbol)
            if instrument is None:
                raise ValueError("ticker selection contains an unknown instrument")
            counts[instrument.underlying] += 1
        if any(count > self.settings.max_ticker_instruments_per_currency for count in counts.values()):
            raise ValueError("ticker selection exceeds per-underlying subscription cap")
        self._validate_catalog_caps_for(catalog)

        previous_mark_channels = set(self._markprice_channels)
        previous_ticker_channels = set(self._ticker_channels)
        self._catalog = catalog
        self._supported_indexes = indexes
        self._ticker_symbols = ticker_values
        self._ticker_counts_by_currency = counts
        self._markprice_channels = mark_channels
        self._ticker_channels = ticker_channels
        self._market_subscriptions_configured = True

        if force_reseed:
            self._markprice_state.clear()
            self._ticker_state.clear()
            self._ready_markprice_indexes.clear()
            self._ready_ticker_symbols.clear()

        retained_symbols = set(catalog)
        for index_name, rows in tuple(self._markprice_state.items()):
            if f"markprice.options.{index_name}" not in mark_channels:
                self._markprice_state.pop(index_name, None)
                self._ready_markprice_indexes.discard(index_name)
            else:
                self._markprice_state[index_name] = {
                    symbol: observation for symbol, observation in rows.items()
                    if symbol in retained_symbols
                }
        for symbol in set(self._ticker_state) - set(ticker_values):
            self._ticker_state.pop(symbol, None)
            self._ready_ticker_symbols.discard(symbol)

        pending = tuple(self._deferred_lifecycle)
        self._deferred_lifecycle.clear()
        self._deferred_lifecycle_bytes = 0
        for payload in pending:
            await self._handle_notification(payload)

        socket = self._active_socket
        if socket is not None and not socket.closed:
            next_mark_channels = set(mark_channels)
            next_ticker_channels = set(ticker_channels)
            old_channels = previous_mark_channels | previous_ticker_channels
            next_channels = next_mark_channels | next_ticker_channels
            removed = sorted((old_channels - next_channels) | (old_channels if force_reseed else set()))
            added = sorted((next_channels - old_channels) | (next_channels if force_reseed else set()))
            if removed:
                await self._send_subscription(socket, removed, method="public/unsubscribe")
            if added:
                await self._send_subscription(socket, added, method="public/subscribe")
        self._ready_changed.set()

    def _validate_catalog_caps_for(self, catalog: Mapping[str, OptionInstrument]) -> None:
        counts = {"BTC": 0, "ETH": 0}
        for instrument in catalog.values():
            counts[instrument.underlying] += 1
        if any(
            value > self.settings.max_full_chain_records_per_underlying
            for value in counts.values()
        ) or sum(counts.values()) > self.settings.max_full_chain_records_total:
            raise ValueError("instrument catalog exceeds configured full-chain record cap")

    async def close(self) -> None:
        if self._main_task is None:
            return
        self._stopping = True
        socket = self._active_socket
        if socket is not None and not socket.closed:
            await socket.close()
        task = self._main_task
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=self.settings.shutdown_timeout_seconds)
        except (asyncio.CancelledError, TimeoutError):
            pass
        finally:
            for child in tuple(self._child_tasks):
                if not child.done():
                    child.cancel()
            if self._child_tasks:
                await asyncio.gather(*self._child_tasks, return_exceptions=True)
            self._child_tasks.clear()
            self._main_task = None
            self._active_socket = None
            self._ready_changed.set()
            await self._invalidate("WEBSOCKET_STOPPED")

    async def _call(self, callback: Callable[..., Any] | None, *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if isinstance(result, Awaitable):
            await result

    async def _invalidate(self, reason: str) -> None:
        self._markprice_state.clear()
        self._ticker_state.clear()
        self._ready_markprice_indexes.clear()
        self._ready_ticker_symbols.clear()
        self._ready_changed.set()
        await self._call(self._on_invalidate, reason)

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._connection_cycle()
                if self._stopping:
                    return
                raise DeribitWebSocketError("Deribit public WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    return
                self.last_error = (
                    str(exc) if isinstance(exc, DeribitWebSocketError)
                    else f"Deribit public WebSocket connection or contract failure ({type(exc).__name__})"
                )
                await self._invalidate("WEBSOCKET_RECONNECT")
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self.settings.ws_max_reconnect_attempts:
                    return
                await self._sleep(
                    reconnect_delay(
                        self._reconnect_attempts,
                        self.settings.ws_reconnect_max_delay_seconds,
                    )
                )

    async def _connection_cycle(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=self.settings.rest_timeout_seconds,
            sock_connect=self.settings.rest_timeout_seconds,
        )
        async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as session:
            async with session.ws_connect(
                self._url,
                heartbeat=30.0,
                autoping=True,
                max_msg_size=self.settings.ws_max_message_bytes,
                receive_timeout=None,
            ) as socket:
                self._active_socket = socket
                queue = BoundedPayloadQueue(
                    max_messages=self.settings.ws_queue_max_messages,
                    max_bytes=self.settings.ws_max_queued_bytes,
                )
                self._pending_requests.clear()
                await self._subscribe(socket)
                self._initial_subscriptions_sent.set()
                reader = asyncio.create_task(self._read_frames(socket, queue), name="phase8-ws-reader")
                processor = asyncio.create_task(self._process_frames(queue), name="phase8-ws-processor")
                self._child_tasks.update((reader, processor))
                try:
                    done, pending = await asyncio.wait(
                        (reader, processor), return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        exception = task.exception()
                        if exception is not None:
                            raise exception
                    if not self._stopping:
                        raise DeribitWebSocketError("Deribit public WebSocket reader stopped")
                finally:
                    for task in (reader, processor):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(reader, processor, return_exceptions=True)
                    self._child_tasks.difference_update((reader, processor))
                    self._active_socket = None

    async def _subscribe(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        groups: list[list[str]] = [list(build_lifecycle_channels())]
        mark_channels = list(self._markprice_channels) if self._market_subscriptions_configured else []
        batch_size = self.settings.ws_subscribe_batch_size
        groups.extend(mark_channels[index:index + batch_size] for index in range(0, len(mark_channels), batch_size))
        ticker_channels = list(self._ticker_channels)
        groups.extend(
            ticker_channels[index:index + batch_size]
            for index in range(0, len(ticker_channels), batch_size)
        )
        for channels in groups:
            await self._send_subscription(socket, channels, method="public/subscribe")

    async def _send_subscription(
        self,
        socket: aiohttp.ClientWebSocketResponse,
        channels: Sequence[str],
        *,
        method: str,
    ) -> None:
        batch_size = self.settings.ws_subscribe_batch_size
        for start in range(0, len(channels), batch_size):
            await self._pace_subscription()
            request_id = next(self._request_ids)
            self._pending_requests.add(request_id)
            await socket.send_str(json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": {"channels": list(channels[start:start + batch_size])},
                },
                separators=(",", ":"),
            ))

    async def _pace_subscription(self) -> None:
        now = self._monotonic()
        if self._last_subscribe_at is not None:
            delay = self.settings.ws_subscribe_min_interval_seconds - (now - self._last_subscribe_at)
            if delay > 0:
                await self._sleep(delay)
        self._last_subscribe_at = self._monotonic()

    async def _read_frames(
        self, socket: aiohttp.ClientWebSocketResponse, queue: BoundedPayloadQueue
    ) -> None:
        async for message in socket:
            if message.type is WSMsgType.TEXT:
                if len(message.data.encode("utf-8")) > self.settings.ws_max_message_bytes:
                    raise WebSocketQueueOverflow("WebSocket frame exceeded configured byte limit")
                await queue.put(message.data)
            elif message.type is WSMsgType.BINARY:
                if len(message.data) > self.settings.ws_max_message_bytes:
                    raise WebSocketQueueOverflow("WebSocket frame exceeded configured byte limit")
                await queue.put(message.data)
            elif message.type is WSMsgType.ERROR:
                raise DeribitWebSocketError("Deribit public WebSocket transport failed")
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}:
                return

    async def _process_frames(self, queue: BoundedPayloadQueue) -> None:
        while not self._stopping:
            raw = await queue.get()
            try:
                payload = loads_decimal_json(raw)
            except (UnicodeDecodeError, TypeError, ValueError):
                raise DeribitWebSocketError("Deribit public WebSocket frame is not valid JSON") from None
            if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
                raise DeribitWebSocketError("Deribit public WebSocket JSON-RPC contract mismatch")
            if payload.get("method") == "subscription":
                await self._handle_notification(payload)
                continue
            request_id = payload.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id not in self._pending_requests:
                raise DeribitWebSocketError("Deribit public WebSocket response id mismatch")
            try:
                _rpc_envelope(payload, request_id)
            except ValueError as exc:
                raise DeribitWebSocketError(str(exc)) from None
            self._pending_requests.remove(request_id)

    async def _handle_notification(self, payload: Mapping[str, Any]) -> None:
        received_at = datetime.now(timezone.utc)
        processed_at = datetime.now(timezone.utc)
        params = payload.get("params")
        channel = params.get("channel") if isinstance(params, Mapping) else None
        if not isinstance(channel, str):
            raise DeribitWebSocketError("Deribit subscription channel is invalid")
        if channel.startswith("instrument.creation.option."):
            if not self._supported_indexes:
                self._defer_lifecycle(payload)
                return
            try:
                instrument, event = parse_instrument_creation_notification(
                    payload,
                    supported_index_names=self._supported_indexes,
                    received_at=received_at,
                    processed_at=processed_at,
                )
                if instrument.symbol not in self._catalog:
                    candidate = dict(self._catalog)
                    candidate[instrument.symbol] = instrument
                    self._check_candidate_catalog_caps(candidate)
                    self._catalog = candidate
                else:
                    self._catalog[instrument.symbol] = instrument
            except (KeyError, TypeError, ValueError) as exc:
                raise DeribitWebSocketError(f"Deribit lifecycle contract failure: {exc}") from None
            await self._call(self._on_lifecycle, instrument, event)
            return
        if channel.startswith("instrument.state.option."):
            if not self._supported_indexes:
                self._defer_lifecycle(payload)
                return
            try:
                event = parse_instrument_state_notification(
                    payload, received_at=received_at, processed_at=processed_at
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DeribitWebSocketError(f"Deribit lifecycle contract failure: {exc}") from None
            instrument = self._catalog.get(event.symbol)
            if instrument is not None:
                self._catalog[event.symbol] = replace(
                    instrument,
                    instrument_state=event.instrument_state,
                    is_active=event.instrument_state == "open",
                    exchange_timestamp=event.exchange_timestamp,
                    fetched_at=event.received_at,
                    processed_at=event.processed_at,
                )
            await self._call(self._on_lifecycle, instrument, event)
            return
        if channel.startswith("markprice.options."):
            index_name = channel.removeprefix("markprice.options.")
            if index_name not in self._supported_indexes or channel not in self._markprice_channels:
                raise DeribitWebSocketError("Deribit markprice channel is not in the validated subscription set")
            is_seed = index_name not in self._ready_markprice_indexes
            try:
                updates = parse_markprice_notification(
                    payload,
                    instrument_catalog=self._catalog,
                    received_at=received_at,
                    processed_at=processed_at,
                    is_seed=is_seed,
                )
                state = merge_markprice_state(
                    self._markprice_state.get(index_name, {}), updates, is_seed=is_seed
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DeribitWebSocketError(f"Deribit markprice contract failure: {exc}") from None
            self._markprice_state[index_name] = state
            if is_seed:
                self._ready_markprice_indexes.add(index_name)
            if self.ready:
                self._reconnect_attempts = 0
                self.last_error = None
            self._ready_changed.set()
            await self._call(self._on_markprice_batch, updates)
            return
        if channel.startswith("incremental_ticker."):
            symbol = channel.removeprefix("incremental_ticker.")
            if symbol not in self._ticker_symbols:
                raise DeribitWebSocketError("Deribit ticker channel is outside the bounded selection")
            try:
                update = parse_incremental_ticker_notification(
                    payload, received_at=received_at, processed_at=processed_at
                )
                state = apply_incremental_ticker(self._ticker_state.get(symbol, {}), update)
            except (KeyError, TypeError, ValueError) as exc:
                raise DeribitWebSocketError(f"Deribit ticker contract failure: {exc}") from None
            self._ticker_state[symbol] = state
            if update.is_snapshot:
                self._ready_ticker_symbols.add(symbol)
            if self.ready:
                self._reconnect_attempts = 0
                self.last_error = None
            self._ready_changed.set()
            await self._call(self._on_ticker, symbol, dict(state))
            return
        raise DeribitWebSocketError("Deribit sent an unsubscribed Phase 8 channel")

    def _check_candidate_catalog_caps(self, candidate: Mapping[str, OptionInstrument]) -> None:
        counts = {"BTC": 0, "ETH": 0}
        for instrument in candidate.values():
            counts[instrument.underlying] += 1
        if any(
            value > self.settings.max_full_chain_records_per_underlying
            for value in counts.values()
        ) or sum(counts.values()) > self.settings.max_full_chain_records_total:
            raise ValueError("lifecycle update exceeds configured full-chain record cap")

    def _defer_lifecycle(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True, default=str
        ).encode("utf-8")
        if (
            len(self._deferred_lifecycle) >= self.settings.ws_queue_max_messages
            or self._deferred_lifecycle_bytes + len(encoded) > self.settings.ws_max_queued_bytes
        ):
            raise DeribitWebSocketError("pre-catalog lifecycle buffer exceeded configured bounds")
        self._deferred_lifecycle.append(payload)
        self._deferred_lifecycle_bytes += len(encoded)
