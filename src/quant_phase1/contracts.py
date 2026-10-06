"""Exchange-neutral canonical contracts for Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from .time import ensure_utc


class DataStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class RawReference:
    provider: str
    endpoint: str
    payload_hash: str | None = None
    request_id: str | None = None
    reason: str | None = None


def _check_timestamp(value: datetime | None, field: str) -> None:
    if value is not None:
        try:
            ensure_utc(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be UTC") from exc


@dataclass(frozen=True, slots=True)
class Observation:
    symbol: str
    value: Any
    source: str
    exchange: str
    exchange_timestamp: datetime | None
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus
    raw_payload: Any = None
    raw_reference: RawReference | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        for field, value in (
            ("exchange_timestamp", self.exchange_timestamp),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _check_timestamp(value, field)
        if self.status is DataStatus.NOT_AVAILABLE and self.value is not None:
            raise ValueError("NOT_AVAILABLE observations cannot have a value")


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    category: str
    base_coin: str
    quote_coin: str
    symbol_type: str
    contract_type: str
    status: str
    price_precision: int
    quantity_precision: int
    min_order_qty: Decimal
    max_order_qty: Decimal | None
    fetched_at: datetime
    raw_payload: Mapping[str, Any]
    exchange_timestamp: datetime | None = None
    source: str = "bitget_v3_rest"
    exchange: str = "bitget"

    def __post_init__(self) -> None:
        _check_timestamp(self.exchange_timestamp, "exchange_timestamp")
        _check_timestamp(self.fetched_at, "fetched_at")


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    volume24h: Decimal
    turnover24h: Decimal
    index_price: Decimal | None
    mark_price: Decimal | None
    exchange_timestamp: datetime
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus
    raw_payload: Mapping[str, Any]
    source: str = "bitget_v3_rest"
    exchange: str = "bitget"

    def __post_init__(self) -> None:
        for field, value in (
            ("exchange_timestamp", self.exchange_timestamp),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _check_timestamp(value, field)
        if self.status is DataStatus.NOT_AVAILABLE:
            raise ValueError("ticker price data cannot be NOT_AVAILABLE")


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    bar_open_timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    exchange_timestamp: datetime
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus
    is_closed: bool
    raw_payload: Any
    source: str = "bitget_v3_rest"
    exchange: str = "bitget"

    def __post_init__(self) -> None:
        for field, value in (
            ("bar_open_timestamp", self.bar_open_timestamp),
            ("exchange_timestamp", self.exchange_timestamp),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _check_timestamp(value, field)
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("candle OHLC values are inconsistent")


def not_available_observation(
    *, symbol: str, source: str, reason: str, fetched_at: datetime
) -> Observation:
    return Observation(
        symbol=symbol,
        value=None,
        source=source,
        exchange="bitget",
        exchange_timestamp=None,
        fetched_at=fetched_at,
        processed_at=fetched_at,
        status=DataStatus.NOT_AVAILABLE,
        raw_reference=RawReference(provider="phase1", endpoint="semantic", reason=reason),
    )
