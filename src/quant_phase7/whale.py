"""Bounded, valuation-aware whale transfer context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from .contracts import CanonicalAssetId, Chain, DataStatus, FinalityStatus, OnChainTransferEvent
from .labels import MIN_LABEL_COVERAGE, LabelCategory


class FlowDomain(StrEnum):
    GENERIC_ONCHAIN = "GENERIC_ONCHAIN"
    STABLECOIN = "STABLECOIN"
    BRIDGE = "BRIDGE"


class WhaleDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    PEER = "PEER"
    UNKNOWN = "UNKNOWN"


_WINDOW_LENGTHS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
    "4H": timedelta(hours=4),
}


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


@dataclass(frozen=True, slots=True)
class WhaleTier:
    name: str
    min_usd: Decimal
    max_usd: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "tier name", 128))
        if not isinstance(self.min_usd, Decimal) or self.min_usd < 0:
            raise ValueError("min_usd must be a non-negative Decimal")
        if self.max_usd is not None:
            if not isinstance(self.max_usd, Decimal) or self.max_usd <= self.min_usd:
                raise ValueError("max_usd must be greater than min_usd")


@dataclass(frozen=True, slots=True)
class WhaleThresholdConfig:
    chain: Chain
    asset_id: CanonicalAssetId
    threshold_version: str
    tiers: tuple[WhaleTier, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.chain, Chain) or not isinstance(self.asset_id, CanonicalAssetId):
            raise ValueError("threshold config requires canonical chain and asset identity")
        if self.asset_id.chain is not self.chain:
            raise ValueError("threshold config chain does not match asset identity")
        object.__setattr__(self, "threshold_version", _text(self.threshold_version, "threshold_version", 128))
        if not self.tiers:
            raise ValueError("threshold config requires at least one tier")
        names = [tier.name for tier in self.tiers]
        if len(set(names)) != len(names):
            raise ValueError("threshold tier names must be unique")
        if tuple(sorted(self.tiers, key=lambda tier: tier.min_usd)) != self.tiers:
            raise ValueError("threshold tiers must be ordered by min_usd")
        for previous, current in zip(self.tiers, self.tiers[1:]):
            if previous.max_usd is None:
                raise ValueError("open-ended threshold tier must be the last tier")
            if previous.max_usd is not None and previous.max_usd != current.min_usd:
                raise ValueError("threshold tiers must not leave gaps")

    def select(self, amount_usd: Decimal | None) -> WhaleTier | None:
        if amount_usd is None:
            return None
        if not isinstance(amount_usd, Decimal) or amount_usd < 0:
            raise ValueError("amount_usd must be a non-negative Decimal")
        for tier in self.tiers:
            if amount_usd >= tier.min_usd and (tier.max_usd is None or amount_usd < tier.max_usd):
                return tier
        return None


@dataclass(frozen=True, slots=True)
class WhaleTransferContext:
    event_id: str
    asset_id: CanonicalAssetId
    chain: Chain
    event_time: datetime
    finality_status: FinalityStatus
    freshness_status: DataStatus
    amount_normalized: Decimal
    amount_usd: Decimal | None
    source_category: LabelCategory | None
    destination_category: LabelCategory | None
    exchange_involvement: bool
    label_coverage_ratio: Decimal | None
    known_address_count: int
    labeled_address_count: int
    direction: WhaleDirection
    flow_domain: FlowDomain
    aggregation_eligible: bool
    threshold_version: str
    tier: str | None
    large_amount_normalized: Decimal | None
    large_amount_usd: Decimal | None
    status: DataStatus
    reason: str
    source_id: str
    source_reference: str


def evaluate_whale_transfer(
    event: OnChainTransferEvent,
    config: WhaleThresholdConfig,
    *,
    direction: WhaleDirection = WhaleDirection.UNKNOWN,
    source_category: LabelCategory | None = None,
    destination_category: LabelCategory | None = None,
    label_coverage_ratio: Decimal | None = None,
    exchange_involvement: bool | None = None,
    known_address_count: int = 0,
    labeled_address_count: int = 0,
    flow_domain: FlowDomain = FlowDomain.GENERIC_ONCHAIN,
    aggregation_eligible: bool = True,
    freshness_status: DataStatus | None = None,
) -> WhaleTransferContext:
    if not isinstance(event, OnChainTransferEvent):
        raise ValueError("whale context requires a canonical transfer event")
    if event.identity.asset_id != config.asset_id or event.identity.chain is not config.chain:
        raise ValueError("threshold config asset identity does not match event")
    if not isinstance(direction, WhaleDirection) or not isinstance(flow_domain, FlowDomain):
        raise ValueError("whale direction and flow domain are invalid")
    if source_category is not None and not isinstance(source_category, LabelCategory):
        raise ValueError("source_category must be LabelCategory")
    if destination_category is not None and not isinstance(destination_category, LabelCategory):
        raise ValueError("destination_category must be LabelCategory")
    if exchange_involvement is not None and not isinstance(exchange_involvement, bool):
        raise ValueError("exchange_involvement must be boolean")
    if label_coverage_ratio is not None and (
        not isinstance(label_coverage_ratio, Decimal)
        or not Decimal("0") <= label_coverage_ratio <= Decimal("1")
    ):
        raise ValueError("label_coverage_ratio must be a Decimal between 0 and 1")
    if (
        isinstance(known_address_count, bool)
        or isinstance(labeled_address_count, bool)
        or not isinstance(known_address_count, int)
        or not isinstance(labeled_address_count, int)
        or known_address_count < 0
        or labeled_address_count < 0
        or labeled_address_count > known_address_count
    ):
        raise ValueError("label address counts are invalid")
    if label_coverage_ratio is not None and known_address_count == 0:
        raise ValueError("label coverage requires known_address_count")
    if label_coverage_ratio is not None and label_coverage_ratio != (
        Decimal(labeled_address_count) / Decimal(known_address_count)
    ):
        raise ValueError("label_coverage_ratio must equal labeled_address_count / known_address_count")
    freshness_status = event.status if freshness_status is None else freshness_status
    if not isinstance(freshness_status, DataStatus):
        raise ValueError("freshness_status must be DataStatus")
    amount_usd = event.valuation.amount_usd if event.valuation.status is DataStatus.AVAILABLE else None
    eligible = aggregation_eligible and flow_domain is FlowDomain.GENERIC_ONCHAIN
    base = dict(
        event_id=event.event_id, asset_id=event.identity.asset_id, chain=event.identity.chain,
        event_time=event.event_time, finality_status=event.finality_status,
        freshness_status=freshness_status, amount_normalized=event.amount.amount_normalized,
        amount_usd=amount_usd, source_category=source_category,
        destination_category=destination_category,
        exchange_involvement=(exchange_involvement if exchange_involvement is not None else (
            source_category is LabelCategory.KNOWN_EXCHANGE
            or destination_category is LabelCategory.KNOWN_EXCHANGE
        )),
        label_coverage_ratio=label_coverage_ratio, direction=direction,
        known_address_count=known_address_count, labeled_address_count=labeled_address_count,
        flow_domain=flow_domain, aggregation_eligible=eligible,
        threshold_version=config.threshold_version, tier=None,
        large_amount_normalized=None, large_amount_usd=None, source_id=event.provenance.source,
        source_reference=event.provenance.source_reference,
    )
    if freshness_status in {DataStatus.ERROR, DataStatus.STALE, DataStatus.NOT_AVAILABLE}:
        return WhaleTransferContext(**base, status=freshness_status, reason="SOURCE_NOT_FRESH")
    if not eligible:
        return WhaleTransferContext(
            **base,
            status=DataStatus.PARTIAL if freshness_status is DataStatus.PARTIAL else DataStatus.AVAILABLE,
            reason="DOMAIN_EXCLUDED",
        )
    if amount_usd is None:
        return WhaleTransferContext(**base, status=DataStatus.PARTIAL, reason="THRESHOLD_NOT_EVALUABLE")
    tier = config.select(amount_usd)
    if tier is None:
        return WhaleTransferContext(**base, status=DataStatus.NOT_AVAILABLE, reason="THRESHOLD_NOT_CONFIGURED")
    base.update(
        tier=tier.name, large_amount_normalized=event.amount.amount_normalized,
        large_amount_usd=amount_usd,
    )
    return WhaleTransferContext(
        **base, status=DataStatus.PARTIAL if freshness_status is DataStatus.PARTIAL else DataStatus.AVAILABLE,
        reason="SOURCE_PARTIAL" if freshness_status is DataStatus.PARTIAL else "WHALE_TIER",
    )


@dataclass(frozen=True, slots=True)
class WhaleWindowResult:
    chain: Chain
    asset_id: CanonicalAssetId
    timeframe: str
    window_open: datetime
    window_close: datetime
    aggregation_version: str
    threshold_version: str
    aggregation_scope: str
    flow_domain: FlowDomain
    aggregation_eligible: bool
    large_inflow_count: int
    large_outflow_count: int
    large_inflow_amount: Decimal
    large_outflow_amount: Decimal
    large_inflow_usd: Decimal | None
    large_outflow_usd: Decimal | None
    exchange_inflow_amount: Decimal
    exchange_outflow_amount: Decimal
    exchange_net_amount: Decimal
    threshold_not_evaluable_count: int
    unknown_transfer_count: int
    sample_count: int
    source_count: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal
    labeled_count: int
    known_address_count: int
    label_coverage_ratio: Decimal
    status: DataStatus
    reason: str


def aggregate_whale_window(
    contexts: Iterable[WhaleTransferContext],
    *,
    chain: Chain,
    asset_id: CanonicalAssetId,
    timeframe: str,
    window_open: datetime,
    aggregation_version: str,
    threshold_version: str,
    max_events: int = 10_000,
    now: datetime | None = None,
    freshness_grace: timedelta = timedelta(),
) -> WhaleWindowResult:
    if timeframe not in _WINDOW_LENGTHS:
        raise ValueError("unsupported whale timeframe")
    window_open = _utc(window_open, "window_open")
    window_close = window_open + _WINDOW_LENGTHS[timeframe]
    now = datetime.now(timezone.utc) if now is None else _utc(now, "now")
    if freshness_grace < timedelta(0):
        raise ValueError("freshness_grace must be non-negative")
    if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events <= 0:
        raise ValueError("max_events must be positive")
    items: list[WhaleTransferContext] = []
    for item in contexts:
        if len(items) >= max_events:
            raise ValueError("whale window event budget exceeded")
        items.append(item)
    if not isinstance(chain, Chain) or not isinstance(asset_id, CanonicalAssetId) or asset_id.chain is not chain:
        raise ValueError("whale window identity is invalid")
    eligible_items: list[WhaleTransferContext] = []
    excluded_items: list[WhaleTransferContext] = []
    for item in items:
        if item.chain is not chain or item.asset_id != asset_id:
            raise ValueError("whale context identity does not match window")
        if not window_open <= _utc(item.event_time, "event_time") < window_close:
            raise ValueError("whale context event_time is outside window")
        if item.flow_domain is FlowDomain.GENERIC_ONCHAIN and item.aggregation_eligible:
            if item.threshold_version != threshold_version:
                raise ValueError("whale threshold version does not match window")
            eligible_items.append(item)
        else:
            excluded_items.append(item)

    inflow_count = outflow_count = threshold_missing = unknown_count = available = missing = 0
    inflow_amount = outflow_amount = Decimal("0")
    exchange_inflow_amount = exchange_outflow_amount = Decimal("0")
    inflow_usd: list[Decimal] = []
    outflow_usd: list[Decimal] = []
    sources: set[str] = set()
    known_address_count = labeled_count = 0
    label_coverage_missing = False
    for item in eligible_items:
        sources.add(item.source_id)
        known_address_count += item.known_address_count
        labeled_count += item.labeled_address_count
        if item.label_coverage_ratio is None:
            label_coverage_missing = True
        if item.status is DataStatus.AVAILABLE:
            available += 1
        else:
            missing += 1
        if item.tier is None:
            threshold_missing += 1
        if item.direction is WhaleDirection.UNKNOWN:
            unknown_count += 1
        if item.tier is None or item.status is not DataStatus.AVAILABLE:
            continue
        if item.direction is WhaleDirection.INBOUND:
            inflow_count += 1
            inflow_amount += item.large_amount_normalized or Decimal("0")
            if item.large_amount_usd is not None:
                inflow_usd.append(item.large_amount_usd)
            if item.exchange_involvement:
                exchange_inflow_amount += item.large_amount_normalized or Decimal("0")
        elif item.direction is WhaleDirection.OUTBOUND:
            outflow_count += 1
            outflow_amount += item.large_amount_normalized or Decimal("0")
            if item.large_amount_usd is not None:
                outflow_usd.append(item.large_amount_usd)
            if item.exchange_involvement:
                exchange_outflow_amount += item.large_amount_normalized or Decimal("0")

    sample_count = len(eligible_items)
    coverage_ratio = Decimal(available) / Decimal(sample_count) if sample_count else Decimal("0")
    label_coverage_ratio = (
        Decimal(labeled_count) / Decimal(known_address_count) if known_address_count else Decimal("0")
    )
    if any(item.status is DataStatus.ERROR for item in excluded_items):
        status, reason = DataStatus.ERROR, "EXCLUDED_DOMAIN_ERROR"
    elif any(item.status is DataStatus.STALE for item in excluded_items):
        status, reason = DataStatus.STALE, "EXCLUDED_DOMAIN_STALE"
    elif any(item.status is DataStatus.PARTIAL for item in excluded_items):
        status, reason = DataStatus.PARTIAL, "EXCLUDED_DOMAIN_PARTIAL"
    elif not sample_count:
        status, reason = DataStatus.NOT_AVAILABLE, "NO_ELIGIBLE_DATA"
    elif any(item.status is DataStatus.ERROR for item in eligible_items):
        status, reason = DataStatus.ERROR, "SOURCE_ERROR"
    elif any(item.status is DataStatus.STALE for item in eligible_items):
        status, reason = DataStatus.STALE, "SOURCE_STALE"
    elif now > window_close + freshness_grace:
        status, reason = DataStatus.STALE, "WINDOW_STALE"
    elif available == 0:
        status, reason = DataStatus.NOT_AVAILABLE, "NO_EVALUABLE_DATA"
    elif label_coverage_missing or label_coverage_ratio < MIN_LABEL_COVERAGE:
        status, reason = DataStatus.NOT_AVAILABLE, "INSUFFICIENT_LABEL_COVERAGE"
    elif label_coverage_ratio < Decimal("1"):
        status, reason = DataStatus.PARTIAL, "PARTIAL_LABEL_COVERAGE"
    elif available != sample_count or unknown_count:
        status, reason = DataStatus.PARTIAL, "PARTIAL_COVERAGE"
    else:
        status, reason = DataStatus.AVAILABLE, "COMPLETE"
    return WhaleWindowResult(
        chain=chain, asset_id=asset_id, timeframe=timeframe, window_open=window_open,
        window_close=window_close, aggregation_version=_text(aggregation_version, "aggregation_version", 128),
        threshold_version=_text(threshold_version, "threshold_version", 128),
        aggregation_scope="GENERIC", flow_domain=FlowDomain.GENERIC_ONCHAIN, aggregation_eligible=True,
        large_inflow_count=inflow_count, large_outflow_count=outflow_count,
        large_inflow_amount=inflow_amount, large_outflow_amount=outflow_amount,
        large_inflow_usd=sum(inflow_usd, Decimal("0")) if inflow_usd else None,
        large_outflow_usd=sum(outflow_usd, Decimal("0")) if outflow_usd else None,
        exchange_inflow_amount=exchange_inflow_amount,
        exchange_outflow_amount=exchange_outflow_amount,
        exchange_net_amount=exchange_inflow_amount - exchange_outflow_amount,
        threshold_not_evaluable_count=threshold_missing, unknown_transfer_count=unknown_count,
        sample_count=sample_count, source_count=len(sources), available_count=available,
        missing_count=missing, coverage_ratio=coverage_ratio,
        labeled_count=labeled_count, known_address_count=known_address_count,
        label_coverage_ratio=label_coverage_ratio, status=status, reason=reason,
    )
