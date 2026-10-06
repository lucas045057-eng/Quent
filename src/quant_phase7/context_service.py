"""Engine-owned replayable aggregation of persisted Phase 7 source events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping

from .contracts import Chain, DataStatus, FinalityStatus
from .context_pipeline import (
    WINDOWS,
    aggregate_generic_flow_window,
    aggregate_stablecoin_context_rows,
    aggregate_whale_context_window,
)
from .labels import LabelSnapshot
from .whale import WhaleThresholdConfig


def aggregate_closed_context_window(
    repository,
    *,
    timeframe: str,
    window_open: datetime,
    processed_at: datetime,
    label_snapshots: Mapping[Chain, LabelSnapshot],
    whale_thresholds: Mapping[tuple[Chain, str], WhaleThresholdConfig],
    chain: Chain | None = None,
    asset_id: str | None = None,
) -> dict[str, int]:
    """Recompute one closed window and idempotently persist each context family."""
    if timeframe not in WINDOWS:
        raise ValueError("unsupported Phase 7 aggregation timeframe")
    window_close = window_open + WINDOWS[timeframe]
    if (chain is None) != (asset_id is None):
        raise ValueError("context window target requires both chain and asset_id")
    assets = (
        ({"chain": chain.value, "asset_id": asset_id},)
        if chain is not None and asset_id is not None
        else repository.load_context_asset_ids(window_open, window_close)
    )
    flow_count = whale_count = stablecoin_count = event_count = 0
    for identity in assets:
        chain = Chain(identity["chain"])
        asset_id = identity["asset_id"]
        events = repository.load_context_events(chain.value, asset_id, window_open, window_close)
        events = tuple(
            event for event in events
            if event.status not in {DataStatus.STALE, DataStatus.ERROR, DataStatus.NOT_AVAILABLE}
            and event.finality_status is not FinalityStatus.REORGED
        )
        if not events:
            continue
        event_count += len(events)
        snapshot = label_snapshots.get(chain)
        stablecoin_events = tuple(
            event for event in events
            if event.identity.chain is Chain.ETHEREUM
            and event.identity.asset_id.kind.value == "ERC20"
            and event.identity.asset_id.contract_or_native in {
                "0xdac17f958d2ee523a2206206994597c13d831ec7",
                "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            }
        )
        stablecoin_ids = {event.event_id for event in stablecoin_events}
        generic_events = tuple(event for event in events if event.event_id not in stablecoin_ids)
        if generic_events:
            flow_row = aggregate_generic_flow_window(
                generic_events, timeframe=timeframe, window_open=window_open,
                snapshot=snapshot, processed_at=processed_at,
            )
            flow_count += repository.upsert_windows("phase7_onchain_flow_windows", [flow_row])
            threshold = whale_thresholds.get((chain, asset_id))
            whale_row = aggregate_whale_context_window(
                generic_events, timeframe=timeframe, window_open=window_open,
                snapshot=snapshot, threshold=threshold, processed_at=processed_at,
            )
            if whale_row is not None:
                whale_count += repository.upsert_windows("phase7_whale_flow_windows", [whale_row])
        stablecoin_rows = aggregate_stablecoin_context_rows(
            stablecoin_events, timeframe=timeframe, window_open=window_open,
            snapshot=snapshot, processed_at=processed_at,
        )
        if stablecoin_rows:
            stablecoin_count += repository.upsert_windows("phase7_stablecoin_context", stablecoin_rows)
    return {
        "assets": len(assets), "events": event_count, "flow_rows": flow_count,
        "whale_rows": whale_count, "stablecoin_rows": stablecoin_count,
    }


def closed_window_open(now: datetime, timeframe: str) -> datetime:
    """Return the latest fully closed UTC window's open timestamp."""
    if timeframe not in WINDOWS:
        raise ValueError("unsupported Phase 7 aggregation timeframe")
    duration = WINDOWS[timeframe]
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("context cycle time must be UTC-aware")
    timestamp = int(now.timestamp())
    current_open = datetime.fromtimestamp(
        timestamp - timestamp % int(duration.total_seconds()), tz=timezone.utc,
    )
    return current_open - duration
