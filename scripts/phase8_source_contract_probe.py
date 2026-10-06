"""One-shot, bounded public Deribit contract smoke for Phase 8."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
from typing import Any

import aiohttp
from aiohttp import WSMsgType

from quant_phase8.adapters.deribit_rest import (
    REST_BASE_URL as ADAPTER_REST_BASE_URL,
    parse_book_summary_response,
    parse_index_price_names_response,
    parse_instruments_response,
)
from quant_phase8.adapters.deribit_ws import (
    WS_URL as ADAPTER_WS_URL,
    apply_incremental_ticker,
    build_lifecycle_channels,
    build_subscribe_request,
    derive_markprice_channels,
    derive_ticker_channels,
    parse_incremental_ticker_notification,
    parse_instrument_creation_notification,
    parse_instrument_state_notification,
    parse_markprice_notification,
    merge_markprice_state,
)
from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    OptionInstrument,
    OptionMetricValue,
    loads_decimal_json,
)
from quant_phase8.universe import select_ticker_universe


REST_BASE_URL = ADAPTER_REST_BASE_URL
WS_URL = ADAPTER_WS_URL
WEBSOCKET_CONNECTION_LIMIT = 1
PUBLIC_REST_METHODS = (
    "public/get_index_price_names",
    "public/get_instruments",
    "public/get_book_summary_by_currency",
)
PROBE_MAX_REST_REQUESTS = 5
REST_MIN_INTERVAL_SECONDS = 1.25
REST_MAX_ATTEMPTS = 1
REST_RESPONSE_BYTES_LIMIT = 8 * 1024 * 1024
WS_MESSAGE_BYTES_LIMIT = 1024 * 1024
PROBE_DEADLINE_SECONDS = 75
REST_TIMEOUT_SECONDS = 8
WS_ACK_TIMEOUT_SECONDS = 5
WS_INITIAL_DATA_TIMEOUT_SECONDS = 12
WS_SPARSE_OBSERVATION_WINDOW_SECONDS = 5


class ProbeFailure(RuntimeError):
    """Sanitized fail-closed result; never carries a response body or header."""

    def __init__(self, record: Mapping[str, Any]):
        self.record = dict(record)
        super().__init__("bounded public source probe failed")


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    lifecycle_channels: tuple[str, ...]
    markprice_channels: tuple[str, ...]
    ticker_channels: tuple[str, ...]
    selected_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TickerMergeEvent:
    symbol: str
    is_snapshot: bool
    merge_verified: bool
    omitted_field_count: int
    updated_field_count: int


@dataclass(slots=True)
class RestRequestBudget:
    max_requests: int = PROBE_MAX_REST_REQUESTS
    min_interval_seconds: float = REST_MIN_INTERVAL_SECONDS
    calls: int = 0
    last_started: float | None = None
    last_completed: float | None = None
    intervals_ms: list[float] = field(default_factory=list)

    async def begin(self) -> float:
        if self.calls >= self.max_requests:
            raise ValueError("REST probe request budget exhausted")
        if self.last_completed is not None:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_completed
                if elapsed >= self.min_interval_seconds:
                    break
                await asyncio.sleep(self.min_interval_seconds - elapsed)
        if self.last_started is not None:
            self.intervals_ms.append(round((time.monotonic() - self.last_started) * 1000, 3))
        self.last_started = time.monotonic()
        self.calls += 1
        return self.last_started

    def complete(self) -> None:
        self.last_completed = time.monotonic()


@dataclass(frozen=True, slots=True)
class RestCallResult:
    payload: Mapping[str, Any]
    record: Mapping[str, Any]


def build_subscription_plan(
    catalog: Sequence[OptionInstrument],
    supported_index_names: set[str] | frozenset[str],
    selected_by_underlying: Mapping[str, Sequence[str]],
) -> SubscriptionPlan:
    """Derive full-chain channels from validated metadata and cap ticker to one per asset."""
    if set(selected_by_underlying).difference({"BTC", "ETH"}):
        raise ValueError("ticker selection contains an unsupported underlying")
    by_symbol = {item.symbol: item for item in catalog}
    if len(by_symbol) != len(catalog):
        raise ValueError("instrument catalog contains duplicate symbols")
    selected: list[str] = []
    for underlying in ("BTC", "ETH"):
        symbols = tuple(selected_by_underlying.get(underlying, ()))
        if len(symbols) > 1:
            raise ValueError("public smoke permits at most one ticker per underlying")
        if len(set(symbols)) != len(symbols):
            raise ValueError("ticker selection contains duplicate symbols")
        for symbol in symbols:
            instrument = by_symbol.get(symbol)
            if (
                instrument is None
                or instrument.underlying != underlying
                or instrument.status is not DataStatus.AVAILABLE
                or not instrument.is_active
                or instrument.instrument_state != "open"
            ):
                raise ValueError("ticker selection is not a validated eligible instrument")
            selected.append(symbol)
    return SubscriptionPlan(
        lifecycle_channels=build_lifecycle_channels(),
        markprice_channels=derive_markprice_channels(
            catalog, supported_index_names, max_channels=4
        ),
        ticker_channels=derive_ticker_channels(selected, max_subscriptions=2) if selected else (),
        selected_symbols=tuple(selected),
    )


def validate_subscription_ack(
    payload: Any, *, request_id: int, expected_channels: Sequence[str]
) -> None:
    if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
        raise ValueError("subscription acknowledgement JSON-RPC contract mismatch")
    response_id = payload.get("id")
    if isinstance(response_id, bool) or response_id != request_id:
        raise ValueError("subscription acknowledgement id mismatch")
    if "error" in payload or not isinstance(payload.get("result"), list):
        raise ValueError("public subscription acknowledgement returned an error")
    result = payload["result"]
    if any(not isinstance(item, str) for item in result) or len(result) != len(set(result)):
        raise ValueError("subscription acknowledgement channel schema mismatch")
    if len(result) != len(expected_channels) or set(result) != set(expected_channels):
        raise ValueError("subscription acknowledgement channel set mismatch")


def apply_ticker_message(
    payload: Any,
    state_by_symbol: Mapping[str, Mapping[str, OptionMetricValue]],
    *,
    selected_symbols: set[str] | frozenset[str],
    received_at: datetime,
    processed_at: datetime,
) -> tuple[dict[str, dict[str, OptionMetricValue]], TickerMergeEvent]:
    update = parse_incremental_ticker_notification(
        payload, received_at=received_at, processed_at=processed_at
    )
    if update.symbol not in selected_symbols:
        raise ValueError("ticker message is outside the bounded smoke selection")
    previous = state_by_symbol.get(update.symbol, {})
    if not update.is_snapshot and not previous:
        raise ValueError("ticker change arrived before its initial snapshot")
    merged = apply_incremental_ticker(previous, update)
    omitted = set(previous).difference(update.metrics)
    preserved = all(merged.get(name) == previous[name] for name in omitted)
    next_state = {symbol: dict(metrics) for symbol, metrics in state_by_symbol.items()}
    next_state[update.symbol] = merged
    return next_state, TickerMergeEvent(
        symbol=update.symbol,
        is_snapshot=update.is_snapshot,
        merge_verified=preserved,
        omitted_field_count=len(omitted),
        updated_field_count=len(update.metrics),
    )


def decode_bounded_ws_message(raw: str | bytes, *, max_bytes: int) -> Mapping[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("WebSocket message cap must be a positive integer")
    if isinstance(raw, str):
        size = len(raw.encode("utf-8"))
    elif isinstance(raw, bytes):
        size = len(raw)
    else:
        raise ValueError("WebSocket message must be text or bytes")
    if size > max_bytes:
        raise ValueError("WebSocket frame exceeded configured byte limit")
    payload = loads_decimal_json(raw)
    if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
        raise ValueError("WebSocket JSON-RPC envelope mismatch")
    return payload


async def read_bounded_body(response: aiohttp.ClientResponse, *, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("REST response cap must be a positive integer")
    if response.content_length is not None and response.content_length > max_bytes:
        raise ValueError("REST response exceeded configured byte limit")
    body = bytearray()
    async for chunk in response.content.iter_chunked(min(64 * 1024, max_bytes + 1)):
        if len(body) + len(chunk) > max_bytes:
            raise ValueError("REST response exceeded configured byte limit")
        body.extend(chunk)
    return bytes(body)


async def post_jsonrpc_once(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    method: str,
    params: Mapping[str, Any],
    request_id: int,
    budget: RestRequestBudget,
    max_response_bytes: int = REST_RESPONSE_BYTES_LIMIT,
) -> RestCallResult:
    if method not in PUBLIC_REST_METHODS:
        raise ValueError("REST method is not in the public smoke allowlist")
    if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id <= 0:
        raise ValueError("JSON-RPC request id must be a positive integer")
    if not isinstance(params, Mapping):
        raise ValueError("JSON-RPC params must be an object")
    started = await budget.begin()
    spacing_since_previous_ms = budget.intervals_ms[-1] if budget.intervals_ms else None
    endpoint = f"{base_url.rstrip('/')}/{method}"
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
    try:
        async with session.post(
            endpoint, json=body, headers={"Content-Type": "application/json"},
            allow_redirects=False,
        ) as response:
            record: dict[str, Any] = {
                "method": method,
                "endpoint": endpoint,
                "status": f"HTTP_{response.status}",
            }
            if spacing_since_previous_ms is not None:
                record["spacing_since_previous_ms"] = spacing_since_previous_ms
            try:
                raw = await read_bounded_body(response, max_bytes=max_response_bytes)
            except ValueError:
                record["error_category"] = "RESPONSE_TOO_LARGE"
                record["latency_ms"] = round((time.monotonic() - started) * 1000)
                raise ProbeFailure(record) from None
            record["latency_ms"] = round((time.monotonic() - started) * 1000)
            record["response_bytes"] = len(raw)
            if response.status != 200:
                record["error_category"] = "HTTP_STATUS"
                raise ProbeFailure(record)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                record["error_category"] = "CONTENT_TYPE_MISMATCH"
                raise ProbeFailure(record)
            try:
                payload = loads_decimal_json(raw)
            except (UnicodeDecodeError, TypeError, ValueError):
                record["error_category"] = "INVALID_JSON"
                raise ProbeFailure(record) from None
            if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0":
                record["error_category"] = "JSONRPC_ENVELOPE_MISMATCH"
                raise ProbeFailure(record)
            if isinstance(payload.get("id"), bool) or payload.get("id") != request_id:
                record["error_category"] = "JSONRPC_ID_MISMATCH"
                raise ProbeFailure(record)
            if "error" in payload:
                record["error_category"] = "JSONRPC_ERROR"
                raise ProbeFailure(record)
            if "result" not in payload:
                record["error_category"] = "JSONRPC_RESULT_MISSING"
                raise ProbeFailure(record)
            result = payload["result"]
            if isinstance(result, list):
                record["row_count"] = len(result)
            return RestCallResult(payload=payload, record=record)
    except ProbeFailure:
        raise
    except asyncio.TimeoutError:
        raise ProbeFailure({
            "method": method, "endpoint": endpoint, "status": "NO_HTTP_RESPONSE",
            "latency_ms": round((time.monotonic() - started) * 1000),
            **({"spacing_since_previous_ms": spacing_since_previous_ms} if spacing_since_previous_ms is not None else {}),
            "error_category": "TIMEOUT",
        }) from None
    except aiohttp.ClientError as exc:
        category = "TLS_ERROR" if isinstance(exc, aiohttp.ClientConnectorCertificateError) else "TRANSPORT_ERROR"
        raise ProbeFailure({
            "method": method, "endpoint": endpoint, "status": "NO_HTTP_RESPONSE",
            "latency_ms": round((time.monotonic() - started) * 1000),
            **({"spacing_since_previous_ms": spacing_since_previous_ms} if spacing_since_previous_ms is not None else {}),
            "error_category": category,
        }) from None
    finally:
        budget.complete()


def serialize_public_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize only allowlisted aggregate evidence, never payloads or metric values."""
    result: dict[str, Any] = {}
    for name in ("status", "started_at_utc", "duration_ms", "rest_request_count", "live_sparse_update_observed"):
        if name in report:
            result[name] = report[name]
    if isinstance(report.get("rest"), Sequence) and not isinstance(report.get("rest"), (str, bytes)):
        allowed = ("method", "currency", "endpoint", "status", "error_category", "latency_ms", "spacing_since_previous_ms", "response_bytes", "row_count")
        result["rest"] = [
            {key: record[key] for key in allowed if key in record}
            for record in report["rest"] if isinstance(record, Mapping)
        ]
    if isinstance(report.get("websocket"), Mapping):
        ws = report["websocket"]
        allowed = (
            "endpoint", "connection_count", "connected", "subscription_acks", "lifecycle_event_count",
            "markprice_seed_counts", "ticker_snapshot_count", "ticker_change_count", "status",
            "subscribed_channel_count", "subscribed_channels", "message_schema_valid",
            "instrument_counts", "summary_counts",
        )
        result["websocket"] = {key: ws[key] for key in allowed if key in ws}
    if "deterministic_state_merge_pass" in report:
        result["deterministic_state_merge_pass"] = report["deterministic_state_merge_pass"]
    if isinstance(report.get("universe"), Mapping):
        universe = report["universe"]
        summarized: dict[str, Any] = {}
        if "status" in universe:
            summarized["status"] = universe["status"]
        if isinstance(universe.get("underlyings"), Mapping):
            summarized["underlyings"] = {
                currency: {
                    key: record[key]
                    for key in ("status", "reason", "eligible_count", "selected_count")
                    if key in record
                }
                for currency, record in universe["underlyings"].items()
                if currency in {"BTC", "ETH"} and isinstance(record, Mapping)
            }
        result["universe"] = summarized
    if isinstance(report.get("failure"), Mapping):
        failure = report["failure"]
        allowed = ("stage", "endpoint", "channel", "status", "error_category", "latency_ms")
        result["failure"] = {key: failure[key] for key in allowed if key in failure}
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _raise_schema_failure(stage: str, endpoint: str, category: str) -> None:
    raise ProbeFailure({
        "stage": stage,
        "endpoint": endpoint,
        "status": "SCHEMA_INVALID",
        "error_category": category,
    })


