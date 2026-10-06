"""Exchange-neutral contracts for public trade flow.

Exchange-specific payload fields stay inside ``raw_payload``.  Flow code
must consume this module rather than exchange response dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class SideSource(StrEnum):
    EXCHANGE_PROVIDED = "EXCHANGE_PROVIDED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class FlowStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    normalized = value.astimezone(timezone.utc)
    if normalized.tzinfo != timezone.utc:
        raise ValueError(f"{field} must be UTC")
    return normalized


def _positive(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CanonicalTrade:
    exchange: str
    exchange_symbol: str
    canonical_symbol: str | None
    trade_id: str
    price: Decimal
    quantity_base: Decimal
    notional_usd: Decimal | None
    aggressor_side: TradeSide
    raw_side: str | None
    raw_side_semantics: str
    side_source: SideSource
    exchange_timestamp: datetime
    received_at: datetime
    processed_at: datetime
    source_channel: str
    status: FlowStatus
    raw_payload: Mapping[str, Any] | None = None
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.exchange.strip() or not self.exchange_symbol.strip() or not self.trade_id.strip():
            raise ValueError("exchange, exchange_symbol, and trade_id are required")
        if not self.raw_side_semantics.strip() or not self.source_channel.strip():
            raise ValueError("raw_side_semantics and source_channel are required")
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "quantity_base", _positive(self.quantity_base, "quantity_base"))
        if self.notional_usd is not None:
            object.__setattr__(self, "notional_usd", _positive(self.notional_usd, "notional_usd"))
        for field in ("exchange_timestamp", "received_at", "processed_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.aggressor_side is TradeSide.UNKNOWN and self.side_source is not SideSource.UNKNOWN:
            raise ValueError("unknown aggressor side requires UNKNOWN side_source")
        if self.side_source is SideSource.INFERRED:
            raise ValueError("INFERRED side source is forbidden in Phase 3")
        if self.aggressor_side is not TradeSide.UNKNOWN and self.side_source is not SideSource.EXCHANGE_PROVIDED:
            raise ValueError("known aggressor side must be EXCHANGE_PROVIDED")

    def to_record(self) -> dict[str, Any]:
        """Return a persistence-safe record without exposing adapter fields."""
        return {
            "exchange": self.exchange,
            "exchange_symbol": self.exchange_symbol,
            "canonical_symbol": self.canonical_symbol,
            "trade_id": self.trade_id,
            "price": str(self.price),
            "quantity_base": str(self.quantity_base),
            "notional_usd": str(self.notional_usd) if self.notional_usd is not None else None,
            "aggressor_side": self.aggressor_side.value,
            "raw_side": self.raw_side,
            "raw_side_semantics": self.raw_side_semantics,
            "side_source": self.side_source.value,
            "exchange_timestamp": self.exchange_timestamp.isoformat(),
            "received_at": self.received_at.isoformat(),
            "processed_at": self.processed_at.isoformat(),
            "source_channel": self.source_channel,
            "status": self.status.value,
            "raw_payload": _jsonable(self.raw_payload) if self.raw_payload is not None else None,
            "raw_reference": self.raw_reference,
        }
