"""Versioned operator-reviewed address labels and conservative exchange flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
import json
import re
from collections.abc import Mapping
from typing import Iterable
from urllib.parse import urlparse

from .contracts import Chain, DataStatus


class LabelCategory(StrEnum):
    KNOWN_EXCHANGE = "KNOWN_EXCHANGE"
    KNOWN_PROTOCOL = "KNOWN_PROTOCOL"
    KNOWN_TREASURY = "KNOWN_TREASURY"
    KNOWN_BRIDGE = "KNOWN_BRIDGE"
    KNOWN_BURN = "KNOWN_BURN"
    KNOWN_EXTERNAL = "KNOWN_EXTERNAL"
    UNKNOWN = "UNKNOWN"


class ExchangeFlowDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    INTERNAL = "INTERNAL"
    EXCLUDED = "EXCLUDED"
    UNKNOWN = "UNKNOWN"


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_KNOWN_NON_EXTERNAL = {
    LabelCategory.KNOWN_PROTOCOL,
    LabelCategory.KNOWN_TREASURY,
    LabelCategory.KNOWN_BRIDGE,
    LabelCategory.KNOWN_BURN,
}
MIN_LABEL_COVERAGE = Decimal("0.90")


def _reviewed_url(value: object) -> bool:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2048:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and bool(parsed.path or parsed.query or parsed.fragment)


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > limit:
        raise ValueError(f"{field} exceeds bounded length")
    return normalized


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    return value


def _address(chain: Chain, value: object) -> str:
    normalized = _text(value, "address", 256)
    if chain is Chain.ETHEREUM:
        if not _ETH_ADDRESS_RE.fullmatch(normalized) or normalized != normalized.lower():
            raise ValueError("Ethereum label address must be lowercase 0x address")
    elif any(character.isspace() for character in normalized):
        raise ValueError("Bitcoin label address cannot contain whitespace")
    return normalized


@dataclass(frozen=True, slots=True)
class AddressLabel:
    chain: Chain
    address: str
    category: LabelCategory
    source_id: str
    source_version: str
    label_version: str
    confidence: Decimal
    snapshot_hash: str
    source_reference: str
    effective_from: datetime
    effective_to: datetime | None
    observed_at: datetime
    updated_at: datetime
    status: DataStatus
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.chain, Chain):
            raise ValueError("chain must be Chain")
        if not isinstance(self.category, LabelCategory):
            raise ValueError("category must be LabelCategory")
        object.__setattr__(self, "address", _address(self.chain, self.address))
        for field in ("source_id", "source_version", "label_version", "source_reference"):
            object.__setattr__(self, field, _text(getattr(self, field), field, 2048))
        if not isinstance(self.confidence, Decimal) or self.confidence < 0 or self.confidence > 1:
            raise ValueError("confidence must be a Decimal between 0 and 1")
        if not isinstance(self.snapshot_hash, str) or not _HASH_RE.fullmatch(self.snapshot_hash):
            raise ValueError("snapshot_hash must be lowercase SHA-256")
        _utc(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _utc(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be after effective_from")
        _utc(self.observed_at, "observed_at")
        _utc(self.updated_at, "updated_at")
        if not isinstance(self.status, DataStatus):
            raise ValueError("status must be DataStatus")
        if self.status is not DataStatus.AVAILABLE and self.reason is None:
            raise ValueError("non-available label requires reason")
        if self.reason is not None:
            object.__setattr__(self, "reason", _text(self.reason, "reason", 2048))

    def active_at(self, event_time: datetime) -> bool:
        event_time = _utc(event_time, "event_time")
        return self.effective_from <= event_time and (
            self.effective_to is None or event_time < self.effective_to
        )

    def to_row(self) -> dict[str, object]:
        return {
            "chain": self.chain.value,
            "address": self.address,
            "category": self.category.value,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "label_version": self.label_version,
            "confidence": self.confidence,
            "snapshot_hash": self.snapshot_hash,
            "source_reference": self.source_reference,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "observed_at": self.observed_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "reason": self.reason,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "AddressLabel":
        required = (
            "chain", "address", "category", "source_id", "source_version", "label_version",
            "confidence", "snapshot_hash", "source_reference", "effective_from", "effective_to",
            "observed_at", "updated_at", "status", "reason",
        )
        missing = [field for field in required if field not in row]
        if missing:
            raise ValueError(f"address label missing required fields: {', '.join(missing)}")
        try:
            chain = row["chain"] if isinstance(row["chain"], Chain) else Chain(row["chain"])
            category = row["category"] if isinstance(row["category"], LabelCategory) else LabelCategory(row["category"])
            status = row["status"] if isinstance(row["status"], DataStatus) else DataStatus(row["status"])
        except (TypeError, ValueError):
            raise ValueError("address label enum is invalid") from None
        return cls(
            chain=chain, address=row["address"], category=category,
            source_id=row["source_id"], source_version=row["source_version"],
            label_version=row["label_version"], confidence=row["confidence"],
            snapshot_hash=row["snapshot_hash"], source_reference=row["source_reference"],
            effective_from=row["effective_from"], effective_to=row["effective_to"],
            observed_at=row["observed_at"], updated_at=row["updated_at"],
            status=status, reason=row["reason"],
        )


@dataclass(frozen=True, slots=True)
class LabelSnapshot:
    chain: Chain
    source_id: str
    source_version: str
    label_version: str
    source_urls: tuple[str, ...]
    reviewer: str
    snapshot_hash: str
    effective_from: datetime
    effective_to: datetime | None
    coverage_denominator: int
    reviewed: bool
    labels: tuple[AddressLabel, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain, Chain):
            raise ValueError("chain must be Chain")
        for field in ("source_id", "source_version", "label_version", "reviewer"):
            _text(getattr(self, field), field, 2048)
        if not self.source_urls or any(not _reviewed_url(url) for url in self.source_urls):
            raise ValueError("source_urls must contain reviewed HTTPS URLs")
        if self.reviewed is not True:
            raise ValueError("label snapshot must be explicitly reviewed")
        if not isinstance(self.snapshot_hash, str) or not _HASH_RE.fullmatch(self.snapshot_hash):
            raise ValueError("snapshot_hash must be lowercase SHA-256")
        _utc(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _utc(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be after effective_from")
        if isinstance(self.coverage_denominator, bool) or not isinstance(self.coverage_denominator, int) or self.coverage_denominator < 0:
            raise ValueError("coverage_denominator must be non-negative")
        for label in self.labels:
            if (
                label.chain is not self.chain
                or label.source_id != self.source_id
                or label.source_version != self.source_version
                or label.label_version != self.label_version
                or label.snapshot_hash != self.snapshot_hash
                or label.effective_from != self.effective_from
                or label.effective_to != self.effective_to
            ):
                raise ValueError("label does not belong to snapshot")

    def provenance_reference(self) -> str:
        return json.dumps(
            {
                "coverage_denominator": self.coverage_denominator,
                "effective_from": self.effective_from.isoformat(),
                "effective_to": self.effective_to.isoformat() if self.effective_to else None,
                "label_version": self.label_version,
                "reviewer": self.reviewer,
                "reviewed": self.reviewed,
                "snapshot_hash": self.snapshot_hash,
                "source_id": self.source_id,
                "source_urls": self.source_urls,
                "source_version": self.source_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_rows(self) -> tuple[dict[str, object], ...]:
        reference = self.provenance_reference()
        return tuple({**label.to_row(), "source_reference": reference} for label in self.labels)


@dataclass(frozen=True, slots=True)
class ExchangeFlowResult:
    direction: ExchangeFlowDirection
    status: DataStatus
    reason: str
    source_category: LabelCategory | None
    destination_category: LabelCategory | None
    known_address_count: int
    labeled_address_count: int
    label_coverage_ratio: Decimal
    aggregation_eligible: bool
    conflict_references: tuple[str, ...]


def _resolve_labels(
    chain: Chain, addresses: set[str], labels: Iterable[AddressLabel], event_time: datetime,
) -> tuple[dict[str, LabelCategory | None], dict[str, tuple[str, ...]], int]:
    active: dict[str, list[AddressLabel]] = {address: [] for address in addresses}
    for label in labels:
        if label.chain is chain and label.address in active and label.active_at(event_time):
            active[label.address].append(label)
    categories: dict[str, LabelCategory | None] = {}
    conflicts: dict[str, tuple[str, ...]] = {}
    for address, candidates in active.items():
        unique_categories = {label.category for label in candidates}
        categories[address] = next(iter(unique_categories)) if len(unique_categories) == 1 else None
        if len(unique_categories) > 1:
            conflicts[address] = tuple(sorted({label.source_reference for label in candidates}))
    return categories, conflicts, sum(bool(candidates) for candidates in active.values())


def classify_exchange_flow(
    *,
    chain: Chain,
    from_address: str | None,
    to_address: str | None,
    observed_addresses: Iterable[str],
    snapshot: LabelSnapshot | tuple[LabelSnapshot, ...],
    event_time: datetime,
    min_coverage: Decimal = MIN_LABEL_COVERAGE,
) -> ExchangeFlowResult:
    """Classify one transfer without inferring an address category."""
    if not isinstance(chain, Chain):
        raise ValueError("chain must be Chain")
    snapshots = (snapshot,) if isinstance(snapshot, LabelSnapshot) else tuple(snapshot)
    if not snapshots or any(not isinstance(item, LabelSnapshot) or item.chain is not chain for item in snapshots):
        raise ValueError("classify_exchange_flow requires matching reviewed LabelSnapshot(s)")
    if len({item.coverage_denominator for item in snapshots}) != 1:
        raise ValueError("label snapshots must agree on coverage_denominator")
    event_time = _utc(event_time, "event_time")
    if (
        not isinstance(min_coverage, Decimal)
        or min_coverage < MIN_LABEL_COVERAGE
        or min_coverage > Decimal("1")
    ):
        raise ValueError("min_coverage cannot be below PHASE7_MIN_LABEL_COVERAGE=0.90")
    addresses = {_address(chain, address) for address in observed_addresses}
    normalized_from = _address(chain, from_address) if from_address is not None else None
    normalized_to = _address(chain, to_address) if to_address is not None else None
    addresses.update(address for address in (normalized_from, normalized_to) if address is not None)
    if not addresses:
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.NOT_AVAILABLE, "LABEL_INCOMPLETE",
            None, None, 0, 0, Decimal("0"), False, (),
        )
    if snapshots[0].coverage_denominator != len(addresses):
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.NOT_AVAILABLE, "SNAPSHOT_DENOMINATOR_MISMATCH",
            None, None, len(addresses), 0, Decimal("0"), False,
            tuple(item.provenance_reference() for item in snapshots),
        )
    categories, conflicts, labeled_count = _resolve_labels(
        chain, addresses, (label for item in snapshots for label in item.labels), event_time,
    )
    ratio = Decimal(labeled_count) / Decimal(len(addresses))
    conflict_refs = tuple(item.provenance_reference() for item in snapshots) if conflicts else ()
    source_category = categories.get(normalized_from) if normalized_from is not None else None
    destination_category = categories.get(normalized_to) if normalized_to is not None else None

    if ratio < min_coverage:
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.NOT_AVAILABLE, "INSUFFICIENT_COVERAGE",
            source_category, destination_category, len(addresses), labeled_count, ratio, False, conflict_refs,
        )
    if conflict_refs:
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.PARTIAL, "SOURCE_CONFLICT",
            source_category, destination_category, len(addresses), labeled_count, ratio, False, conflict_refs,
        )
    if source_category is None or destination_category is None:
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.PARTIAL, "UNKNOWN_ENDPOINT",
            source_category, destination_category, len(addresses), labeled_count, ratio, False, (),
        )
    if LabelCategory.UNKNOWN in {source_category, destination_category}:
        return ExchangeFlowResult(
            ExchangeFlowDirection.UNKNOWN, DataStatus.PARTIAL, "UNKNOWN_ENDPOINT",
            source_category, destination_category, len(addresses), labeled_count, ratio, False, (),
        )

    status = DataStatus.AVAILABLE if ratio == Decimal("1") else DataStatus.PARTIAL
    direction = ExchangeFlowDirection.UNKNOWN
    reason = "UNKNOWN_ENDPOINT"
    if source_category is LabelCategory.KNOWN_EXTERNAL and destination_category is LabelCategory.KNOWN_EXCHANGE:
        direction, reason = ExchangeFlowDirection.INBOUND, "EXCHANGE_INFLOW"
    elif source_category is LabelCategory.KNOWN_EXCHANGE and destination_category is LabelCategory.KNOWN_EXTERNAL:
        direction, reason = ExchangeFlowDirection.OUTBOUND, "EXCHANGE_OUTFLOW"
    elif source_category is LabelCategory.KNOWN_EXCHANGE and destination_category is LabelCategory.KNOWN_EXCHANGE:
        direction, reason = ExchangeFlowDirection.INTERNAL, "EXCHANGE_INTERNAL"
    elif source_category is LabelCategory.KNOWN_EXCHANGE or destination_category is LabelCategory.KNOWN_EXCHANGE:
        if source_category in _KNOWN_NON_EXTERNAL or destination_category in _KNOWN_NON_EXTERNAL:
            direction, reason = ExchangeFlowDirection.EXCLUDED, "NON_USER_EXCHANGE_FLOW"
        else:
            direction, reason = ExchangeFlowDirection.UNKNOWN, "UNKNOWN_ENDPOINT"
    else:
        direction, reason = ExchangeFlowDirection.EXCLUDED, "NO_EXCHANGE_INVOLVEMENT"
    return ExchangeFlowResult(
        direction, status, reason, source_category, destination_category,
        len(addresses), labeled_count, ratio,
        direction in {ExchangeFlowDirection.INBOUND, ExchangeFlowDirection.OUTBOUND},
        (),
    )
