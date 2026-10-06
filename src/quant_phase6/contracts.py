"""Canonical Phase 6 external-event contracts.

The contracts intentionally contain normalized fields only.  Source-specific
payloads stay behind adapters and bounded references.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import re
from typing import Any

from quant_phase1.time import ensure_utc

from .sources import SourceType


class EventStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class Importance(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class RecipientCategory(StrEnum):
    TEAM = "TEAM"
    INVESTORS = "INVESTORS"
    TREASURY = "TREASURY"
    ECOSYSTEM = "ECOSYSTEM"
    COMMUNITY = "COMMUNITY"
    UNKNOWN = "UNKNOWN"


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_optional_utc(value: datetime | None, field: str) -> None:
    if value is not None:
        try:
            ensure_utc(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be UTC") from exc


@dataclass(frozen=True, slots=True)
class RawReference:
    reference: str
    content_hash: str
    byte_size: int | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("raw reference is required")
        if not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        if self.byte_size is not None and not 0 <= self.byte_size <= 8 * 1024 * 1024:
            raise ValueError("raw reference byte_size is unbounded")
        _check_optional_utc(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class Provenance:
    source_id: str
    source_ref: str
    url: str | None
    content_hash: str
    observed_at: datetime
    fetched_at: datetime
    processed_at: datetime
    parser_version: str
    schema_version: str
    normalization_version: str
    model_id: str | None = None
    prompt_version: str | None = None
    extraction_version: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.source_ref.strip():
            raise ValueError("source identity is required")
        if not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("provenance content_hash must be SHA-256")
        for field, value in (
            ("observed_at", self.observed_at),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _check_optional_utc(value, field)
        for field, value in (
            ("parser_version", self.parser_version),
            ("schema_version", self.schema_version),
            ("normalization_version", self.normalization_version),
        ):
            if not value.strip():
                raise ValueError(f"{field} is required")


@dataclass(frozen=True, slots=True)
class ExternalEvent:
    event_id: str
    source: str
    source_type: SourceType
    source_ref: str
    url: str | None
    published_at: datetime | None
    observed_at: datetime
    event_at: datetime | None
    fetched_at: datetime
    processed_at: datetime
    event_type: str
    entities: tuple[str, ...]
    symbols: tuple[str, ...]
    summary: str
    importance: Importance
    status: EventStatus
    confidence: Decimal | None
    content_hash: str
    parser_version: str
    raw_reference: RawReference | None
    provenance: Provenance
    reason_code: str | None = None
    sentiment: str | None = None

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.source.strip() or not self.source_ref.strip():
            raise ValueError("event and source identity are required")
        if not self.event_type.strip():
            raise ValueError("event_type is required")
        if len(self.summary.encode("utf-8")) > 8192:
            raise ValueError("summary exceeds bounded size")
        if not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        for field, value in (
            ("published_at", self.published_at),
            ("observed_at", self.observed_at),
            ("event_at", self.event_at),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _check_optional_utc(value, field)
        if self.confidence is not None and not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        if self.provenance.source_id != self.source or self.provenance.content_hash != self.content_hash:
            raise ValueError("provenance does not match event identity")
        if self.raw_reference is not None and self.raw_reference.content_hash != self.content_hash:
            raise ValueError("raw reference does not match event content hash")
        if self.status in {EventStatus.NOT_AVAILABLE, EventStatus.ERROR} and self.reason_code is None:
            raise ValueError(f"{self.status.value} event requires reason_code")


@dataclass(frozen=True, slots=True)
class NewsEvent(ExternalEvent):
    headline: str = ""
    impact_horizon: str | None = None

    def __post_init__(self) -> None:
        ExternalEvent.__post_init__(self)
        if not self.headline.strip():
            raise ValueError("headline is required")
        if len(self.headline.encode("utf-8")) > 2048:
            raise ValueError("headline exceeds bounded size")


@dataclass(frozen=True, slots=True)
class MacroEvent(ExternalEvent):
    macro_event_type: str = ""
    region: str = ""
    scheduled_at: datetime | None = None
    released_at: datetime | None = None
    actual: Decimal | None = None
    forecast: Decimal | None = None
    previous: Decimal | None = None
    unit: str | None = None
    surprise: Decimal | None = None

    def __post_init__(self) -> None:
        ExternalEvent.__post_init__(self)
        if not self.macro_event_type.strip() or not self.region.strip():
            raise ValueError("macro_event_type and region are required")
        _check_optional_utc(self.scheduled_at, "scheduled_at")
        _check_optional_utc(self.released_at, "released_at")
        values = (self.actual, self.forecast, self.previous, self.surprise)
        if any(value is not None for value in values) and not self.unit:
            raise ValueError("unit is required for macro numeric values")
        if self.status in {EventStatus.NOT_AVAILABLE, EventStatus.ERROR} and any(
            value is not None for value in values
        ):
            raise ValueError("unavailable macro event cannot carry numeric values")


@dataclass(frozen=True, slots=True)
class UnlockEvent(ExternalEvent):
    symbol: str = ""
    asset: str = ""
    amount: Decimal | None = None
    amount_unit: str | None = None
    value: Decimal | None = None
    value_currency: str | None = None
    value_at: datetime | None = None
    circulating_supply: Decimal | None = None
    circulating_supply_unit: str | None = None
    circulating_supply_ref: str | None = None
    unlock_pct: Decimal | None = None
    recipient_category: RecipientCategory = RecipientCategory.UNKNOWN

    def __post_init__(self) -> None:
        ExternalEvent.__post_init__(self)
        if not self.symbol.strip() or not self.asset.strip():
            raise ValueError("symbol and asset are required")
        for field, value in (("amount", self.amount), ("value", self.value), ("circulating_supply", self.circulating_supply)):
            if value is not None and value < 0:
                raise ValueError(f"{field} cannot be negative")
        _check_optional_utc(self.value_at, "value_at")
        if self.amount is not None and not self.amount_unit:
            raise ValueError("amount_unit is required with amount")
        if self.value is not None and not self.value_currency:
            raise ValueError("value_currency is required with value")
        if self.circulating_supply is not None and not self.circulating_supply_unit:
            raise ValueError("circulating_supply_unit is required with supply")
        if self.unlock_pct is not None:
            if self.amount is None or self.circulating_supply in (None, Decimal("0")):
                raise ValueError("unlock_pct requires amount and circulating supply")
            if self.amount_unit != self.circulating_supply_unit:
                raise ValueError("unlock_pct requires compatible supply units")
            if self.event_at is None:
                raise ValueError("unlock_pct requires event_at")
            if not Decimal("0") <= self.unlock_pct <= Decimal("1"):
                raise ValueError("unlock_pct must be between 0 and 1")
        if self.status in {EventStatus.NOT_AVAILABLE, EventStatus.ERROR} and any(
            value is not None for value in (self.amount, self.value, self.circulating_supply, self.unlock_pct)
        ):
            raise ValueError("unavailable unlock event cannot carry numeric values")


def compute_macro_surprise(
    actual: Decimal | None,
    forecast: Decimal | None,
    *,
    unit: str | None,
    forecast_unit: str | None,
) -> Decimal | None:
    """Compute a deterministic surprise only for compatible units."""
    if actual is None or forecast is None or not unit or unit != forecast_unit:
        return None
    return actual - forecast