def _underlying_prices(observations: Sequence[Any]) -> dict[str, OptionMetricValue]:
    prices: dict[str, OptionMetricValue] = {}
    for observation in observations:
        metric = observation.metrics.get("underlying_price")
        if (
            metric is not None
            and metric.status is DataStatus.AVAILABLE
            and metric.value is not None
            and metric.value > 0
        ):
            prices.setdefault(observation.underlying, metric)
    return prices


def select_smoke_tickers(selection: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, Any]]:
    """Take one valid deterministic candidate per asset from the bounded universe.

    PARTIAL is expected when the production selector deliberately samples a
    larger eligible universe. STALE/ERROR/NOT_AVAILABLE and empty selections
    remain fail-closed.
    """
    selected: dict[str, tuple[str, ...]] = {}
    report: dict[str, Any] = {}
    for currency in ("BTC", "ETH"):
        coverage = selection.coverage[currency]
        if coverage.status not in {DataStatus.AVAILABLE, DataStatus.PARTIAL}:
            raise ValueError("underlying ticker universe is not trustworthy")
        rows = selection.selected[currency]
        if not rows:
            raise ValueError("bounded ticker universe has no live candidate")
        selected[currency] = (rows[0].symbol,)
        report[currency] = {
            "status": coverage.status.value,
            "reason": coverage.reason,
            "eligible_count": coverage.eligible_count,
            "selected_count": coverage.selected_count,
        }
    return selected, report


