"""Deterministic Phase 7 event-to-context projections used by the Engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import Iterable

from .contracts import AssetKind, Chain, DataStatus, FinalityStatus, OnChainTransferEvent
from .ethereum import DEFAULT_ETHEREUM_ASSETS
from .labels import (
    MIN_LABEL_COVERAGE,
    AddressLabel,
    ExchangeFlowDirection,
    ExchangeFlowResult,
    LabelCategory,
    LabelSnapshot,
    classify_exchange_flow,
)
from .stablecoin import (
    StablecoinCategory,
    StablecoinRegistry,
    StablecoinTransfer,
    aggregate_stablecoin_context,
    classify_stablecoin_transfer,
)
from .whale import (
    FlowDomain,
    WhaleDirection,
    WhaleThresholdConfig,
    aggregate_whale_window,
    evaluate_whale_transfer,
)


WINDOWS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
    "4H": timedelta(hours=4),
}
MAX_EVENTS_PER_WINDOW = 10_000
GENERIC_FLOW_VERSION = "phase7-onchain-flow-v1"
WHALE_AGGREGATION_VERSION = "phase7-whale-v1"
STABLECOIN_AGGREGATION_VERSION = "phase7-stablecoin-v1"


def event_window_open(event_time: datetime, timeframe: str) -> datetime:
    if timeframe not in WINDOWS:
        raise ValueError("unsupported Phase 7 window timeframe")
    if event_time.tzinfo is None or event_time.utcoffset() != timedelta(0):
        raise ValueError("event_time must be UTC-aware")
    seconds = int(WINDOWS[timeframe].total_seconds())
    epoch = int(event_time.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


def _bounded_events(
    events: Iterable[OnChainTransferEvent], *, window_open: datetime, timeframe: str,
) -> tuple[OnChainTransferEvent, ...]:
    window_close = window_open + WINDOWS[timeframe]
    result: list[OnChainTransferEvent] = []
    seen: set[str] = set()
    for event in events:
        if event.event_id in seen:
            continue
        if len(result) >= MAX_EVENTS_PER_WINDOW:
            raise ValueError("Phase 7 context window event budget exceeded")
        if not window_open <= event.event_time < window_close:
            raise ValueError("event lies outside its aggregation window")
        if event.status in {DataStatus.STALE, DataStatus.ERROR, DataStatus.NOT_AVAILABLE}:
            continue
        if event.finality_status is FinalityStatus.REORGED:
            continue
        result.append(event)
        seen.add(event.event_id)
    return tuple(result)


def _active_categories(snapshot: LabelSnapshot | None, event: OnChainTransferEvent) -> dict[str, LabelCategory]:
    if snapshot is None:
        return {}
    addresses = {address for address in (event.from_address, event.to_address) if address is not None}
    active: dict[str, set[LabelCategory]] = {address: set() for address in addresses}
    for label in snapshot.labels:
        if label.address in active and label.active_at(event.event_time):
            active[label.address].add(label.category)
    return {
        address: next(iter(categories))
        for address, categories in active.items()
        if len(categories) == 1
    }


def _classify(
    event: OnChainTransferEvent, snapshot: LabelSnapshot | None,
) -> ExchangeFlowResult | None:
    if snapshot is None:
        return None
    observed = tuple(sorted({a for a in (event.from_address, event.to_address) if a is not None}))
    return classify_exchange_flow(
        chain=event.identity.chain,
        from_address=event.from_address,
        to_address=event.to_address,
        observed_addresses=observed,
        snapshot=snapshot,
        event_time=event.event_time,
    )


def _is_stablecoin(event: OnChainTransferEvent) -> bool:
    return (
        event.identity.chain is Chain.ETHEREUM
        and event.identity.asset_id.kind is AssetKind.ERC20
        and event.identity.asset_id.contract_or_native in DEFAULT_ETHEREUM_ASSETS
    )


def _source_reference(events: tuple[OnChainTransferEvent, ...], snapshot: LabelSnapshot | None, kind: str) -> str:
    digest_input = "\n".join(event.event_id for event in events)
    if snapshot is not None:
        digest_input += "\n" + snapshot.snapshot_hash
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    return f"phase7:{kind}:{digest}"


def _label_coverage(events: tuple[OnChainTransferEvent, ...], snapshot: LabelSnapshot | None) -> tuple[int, int, Decimal]:
    observed: set[str] = set()
    labeled: set[str] = set()
    for event in events:
        observed.update(address for address in (event.from_address, event.to_address) if address is not None)
        labeled.update(_active_categories(snapshot, event))
    denominator = len(observed)
    numerator = len(labeled & observed)
    ratio = Decimal(numerator) / Decimal(denominator) if denominator else Decimal("0")
    return denominator, numerator, ratio


def aggregate_generic_flow_window(
    events: Iterable[OnChainTransferEvent],
    *,
    timeframe: str,
    window_open: datetime,
    snapshot: LabelSnapshot | None,
    processed_at: datetime,
) -> dict[str, object]:
    """Aggregate reviewed exchange flows only; raw transfers are not directional by guess."""
    if timeframe not in WINDOWS or window_open.tzinfo is None or window_open.utcoffset() != timedelta(0):
        raise ValueError("generic flow window must be a known UTC window")
    if processed_at.tzinfo is None or processed_at.utcoffset() != timedelta(0):
        raise ValueError("processed_at must be UTC-aware")
    items = tuple(event for event in _bounded_events(events, window_open=window_open, timeframe=timeframe)
                  if not _is_stablecoin(event))
    if not items:
        raise ValueError("generic flow requires at least one eligible canonical event")
    if len({event.identity.chain for event in items}) != 1 or len({event.identity.asset_id for event in items}) != 1:
        raise ValueError("generic flow window cannot mix chains or assets")
    chain = items[0].identity.chain
    if snapshot is not None and snapshot.chain is not chain:
        raise ValueError("label snapshot chain does not match generic flow events")

    outcomes = [_classify(event, snapshot) for event in items]
    inflow = outflow = Decimal("0")
    inflow_usd_values: list[Decimal] = []
    outflow_usd_values: list[Decimal] = []
    inflow_usd_complete = outflow_usd_complete = True
    unknown_count = available_count = 0
    for event, outcome in zip(items, outcomes, strict=True):
        if outcome is None:
            unknown_count += 1
            continue
        if outcome.status is DataStatus.AVAILABLE:
            available_count += 1
        if outcome.direction is ExchangeFlowDirection.UNKNOWN:
            unknown_count += 1
            continue
        if outcome.direction is ExchangeFlowDirection.INBOUND:
            inflow += event.amount.amount_normalized
            if event.valuation.status is DataStatus.AVAILABLE and event.valuation.amount_usd is not None:
                inflow_usd_values.append(event.valuation.amount_usd)
            else:
                inflow_usd_complete = False
        elif outcome.direction is ExchangeFlowDirection.OUTBOUND:
            outflow += event.amount.amount_normalized
            if event.valuation.status is DataStatus.AVAILABLE and event.valuation.amount_usd is not None:
                outflow_usd_values.append(event.valuation.amount_usd)
            else:
                outflow_usd_complete = False

    known, labeled, label_ratio = _label_coverage(items, snapshot)
    coverage_ratio = Decimal(available_count) / Decimal(len(items))
    if snapshot is None:
        status, reason = DataStatus.NOT_AVAILABLE, "LABEL_SNAPSHOT_NOT_CONFIGURED"
    elif known == 0 or label_ratio < MIN_LABEL_COVERAGE:
        status, reason = DataStatus.NOT_AVAILABLE, "INSUFFICIENT_LABEL_COVERAGE"
    elif label_ratio < Decimal("1") or unknown_count or available_count < len(items):
        status, reason = DataStatus.PARTIAL, "PARTIAL_LABEL_OR_EVENT_COVERAGE"
    else:
        status, reason = DataStatus.AVAILABLE, "COMPLETE_REVIEWED_EXCHANGE_FLOW"

    inflow_usd = sum(inflow_usd_values, Decimal("0")) if inflow_usd_complete else None
    outflow_usd = sum(outflow_usd_values, Decimal("0")) if outflow_usd_complete else None
    return {
        "chain": chain.value,
        "asset_id": _asset_id(items[0]),
        "timeframe": timeframe,
        "window_open": window_open,
        "window_close": window_open + WINDOWS[timeframe],
        "context_version": GENERIC_FLOW_VERSION,
        "flow_domain": FlowDomain.GENERIC_ONCHAIN.value,
        "aggregation_scope": "GENERIC",
        "aggregation_eligible": True,
        "bridge_leg_id": None,
        "inbound_amount": inflow,
        "outbound_amount": outflow,
        "net_amount": inflow - outflow,
        "inbound_amount_usd": inflow_usd,
        "outbound_amount_usd": outflow_usd,
        "net_amount_usd": inflow_usd - outflow_usd if inflow_usd is not None and outflow_usd is not None else None,
        "exchange_inflow_amount": inflow,
        "exchange_outflow_amount": outflow,
        "unknown_transfer_count": unknown_count,
        "sample_count": len(items),
        "source_count": len({event.provenance.source for event in items}),
        "available_count": available_count,
        "missing_count": len(items) - available_count,
        "labeled_count": labeled,
        "known_address_count": known,
        "labeled_address_count": labeled,
        "source_quality": (
            f"{snapshot.source_id}:{snapshot.source_version}:{snapshot.label_version}"[:128]
            if snapshot is not None else "NO_REVIEWED_LABEL_SNAPSHOT"
        ),
        "coverage_status": status.value,
        "coverage_ratio": coverage_ratio,
        "label_coverage_ratio": label_ratio,
        "status": status.value,
        "reason": reason,
        "source_version": ",".join(sorted({event.provenance.source_version for event in items}))[:128] or "UNKNOWN",
        "normalization_version": GENERIC_FLOW_VERSION,
        "source_reference": _source_reference(items, snapshot, "flow"),
        "processed_at": processed_at,
        "created_at": processed_at,
    }


def aggregate_whale_context_window(
    events: Iterable[OnChainTransferEvent],
    *,
    timeframe: str,
    window_open: datetime,
    snapshot: LabelSnapshot | None,
    threshold: WhaleThresholdConfig | None,
    processed_at: datetime,
) -> dict[str, object] | None:
    items = tuple(event for event in _bounded_events(events, window_open=window_open, timeframe=timeframe)
                  if not _is_stablecoin(event))
    if not items:
        return None
    asset_id = items[0].identity.asset_id
    chain = items[0].identity.chain
    if any(event.identity.asset_id != asset_id or event.identity.chain is not chain for event in items):
        raise ValueError("whale window cannot mix chains or assets")
    if threshold is None:
        known, labeled, label_ratio = _label_coverage(items, snapshot)
        return {
            "chain": chain.value, "asset_id": _asset_id(items[0]), "timeframe": timeframe,
            "window_open": window_open, "window_close": window_open + WINDOWS[timeframe],
            "aggregation_version": WHALE_AGGREGATION_VERSION,
            "flow_domain": FlowDomain.GENERIC_ONCHAIN.value, "aggregation_scope": "GENERIC",
            "aggregation_eligible": True, "bridge_leg_id": None, "threshold_version": "NOT_CONFIGURED",
            "threshold_tier": None, "large_inflow_count": 0, "large_outflow_count": 0,
            "large_inflow_amount": Decimal("0"), "large_outflow_amount": Decimal("0"),
            "large_inflow_usd": None, "large_outflow_usd": None,
            "exchange_inflow_amount": Decimal("0"), "exchange_outflow_amount": Decimal("0"),
            "threshold_not_evaluable_count": len(items), "unknown_transfer_count": len(items),
            "sample_count": len(items), "source_count": len({e.provenance.source for e in items}),
            "available_count": 0, "missing_count": len(items), "coverage_ratio": Decimal("0"),
            "known_address_count": known, "labeled_address_count": labeled, "labeled_count": labeled,
            "source_quality": snapshot.label_version if snapshot is not None else "NO_REVIEWED_LABEL_SNAPSHOT",
            "coverage_status": DataStatus.NOT_AVAILABLE.value, "label_coverage_ratio": label_ratio,
            "status": DataStatus.NOT_AVAILABLE.value, "reason": "THRESHOLD_NOT_CONFIGURED",
            "source_reference": _source_reference(items, snapshot, "whale"),
            "normalization_version": WHALE_AGGREGATION_VERSION,
            "processed_at": processed_at, "created_at": processed_at,
        }
    if threshold.chain is not chain or threshold.asset_id != asset_id:
        raise ValueError("whale threshold does not match chain/asset")
    contexts = []
    for event in items:
        outcome = _classify(event, snapshot)
        direction = WhaleDirection.UNKNOWN
        if outcome is not None:
            direction = {
                ExchangeFlowDirection.INBOUND: WhaleDirection.INBOUND,
                ExchangeFlowDirection.OUTBOUND: WhaleDirection.OUTBOUND,
                ExchangeFlowDirection.INTERNAL: WhaleDirection.PEER,
            }.get(outcome.direction, WhaleDirection.UNKNOWN)
        categories = _active_categories(snapshot, event)
        contexts.append(evaluate_whale_transfer(
            event,
            threshold,
            direction=direction,
            source_category=categories.get(event.from_address),
            destination_category=categories.get(event.to_address),
            label_coverage_ratio=(
                outcome.label_coverage_ratio
                if outcome is not None and outcome.known_address_count > 0 else None
            ),
            exchange_involvement=(
                outcome.source_category is LabelCategory.KNOWN_EXCHANGE
                or outcome.destination_category is LabelCategory.KNOWN_EXCHANGE
            ) if outcome is not None else None,
            known_address_count=outcome.known_address_count if outcome is not None else 0,
            labeled_address_count=outcome.labeled_address_count if outcome is not None else 0,
            flow_domain=FlowDomain.GENERIC_ONCHAIN,
            aggregation_eligible=True,
            freshness_status=event.status,
        ))
    result = aggregate_whale_window(
        contexts, chain=chain, asset_id=asset_id, timeframe=timeframe,
        window_open=window_open, aggregation_version=WHALE_AGGREGATION_VERSION,
        threshold_version=threshold.threshold_version, now=processed_at,
    )
    known, labeled, label_ratio = _label_coverage(items, snapshot)
    return {
        "chain": result.chain.value, "asset_id": _asset_id(items[0]),
        "timeframe": result.timeframe, "window_open": result.window_open,
        "window_close": result.window_close, "aggregation_version": result.aggregation_version,
        "flow_domain": result.flow_domain.value, "aggregation_scope": result.aggregation_scope,
        "aggregation_eligible": result.aggregation_eligible, "bridge_leg_id": None,
        "threshold_version": result.threshold_version, "threshold_tier": None,
        "large_inflow_count": result.large_inflow_count, "large_outflow_count": result.large_outflow_count,
        "large_inflow_amount": result.large_inflow_amount, "large_outflow_amount": result.large_outflow_amount,
        "large_inflow_usd": result.large_inflow_usd, "large_outflow_usd": result.large_outflow_usd,
        "exchange_inflow_amount": result.exchange_inflow_amount,
        "exchange_outflow_amount": result.exchange_outflow_amount,
        "threshold_not_evaluable_count": result.threshold_not_evaluable_count,
        "unknown_transfer_count": result.unknown_transfer_count,
        "sample_count": result.sample_count, "source_count": result.source_count,
        "available_count": result.available_count, "missing_count": result.missing_count,
        "coverage_ratio": result.coverage_ratio, "known_address_count": known,
        "labeled_address_count": labeled, "labeled_count": labeled,
        "source_quality": snapshot.label_version if snapshot is not None else "NO_REVIEWED_LABEL_SNAPSHOT",
        "coverage_status": result.status.value, "label_coverage_ratio": label_ratio,
        "status": result.status.value, "reason": result.reason,
        "source_reference": _source_reference(items, snapshot, "whale"),
        "normalization_version": WHALE_AGGREGATION_VERSION,
        "processed_at": processed_at, "created_at": processed_at,
    }


def aggregate_stablecoin_context_rows(
    events: Iterable[OnChainTransferEvent],
    *,
    timeframe: str,
    window_open: datetime,
    snapshot: LabelSnapshot | None,
    processed_at: datetime,
) -> tuple[dict[str, object], ...]:
    items = tuple(event for event in _bounded_events(events, window_open=window_open, timeframe=timeframe)
                  if _is_stablecoin(event))
    if not items:
        return ()
    if items[0].identity.chain is not Chain.ETHEREUM:
        raise ValueError("stablecoin context is Ethereum-only")
    registry = StablecoinRegistry.from_rows(
        {"symbol": definition["symbol"], "contract_address": address,
         "decimals": definition["decimals"], "issuer_addresses": (),
         "registry_version": definition["registry_version"]}
        for address, definition in DEFAULT_ETHEREUM_ASSETS.items()
    )
    transfers: list[StablecoinTransfer] = []
    for event in items:
        if event.identity.event_index is None or event.identity.tx_hash is None:
            continue
        if event.from_address is None or event.to_address is None:
            continue
        transfers.append(StablecoinTransfer(
            chain=Chain.ETHEREUM.value,
            symbol=str(DEFAULT_ETHEREUM_ASSETS[event.identity.asset_id.contract_or_native]["symbol"]),
            contract_address=event.identity.asset_id.contract_or_native,
            tx_hash=event.identity.tx_hash.removeprefix("0x"),
            log_index=event.identity.event_index,
            from_address=event.from_address,
            to_address=event.to_address,
            amount=event.amount.amount_normalized,
            event_timestamp=event.event_time,
            observed_at=event.provenance.observed_at,
            fetched_at=event.provenance.fetched_at,
            processed_at=event.provenance.processed_at,
            finality_status=event.finality_status,
        ))
    if not transfers:
        return ()
    # Classification and aggregation are deterministic; absent issuer data
    # leaves zero-address mint/burn observations UNKNOWN rather than guessed.
    groups: dict[StablecoinCategory, list[StablecoinTransfer]] = {}
    for transfer in transfers:
        classification = classify_stablecoin_transfer(
            transfer, registry=registry, label_snapshot=snapshot,
        )
        groups.setdefault(classification.category, []).append(transfer)
    rows = []
    for category, category_transfers in groups.items():
        result = aggregate_stablecoin_context(
            category_transfers, registry=registry, category=category,
            window_open=window_open, timeframe=timeframe, processed_at=processed_at,
            label_snapshot=snapshot, aggregation_version=STABLECOIN_AGGREGATION_VERSION,
        )
        rows.append(result.to_row(created_at=processed_at))
    return tuple(rows)


def _asset_id(event: OnChainTransferEvent) -> str:
    asset = event.identity.asset_id
    return f"{asset.chain.value}:{asset.kind.value}:{asset.contract_or_native}:{asset.registry_version}"
