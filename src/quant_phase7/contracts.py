"""Immutable canonical asset and chain contracts for Phase 7."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext
from enum import StrEnum
import json
import re
from typing import TypeAlias


class Chain(StrEnum):
    BITCOIN = "BITCOIN"
    ETHEREUM = "ETHEREUM"


class AssetKind(StrEnum):
    NATIVE = "NATIVE"
    ERC20 = "ERC20"


class AliasMappingStatus(StrEnum):
    MAPPED = "MAPPED"
    UNKNOWN = "UNKNOWN"


class DataStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class FinalityStatus(StrEnum):
    OBSERVED = "OBSERVED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FINALIZED = "FINALIZED"
    REORGED = "REORGED"


class EventIndexKind(StrEnum):
    LOG_INDEX = "LOG_INDEX"
    VOUT_INDEX = "VOUT_INDEX"
    TX_VALUE = "TX_VALUE"


class MarketKind(StrEnum):
    SPOT = "SPOT"


class FeeGasSemantics(StrEnum):
    BITCOIN_INPUT_OUTPUT_DIFFERENCE = "BITCOIN_INPUT_OUTPUT_DIFFERENCE"
    ETHEREUM_RECEIPT_TRANSACTION_GAS = "ETHEREUM_RECEIPT_TRANSACTION_GAS"


class ReasonCode(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    STALE_DATA = "STALE_DATA"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"
    SOURCE_ERROR = "SOURCE_ERROR"
    VALUATION_NOT_AVAILABLE = "VALUATION_NOT_AVAILABLE"
    REORGED_EVENT = "REORGED_EVENT"


DetailValue: TypeAlias = str | int | bool | None

_EVM_CONTRACT_RE = re.compile(r"^0x[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RAW_AMOUNT_RE = re.compile(r"^[0-9]+$")
_MAX_RAW_REFERENCE_BYTES = 8 * 1024 * 1024
_MAX_REFERENCE_LENGTH = 2048
_MAX_DETAILS_BYTES = 8 * 1024
_MAX_DETAILS_ITEMS = 64
_MAX_BITCOIN_SATOSHIS = 21_000_000 * 100_000_000
_MAX_EVM_UINT256 = 2**256 - 1
_NUMERIC_INTEGER_DIGITS = 84
_NUMERIC_SCALE = 36


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field_name} must be a {enum_type.__name__}")


def _require_text(value: str, field_name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > max_length:
        raise ValueError(f"{field_name} exceeds bounded length")
    return normalized


def _optional_text(value: str | None, field_name: str, *, max_length: int = 256) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name, max_length=max_length)


def _require_utc(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC")


def _require_nonnegative_index(value: int | None, field_name: str, *, required: bool) -> None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _normalize_contract(value: str, field_name: str = "contract address") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an EVM contract address")
    normalized = value.strip().lower()
    if not _EVM_CONTRACT_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an EVM contract address")
    return normalized


def _normalize_block_hash(value: str, chain: Chain) -> str:
    if not isinstance(value, str):
        raise ValueError("block_hash is required")
    normalized = value.strip()
    if chain is Chain.BITCOIN:
        if not _HASH_RE.fullmatch(normalized):
            raise ValueError("Bitcoin block_hash must be lowercase 256-bit hex")
        return normalized
    if not normalized.startswith("0x") or len(normalized) != 66 or not all(
        char in "0123456789abcdef" for char in normalized[2:]
    ):
        raise ValueError("Ethereum block_hash must be lowercase 0x-prefixed 256-bit hex")
    return normalized


def _exact_decimal(raw: str, decimals: int) -> Decimal:
    if decimals == 0:
        return Decimal(raw)
    padded = raw.zfill(decimals + 1)
    return Decimal(f"{padded[:-decimals]}.{padded[-decimals:]}")


def _as_decimal(value: Decimal, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{field_name} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _require_numeric_120_36(value: Decimal, field_name: str) -> None:
    value = _strip_fractional_trailing_zeros(value)
    sign, digits, exponent = value.as_tuple()
    del sign
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if scale > _NUMERIC_SCALE or integer_digits > _NUMERIC_INTEGER_DIGITS:
        raise ValueError(f"{field_name} is incompatible with NUMERIC(120,36)")


def _strip_fractional_trailing_zeros(value: Decimal) -> Decimal:
    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while exponent < 0 and digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return Decimal(0)
    return Decimal((sign, tuple(digits), exponent))


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    precision = max(256, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 8)
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        product = left * right
    return _strip_fractional_trailing_zeros(product)


def _require_status_reason(status: DataStatus, reason_code: ReasonCode | None) -> None:
    _require_enum(status, DataStatus, "status")
    if reason_code is not None:
        _require_enum(reason_code, ReasonCode, "reason_code")
    if status is DataStatus.AVAILABLE and reason_code is not None:
        raise ValueError("AVAILABLE status cannot carry a reason_code")
    if status is not DataStatus.AVAILABLE and reason_code is None:
        raise ValueError(f"{status.value} status requires a non-empty reason_code")


def _require_event_status_reason(status: DataStatus, reason_code: ReasonCode | None) -> None:
    _require_enum(status, DataStatus, "status")
    if reason_code is None:
        if status is DataStatus.AVAILABLE:
            raise ValueError("AVAILABLE persisted event requires COMPLETE reason_code")
        raise ValueError("persisted event requires a non-null reason_code")
    _require_enum(reason_code, ReasonCode, "reason_code")
    if status is DataStatus.AVAILABLE and reason_code is not ReasonCode.COMPLETE:
        raise ValueError("AVAILABLE persisted event requires COMPLETE reason_code")
    if status is not DataStatus.AVAILABLE and reason_code is ReasonCode.COMPLETE:
        raise ValueError("degraded persisted event requires a non-COMPLETE reason_code")


@dataclass(frozen=True, slots=True)
class CanonicalAssetId:
    chain: Chain
    kind: AssetKind
    contract_or_native: str
    registry_version: str

    def __post_init__(self) -> None:
        _require_enum(self.chain, Chain, "chain")
        _require_enum(self.kind, AssetKind, "kind")
        object.__setattr__(
            self,
            "registry_version",
            _require_text(self.registry_version, "registry_version", max_length=128),
        )
        if self.chain is Chain.BITCOIN:
            if self.kind is not AssetKind.NATIVE:
                raise ValueError("Bitcoin supports only native assets in Phase 7 V1")
            if self.contract_or_native != "NATIVE":
                raise ValueError("Bitcoin native asset must use NATIVE")
            return
        if self.kind is AssetKind.NATIVE:
            if self.contract_or_native != "NATIVE":
                raise ValueError("native asset identity must use NATIVE")
            return
        if self.kind is AssetKind.ERC20:
            object.__setattr__(self, "contract_or_native", _normalize_contract(self.contract_or_native))
            return
        raise ValueError("unsupported asset identity")

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        return (
            self.chain.value,
            self.kind.value,
            self.contract_or_native,
            self.registry_version,
        )


@dataclass(frozen=True, slots=True)
class ChainCapabilities:
    chain: Chain
    block_transaction_data: DataStatus
    native_transfer: DataStatus
    allowlisted_token_transfer: DataStatus
    allowlisted_stablecoin_transfer: DataStatus
    address_balance: DataStatus
    bridge: DataStatus
    address_labels: DataStatus
    block_timestamp: DataStatus
    finality: DataStatus
    reorg_detection: DataStatus
    fee_gas: DataStatus
    fee_gas_semantics: FeeGasSemantics

    def __post_init__(self) -> None:
        _require_enum(self.chain, Chain, "chain")
        for field_name in (
            "block_transaction_data",
            "native_transfer",
            "allowlisted_token_transfer",
            "allowlisted_stablecoin_transfer",
            "address_balance",
            "bridge",
            "address_labels",
            "block_timestamp",
            "finality",
            "reorg_detection",
            "fee_gas",
        ):
            _require_enum(getattr(self, field_name), DataStatus, field_name)
        _require_enum(self.fee_gas_semantics, FeeGasSemantics, "fee_gas_semantics")

        unavailable = DataStatus.NOT_AVAILABLE
        expected = (
            (
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                unavailable,
                unavailable,
                unavailable,
                unavailable,
                DataStatus.PARTIAL,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                FeeGasSemantics.BITCOIN_INPUT_OUTPUT_DIFFERENCE,
            )
            if self.chain is Chain.BITCOIN
            else (
                DataStatus.AVAILABLE,
                DataStatus.PARTIAL,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                unavailable,
                unavailable,
                DataStatus.PARTIAL,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                DataStatus.AVAILABLE,
                FeeGasSemantics.ETHEREUM_RECEIPT_TRANSACTION_GAS,
            )
        )
        actual = (
            self.block_transaction_data,
            self.native_transfer,
            self.allowlisted_token_transfer,
            self.allowlisted_stablecoin_transfer,
            self.address_balance,
            self.bridge,
            self.address_labels,
            self.block_timestamp,
            self.finality,
            self.reorg_detection,
            self.fee_gas,
            self.fee_gas_semantics,
        )
        if actual != expected:
            raise ValueError("chain capability matrix must match the frozen Phase 7 V1 design")


def chain_capabilities(chain: Chain) -> ChainCapabilities:
    _require_enum(chain, Chain, "chain")
    unavailable = DataStatus.NOT_AVAILABLE
    if chain is Chain.BITCOIN:
        return ChainCapabilities(
            chain=chain,
            block_transaction_data=DataStatus.AVAILABLE,
            native_transfer=DataStatus.AVAILABLE,
            allowlisted_token_transfer=unavailable,
            allowlisted_stablecoin_transfer=unavailable,
            address_balance=unavailable,
            bridge=unavailable,
            address_labels=DataStatus.PARTIAL,
            block_timestamp=DataStatus.AVAILABLE,
            finality=DataStatus.AVAILABLE,
            reorg_detection=DataStatus.AVAILABLE,
            fee_gas=DataStatus.AVAILABLE,
            fee_gas_semantics=FeeGasSemantics.BITCOIN_INPUT_OUTPUT_DIFFERENCE,
        )
    return ChainCapabilities(
        chain=chain,
        block_transaction_data=DataStatus.AVAILABLE,
        native_transfer=DataStatus.PARTIAL,
        allowlisted_token_transfer=DataStatus.AVAILABLE,
        allowlisted_stablecoin_transfer=DataStatus.AVAILABLE,
        address_balance=unavailable,
        bridge=unavailable,
        address_labels=DataStatus.PARTIAL,
        block_timestamp=DataStatus.AVAILABLE,
        finality=DataStatus.AVAILABLE,
        reorg_detection=DataStatus.AVAILABLE,
        fee_gas=DataStatus.AVAILABLE,
        fee_gas_semantics=FeeGasSemantics.ETHEREUM_RECEIPT_TRANSACTION_GAS,
    )


@dataclass(frozen=True, slots=True)
class ExchangeAssetAlias:
    exchange: str
    exchange_asset: str
    registry_version: str
    status: AliasMappingStatus
    canonical_asset_id: CanonicalAssetId | None

    def __post_init__(self) -> None:
        for field_name in ("exchange", "exchange_asset", "registry_version"):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name, max_length=128),
            )
        _require_enum(self.status, AliasMappingStatus, "status")
        if self.status is AliasMappingStatus.UNKNOWN and self.canonical_asset_id is not None:
            raise ValueError("UNKNOWN mapping cannot carry a canonical asset identity")
        if self.status is AliasMappingStatus.MAPPED and not isinstance(
            self.canonical_asset_id, CanonicalAssetId
        ):
            raise ValueError("MAPPED alias requires a canonical asset identity")


@dataclass(frozen=True, slots=True)
class CanonicalAmount:
    asset_id: CanonicalAssetId
    amount_raw: str
    decimals: int
    amount_normalized: Decimal = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, CanonicalAssetId):
            raise ValueError("asset_id must be canonical")
        if not isinstance(self.amount_raw, str) or not _RAW_AMOUNT_RE.fullmatch(self.amount_raw):
            raise ValueError("amount_raw must contain only non-negative decimal digits")
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int):
            raise ValueError("decimals must be an integer")
        if self.asset_id.chain is Chain.BITCOIN:
            if self.decimals != 8:
                raise ValueError("Bitcoin decimals must be exactly 8")
        elif not 0 <= self.decimals <= _NUMERIC_SCALE:
            raise ValueError("V1 EVM decimals must be 0 through 36")

        canonical_raw = self.amount_raw.lstrip("0") or "0"
        integer_value = int(canonical_raw)
        if self.asset_id.chain is Chain.BITCOIN and integer_value > _MAX_BITCOIN_SATOSHIS:
            raise ValueError("amount_raw exceeds Bitcoin maximum supply in satoshis")
        if self.asset_id.chain is Chain.ETHEREUM and integer_value > _MAX_EVM_UINT256:
            raise ValueError("amount_raw exceeds EVM uint256")

        normalized = _exact_decimal(canonical_raw, self.decimals)
        _require_numeric_120_36(normalized, "amount_normalized")
        object.__setattr__(self, "amount_raw", canonical_raw)
        object.__setattr__(self, "amount_normalized", normalized)


@dataclass(frozen=True, slots=True)
class OnChainEventIdentity:
    chain: Chain
    tx_hash: str
    tx_index: int | None
    event_index_kind: EventIndexKind
    event_index: int | None
    asset_id: CanonicalAssetId
    contract_address: str | None
    block_hash: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.chain, Chain, "chain")
        _require_enum(self.event_index_kind, EventIndexKind, "event_index_kind")
        object.__setattr__(self, "tx_hash", _require_text(self.tx_hash, "tx_hash", max_length=128))
        if self.block_hash is None:
            raise ValueError("block_hash is required for canonical event identity")
        object.__setattr__(self, "block_hash", _normalize_block_hash(self.block_hash, self.chain))
        _require_nonnegative_index(self.tx_index, "tx_index", required=False)
        _require_nonnegative_index(self.event_index, "event_index", required=False)
        if not isinstance(self.asset_id, CanonicalAssetId) or self.asset_id.chain is not self.chain:
            raise ValueError("asset identity chain must match event chain")

        if self.chain is Chain.BITCOIN:
            if self.event_index_kind is not EventIndexKind.VOUT_INDEX:
                raise ValueError("Bitcoin events require VOUT_INDEX")
            if self.tx_index is not None:
                raise ValueError("Bitcoin events cannot carry tx_index")
            _require_nonnegative_index(self.event_index, "event_index", required=True)
            if self.asset_id.kind is not AssetKind.NATIVE or self.contract_address is not None:
                raise ValueError("Bitcoin output identity must use native BTC without a contract")
            return

        if self.event_index_kind is EventIndexKind.LOG_INDEX:
            _require_nonnegative_index(self.tx_index, "tx_index", required=True)
            _require_nonnegative_index(self.event_index, "event_index", required=True)
            if self.asset_id.kind is not AssetKind.ERC20 or self.contract_address is None:
                raise ValueError("Ethereum LOG_INDEX requires an ERC20 contract")
            normalized = _normalize_contract(self.contract_address)
            if normalized != self.asset_id.contract_or_native:
                raise ValueError("event contract does not match canonical asset identity")
            object.__setattr__(self, "contract_address", normalized)
            return

        if self.event_index_kind is EventIndexKind.TX_VALUE:
            _require_nonnegative_index(self.tx_index, "tx_index", required=True)
            if self.event_index is not None:
                raise ValueError("TX_VALUE requires event_index to be null")
            if self.asset_id.kind is not AssetKind.NATIVE:
                raise ValueError("TX_VALUE requires native ETH")
            if self.contract_address is not None:
                raise ValueError("TX_VALUE cannot have a contract address")
            return

        raise ValueError("Ethereum events cannot use VOUT_INDEX")

    @property
    def identity_key(self) -> tuple[str, str, int] | tuple[str, str, int, str]:
        block_suffix = (self.block_hash,) if self.block_hash is not None else ()
        if self.chain is Chain.BITCOIN:
            assert self.event_index is not None
            return (self.chain.value, self.tx_hash, self.event_index, *block_suffix)
        if self.event_index_kind is EventIndexKind.LOG_INDEX:
            assert self.event_index is not None and self.contract_address is not None
            return (self.chain.value, self.tx_hash, self.event_index, self.contract_address, *block_suffix)
        assert self.tx_index is not None
        return (self.chain.value, self.tx_hash, self.tx_index, "NATIVE", *block_suffix)


@dataclass(frozen=True, slots=True)
class RawReference:
    reference: str
    content_hash: str
    byte_size: int | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reference",
            _require_text(self.reference, "reference", max_length=_MAX_REFERENCE_LENGTH),
        )
        if not isinstance(self.content_hash, str) or not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")
        if self.byte_size is not None and (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 0 <= self.byte_size <= _MAX_RAW_REFERENCE_BYTES
        ):
            raise ValueError("raw reference byte_size is unbounded")
        if self.expires_at is not None:
            _require_utc(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str
    source_version: str
    source_reference: str
    source_hash: str
    observed_at: datetime
    fetched_at: datetime
    processed_at: datetime
    raw_reference: RawReference | None = None

    def __post_init__(self) -> None:
        for field_name, max_length in (
            ("source", 128),
            ("source_version", 128),
            ("source_reference", _MAX_REFERENCE_LENGTH),
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name, max_length=max_length),
            )
        if not isinstance(self.source_hash, str) or not _HASH_RE.fullmatch(self.source_hash):
            raise ValueError("source_hash must be a lowercase SHA-256 hex digest")
        for field_name in ("observed_at", "fetched_at", "processed_at"):
            _require_utc(getattr(self, field_name), field_name)
        if not self.observed_at <= self.fetched_at <= self.processed_at:
            raise ValueError("provenance timestamps must be ordered")
        if self.raw_reference is not None:
            if not isinstance(self.raw_reference, RawReference):
                raise ValueError("raw_reference must be bounded")
            if self.raw_reference.content_hash != self.source_hash:
                raise ValueError("raw_reference content_hash must match source_hash")


@dataclass(frozen=True, slots=True)
class Coverage:
    sample_size: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal
    known_address_count: int
    labeled_address_count: int
    label_coverage_ratio: Decimal | None
    source_count: int
    status: DataStatus
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "sample_size",
            "available_count",
            "missing_count",
            "known_address_count",
            "labeled_address_count",
            "source_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.available_count + self.missing_count != self.sample_size:
            raise ValueError("available_count + missing_count must equal sample_size")
        if self.labeled_address_count > self.known_address_count:
            raise ValueError("labeled_address_count cannot exceed known_address_count")

        ratio = _as_decimal(self.coverage_ratio, "coverage_ratio")
        if ratio > 1:
            raise ValueError("coverage_ratio must be between 0 and 1")
        expected_ratio = Decimal(self.available_count) / Decimal(self.sample_size) if self.sample_size else Decimal(0)
        if ratio != expected_ratio:
            raise ValueError("coverage_ratio does not match counts")

        if self.known_address_count == 0:
            if self.label_coverage_ratio is not None:
                raise ValueError("label_coverage_ratio must be absent without known addresses")
        else:
            if self.label_coverage_ratio is None:
                raise ValueError("label_coverage_ratio is required with known addresses")
            label_ratio = _as_decimal(self.label_coverage_ratio, "label_coverage_ratio")
            if label_ratio > 1:
                raise ValueError("label_coverage_ratio must be between 0 and 1")
            expected_label_ratio = Decimal(self.labeled_address_count) / Decimal(self.known_address_count)
            if label_ratio != expected_label_ratio:
                raise ValueError("label_coverage_ratio does not match address counts")
        _require_status_reason(self.status, self.reason_code)


@dataclass(frozen=True, slots=True)
class UsdValuation:
    asset_id: CanonicalAssetId
    event_time: datetime
    amount_usd: Decimal | None
    valuation_price: Decimal | None
    valuation_exchange: str | None
    valuation_source: str | None
    valuation_symbol: str | None
    market_kind: MarketKind
    valuation_exchange_timestamp: datetime | None
    valuation_fetched_at: datetime | None
    valuation_skew: timedelta | None
    max_valuation_skew: timedelta
    status: DataStatus
    reason_code: ReasonCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, CanonicalAssetId):
            raise ValueError("asset_id must be canonical")
        _require_utc(self.event_time, "event_time")
        _require_enum(self.market_kind, MarketKind, "market_kind")
        if self.market_kind is not MarketKind.SPOT:
            raise ValueError("Phase 7 USD valuation requires SPOT market kind")
        if not isinstance(self.max_valuation_skew, timedelta) or self.max_valuation_skew <= timedelta(0):
            raise ValueError("max_valuation_skew must be positive")
        _require_status_reason(self.status, self.reason_code)

        expected_symbol: str | None = None
        if self.asset_id.kind is AssetKind.NATIVE:
            expected_symbol = "BTCUSDT" if self.asset_id.chain is Chain.BITCOIN else "ETHUSDT"
        if expected_symbol is not None:
            if self.valuation_symbol != expected_symbol:
                raise ValueError(f"native asset valuation requires {expected_symbol}")
        elif self.status is DataStatus.AVAILABLE:
            raise ValueError("asset has no explicit Phase 7 V1 USD valuation mapping")

        evidence = (
            self.amount_usd,
            self.valuation_price,
            self.valuation_exchange,
            self.valuation_source,
            self.valuation_exchange_timestamp,
            self.valuation_fetched_at,
            self.valuation_skew,
        )
        if self.status is not DataStatus.AVAILABLE:
            if any(value is not None for value in evidence):
                raise ValueError("missing valuation cannot carry numeric or source evidence")
            return

        if any(value is None for value in evidence):
            raise ValueError("AVAILABLE valuation requires complete price evidence")
        assert self.amount_usd is not None and self.valuation_price is not None
        assert self.valuation_exchange is not None and self.valuation_source is not None
        assert self.valuation_exchange_timestamp is not None and self.valuation_fetched_at is not None
        assert self.valuation_skew is not None
        _as_decimal(self.amount_usd, "amount_usd")
        _as_decimal(self.valuation_price, "valuation_price", positive=True)
        _require_numeric_120_36(self.amount_usd, "amount_usd")
        _require_numeric_120_36(self.valuation_price, "valuation_price")
        object.__setattr__(
            self,
            "valuation_exchange",
            _require_text(self.valuation_exchange, "valuation_exchange", max_length=128),
        )
        object.__setattr__(
            self,
            "valuation_source",
            _require_text(self.valuation_source, "valuation_source", max_length=256),
        )
        _require_utc(self.valuation_exchange_timestamp, "valuation_exchange_timestamp")
        _require_utc(self.valuation_fetched_at, "valuation_fetched_at")
        if self.valuation_exchange_timestamp > self.event_time:
            raise ValueError("valuation_exchange_timestamp cannot be after event_time")
        if self.valuation_fetched_at > self.event_time:
            raise ValueError("valuation_fetched_at cannot be after event_time")
        if self.valuation_fetched_at < self.valuation_exchange_timestamp:
            raise ValueError("valuation_fetched_at cannot precede valuation_exchange_timestamp")
        expected_skew = self.event_time - self.valuation_exchange_timestamp
        if self.valuation_skew != expected_skew:
            raise ValueError("valuation_skew must match event and exchange timestamps")
        if self.valuation_skew > self.max_valuation_skew:
            raise ValueError("valuation_skew exceeds configured maximum")


def _normalize_details(details: tuple[tuple[str, DetailValue], ...]) -> tuple[tuple[str, DetailValue], ...]:
    if not isinstance(details, tuple) or len(details) > _MAX_DETAILS_ITEMS:
        raise ValueError("details must be a bounded immutable tuple")
    normalized: list[tuple[str, DetailValue]] = []
    seen: set[str] = set()
    for item in details:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("details entries must be key/value tuples")
        key, value = item
        key = _require_text(key, "details key", max_length=128)
        if key in seen:
            raise ValueError("details keys must be unique")
        if value is not None and (isinstance(value, float) or not isinstance(value, (str, int, bool))):
            raise ValueError("details values must be bounded scalar values")
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_DETAILS_BYTES:
            raise ValueError("details value exceeds bounded length")
        seen.add(key)
        normalized.append((key, value))
    canonical = tuple(normalized)
    serialized = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_DETAILS_BYTES:
        raise ValueError("details exceed bounded size")
    return canonical


@dataclass(frozen=True, slots=True)
class OnChainTransferEvent:
    event_id: str
    identity: OnChainEventIdentity
    block_number: int
    block_hash: str
    event_time: datetime
    from_address: str | None
    to_address: str | None
    from_address_set_reference: RawReference | None
    amount: CanonicalAmount
    valuation: UsdValuation
    provenance: Provenance
    raw_reference: RawReference | None
    schema_version: str
    normalization_version: str
    details: tuple[tuple[str, DetailValue], ...]
    status: DataStatus
    finality_status: FinalityStatus
    reason_code: ReasonCode

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _require_text(self.event_id, "event_id", max_length=256))
        if not isinstance(self.identity, OnChainEventIdentity):
            raise ValueError("identity must be canonical")
        if self.block_number is None or self.block_hash is None:
            raise ValueError("block_number and block_hash are required for every persisted event")
        _require_nonnegative_index(self.block_number, "block_number", required=True)
        object.__setattr__(
            self,
            "block_hash",
            _require_text(self.block_hash, "block_hash", max_length=128),
        )
        if self.identity.block_hash != self.block_hash:
            raise ValueError("event identity block_hash must match event block_hash")
        _require_utc(self.event_time, "event_time")
        object.__setattr__(self, "from_address", _optional_text(self.from_address, "from_address"))
        object.__setattr__(self, "to_address", _optional_text(self.to_address, "to_address"))
        if self.from_address_set_reference is not None:
            if not isinstance(self.from_address_set_reference, RawReference):
                raise ValueError("from_address_set_reference must be bounded")
            if self.identity.chain is not Chain.BITCOIN:
                raise ValueError("from_address_set_reference is Bitcoin-only")
        if not isinstance(self.amount, CanonicalAmount) or self.amount.asset_id != self.identity.asset_id:
            raise ValueError("amount asset must match event identity")
        if not isinstance(self.valuation, UsdValuation):
            raise ValueError("valuation must explicitly represent availability")
        if self.valuation.asset_id != self.amount.asset_id or self.valuation.event_time != self.event_time:
            raise ValueError("valuation must match event asset and event_time")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance is required")
        if self.raw_reference is not None:
            if not isinstance(self.raw_reference, RawReference):
                raise ValueError("raw_reference must be bounded")
            if self.raw_reference.content_hash != self.provenance.source_hash:
                raise ValueError("event raw_reference must match provenance source_hash")
        if self.provenance.raw_reference is not None:
            if self.raw_reference is None:
                object.__setattr__(self, "raw_reference", self.provenance.raw_reference)
            elif self.raw_reference != self.provenance.raw_reference:
                raise ValueError("event and provenance raw_reference must match")
        object.__setattr__(
            self,
            "schema_version",
            _require_text(self.schema_version, "schema_version", max_length=128),
        )
        object.__setattr__(
            self,
            "normalization_version",
            _require_text(self.normalization_version, "normalization_version", max_length=128),
        )
        object.__setattr__(self, "details", _normalize_details(self.details))
        _require_enum(self.finality_status, FinalityStatus, "finality_status")
        _require_event_status_reason(self.status, self.reason_code)

        if self.status in {DataStatus.ERROR, DataStatus.NOT_AVAILABLE, DataStatus.STALE} and (
            self.valuation.status is DataStatus.AVAILABLE
        ):
            raise ValueError(f"{self.status.value} event cannot carry AVAILABLE valuation")
        if self.valuation.status is DataStatus.AVAILABLE:
            assert self.valuation.valuation_price is not None
            assert self.valuation.amount_usd is not None
            expected_amount_usd = _exact_multiply(
                self.amount.amount_normalized,
                self.valuation.valuation_price,
            )
            _require_numeric_120_36(expected_amount_usd, "calculated amount_usd")
            if self.valuation.amount_usd != expected_amount_usd:
                raise ValueError("amount_usd must exactly equal amount_normalized * valuation_price")
        if self.finality_status is FinalityStatus.REORGED and not (
            self.status is DataStatus.STALE and self.reason_code is ReasonCode.REORGED_EVENT
        ):
            raise ValueError("REORGED finality requires STALE status and REORGED_EVENT reason")
        if self.finality_status is not FinalityStatus.REORGED and self.reason_code is ReasonCode.REORGED_EVENT:
            raise ValueError("REORGED_EVENT reason requires REORGED finality")