class _WebSocketEvidence:
    """In-memory bounded schema evidence; provider payloads are never retained."""

    def __init__(
        self,
        *,
        catalog: Sequence[OptionInstrument],
        supported_indexes: set[str] | frozenset[str],
        plan: SubscriptionPlan,
    ) -> None:
        self.by_symbol = {item.symbol: item for item in catalog}
        self.supported_indexes = supported_indexes
        self.plan = plan
        self.expected_by_index: dict[str, set[str]] = {}
        for item in catalog:
            if item.status is DataStatus.AVAILABLE and item.is_active and item.instrument_state == "open":
                self.expected_by_index.setdefault(item.price_index, set()).add(item.symbol)
        self.mark_states: dict[str, dict[str, Any]] = {}
        self.mark_seeds: set[str] = set()
        self.ticker_states: dict[str, dict[str, OptionMetricValue]] = {}
        self.ticker_snapshots: set[str] = set()
        self.ticker_changes = 0
        self.live_sparse_update_observed = False
        self.lifecycle_event_count = 0
        self.expected_channels = frozenset(
            plan.lifecycle_channels + plan.markprice_channels + plan.ticker_channels
        )

    @property
    def initial_data_complete(self) -> bool:
        indexes = {channel.removeprefix("markprice.options.") for channel in self.plan.markprice_channels}
        return indexes.issubset(self.mark_seeds) and set(self.plan.selected_symbols).issubset(
            self.ticker_snapshots
        )

    def consume(self, payload: Mapping[str, Any]) -> None:
        if "error" in payload or payload.get("method") != "subscription":
            raise ProbeFailure({
                "stage": "websocket", "endpoint": WS_URL, "status": "UNEXPECTED_MESSAGE",
                "error_category": "UNEXPECTED_JSONRPC_MESSAGE",
            })
        params = payload.get("params")
        if not isinstance(params, Mapping) or not isinstance(params.get("channel"), str):
            _raise_schema_failure("websocket", WS_URL, "NOTIFICATION_ENVELOPE_INVALID")
        channel = params["channel"]
        if channel not in self.expected_channels:
            _raise_schema_failure("websocket", channel, "UNREQUESTED_CHANNEL")
        received_at = _utc_now()
        processed_at = _utc_now()

        if channel.startswith("instrument.creation.option."):
            try:
                instrument, _event = parse_instrument_creation_notification(
                    payload,
                    supported_index_names=self.supported_indexes,
                    received_at=received_at,
                    processed_at=processed_at,
                )
            except (TypeError, ValueError):
                _raise_schema_failure("websocket", channel, "LIFECYCLE_CREATION_SCHEMA_INVALID")
            self.by_symbol[instrument.symbol] = instrument
            self.lifecycle_event_count += 1
            return

        if channel.startswith("instrument.state.option."):
            try:
                parse_instrument_state_notification(
                    payload, received_at=received_at, processed_at=processed_at
                )
            except (TypeError, ValueError):
                _raise_schema_failure("websocket", channel, "LIFECYCLE_STATE_SCHEMA_INVALID")
            self.lifecycle_event_count += 1
            return

        if channel.startswith("markprice.options."):
            index_name = channel.removeprefix("markprice.options.")
            is_seed = index_name not in self.mark_seeds
            try:
                observations = parse_markprice_notification(
                    payload,
                    instrument_catalog=self.by_symbol,
                    received_at=received_at,
                    processed_at=processed_at,
                    is_seed=is_seed,
                )
            except (TypeError, ValueError):
                _raise_schema_failure("websocket", channel, "MARKPRICE_SCHEMA_INVALID")
            actual_symbols = {item.symbol for item in observations}
            expected_symbols = self.expected_by_index.get(index_name, set())
            if is_seed and (not expected_symbols or actual_symbols != expected_symbols):
                _raise_schema_failure("websocket", channel, "MARKPRICE_INITIAL_SEED_INCOMPLETE")
            self.mark_states[index_name] = merge_markprice_state(
                self.mark_states.get(index_name, {}), observations, is_seed=is_seed
            )
            if is_seed:
                self.mark_seeds.add(index_name)
            return

        symbol = channel.removeprefix("incremental_ticker.")
        try:
            state, event = apply_ticker_message(
                payload,
                self.ticker_states,
                selected_symbols=self.plan.selected_symbols,
                received_at=received_at,
                processed_at=processed_at,
            )
        except (TypeError, ValueError):
            _raise_schema_failure("websocket", channel, "INCREMENTAL_TICKER_SCHEMA_INVALID")
        if symbol not in self.plan.selected_symbols or event.symbol != symbol:
            _raise_schema_failure("websocket", channel, "TICKER_SYMBOL_MISMATCH")
        if event.is_snapshot:
            self.ticker_snapshots.add(symbol)
        else:
            if symbol not in self.ticker_snapshots:
                _raise_schema_failure("websocket", channel, "TICKER_CHANGE_BEFORE_SNAPSHOT")
            self.ticker_changes += 1
            if event.omitted_field_count > 0 and event.merge_verified:
                self.live_sparse_update_observed = True
        self.ticker_states = state


