"""Point-in-time universe breadth from aligned Phase 5 leader-style contexts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from quant_phase1.config import Settings
from quant_phase1.time import ensure_utc

from .contracts import (
    BreadthState,
    ContextStatus,
    MarketBreadthSnapshot,
    MarketLeaderContext,
    ReasonCode,
)


def _state(up_ratio: Decimal, down_ratio: Decimal, settings: Settings) -> BreadthState:
    broad = Decimal(str(settings.phase5_breadth_broad_ratio))
    narrow = Decimal(str(settings.phase5_breadth_narrow_ratio))
    if up_ratio >= broad:
        return BreadthState.BROAD_STRENGTH
    if up_ratio >= narrow:
        return BreadthState.NARROW_STRENGTH
    if down_ratio >= broad:
        return BreadthState.BROAD_WEAKNESS
    if down_ratio >= narrow:
        return BreadthState.NARROW_WEAKNESS
    return BreadthState.MIXED


def compute_market_breadth(
    universe_symbols: Sequence[str],
    contexts: Mapping[str, MarketLeaderContext],
    *,
    timeframe: str,
    context_timestamp: datetime,
    universe_run_id: int,
    settings: Settings,
    processed_at: datetime,
) -> MarketBreadthSnapshot:
    """Calculate breadth for exactly one immutable point-in-time universe run."""
    if not universe_symbols or len(set(universe_symbols)) != len(universe_symbols):
        raise ValueError("universe must contain unique symbols")
    if universe_run_id <= 0:
        raise ValueError("universe_run_id must be positive")
    context_timestamp = ensure_utc(context_timestamp)
    processed_at = ensure_utc(processed_at)
    universe = tuple(universe_symbols)
    extra_symbols = set(contexts).difference(universe)
    if extra_symbols:
        raise ValueError("context contains symbols outside the selected universe")

    available: list[MarketLeaderContext] = []
    missing_symbols: list[str] = []
    stale_symbols: list[str] = []
    error_symbols: list[str] = []
    for symbol in universe:
        context = contexts.get(symbol)
        if context is None:
            missing_symbols.append(symbol)
            continue
        if context.symbol != symbol:
            raise ValueError("context symbol does not match universe key")
        if context.timeframe != timeframe or context.context_timestamp != context_timestamp:
            raise ValueError("context_timestamp/timeframe does not match universe breadth")
        if context.status is ContextStatus.ERROR or context.freshness_status is ContextStatus.ERROR:
            error_symbols.append(symbol)
        elif context.status is ContextStatus.STALE or context.freshness_status is ContextStatus.STALE:
            stale_symbols.append(symbol)
        elif context.status is not ContextStatus.AVAILABLE or context.freshness_status is not ContextStatus.AVAILABLE:
            missing_symbols.append(symbol)
        else:
            available.append(context)

    sample_size = len(universe)
    available_count = len(available)
    missing_count = sample_size - available_count
    coverage_ratio = Decimal(available_count) / Decimal(sample_size)
    neutral_band = Decimal(str(settings.phase5_breadth_neutral_band_pct))
    positive_count = sum(1 for context in available if context.return_pct is not None and context.return_pct > neutral_band)
    negative_count = sum(1 for context in available if context.return_pct is not None and context.return_pct < -neutral_band)
    positive_ratio = Decimal(positive_count) / Decimal(available_count) if available_count else None
    down_ratio = Decimal(negative_count) / Decimal(available_count) if available_count else None
    structure_contexts = [
        context
        for context in available
        if context.structure_state.value != "NOT_AVAILABLE"
    ]
    up_structure_ratio = (
        Decimal(sum(context.structure_state.value == "HIGHER_HIGH_HIGHER_LOW" for context in structure_contexts))
        / Decimal(len(structure_contexts))
        if structure_contexts
        else None
    )
    down_structure_ratio = (
        Decimal(sum(context.structure_state.value == "LOWER_HIGH_LOWER_LOW" for context in structure_contexts))
        / Decimal(len(structure_contexts))
        if structure_contexts
        else None
    )
    all_missing_evidence = (
        [f"missing:{symbol}" for symbol in missing_symbols]
        + [f"stale:{symbol}" for symbol in stale_symbols]
        + [f"error:{symbol}" for symbol in error_symbols]
    )
    if len(all_missing_evidence) > 64:
        all_missing_evidence = [
            *all_missing_evidence[:63],
            f"omitted:{len(all_missing_evidence) - 63}",
        ]
    missing_evidence = tuple(all_missing_evidence)
    if stale_symbols:
        status = ContextStatus.STALE
        reason = ReasonCode.STALE_INPUT
    elif error_symbols:
        status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.SOURCE_UNAVAILABLE
    elif sample_size < settings.phase5_min_universe_sample or coverage_ratio < Decimal(str(settings.phase5_min_breadth_coverage)):
        status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.INSUFFICIENT_COVERAGE
    elif missing_count:
        status = ContextStatus.PARTIAL
        reason = None
    else:
        status = ContextStatus.AVAILABLE
        reason = None
    calculated = status in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL} and positive_ratio is not None and down_ratio is not None
    breadth_state = _state(positive_ratio, down_ratio, settings) if calculated else BreadthState.NOT_AVAILABLE
    if status in {ContextStatus.NOT_AVAILABLE, ContextStatus.STALE, ContextStatus.ERROR}:
        positive_ratio = None
        up_structure_ratio = None
        down_structure_ratio = None
    return MarketBreadthSnapshot(
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        universe_run_id=universe_run_id,
        sample_size=sample_size,
        available_count=available_count,
        missing_count=missing_count,
        coverage_ratio=coverage_ratio,
        positive_ratio=positive_ratio,
        up_structure_ratio=up_structure_ratio,
        down_structure_ratio=down_structure_ratio,
        breadth_state=breadth_state,
        status=status,
        processed_at=processed_at,
        missing_evidence=missing_evidence,
        input_reference={"source": "phase5_market_leader_context", "universe_run_id": universe_run_id},
        reason_code=reason,
    )
