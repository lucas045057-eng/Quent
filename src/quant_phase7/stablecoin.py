"""Allowlisted Ethereum USDT/USDC stablecoin classification.

Stablecoin semantics are context-only.  This module never turns a transfer into
a trading decision and never treats an unknown endpoint as an exchange or
issuer without an explicit registry/label input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
import json
import re
from itertools import islice
from typing import Any, Iterable, Mapping


from .contracts import DataStatus, FinalityStatus
from .labels import MIN_LABEL_COVERAGE, LabelCategory, LabelSnapshot


ZERO_ADDRESS = "0x" + "0" * 40
BURNER_ZERO_ADDRESS = ZERO_ADDRESS
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_ALLOWED_SYMBOLS = frozenset({"USDT", "USDC"})
_TIMEFRAME_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
    "4H": timedelta(hours=4),
}
_MAX_TRANSFERS = 1_000


class StablecoinError(ValueError):
    """Stablecoin registry or classification contract violation."""


class StablecoinCategory(StrEnum):
    ORDINARY_TRANSFER = "ORDINARY_TRANSFER"
    MINT = "MINT"
    BURN = "BURN"
    EXCHANGE_DEPOSIT = "EXCHANGE_DEPOSIT"
    EXCHANGE_WITHDRAWAL = "EXCHANGE_WITHDRAWAL"
    BRIDGE_TRANSFER = "BRIDGE_TRANSFER"
    UNKNOWN = "UNKNOWN"


def _address(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise StablecoinError(f"{field} must be an EVM address")
    normalized = value.strip().lower()
    if not _EVM_ADDRESS_RE.fullmatch(normalized):
        raise StablecoinError(f"{field} must be an EVM address")
    return normalized


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StablecoinError(f"{field} is required")
    return value.strip()


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StablecoinError(f"{field} must be UTC-aware")
    return value


def _amount(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - convert Decimal parser errors
        raise StablecoinError(f"{field} must be Decimal-compatible") from exc
    if not result.is_finite() or result < 0:
        raise StablecoinError(f"{field} must be non-negative and finite")
    return result


@dataclass(frozen=True, slots=True)
class StablecoinSpec:
    symbol: str
    contract_address: str
    decimals: int
    issuer_addresses: tuple[str, ...]
    registry_version: str

    def __post_init__(self) -> None:
        if self.symbol not in _ALLOWED_SYMBOLS:
            raise StablecoinError("only Ethereum USDT and USDC are approved")
        if not 0 <= self.decimals <= 255:
            raise StablecoinError("stablecoin decimals must be between 0 and 255")
        _address(self.contract_address, "contract_address")
        if not self.registry_version.strip():
            raise StablecoinError("registry_version is required")
        normalized = tuple(_address(item, "issuer_address") for item in self.issuer_addresses)
        if len(set(normalized)) != len(normalized):
            raise StablecoinError("issuer addresses must be unique")
        object.__setattr__(self, "contract_address", self.contract_address.lower())
        object.__setattr__(self, "issuer_addresses", normalized)

    @property
    def asset_id(self) -> str:
        return f"ETHEREUM:ERC20:{self.contract_address}:{self.registry_version}"


class StablecoinRegistry:
    def __init__(self, specs: Iterable[StablecoinSpec]) -> None:
        indexed: dict[tuple[str, str], StablecoinSpec] = {}
        for spec in specs:
            key = (spec.symbol, spec.contract_address)
            if key in indexed:
                raise StablecoinError("duplicate stablecoin registry identity")
            indexed[key] = spec
        if not indexed:
            raise StablecoinError("stablecoin registry cannot be empty")
        self._specs = indexed

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "StablecoinRegistry":
        specs = []
        for row in rows:
            try:
                specs.append(StablecoinSpec(
                    symbol=_text(row["symbol"], "symbol").upper(),
                    contract_address=_address(row["contract_address"], "contract_address"),
                    decimals=int(row["decimals"]),
                    issuer_addresses=tuple(row.get("issuer_addresses", ())),
                    registry_version=_text(row["registry_version"], "registry_version"),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise StablecoinError("invalid stablecoin registry row") from exc
        return cls(specs)

    def require(self, symbol: str, contract_address: str) -> StablecoinSpec:
        key = (_text(symbol, "symbol").upper(), _address(contract_address, "contract_address"))
        try:
            return self._specs[key]
        except KeyError as exc:
            raise StablecoinError("stablecoin contract is not allowlisted") from exc

    def is_issuer(self, symbol: str, contract_address: str, issuer_address: str | None) -> bool:
        if issuer_address is None:
            return False
        spec = self.require(symbol, contract_address)
        return _address(issuer_address, "issuer_address") in spec.issuer_addresses


@dataclass(frozen=True, slots=True)
class StablecoinTransfer:
    chain: str
    symbol: str
    contract_address: str
    tx_hash: str
    log_index: int
    from_address: str
    to_address: str
    amount: Decimal
    event_timestamp: datetime
    observed_at: datetime
    fetched_at: datetime
    processed_at: datetime
    finality_status: FinalityStatus
    bridge_leg_id: str | None = None
    from_label: str | None = None
    to_label: str | None = None

    def __post_init__(self) -> None:
        if self.chain != "ETHEREUM":
            raise StablecoinError("stablecoin scope is Ethereum only")
        if not re.fullmatch(r"[0-9a-f]{64}", self.tx_hash):
            raise StablecoinError("tx_hash must be lowercase 256-bit hex")
        if isinstance(self.log_index, bool) or not isinstance(self.log_index, int) or self.log_index < 0:
            raise StablecoinError("log_index must be non-negative")
        _address(self.contract_address, "contract_address")
        _address(self.from_address, "from_address")
        _address(self.to_address, "to_address")
        _amount(self.amount, "amount")
        for field, value in (
            ("event_timestamp", self.event_timestamp),
            ("observed_at", self.observed_at),
            ("fetched_at", self.fetched_at),
            ("processed_at", self.processed_at),
        ):
            _utc(value, field)
        if not isinstance(self.finality_status, FinalityStatus):
            raise StablecoinError("finality_status is invalid")
        if self.bridge_leg_id is not None and len(_text(self.bridge_leg_id, "bridge_leg_id")) > 256:
            raise StablecoinError("bridge_leg_id is too long")

    @property
    def identity(self) -> tuple[str, int]:
        return self.tx_hash, self.log_index


@dataclass(frozen=True, slots=True)
class StablecoinTransferClassification:
    transfer: StablecoinTransfer
    category: StablecoinCategory
    aggregation_eligible: bool
    reason: str
    registry_version: str


def classify_stablecoin_transfer(
    transfer: StablecoinTransfer,
    *,
    registry: StablecoinRegistry,
    issuer_address: str | None = None,
    label_snapshot: LabelSnapshot | None = None,
) -> StablecoinTransferClassification:
    spec = registry.require(transfer.symbol, transfer.contract_address)
    if label_snapshot is not None:
        if not isinstance(label_snapshot, LabelSnapshot) or label_snapshot.chain.value != "ETHEREUM":
            raise StablecoinError("exchange labels require a reviewed Ethereum LabelSnapshot")
        if not label_snapshot.effective_from <= transfer.event_timestamp or (
            label_snapshot.effective_to is not None and transfer.event_timestamp >= label_snapshot.effective_to
        ):
            raise StablecoinError("label snapshot is not effective at transfer time")
    if transfer.bridge_leg_id is not None:
        return StablecoinTransferClassification(
            transfer, StablecoinCategory.BRIDGE_TRANSFER, False,
            "BRIDGE_CORRELATION_NOT_AVAILABLE", spec.registry_version,
        )

    is_issuer = registry.is_issuer(transfer.symbol, transfer.contract_address, issuer_address)
    from_zero = transfer.from_address == ZERO_ADDRESS
    to_zero = transfer.to_address == ZERO_ADDRESS
    if from_zero or to_zero:
        if from_zero and not to_zero and is_issuer:
            category, reason = StablecoinCategory.MINT, "ISSUER_VERIFIED_MINT"
        elif to_zero and not from_zero and is_issuer:
            category, reason = StablecoinCategory.BURN, "ISSUER_VERIFIED_BURN"
        else:
            category, reason = StablecoinCategory.UNKNOWN, "ZERO_ENDPOINT_NOT_ISSUER_VERIFIED"
        return StablecoinTransferClassification(
            transfer, category, False if category is StablecoinCategory.UNKNOWN else True,
            reason, spec.registry_version,
        )

    from_categories = {
        label.category for label in (label_snapshot.labels if label_snapshot is not None else ())
        if label.address == transfer.from_address and label.active_at(transfer.event_timestamp)
    }
    to_categories = {
        label.category for label in (label_snapshot.labels if label_snapshot is not None else ())
        if label.address == transfer.to_address and label.active_at(transfer.event_timestamp)
    }
    from_category = next(iter(from_categories)) if len(from_categories) == 1 else None
    to_category = next(iter(to_categories)) if len(to_categories) == 1 else None
    if LabelCategory.KNOWN_BRIDGE in {from_category, to_category}:
        return StablecoinTransferClassification(
            transfer, StablecoinCategory.BRIDGE_TRANSFER, False,
            "BRIDGE_CORRELATION_NOT_AVAILABLE", spec.registry_version,
        )
    observed_addresses = {transfer.from_address, transfer.to_address}
    covered_addresses = {
        address for address in observed_addresses
        if any(
            label.address == address and label.active_at(transfer.event_timestamp)
            for label in (label_snapshot.labels if label_snapshot is not None else ())
        )
    }
    label_coverage_ok = (
        label_snapshot is not None
        and label_snapshot.coverage_denominator == len(observed_addresses)
        and Decimal(len(covered_addresses)) / Decimal(label_snapshot.coverage_denominator or 1) >= MIN_LABEL_COVERAGE
    )
    if from_category is LabelCategory.KNOWN_EXTERNAL and to_category is LabelCategory.KNOWN_EXCHANGE:
        category, reason = (
            (StablecoinCategory.EXCHANGE_DEPOSIT, "REVIEWED_LABELS")
            if label_coverage_ok
            else (StablecoinCategory.ORDINARY_TRANSFER, "LABEL_COVERAGE_INCOMPLETE")
        )
    elif from_category is LabelCategory.KNOWN_EXCHANGE and to_category is LabelCategory.KNOWN_EXTERNAL:
        category, reason = (
            (StablecoinCategory.EXCHANGE_WITHDRAWAL, "REVIEWED_LABELS")
            if label_coverage_ok
            else (StablecoinCategory.ORDINARY_TRANSFER, "LABEL_COVERAGE_INCOMPLETE")
        )
    else:
        category, reason = StablecoinCategory.ORDINARY_TRANSFER, "NON_ZERO_TRANSFER"
    return StablecoinTransferClassification(transfer, category, True, reason, spec.registry_version)


@dataclass(frozen=True, slots=True)
class StablecoinWindowResult:
    symbol: str
    contract_address: str
    category: StablecoinCategory
    timeframe: str
    window_open: datetime
    window_close: datetime
    aggregation_version: str
    flow_domain: str
    aggregation_scope: str
    bridge_leg_id: str | None
    bridge_leg_ids: tuple[str, ...]
    aggregation_eligible: bool
    transfer_count: int
    amount_normalized: Decimal
    amount_usd: Decimal | None
    mint_count: int
    burn_count: int
    exchange_deposit_count: int
    exchange_withdrawal_count: int
    sample_count: int
    source_count: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal
    finality_status: FinalityStatus
    freshness_status: DataStatus
    status: DataStatus
    reason: str
    source_reference: str
    registry_version: str
    processed_at: datetime

    def __post_init__(self) -> None:
        if self.flow_domain != "STABLECOIN" or self.aggregation_scope != "STABLECOIN":
            raise StablecoinError("stablecoin context domain is fixed")
        if not isinstance(self.category, StablecoinCategory):
            raise StablecoinError("invalid stablecoin category")
        if self.timeframe not in _TIMEFRAME_DURATION:
            raise StablecoinError("unsupported stablecoin timeframe")
        _utc(self.window_open, "window_open")
        _utc(self.window_close, "window_close")
        if self.window_close - self.window_open != _TIMEFRAME_DURATION[self.timeframe]:
            raise StablecoinError("window bounds do not match timeframe")
        _amount(self.amount_normalized, "amount_normalized") if self.amount_normalized > 0 else None
        if self.amount_normalized < 0 or (self.amount_usd is not None and self.amount_usd < 0):
            raise StablecoinError("stablecoin amounts cannot be negative")
        if self.transfer_count < 0 or self.sample_count < 0 or self.source_count < 0:
            raise StablecoinError("stablecoin counts cannot be negative")
        if not Decimal("0") <= self.coverage_ratio <= Decimal("1"):
            raise StablecoinError("coverage_ratio must be between zero and one")
        if not isinstance(self.finality_status, FinalityStatus) or not isinstance(self.status, DataStatus):
            raise StablecoinError("stablecoin status is invalid")
        _utc(self.processed_at, "processed_at")

    @property
    def category_count(self) -> int:
        return self.transfer_count

    def to_row(self, *, created_at: datetime) -> dict[str, Any]:
        persisted_bridge_id = self.bridge_leg_id
        persisted_reference = self.source_reference
        if len(self.bridge_leg_ids) > 1:
            persisted_bridge_id = json.dumps(self.bridge_leg_ids, separators=(",", ":"))
            persisted_reference = f"{persisted_reference}|bridge_leg_ids={persisted_bridge_id}"
        return {
            "chain": "ETHEREUM",
            "asset_id": f"ETHEREUM:ERC20:{self.contract_address}:{self.registry_version}",
            "contract_address": self.contract_address,
            "category": self.category.value,
            "timeframe": self.timeframe,
            "window_open": self.window_open,
            "window_close": self.window_close,
            "aggregation_version": self.aggregation_version,
            "flow_domain": self.flow_domain,
            "aggregation_scope": self.aggregation_scope,
            "bridge_leg_id": persisted_bridge_id,
            "aggregation_eligible": self.aggregation_eligible,
            "transfer_count": self.transfer_count,
            "amount_normalized": self.amount_normalized,
            "amount_usd": self.amount_usd,
            "mint_count": self.mint_count,
            "burn_count": self.burn_count,
            "exchange_deposit_count": self.exchange_deposit_count,
            "exchange_withdrawal_count": self.exchange_withdrawal_count,
            "sample_count": self.sample_count,
            "source_count": self.source_count,
            "available_count": self.available_count,
            "missing_count": self.missing_count,
            "coverage_ratio": self.coverage_ratio,
            "finality_status": self.finality_status.value,
            "freshness_status": self.freshness_status.value,
            "status": self.status.value,
            "reason": self.reason,
            "source_reference": persisted_reference,
            "normalization_version": "phase7-stablecoin-v1",
            "processed_at": self.processed_at,
            "created_at": _utc(created_at, "created_at"),
        }


def aggregate_stablecoin_context(
    transfers: Iterable[StablecoinTransfer],
    *,
    registry: StablecoinRegistry,
    category: StablecoinCategory,
    window_open: datetime,
    timeframe: str,
    processed_at: datetime,
    issuer_address: str | None = None,
    label_snapshot: LabelSnapshot | None = None,
    aggregation_version: str = "phase7-stablecoin-v1",
) -> StablecoinWindowResult:
    try:
        duration = _TIMEFRAME_DURATION[timeframe]
    except KeyError as exc:
        raise StablecoinError("unsupported stablecoin timeframe") from exc
    window_open = _utc(window_open, "window_open")
    processed_at = _utc(processed_at, "processed_at")
    window_close = window_open + duration
    if not isinstance(category, StablecoinCategory):
        raise StablecoinError("category must be StablecoinCategory")

    bounded = tuple(islice(transfers, _MAX_TRANSFERS + 1))
    if len(bounded) > _MAX_TRANSFERS:
        raise StablecoinError("stablecoin transfer batch exceeds bounded cap")
    classifications = tuple(
        classify_stablecoin_transfer(
            item, registry=registry, issuer_address=issuer_address, label_snapshot=label_snapshot,
        )
        for item in bounded
        if window_open <= item.event_timestamp < window_close
    )
    if any(item.category is not category for item in classifications):
        raise StablecoinError("transfer category does not match requested context")
    if not classifications:
        raise StablecoinError("stablecoin context requires at least one event")
    identities = {
        (item.transfer.symbol, item.transfer.contract_address)
        for item in classifications
    }
    if len(identities) != 1:
        raise StablecoinError("stablecoin context cannot mix asset contracts")
    ids = tuple(sorted({item.transfer.bridge_leg_id for item in classifications if item.transfer.bridge_leg_id}))
    is_bridge = category is StablecoinCategory.BRIDGE_TRANSFER
    unknown_category = category is StablecoinCategory.UNKNOWN
    reorged = any(item.transfer.finality_status is FinalityStatus.REORGED for item in classifications)
    amount = (
        Decimal("0")
        if is_bridge or unknown_category or reorged
        else sum((item.transfer.amount for item in classifications), Decimal("0"))
    )
    non_finalized = any(item.transfer.finality_status is not FinalityStatus.FINALIZED for item in classifications)
    finality_rank = {
        FinalityStatus.OBSERVED: 0,
        FinalityStatus.PENDING: 1,
        FinalityStatus.CONFIRMED: 2,
        FinalityStatus.FINALIZED: 3,
        FinalityStatus.REORGED: -1,
    }
    finality = FinalityStatus.REORGED if reorged else min(
        (item.transfer.finality_status for item in classifications),
        key=lambda value: finality_rank[value],
    )
    status = (
        DataStatus.STALE if reorged else
        DataStatus.NOT_AVAILABLE if is_bridge or unknown_category else
        DataStatus.PARTIAL if non_finalized else
        DataStatus.AVAILABLE
    )
    reason = (
        "REORGED_EVENT" if reorged else
        "BRIDGE_CORRELATION_NOT_AVAILABLE" if is_bridge else
        "ZERO_ENDPOINT_NOT_ISSUER_VERIFIED" if unknown_category else
        "FINALITY_INCOMPLETE" if non_finalized else
        "COMPLETE"
    )
    coverage = Decimal("1.0000") if status is DataStatus.AVAILABLE else Decimal("0.0000")
    return StablecoinWindowResult(
        symbol=classifications[0].transfer.symbol,
        contract_address=classifications[0].transfer.contract_address,
        category=category, timeframe=timeframe, window_open=window_open,
        window_close=window_close, aggregation_version=aggregation_version,
        flow_domain="STABLECOIN", aggregation_scope="STABLECOIN",
        bridge_leg_id=ids[0] if len(ids) == 1 else None, bridge_leg_ids=ids,
        aggregation_eligible=not is_bridge and not unknown_category and not reorged,
        transfer_count=len(classifications), amount_normalized=amount, amount_usd=None,
        mint_count=sum(item.category is StablecoinCategory.MINT for item in classifications),
        burn_count=sum(item.category is StablecoinCategory.BURN for item in classifications),
        exchange_deposit_count=sum(item.category is StablecoinCategory.EXCHANGE_DEPOSIT for item in classifications),
        exchange_withdrawal_count=sum(item.category is StablecoinCategory.EXCHANGE_WITHDRAWAL for item in classifications),
        sample_count=len(classifications), source_count=1,
        available_count=len(classifications) if status is DataStatus.AVAILABLE else 0,
        missing_count=0 if status is DataStatus.AVAILABLE else len(classifications),
        coverage_ratio=coverage, finality_status=finality, freshness_status=status,
        status=status, reason=reason,
        source_reference=f"ethereum:{classifications[0].transfer.contract_address}:{classifications[0].registry_version}",
        registry_version=classifications[0].registry_version,
        processed_at=processed_at,
    )