async def _receive_frame(
    ws: aiohttp.ClientWebSocketResponse, *, timeout: float
) -> Mapping[str, Any] | None:
    try:
        message = await ws.receive(timeout=max(0.01, timeout))
    except asyncio.TimeoutError:
        return None
    if message.type is WSMsgType.TEXT:
        raw: str | bytes = message.data
    elif message.type is WSMsgType.BINARY:
        raw = message.data
    elif message.type in {WSMsgType.PING, WSMsgType.PONG}:
        return None
    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR}:
        raise ProbeFailure({
            "stage": "websocket", "endpoint": WS_URL, "status": "CONNECTION_CLOSED",
            "error_category": "WEBSOCKET_CLOSED",
        })
    else:
        return None
    try:
        return decode_bounded_ws_message(raw, max_bytes=WS_MESSAGE_BYTES_LIMIT)
    except (TypeError, ValueError):
        raise ProbeFailure({
            "stage": "websocket", "endpoint": WS_URL, "status": "SCHEMA_INVALID",
            "error_category": "FRAME_INVALID_OR_OVERSIZED",
        }) from None


async def _run_websocket_smoke(
    *,
    catalog: Sequence[OptionInstrument],
    supported_indexes: set[str] | frozenset[str],
    plan: SubscriptionPlan,
    deadline: float,
) -> dict[str, Any]:
    ws_started = time.monotonic()
    channels = plan.lifecycle_channels + plan.markprice_channels + plan.ticker_channels
    if not channels:
        raise ProbeFailure({
            "stage": "websocket", "endpoint": WS_URL, "status": "NO_CHANNELS",
            "error_category": "EMPTY_SUBSCRIPTION",
        })
    evidence = _WebSocketEvidence(
        catalog=catalog, supported_indexes=supported_indexes, plan=plan
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=WS_ACK_TIMEOUT_SECONDS), trust_env=False
    )
    try:
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(
                    WS_URL,
                    autoping=True,
                    heartbeat=None,
                    max_msg_size=WS_MESSAGE_BYTES_LIMIT,
                    receive_timeout=None,
                ),
                timeout=min(WS_ACK_TIMEOUT_SECONDS, max(0.01, deadline - time.monotonic())),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            category = (
                "TLS_ERROR" if isinstance(exc, aiohttp.ClientConnectorCertificateError)
                else "CONNECT_OR_UPGRADE_FAILED"
            )
            raise ProbeFailure({
                "stage": "websocket", "endpoint": WS_URL, "status": "NO_UPGRADE",
                "error_category": category,
                "latency_ms": round((time.monotonic() - ws_started) * 1000),
            }) from None

        async with ws:
            try:
                await ws.send_json(build_subscribe_request(channels, request_id=1))
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise ProbeFailure({
                    "stage": "websocket", "endpoint": WS_URL, "status": "SEND_FAILED",
                    "error_category": "SUBSCRIBE_SEND_FAILED",
                }) from None

            ack_deadline = min(deadline, time.monotonic() + WS_ACK_TIMEOUT_SECONDS)
            ack_ok = False
            while time.monotonic() < ack_deadline:
                payload = await _receive_frame(ws, timeout=ack_deadline - time.monotonic())
                if not payload:
                    continue
                if "id" in payload:
                    if payload.get("id") != 1:
                        raise ProbeFailure({
                            "stage": "websocket", "endpoint": WS_URL,
                            "status": "ACK_MISMATCH", "error_category": "UNEXPECTED_RESPONSE_ID",
                        })
                    try:
                        validate_subscription_ack(payload, request_id=1, expected_channels=channels)
                    except ValueError:
                        raise ProbeFailure({
                            "stage": "websocket", "endpoint": WS_URL, "status": "ACK_REJECTED",
                            "error_category": "SUBSCRIPTION_ACK_MISMATCH",
                        }) from None
                    ack_ok = True
                    break
                evidence.consume(payload)
            if not ack_ok:
                raise ProbeFailure({
                    "stage": "websocket", "endpoint": WS_URL, "status": "ACK_TIMEOUT",
                    "error_category": "SUBSCRIPTION_ACK_TIMEOUT",
                })

            initial_deadline = min(deadline, time.monotonic() + WS_INITIAL_DATA_TIMEOUT_SECONDS)
            while not evidence.initial_data_complete and time.monotonic() < initial_deadline:
                payload = await _receive_frame(ws, timeout=initial_deadline - time.monotonic())
                if payload:
                    evidence.consume(payload)
            if not evidence.initial_data_complete:
                missing_indexes = sorted(
                    {channel.removeprefix("markprice.options.") for channel in plan.markprice_channels}
                    .difference(evidence.mark_seeds)
                )
                missing_tickers = sorted(
                    set(plan.selected_symbols).difference(evidence.ticker_snapshots)
                )
                missing_channel = (
                    f"markprice.options.{missing_indexes[0]}" if missing_indexes
                    else f"incremental_ticker.{missing_tickers[0]}"
                )
                raise ProbeFailure({
                    "stage": "websocket", "endpoint": WS_URL, "channel": missing_channel,
                    "status": "INITIAL_DATA_TIMEOUT",
                    "error_category": "MARKPRICE_OR_TICKER_SNAPSHOT_MISSING",
                })

            sparse_deadline = min(deadline, time.monotonic() + WS_SPARSE_OBSERVATION_WINDOW_SECONDS)
            while time.monotonic() < sparse_deadline:
                payload = await _receive_frame(ws, timeout=sparse_deadline - time.monotonic())
                if payload:
                    evidence.consume(payload)

            return {
                "endpoint": WS_URL,
                "connection_count": 1,
                "connected": True,
                "subscription_acks": 1,
                "subscribed_channel_count": len(channels),
                "subscribed_channels": list(channels),
                "message_schema_valid": True,
                "lifecycle_event_count": evidence.lifecycle_event_count,
                "markprice_seed_counts": {
                    index: len(state) for index, state in sorted(evidence.mark_states.items())
                },
                "ticker_snapshot_count": len(evidence.ticker_snapshots),
                "ticker_change_count": evidence.ticker_changes,
                "status": "PASS",
                "live_sparse_update_observed": evidence.live_sparse_update_observed,
            }
    except ProbeFailure as exc:
        record = dict(exc.record)
        record.setdefault("latency_ms", round((time.monotonic() - ws_started) * 1000))
        raise ProbeFailure(record) from None
    except asyncio.TimeoutError:
        raise ProbeFailure({
            "stage": "websocket", "endpoint": WS_URL, "status": "TIMEOUT",
            "error_category": "GLOBAL_DEADLINE_EXCEEDED",
            "latency_ms": round((time.monotonic() - ws_started) * 1000),
        }) from None
    except aiohttp.ClientError:
        raise ProbeFailure({
            "stage": "websocket", "endpoint": WS_URL, "status": "TRANSPORT_ERROR",
            "error_category": "WEBSOCKET_TRANSPORT_ERROR",
            "latency_ms": round((time.monotonic() - ws_started) * 1000),
        }) from None
    finally:
        await session.close()


