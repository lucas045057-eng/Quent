"""Canonical Phase 2 OI/Funding contracts.

Exchange-specific fields belong in adapters. These contracts are the only
objects consumed by normalization, aggregation, persistence, or enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from quant_phase1.time import ensure_utc


class DataStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class ContractType(StrEnum):
    PERPETUAL = "PERPETUAL"


class FundingClassification(StrEnum):
    NORMAL = "NORMAL"
    LONG_CROWDED = "LONG_CROWDED"
    SHORT_CROWDED = "SHORT_CROWDED"
    EXTREME_POSITIVE = "EXTREME_POSITIVE"
    EXTREME_NEGATIVE = "EXTREME_NEGATIVE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


def _utc(value: datetime | None, field_name: str) -> None:
    if value is not None:
        try:
            ensure_utc(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be UTC") from exc


@dataclass(frozen=True, slots=True)
class CanonicalSymbol:
    canonical_symbol: str
    base_asset: str
    quote_asset: str
    settle_asset: str
    contract_type: ContractType
    exchange_symbols: Mapping[str, str]
    is_crypto_perpetual: bool = True

    def __post_init__(self) -> None:
        if not self.is_crypto_perpetual:
            raise ValueError("non-crypto instruments are outside the Phase 2 pool")
        if not self.canonical_symbol or not self.exchange_symbols:
            raise ValueError("canonical symbol and explicit exchange mappings are required")


@dataclass(frozen=True, slots=True)
class InstrumentMetadata:
    exchange: str
    exchange_symbol: str
    canonical_symbol: str | None
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    contract_type: ContractType
    margin_asset: str | None
    contract_multiplier: Decimal | None
    contract_size: Decimal | None
    funding_interval_seconds: int | None
    mark_price: Decimal | None
    source_endpoint: str
    fetched_at: datetime
    exchange_timestamp: datetime | None
    raw_payload: Mapping[str, Any]
    status: DataStatus = DataStatus.AVAILABLE

    def __post_init__(self) -> None:
        _utc(self.fetched_at, "fetched_at")
        _utc(self.exchange_timestamp, "exchange_timestamp")
        if self.funding_interval_seconds is not None and self.funding_interval_seconds <= 0:
            raise ValueError("funding_interval_seconds must be positive")


@dataclass(frozen=True, slots=True)
class OIObservation:
    symbol: str
    canonical_symbol: str | None
    exchange: str
    contract_type: ContractType
    margin_asset: str | None
    settle_asset: str | None
    raw_open_interest: Decimal | None
    raw_unit: str
    open_interest_base: Decimal | None
    open_interest_quote: Decimal | None
    open_interest_usd: Decimal | None
    mark_price: Decimal | None
    normalization_method: str | None
    exchange_timestamp: datetime | None
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus
    source_endpoint: str
    raw_payload: Mapping[str, Any]
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        _utc(self.exchange_timestamp, "exchange_timestamp")
        _utc(self.fetched_at, "fetched_at")
        _utc(self.processed_at, "processed_at")
        if self.status in {DataStatus.NOT_AVAILABLE, DataStatus.ERROR} and any(
            value is not None for value in (self.open_interest_base, self.open_interest_quote, self.open_interest_usd)
        ):
            raise ValueError(f"{self.status} OI cannot have normalized values")
        if any(value is not None and value < 0 for value in (self.raw_open_interest, self.open_interest_base, self.open_interest_quote, self.open_interest_usd)):
            raise ValueError("open interest values cannot be negative")


@dataclass(frozen=True, slots=True)
class FundingObservation:
    symbol: str
    canonical_symbol: str | None
    exchange: str
    contract_type: ContractType
    funding_rate: Decimal | None
    funding_interval_seconds: int | None
    normalized_8h_rate: Decimal | None
    predicted_funding_rate: Decimal | None
    realized_funding_rate: Decimal | None
    next_funding_time: datetime | None
    exchange_timestamp: datetime | None
    fetched_at: datetime
    processed_at: datetime
    status: DataStatus
    source_endpoint: str
    raw_payload: Mapping[str, Any]
    raw_reference: str | None = None
    classification: FundingClassification | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("next_funding_time", self.next_funding_time),
            ("exchange_timestamp", self.exchange_timestamp),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _utc(value, name)
        if self.funding_interval_seconds is not None and self.funding_interval_seconds <= 0:
            raise ValueError("funding_interval_seconds must be positive")
        if self.status in {DataStatus.NOT_AVAILABLE, DataStatus.ERROR} and any(
            value is not None for value in (self.normalized_8h_rate, self.predicted_funding_rate, self.realized_funding_rate)
        ):
            raise ValueError(f"{self.status} funding cannot have normalized/semantic values")


@dataclass(frozen=True, slots=True)
class OIChange:
    canonical_symbol: str
    exchange: str
    requested_window_seconds: int
    actual_old_timestamp: datetime | None
    actual_new_timestamp: datetime | None
    old_value_usd: Decimal | None
    new_value_usd: Decimal | None
    change_absolute_usd: Decimal | None
    change_percent: Decimal | None
    status: DataStatus
    reason: str | None = None

    def __post_init__(self) -> None:
        _utc(self.actual_old_timestamp, "actual_old_timestamp")
        _utc(self.actual_new_timestamp, "actual_new_timestamp")
        if self.requested_window_seconds <= 0:
            raise ValueError("requested_window_seconds must be positive")


@dataclass(frozen=True, slots=True)
class CrossExchangeSnapshot:
    canonical_symbol: str
    timestamp: datetime
    exchanges: tuple[OIObservation | FundingObservation, ...] = field(default_factory=tuple)
    oi_exchange_count: int = 0
    funding_exchange_count: int = 0
    oi_total_usd: Decimal | None = None
    oi_weighted_funding: Decimal | None = None
    median_funding: Decimal | None = None
    max_funding: Decimal | None = None
    min_funding: Decimal | None = None
    funding_dispersion: Decimal | None = None
    oi_changes: tuple[OIChange, ...] = field(default_factory=tuple)
    status: DataStatus = DataStatus.AVAILABLE
    reason: str | None = None

    def __post_init__(self) -> None:
        _utc(self.timestamp, "timestamp")
