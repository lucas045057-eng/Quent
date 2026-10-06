"""Canonical, validated contracts for Phase 4 public-market metrics."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from numbers import Real
from typing import Any, TypeAlias


Number: TypeAlias = int | float | Decimal


class DataStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class LiquidationSide(str, Enum):
    LIQUIDATED_LONG = "LIQUIDATED_LONG"
    LIQUIDATED_SHORT = "LIQUIDATED_SHORT"
    UNKNOWN = "UNKNOWN"


class QuantityUnit(str, Enum):
    QUOTE_COIN = "QUOTE_COIN"
    BASE_ASSET = "BASE_ASSET"
    CONTRACTS = "CONTRACTS"
    UNKNOWN = "UNKNOWN"


class SourceGranularity(str, Enum):
    AGGREGATED_MAX_PER_SECOND = "AGGREGATED_MAX_PER_SECOND"
    ALL_LIQUIDATIONS_STREAM = "ALL_LIQUIDATIONS_STREAM"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class CoverageSemantics(str, Enum):
    PARTIAL_AGGREGATED = "PARTIAL_AGGREGATED"
    EXCHANGE_DECLARED_ALL_LIQUIDATIONS = "EXCHANGE_DECLARED_ALL_LIQUIDATIONS"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class LongShortMetricType(str, Enum):
    ACCOUNT_HOLDER_RATIO = "ACCOUNT_HOLDER_RATIO"


class LongShortPopulationSemantics(str, Enum):
    HOLDER_COUNT_RATIO = "HOLDER_COUNT_RATIO"
    ALL_POSITION_HOLDER_ACCOUNT_RATIO = "ALL_POSITION_HOLDER_ACCOUNT_RATIO"
    UNCONFIRMED_PUBLIC_SOURCE = "UNCONFIRMED_PUBLIC_SOURCE"


class BasisType(str, Enum):
    MARK_INDEX = "MARK_INDEX"
    MARK_ORACLE = "MARK_ORACLE"


class ReasonCode(str, Enum):
    UNCONFIRMED_SEMANTICS = "UNCONFIRMED_SEMANTICS"
    UNCONFIRMED_PUBLIC_SOURCE = "UNCONFIRMED_PUBLIC_SOURCE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be explicit")


def _require_positive(value: Number | None, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        raise ValueError(f"{field_name} must be positive when supplied")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite when supplied")
    if decimal_value <= 0:
        raise ValueError(f"{field_name} must be positive when supplied")


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field_name} must be explicit")


def _require_reason_code(value: ReasonCode | None) -> None:
    if value is not None and not isinstance(value, ReasonCode):
        raise ValueError("reason_code must be a supported reason code")


@dataclass(frozen=True)
class CanonicalLiquidation:
    event_id: str
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    event_timestamp: datetime
    received_at: datetime
    processed_at: datetime
    side: LiquidationSide
    raw_side: str | None
    raw_side_semantics: str | None
    price: Number | None
    raw_quantity: Number | None
    quantity_unit: QuantityUnit
    quantity_base: Number | None
    notional_usd: Number | None
    source_endpoint: str
    source_channel: str
    source_granularity: SourceGranularity
    coverage_semantics: CoverageSemantics
    status: DataStatus
    raw_reference: str | None
    raw_payload: Any
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.exchange, "exchange"),
            (self.exchange_symbol, "exchange_symbol"),
            (self.canonical_symbol, "canonical_symbol"),
            (self.source_endpoint, "source_endpoint"),
            (self.source_channel, "source_channel"),
        ):
            _require_non_empty(value, name)
        for value, name in (
            (self.event_timestamp, "event_timestamp"),
            (self.received_at, "received_at"),
            (self.processed_at, "processed_at"),
        ):
            _require_utc(value, name)
        for value, enum_type, name in (
            (self.side, LiquidationSide, "side"),
            (self.quantity_unit, QuantityUnit, "quantity_unit"),
            (self.source_granularity, SourceGranularity, "source_granularity"),
            (self.coverage_semantics, CoverageSemantics, "coverage_semantics"),
            (self.status, DataStatus, "status"),
        ):
            _require_enum(value, enum_type, name)
        for value, name in (
            (self.price, "price"),
            (self.raw_quantity, "raw_quantity"),
            (self.quantity_base, "quantity_base"),
            (self.notional_usd, "notional_usd"),
        ):
            _require_positive(value, name)
        _require_reason_code(self.reason_code)
        if self.quantity_unit is QuantityUnit.UNKNOWN and (
            self.quantity_base is not None or self.notional_usd is not None
        ):
            raise ValueError("unverified quantity units cannot have normalized values")
        if self.status in {DataStatus.NOT_AVAILABLE, DataStatus.ERROR} and any(
            value is not None for value in (self.price, self.quantity_base, self.notional_usd)
        ):
            raise ValueError("NOT_AVAILABLE and ERROR rows cannot have normalized values")


@dataclass(frozen=True)
class LongShortObservation:
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    metric_type: LongShortMetricType
    population_semantics: LongShortPopulationSemantics
    period: str
    long_value: Number | None
    short_value: Number | None
    ratio: Number | None
    exchange_timestamp: datetime
    fetched_at: datetime
    received_at: datetime
    processed_at: datetime
    source_endpoint: str
    status: DataStatus
    raw_reference: str | None
    raw_payload: Any
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.exchange, "exchange"),
            (self.exchange_symbol, "exchange_symbol"),
            (self.canonical_symbol, "canonical_symbol"),
            (self.period, "period"),
            (self.source_endpoint, "source_endpoint"),
        ):
            _require_non_empty(value, name)
        for value, name in (
            (self.exchange_timestamp, "exchange_timestamp"),
            (self.fetched_at, "fetched_at"),
            (self.received_at, "received_at"),
            (self.processed_at, "processed_at"),
        ):
            _require_utc(value, name)
        _require_enum(self.metric_type, LongShortMetricType, "metric_type")
        _require_enum(self.population_semantics, LongShortPopulationSemantics, "population_semantics")
        _require_enum(self.status, DataStatus, "status")
        for value, name in (
            (self.long_value, "long_value"),
            (self.short_value, "short_value"),
            (self.ratio, "ratio"),
        ):
            _require_positive(value, name)
        _require_reason_code(self.reason_code)
        if self.status in {DataStatus.NOT_AVAILABLE, DataStatus.ERROR} and any(
            value is not None for value in (self.long_value, self.short_value, self.ratio)
        ):
            raise ValueError("NOT_AVAILABLE and ERROR rows cannot have normalized values")


@dataclass(frozen=True)
class BasisObservation:
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    basis_type: BasisType
    perpetual_price: Number | None
    reference_price: Number | None
    absolute_basis: Number | None
    basis_bps: Number | None
    basis_pct: Number | None
    exchange_timestamp: datetime
    fetched_at: datetime
    received_at: datetime
    processed_at: datetime
    max_timestamp_skew: timedelta
    timestamp_skew: timedelta
    source_endpoint: str
    status: DataStatus
    raw_reference: str | None
    raw_payload: Any
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.exchange, "exchange"),
            (self.exchange_symbol, "exchange_symbol"),
            (self.canonical_symbol, "canonical_symbol"),
            (self.source_endpoint, "source_endpoint"),
        ):
            _require_non_empty(value, name)
        for value, name in (
            (self.exchange_timestamp, "exchange_timestamp"),
            (self.fetched_at, "fetched_at"),
            (self.received_at, "received_at"),
            (self.processed_at, "processed_at"),
        ):
            _require_utc(value, name)
        _require_enum(self.basis_type, BasisType, "basis_type")
        _require_enum(self.status, DataStatus, "status")
        _require_positive(self.perpetual_price, "perpetual_price")
        _require_positive(self.reference_price, "reference_price")
        if self.max_timestamp_skew < timedelta(0) or self.timestamp_skew < timedelta(0):
            raise ValueError("timestamp skew cannot be negative")
        _require_reason_code(self.reason_code)
        if self.status in {DataStatus.NOT_AVAILABLE, DataStatus.ERROR} and any(
            value is not None
            for value in (
                self.perpetual_price,
                self.reference_price,
                self.absolute_basis,
                self.basis_bps,
                self.basis_pct,
            )
        ):
            raise ValueError("NOT_AVAILABLE and ERROR rows cannot have normalized values")
