"""Canonical Phase 8 options data contracts; provider fields stop at adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Any, Mapping


class DataStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"


class Provenance(StrEnum):
    SOURCE_PROVIDED = "SOURCE_PROVIDED"
    COMPUTED = "COMPUTED"


class UnitStatus(StrEnum):
    VERIFIED = "VERIFIED"
    SOURCE_NATIVE_UNVERIFIED = "SOURCE_NATIVE_UNVERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class ObservationKind(StrEnum):
    REST_CHAIN_SUMMARY = "REST_CHAIN_SUMMARY"
    WS_MARKPRICE_SNAPSHOT = "WS_MARKPRICE_SNAPSHOT"
    WS_MARKPRICE_CHANGE = "WS_MARKPRICE_CHANGE"
    WS_INCREMENTAL_TICKER_SNAPSHOT = "WS_INCREMENTAL_TICKER_SNAPSHOT"
    WS_INCREMENTAL_TICKER_CHANGE = "WS_INCREMENTAL_TICKER_CHANGE"


class InstrumentEventType(StrEnum):
    CREATION = "CREATION"
    STATE = "STATE"


class TimestampSemantics(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    NOT_PROVIDED = "NOT_PROVIDED"


def _enum(value: Any, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not a supported value") from exc


def _utc(value: datetime | None, field_name: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal | None, field_name: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal; float/string coercion is forbidden")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return value


def decimal_from_json(value: Decimal | int | str) -> Decimal:
    """Convert a JSON numeric token without passing through binary float."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("JSON decimal input must not be a bool or float")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError("JSON decimal input must be Decimal, integer token, or string")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("JSON decimal input is invalid") from exc
    if not result.is_finite():
        raise ValueError("JSON decimal input must be finite")
    return result


def _reject_json_constant(_: str) -> Any:
    raise ValueError("non-finite JSON numeric constant is not supported")


def loads_decimal_json(payload: str | bytes | bytearray) -> Any:
    """Decode JSON while preserving decimal tokens as ``Decimal`` values."""
    return json.loads(payload, parse_float=Decimal, parse_constant=_reject_json_constant)


