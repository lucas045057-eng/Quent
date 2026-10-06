"""Common public-trade adapter protocol and strict parsing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from quant_phase3.capabilities import TradeSourceCapabilities
from quant_phase3.contracts import CanonicalTrade


class AdapterSchemaError(ValueError):
    """Raised when a public response cannot satisfy its source contract."""


def require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterSchemaError(f"{field} must be an object")
    return value


def decimal_field(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdapterSchemaError(f"{field} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise AdapterSchemaError(f"{field} must be finite")
    return parsed


def epoch_milliseconds(value: Any, field: str) -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
        return datetime.fromtimestamp(parsed / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AdapterSchemaError(f"{field} must be epoch milliseconds") from exc


def required_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdapterSchemaError(f"{field} must be a non-empty string")
    return value


@runtime_checkable
class PublicTradeAdapter(Protocol):
    exchange: str
    capabilities: TradeSourceCapabilities

    def subscription(self, symbol: str) -> Mapping[str, Any]: ...

    def parse_ws_message(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Sequence[CanonicalTrade]: ...

    def parse_recent_trades(
        self,
        payload: Mapping[str, Any],
        received_at: datetime,
        *,
        exchange_symbol: str | None = None,
    ) -> Sequence[CanonicalTrade]: ...

    def identity_key(self, trade: CanonicalTrade) -> tuple[str, ...]: ...