async def run_probe() -> dict[str, Any]:
    settings = Phase8Settings.from_env({"TRADING_MODE": "paper"})
    started_at = _utc_now()
    started = time.monotonic()
    deadline = started + PROBE_DEADLINE_SECONDS
    budget = RestRequestBudget()
    rest_records: list[dict[str, Any]] = []
    instrument_counts: dict[str, int] = {}
    summary_counts: dict[str, int] = {}
    universe_report: dict[str, Any] | None = None
    failure: Mapping[str, Any] | None = None
    ws_evidence: dict[str, Any] | None = None

    try:
        async with asyncio.timeout(PROBE_DEADLINE_SECONDS):
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT_SECONDS), trust_env=False
            )
            try:
                async def call(
                    method: str, params: Mapping[str, Any], request_id: int
                ) -> RestCallResult:
                    try:
                        result = await post_jsonrpc_once(
                            session,
                            base_url=REST_BASE_URL,
                            method=method,
                            params=params,
                            request_id=request_id,
                            budget=budget,
                            max_response_bytes=REST_RESPONSE_BYTES_LIMIT,
                        )
                    except ProbeFailure as exc:
                        record = dict(exc.record)
                        if "currency" in params:
                            record["currency"] = params["currency"]
                        rest_records.append(record)
                        raise
                    record = dict(result.record)
                    if "currency" in params:
                        record["currency"] = params["currency"]
                    rest_records.append(record)
                    return result

                index_response = await call("public/get_index_price_names", {}, 101)
                try:
                    supported_indexes = parse_index_price_names_response(index_response.payload)
                except (TypeError, ValueError):
                    _raise_schema_failure("rest", "public/get_index_price_names", "INDEX_NAME_SCHEMA_INVALID")

                instruments_by_underlying: dict[str, tuple[OptionInstrument, ...]] = {}
                for currency, request_id in (("BTC", 102), ("ETH", 103)):
                    response = await call(
                        "public/get_instruments",
                        {"currency": currency, "kind": "option", "expired": False},
                        request_id,
                    )
                    fetched_at = _utc_now()
                    try:
                        rows = parse_instruments_response(
                            response.payload,
                            requested_currency=currency,
                            supported_index_names=supported_indexes,
                            fetched_at=fetched_at,
                            processed_at=_utc_now(),
                            max_records=settings.max_full_chain_records_per_underlying,
                        )
                    except (TypeError, ValueError):
                        _raise_schema_failure("rest", "public/get_instruments", f"{currency}_INSTRUMENT_SCHEMA_INVALID")
                    instruments_by_underlying[currency] = rows
                    instrument_counts[currency] = len(rows)

                catalog = tuple(
                    item for currency in ("BTC", "ETH")
                    for item in instruments_by_underlying[currency]
                )
                if len(catalog) > settings.max_full_chain_records_total:
                    _raise_schema_failure("rest", "public/get_instruments", "TOTAL_INSTRUMENT_CAP_EXCEEDED")

                summaries_by_underlying: dict[str, tuple[Any, ...]] = {}
                for currency, request_id in (("BTC", 104), ("ETH", 105)):
                    response = await call(
                        "public/get_book_summary_by_currency",
                        {"currency": currency, "kind": "option"},
                        request_id,
                    )
                    try:
                        rows = parse_book_summary_response(
                            response.payload,
                            requested_currency=currency,
                            instrument_catalog=instruments_by_underlying[currency],
                            fetched_at=_utc_now(),
                            processed_at=_utc_now(),
                            max_records=settings.max_full_chain_records_per_underlying,
                        )
                    except (TypeError, ValueError):
                        _raise_schema_failure("rest", "public/get_book_summary_by_currency", f"{currency}_SUMMARY_SCHEMA_INVALID")
                    expected = {
                        item.symbol for item in instruments_by_underlying[currency]
                        if item.status is DataStatus.AVAILABLE and item.is_active and item.instrument_state == "open"
                    }
                    actual = {item.symbol for item in rows}
                    if actual != expected:
                        _raise_schema_failure("rest", "public/get_book_summary_by_currency", f"{currency}_SUMMARY_COVERAGE_MISMATCH")
                    summaries_by_underlying[currency] = rows
                    summary_counts[currency] = len(rows)
            finally:
                await session.close()

            all_summaries = tuple(
                item for currency in ("BTC", "ETH") for item in summaries_by_underlying[currency]
            )
            selection = select_ticker_universe(
                catalog, _underlying_prices(all_summaries), _utc_now(), settings
            )
            try:
                selected_by_underlying, universe_report = select_smoke_tickers(selection)
            except (KeyError, TypeError, ValueError):
                _raise_schema_failure("universe", "public/get_instruments", "NO_VALID_BOUNDED_TICKER_CANDIDATE")
            plan = build_subscription_plan(catalog, supported_indexes, selected_by_underlying)
            active_indexes = {
                item.price_index for item in catalog
                if item.status is DataStatus.AVAILABLE and item.is_active and item.instrument_state == "open"
            }
            derived_indexes = {
                channel.removeprefix("markprice.options.") for channel in plan.markprice_channels
            }
            if derived_indexes != active_indexes:
                _raise_schema_failure("websocket", WS_URL, "DYNAMIC_INDEX_CHANNEL_MISMATCH")

            ws_evidence = await _run_websocket_smoke(
                catalog=catalog,
                supported_indexes=supported_indexes,
                plan=plan,
                deadline=deadline,
            )
            ws_evidence["instrument_counts"] = instrument_counts
            ws_evidence["summary_counts"] = summary_counts
    except ProbeFailure as exc:
        failure = dict(exc.record)
        if "latency_ms" not in failure:
            failure["latency_ms"] = round((time.monotonic() - started) * 1000)
    except asyncio.TimeoutError:
        failure = {
            "stage": "probe", "endpoint": REST_BASE_URL, "status": "TIMEOUT",
            "error_category": "GLOBAL_DEADLINE_EXCEEDED",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except (aiohttp.ClientError, OSError):
        failure = {
            "stage": "probe", "endpoint": REST_BASE_URL, "status": "NO_RESPONSE",
            "error_category": "TRANSPORT_ERROR",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except (TypeError, ValueError):
        failure = {
            "stage": "contract", "endpoint": REST_BASE_URL, "status": "SCHEMA_INVALID",
            "error_category": "CONTRACT_VALIDATION_FAILED",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    report = {
        "status": "FAIL_CLOSED" if failure else "PASS",
        "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "rest_request_count": budget.calls,
        "rest": rest_records,
        "websocket": ws_evidence,
        "universe": {
            "status": "PARTIAL" if universe_report and any(row["status"] == "PARTIAL" for row in universe_report.values()) else "AVAILABLE",
            "underlyings": universe_report or {},
        },
        "failure": failure,
        "live_sparse_update_observed": (
            bool(ws_evidence and ws_evidence.get("live_sparse_update_observed"))
        ),
    }
    return serialize_public_report(report)


def main() -> int:
    try:
        report = asyncio.run(run_probe())
    except KeyboardInterrupt:
        report = serialize_public_report({
            "status": "FAIL_CLOSED",
            "failure": {
                "stage": "probe", "endpoint": REST_BASE_URL, "status": "INTERRUPTED",
                "error_category": "USER_INTERRUPT",
            },
        })
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
