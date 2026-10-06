"""Strict public spot adapters for Binance and Bitget UTA v3.

This module contains deterministic schema builders and parsers only.  It does
not open sockets, authenticate, or fall back between exchange API versions.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .contracts import MarketKind
from .sources import SourceKind, SourceRegistry


APPROVED_SPOT_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT"})

_SPOT_SYMBOL_SCOPE = ContextVar("v2_spot_symbol_capabilities", default=APPROVED_SPOT_SYMBOLS)

def is_approved_spot_symbol(symbol: str) -> bool:
    return symbol in _SPOT_SYMBOL_SCOPE.get()

@contextmanager
def spot_symbol_scope(symbols, *, source_ref: str):
    """Scope verified Bitget spot instrument capabilities to one collection task.

    The caller must supply symbols from the public SPOT instrument metadata,
    independently of derivatives or on-chain enablement.
    """
    allowed=frozenset(symbols)
    if not source_ref.startswith("bitget:spot:instruments") or not allowed or any(
        not re.fullmatch(r"[A-Z0-9]{2,24}USDT",s) for s in allowed):
        raise SpotAdapterError("invalid spot capability receipt")
    token=_SPOT_SYMBOL_SCOPE.set(allowed)
    try:
        yield
    finally:
        _SPOT_SYMBOL_SCOPE.reset(token)

_MAX_RECONNECT_ATTEMPTS = 3
_MAX_REST_LIMIT = 1000


class SpotAdapterError(ValueError):
    """A spot request or payload violates its single-source contract."""


class SpotSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpotAdapterError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpotAdapterError(f"{field} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise SpotAdapterError(f"{field} must be a scalar identifier")
    result = str(value).strip()
    if not result or not result.isdigit():
        raise SpotAdapterError(f"{field} must be a non-negative integer identifier")
    return result


def _trade_identifier(value: Any, field: str) -> str:
    result = _text(value, field) if isinstance(value, str) else _identifier(value, field)
    if not result:
        raise SpotAdapterError(f"{field} is required")
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SpotAdapterError(f"{field} must be Decimal-compatible") from exc
    if not result.is_finite() or result <= 0:
        raise SpotAdapterError(f"{field} must be positive and finite")
    return result


def _utc_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SpotAdapterError(f"{field} must be UTC-aware")
    return value


def _epoch_milliseconds(value: Any, field: str) -> datetime:
    if isinstance(value, bool):
        raise SpotAdapterError(f"{field} must be epoch milliseconds")
    try:
        raw = str(value).strip()
        if not raw.isdigit():
            raise ValueError
        milliseconds = int(raw)
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise SpotAdapterError(f"{field} must be epoch milliseconds") from exc


def _optional_positive_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    return _positive_decimal(value, field)


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _validate_limit(limit: int) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_REST_LIMIT:
        raise SpotAdapterError(f"limit must be between 1 and {_MAX_REST_LIMIT}")
    return str(limit)


def _validate_nonnegative_integer(value: int | str, field: str) -> str:
    if isinstance(value, bool):
        raise SpotAdapterError(f"{field} must be a non-negative integer")
    raw = str(value).strip()
    if not raw.isdigit():
        raise SpotAdapterError(f"{field} must be a non-negative integer")
    return raw


def _reject_v2_fields(payload: Mapping[str, Any]) -> None:
    if "channel" in payload or "instId" in payload:
        raise SpotAdapterError("Classic v2 channel/instId schema is forbidden")
    args = payload.get("args")
    if isinstance(args, list):
        for item in args:
            if isinstance(item, Mapping) and ("channel" in item or "instId" in item):
                raise SpotAdapterError("Classic v2 channel/instId schema is forbidden")


def validate_spot_subscription(payload: Mapping[str, Any]) -> None:
    """Reject known Classic v2 subscription fields before any adapter sees them."""
    _reject_v2_fields(_mapping(payload, "subscription"))


def _normalize_symbol(value: Any) -> str:
    symbol = _text(value, "symbol").upper()
    if not is_approved_spot_symbol(symbol):
        raise SpotAdapterError("only BTCUSDT and ETHUSDT spot symbols are approved")
    return symbol


@dataclass(frozen=True, slots=True)
class SpotTradeEvent:
    source_id: str
    exchange: str
    symbol: str
    trade_id: str
    event_timestamp: datetime
    fetched_at: datetime
    processed_at: datetime
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal | None
    side: SpotSide
    maker_flag: bool | None
    market_kind: MarketKind
    source_endpoint: str
    source_channel: str
    raw_side: str | None = None
    first_trade_id: str | None = None
    last_trade_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.exchange or not self.trade_id:
            raise SpotAdapterError("source, exchange and trade identity are required")
        normalized = _normalize_symbol(self.symbol)
        if normalized != self.symbol:
            raise SpotAdapterError("SpotTradeEvent.symbol must be normalized")
        _utc_datetime(self.event_timestamp, "event_timestamp")
        _utc_datetime(self.fetched_at, "fetched_at")
        _utc_datetime(self.processed_at, "processed_at")
        if self.market_kind is not MarketKind.SPOT:
            raise SpotAdapterError("spot event market_kind must be SPOT")
        _positive_decimal(self.price, "price")
        _positive_decimal(self.quantity, "quantity")
        _optional_positive_decimal(self.quote_quantity, "quote_quantity")
        if not isinstance(self.side, SpotSide):
            raise SpotAdapterError("side must be SpotSide")
        if self.maker_flag is not None and not isinstance(self.maker_flag, bool):
            raise SpotAdapterError("maker_flag must be boolean or None")
        if self.first_trade_id is not None:
            _identifier(self.first_trade_id, "first_trade_id")
        if self.last_trade_id is not None:
            _identifier(self.last_trade_id, "last_trade_id")

    @property
    def exchange_timestamp(self) -> datetime:
        return self.event_timestamp

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.source_id, self.symbol, self.trade_id)


class _SpotAdapterBase:
    market_kind = MarketKind.SPOT

    def normalize_symbol(self, symbol: Any) -> str:
        return _normalize_symbol(symbol)

    def validate_market_kind(self, value: Any) -> None:
        if value is not MarketKind.SPOT and value != MarketKind.SPOT.value:
            raise SpotAdapterError("only market_kind=SPOT is accepted")

    def dedup_key(self, event: SpotTradeEvent) -> tuple[str, str, str]:
        if event.market_kind is not MarketKind.SPOT:
            raise SpotAdapterError("deduplication is only available for spot events")
        return event.identity


class BinanceSpotAdapter(_SpotAdapterBase):
    exchange = "binance"
    source_id = "binance_spot"
    source_kind = SourceKind.BINANCE_SPOT
    rest_endpoint = "https://api.binance.com/api/v3/aggTrades"
    ws_endpoint = "wss://stream.binance.com:9443/ws"

    def subscription(self, symbol: str, *, request_id: int = 1) -> dict[str, object]:
        symbol = self.normalize_symbol(symbol)
        if isinstance(request_id, bool) or not isinstance(request_id, int) or request_id < 1:
            raise SpotAdapterError("request_id must be a positive integer")
        return {
            "method": "SUBSCRIBE",
            "params": [f"{symbol.lower()}@aggTrade"],
            "id": request_id,
        }

    def reconnect_subscriptions(self, symbol: str, *, max_attempts: int = _MAX_RECONNECT_ATTEMPTS) -> tuple[dict[str, object], ...]:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= _MAX_RECONNECT_ATTEMPTS:
            raise SpotAdapterError("reconnect attempts exceed bounded cap")
        return tuple(self.subscription(symbol, request_id=index) for index in range(1, max_attempts + 1))

    def rest_request(
        self,
        symbol: str,
        *,
        limit: int = 1000,
        from_id: int | str | None = None,
        start_time_ms: int | str | None = None,
        end_time_ms: int | str | None = None,
    ) -> tuple[str, dict[str, str]]:
        if from_id is not None and (start_time_ms is not None or end_time_ms is not None):
            raise SpotAdapterError("fromId cannot be combined with time-bounded backfill")
        params = {"symbol": self.normalize_symbol(symbol), "limit": _validate_limit(limit)}
        if from_id is not None:
            params["fromId"] = _validate_nonnegative_integer(from_id, "from_id")
        if start_time_ms is not None:
            params["startTime"] = _validate_nonnegative_integer(start_time_ms, "start_time_ms")
        if end_time_ms is not None:
            params["endTime"] = _validate_nonnegative_integer(end_time_ms, "end_time_ms")
        if "startTime" in params and "endTime" in params and int(params["endTime"]) < int(params["startTime"]):
            raise SpotAdapterError("end_time_ms must not precede start_time_ms")
        return self.rest_endpoint_path, params

    @property
    def rest_endpoint_path(self) -> str:
        return "/api/v3/aggTrades"

    def parse_ws_message(self, payload: Mapping[str, Any], *, fetched_at: datetime, processed_at: datetime | None = None) -> SpotTradeEvent:
        item = _mapping(payload, "Binance aggTrade message")
        if item.get("e") != "aggTrade":
            raise SpotAdapterError("Binance WebSocket event must be aggTrade")
        return self._parse_trade(item, fetched_at=fetched_at, processed_at=processed_at, source_channel="aggTrade", require_event_name=True)

    def parse_rest_response(self, payload: Any, *, symbol: str, fetched_at: datetime, processed_at: datetime | None = None) -> tuple[SpotTradeEvent, ...]:
        if not isinstance(payload, list):
            raise SpotAdapterError("Binance aggTrades response must be an array")
        if len(payload) > _MAX_REST_LIMIT:
            raise SpotAdapterError("Binance REST page exceeds bounded event cap")
        normalized = self.normalize_symbol(symbol)
        events = tuple(
            self._parse_trade(
                (
                    dict(_mapping(row, "Binance aggTrades row"))
                    if "s" in _mapping(row, "Binance aggTrades row")
                    else {**_mapping(row, "Binance aggTrades row"), "s": normalized}
                ),
                fetched_at=fetched_at,
                processed_at=processed_at,
                source_channel="aggTrades",
                expected_symbol=normalized,
                require_event_name=False,
            )
            for row in payload
        )
        return events

    def _parse_trade(
        self,
        item: Mapping[str, Any],
        *,
        fetched_at: datetime,
        processed_at: datetime | None,
        source_channel: str,
        expected_symbol: str | None = None,
        require_event_name: bool,
    ) -> SpotTradeEvent:
        validate_spot_subscription({})
        received = _utc_datetime(fetched_at, "fetched_at")
        processed = received if processed_at is None else _utc_datetime(processed_at, "processed_at")
        symbol = self.normalize_symbol(item.get("s"))
        if expected_symbol is not None and symbol != expected_symbol:
            raise SpotAdapterError("Binance trade symbol does not match requested symbol")
        required = ("a", "p", "q", "f", "l", "T", "m")
        if any(field not in item for field in required):
            raise SpotAdapterError("Binance aggTrade payload is missing a required field")
        trade_id = _identifier(item["a"], "a")
        _identifier(item["f"], "f")
        _identifier(item["l"], "l")
        event_timestamp = _epoch_milliseconds(item["T"], "T")
        if "E" in item:
            _epoch_milliseconds(item["E"], "E")
        maker = item["m"] if isinstance(item["m"], bool) else None
        side = SpotSide.BUY if maker is False else SpotSide.SELL if maker is True else SpotSide.UNKNOWN
        return SpotTradeEvent(
            source_id=self.source_id,
            exchange=self.exchange,
            symbol=symbol,
            trade_id=trade_id,
            event_timestamp=event_timestamp,
            fetched_at=received,
            processed_at=processed,
            price=_positive_decimal(item["p"], "p"),
            quantity=_positive_decimal(item["q"], "q"),
            quote_quantity=None,
            side=side,
            maker_flag=maker,
            market_kind=MarketKind.SPOT,
            source_endpoint=self.ws_endpoint if source_channel == "aggTrade" else self.rest_endpoint,
            source_channel=source_channel,
            first_trade_id=_identifier(item["f"], "f"),
            last_trade_id=_identifier(item["l"], "l"),
        )


class BitgetUtaV3SpotAdapter(_SpotAdapterBase):
    exchange = "bitget"
    source_id = "bitget_spot_uta_v3"
    source_kind = SourceKind.BITGET_SPOT_UTA_V3
    rest_endpoint = "https://api.bitget.com/api/v3/market/fills"
    ws_endpoint = "wss://ws.bitget.com/v3/ws/public"

    def __init__(self, *, registry: SourceRegistry | None = None) -> None:
        self.registry = registry if registry is not None else SourceRegistry()

    def _require_enabled_source(self) -> None:
        definition = self.registry.require(self.source_id)
        if not definition.enabled:
            raise SpotAdapterError(
                f"{self.source_id} is {definition.status.value}; official contract must be enabled before REST use"
            )

    def subscription(self, symbol: str) -> dict[str, object]:
        return {
            "op": "subscribe",
            "args": [{
                "instType": "spot",
                "topic": "publicTrade",
                "symbol": self.normalize_symbol(symbol),
            }],
        }

    def reconnect_subscriptions(self, symbol: str, *, max_attempts: int = _MAX_RECONNECT_ATTEMPTS) -> tuple[dict[str, object], ...]:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= _MAX_RECONNECT_ATTEMPTS:
            raise SpotAdapterError("reconnect attempts exceed bounded cap")
        return tuple(self.subscription(symbol) for _ in range(max_attempts))

    def rest_request(self, symbol: str, *, limit: int = 100, category: str = "SPOT") -> tuple[str, dict[str, str]]:
        self._require_enabled_source()
        if category != "SPOT":
            raise SpotAdapterError("Bitget v3 spot adapter does not fall back across categories")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SpotAdapterError("Bitget UTA fills limit must be between 1 and 100")
        return "/api/v3/market/fills", {
            "category": "SPOT",
            "symbol": self.normalize_symbol(symbol),
            "limit": _validate_limit(limit),
        }

    def parse_ws_message(self, payload: Mapping[str, Any], *, fetched_at: datetime, processed_at: datetime | None = None) -> SpotTradeEvent:
        item = _mapping(payload, "Bitget UTA v3 publicTrade message")
        validate_spot_subscription(item)
        arg = _mapping(item.get("arg"), "arg")
        if arg.get("instType") != "spot" or arg.get("topic") != "publicTrade":
            raise SpotAdapterError("Bitget message must use UTA v3 spot/publicTrade")
        if "channel" in arg or "instId" in arg:
            raise SpotAdapterError("Classic v2 channel/instId schema is forbidden")
        rows = item.get("data")
        if not isinstance(rows, list) or len(rows) == 0 or len(rows) > _MAX_REST_LIMIT:
            raise SpotAdapterError("Bitget publicTrade data must be a bounded non-empty array")
        events = self.parse_ws_events(item, fetched_at=fetched_at, processed_at=processed_at)
        if len(events) != 1:
            raise SpotAdapterError("use parse_ws_events for a batched publicTrade message")
        return events[0]

    def parse_ws_events(
        self,
        payload: Mapping[str, Any],
        *,
        fetched_at: datetime,
        processed_at: datetime | None = None,
    ) -> tuple[SpotTradeEvent, ...]:
        item = _mapping(payload, "Bitget UTA v3 publicTrade message")
        validate_spot_subscription(item)
        arg = _mapping(item.get("arg"), "arg")
        if arg.get("instType") != "spot" or arg.get("topic") != "publicTrade":
            raise SpotAdapterError("Bitget message must use UTA v3 spot/publicTrade")
        if "channel" in arg or "instId" in arg:
            raise SpotAdapterError("Classic v2 channel/instId schema is forbidden")
        rows = item.get("data")
        if not isinstance(rows, list) or len(rows) == 0 or len(rows) > _MAX_REST_LIMIT:
            raise SpotAdapterError("Bitget publicTrade data must be a bounded non-empty array")
        return tuple(
            self._parse_row(
                _mapping(row, "Bitget publicTrade row"),
                symbol=arg.get("symbol"),
                fetched_at=fetched_at,
                processed_at=processed_at,
                source_channel="publicTrade",
            )
            for row in rows
        )

    def parse_rest_response(self, payload: Any, *, symbol: str, fetched_at: datetime, processed_at: datetime | None = None) -> tuple[SpotTradeEvent, ...]:
        item = _mapping(payload, "Bitget fills response")
        if item.get("code") != "00000":
            raise SpotAdapterError("Bitget v3 response code must be 00000")
        rows = item.get("data")
        if not isinstance(rows, list) or len(rows) > 100:
            raise SpotAdapterError("Bitget v3 fills data must be a bounded array")
        requested = self.normalize_symbol(symbol)
        return tuple(
            self._parse_row(_mapping(row, "Bitget v3 fill row"), symbol=requested, fetched_at=fetched_at, processed_at=processed_at, source_channel="fills")
            for row in rows
        )

    def _parse_row(
        self,
        row: Mapping[str, Any],
        *,
        symbol: Any,
        fetched_at: datetime,
        processed_at: datetime | None,
        source_channel: str,
    ) -> SpotTradeEvent:
        if "instId" in row or "channel" in row:
            raise SpotAdapterError("Classic v2 response schema is forbidden; no fallback is permitted")
        received = _utc_datetime(fetched_at, "fetched_at")
        processed = received if processed_at is None else _utc_datetime(processed_at, "processed_at")
        channel_symbol = self.normalize_symbol(symbol) if symbol is not None else None
        row_symbol = row.get("symbol")
        normalized = self.normalize_symbol(row_symbol) if channel_symbol is None else channel_symbol
        if row_symbol is not None and self.normalize_symbol(row_symbol) != normalized:
            raise SpotAdapterError("Bitget trade symbol does not match channel")
        websocket = source_channel == "publicTrade"
        id_field, price_field, size_field, time_field, side_field = (
            ("i", "p", "v", "T", "S") if websocket
            else ("execId", "price", "size", "ts", "side")
        )
        required = (id_field, price_field, size_field, time_field, side_field)
        if any(field not in row for field in required):
            raise SpotAdapterError("Bitget UTA v3 trade payload is missing a required field")
        raw_side = _text(row[side_field], side_field).lower()
        if raw_side not in {"buy", "sell"}:
            raise SpotAdapterError("Bitget UTA trade side must be buy or sell")
        return SpotTradeEvent(
            source_id=self.source_id,
            exchange=self.exchange,
            symbol=normalized,
            trade_id=_trade_identifier(row[id_field], id_field),
            event_timestamp=_epoch_milliseconds(row[time_field], time_field),
            fetched_at=received,
            processed_at=processed,
            price=_positive_decimal(row[price_field], price_field),
            quantity=_positive_decimal(row[size_field], size_field),
            quote_quantity=_optional_positive_decimal(row.get("quoteVol"), "quoteVol"),
            side=SpotSide.UNKNOWN,
            maker_flag=None,
            market_kind=MarketKind.SPOT,
            source_endpoint=self.ws_endpoint if source_channel == "publicTrade" else self.rest_endpoint,
            source_channel=source_channel,
            raw_side=raw_side,
        )