def _identity(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value


def _metric_map(values: Mapping[str, Any], value_type: type, field_name: str) -> Mapping[str, Any]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError(f"{field_name} must be a non-empty mapping")
    copied = dict(values)
    for key, value in copied.items():
        if not isinstance(key, str) or not key or not isinstance(value, value_type):
            raise ValueError(f"{field_name} contains an invalid metric entry")
        if getattr(value, "metric", None) != key:
            raise ValueError(f"{field_name} key must match the metric name")
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class OptionMetricValue:
    metric: str
    value: Decimal | None
    source: str = "deribit"
    exchange: str = "DERIBIT"
    source_field: str | None = None
    source_method_or_channel: str | None = None
    exchange_timestamp: datetime | None = None
    field_last_updated_at: datetime | None = None
    fetched_at: datetime | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None
    timestamp_semantics: TimestampSemantics | str = TimestampSemantics.UNVERIFIED
    unit_code: str | None = None
    unit_status: UnitStatus | str = UnitStatus.UNKNOWN
    status: DataStatus | str | None = None
    provenance: Provenance | str = Provenance.SOURCE_PROVIDED
    quality_reason: str | None = None
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        _identity(self.metric, "metric")
        _identity(self.source, "source")
        _identity(self.exchange, "exchange")
        object.__setattr__(self, "source_field", self.source_field or self.metric)
        object.__setattr__(self, "value", _decimal(self.value, "value", nullable=True))
        for name in ("exchange_timestamp", "field_last_updated_at", "fetched_at", "received_at", "processed_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name, nullable=True))
        object.__setattr__(self, "timestamp_semantics", _enum(self.timestamp_semantics, TimestampSemantics, "timestamp_semantics"))
        object.__setattr__(self, "unit_status", _enum(self.unit_status, UnitStatus, "unit_status"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))
        status = self.status
        if status is None:
            status = DataStatus.AVAILABLE if self.value is not None else DataStatus.NOT_AVAILABLE
        status = _enum(status, DataStatus, "status")
        object.__setattr__(self, "status", status)
        if self.value is None and status is DataStatus.AVAILABLE:
            raise ValueError("status cannot be AVAILABLE when value is null")
        if self.value is None and not self.quality_reason:
            object.__setattr__(self, "quality_reason", "MISSING_FIELD")


@dataclass(frozen=True, slots=True)
class OptionInstrument:
    exchange: str
    source: str
    symbol: str
    underlying: str
    option_type: OptionType | str
    strike: Decimal
    expires_at: datetime
    instrument_created_at: datetime | None
    instrument_state: str
    is_active: bool
    price_index: str
    base_currency: str
    quote_currency: str
    settlement_currency: str
    exchange_timestamp: datetime | None
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus | str = DataStatus.AVAILABLE
    provider_instrument_id: int | None = None
    source_field: str = "result[]"
    timestamp_semantics: TimestampSemantics | str = TimestampSemantics.NOT_PROVIDED
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("exchange", "source", "symbol", "underlying", "instrument_state", "price_index",
                     "base_currency", "quote_currency", "settlement_currency"):
            _identity(getattr(self, name), name)
        if self.underlying not in {"BTC", "ETH"}:
            raise ValueError("underlying must be BTC or ETH")
        object.__setattr__(self, "option_type", _enum(self.option_type, OptionType, "option_type"))
        object.__setattr__(self, "strike", _decimal(self.strike, "strike"))
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        for name in ("expires_at", "fetched_at", "processed_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        for name in ("instrument_created_at", "exchange_timestamp"):
            object.__setattr__(self, name, _utc(getattr(self, name), name, nullable=True))
        if not isinstance(self.is_active, bool):
            raise ValueError("is_active must be boolean")
        if self.provider_instrument_id is not None and (
            isinstance(self.provider_instrument_id, bool) or not isinstance(self.provider_instrument_id, int)
        ):
            raise ValueError("provider_instrument_id must be an integer")
        object.__setattr__(self, "status", _enum(self.status, DataStatus, "status"))
        object.__setattr__(self, "timestamp_semantics", _enum(self.timestamp_semantics, TimestampSemantics, "timestamp_semantics"))


@dataclass(frozen=True, slots=True)
class OptionMarketObservation:
    exchange: str
    source: str
    symbol: str
    underlying: str
    observation_kind: ObservationKind | str
    metrics: Mapping[str, OptionMetricValue]
    exchange_timestamp: datetime | None
    fetched_at: datetime | None
    received_at: datetime | None
    processed_at: datetime
    status: DataStatus | str
    price_index: str | None = None
    underlying_index: str | None = None
    quote_currency: str | None = None
    observation_id: str | None = None
    raw_reference: str | None = None
    schema_version: str = "phase8.v1"

    def __post_init__(self) -> None:
        for name in ("exchange", "source", "symbol", "underlying", "schema_version"):
            _identity(getattr(self, name), name)
        for name in ("price_index", "underlying_index", "quote_currency"):
            value = getattr(self, name)
            if value is not None:
                _identity(value, name)
        object.__setattr__(self, "observation_kind", _enum(self.observation_kind, ObservationKind, "observation_kind"))
        object.__setattr__(self, "metrics", _metric_map(self.metrics, OptionMetricValue, "metrics"))
        for name in ("exchange_timestamp", "fetched_at", "received_at", "processed_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name, nullable=True))
        kind = self.observation_kind
        if kind is ObservationKind.REST_CHAIN_SUMMARY:
            if self.fetched_at is None or self.received_at is not None:
                raise ValueError("REST observations require fetched_at and must not set received_at")
        elif self.received_at is None or self.fetched_at is not None:
            raise ValueError("WebSocket observations require received_at and must not set fetched_at")
        object.__setattr__(self, "status", _enum(self.status, DataStatus, "status"))


@dataclass(frozen=True, slots=True)
class OptionInstrumentEvent:
    exchange: str
    source: str
    symbol: str
    event_type: InstrumentEventType | str
    instrument_state: str
    exchange_timestamp: datetime | None
    received_at: datetime
    processed_at: datetime
    status: DataStatus | str
    raw_reference: str | None = None
    event_identity: str | None = None

    def __post_init__(self) -> None:
        for name in ("exchange", "source", "symbol", "instrument_state"):
            _identity(getattr(self, name), name)
        object.__setattr__(self, "event_type", _enum(self.event_type, InstrumentEventType, "event_type"))
        for name in ("exchange_timestamp", "received_at", "processed_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name, nullable=True))
        if self.received_at is None or self.processed_at is None:
            raise ValueError("received_at and processed_at are required")
        object.__setattr__(self, "status", _enum(self.status, DataStatus, "status"))


@dataclass(frozen=True, slots=True)
class OptionContextMetric:
    metric: str
    value: Decimal | None
    source_timestamps: tuple[datetime, ...]
    source_fetched_at: datetime | None
    source_received_at: datetime | None
    data_age_seconds: int | None
    coverage_expected: int
    coverage_available: int
    status: DataStatus | str
    quality_reason: str | None
    provenance: Provenance | str
    unit_code: str | None = None
    unit_status: UnitStatus | str = UnitStatus.UNKNOWN
    input_observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identity(self.metric, "metric")
        object.__setattr__(self, "value", _decimal(self.value, "value", nullable=True))
        object.__setattr__(self, "source_timestamps", tuple(_utc(ts, "source_timestamp") for ts in self.source_timestamps))
        for name in ("source_fetched_at", "source_received_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name, nullable=True))
        if self.data_age_seconds is not None and self.data_age_seconds < 0:
            raise ValueError("data_age_seconds must be non-negative")
        if self.coverage_expected < 0 or self.coverage_available < 0 or self.coverage_available > self.coverage_expected:
            raise ValueError("coverage counts are invalid")
        object.__setattr__(self, "status", _enum(self.status, DataStatus, "status"))
        object.__setattr__(self, "provenance", _enum(self.provenance, Provenance, "provenance"))
        object.__setattr__(self, "unit_status", _enum(self.unit_status, UnitStatus, "unit_status"))
        if self.status is DataStatus.AVAILABLE and self.value is None:
            raise ValueError("AVAILABLE context metric must have a value")

    @property
    def coverage_ratio(self) -> Decimal:
        if self.coverage_expected == 0:
            return Decimal("0")
        with localcontext() as context:
            context.prec = 34
            return Decimal(self.coverage_available) / Decimal(self.coverage_expected)


@dataclass(frozen=True, slots=True)
class OptionContextSnapshot:
    underlying: str
    context_timestamp: datetime
    processed_at: datetime
    calculation_version: str
    metrics: Mapping[str, OptionContextMetric]
    snapshot_id: str | None = None
    source: str = "phase8.options_context"

    def __post_init__(self) -> None:
        if self.underlying not in {"BTC", "ETH"}:
            raise ValueError("underlying must be BTC or ETH")
        _identity(self.calculation_version, "calculation_version")
        _identity(self.source, "source")
        object.__setattr__(self, "context_timestamp", _utc(self.context_timestamp, "context_timestamp"))
        object.__setattr__(self, "processed_at", _utc(self.processed_at, "processed_at"))
        object.__setattr__(self, "metrics", _metric_map(self.metrics, OptionContextMetric, "metrics"))
