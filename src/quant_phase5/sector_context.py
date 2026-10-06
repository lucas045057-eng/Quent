"""Deterministic aggregate sector context over point-in-time universe members."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from quant_phase1.config import Settings
from quant_phase1.time import ensure_utc

from .contracts import ContextStatus, MarketLeaderContext, ReasonCode, SectorContextSnapshot, SectorMembership
from .sector_taxonomy import resolve_sector


def _context_status(context: MarketLeaderContext | None) -> ContextStatus:
    if context is None:
        return ContextStatus.NOT_AVAILABLE
    if context.status is ContextStatus.ERROR or context.freshness_status is ContextStatus.ERROR:
        return ContextStatus.ERROR
    if context.status is ContextStatus.STALE or context.freshness_status is ContextStatus.STALE:
        return ContextStatus.STALE
    if context.status is ContextStatus.AVAILABLE and context.freshness_status is ContextStatus.AVAILABLE:
        return ContextStatus.AVAILABLE
    return ContextStatus.NOT_AVAILABLE


def compute_sector_context(
    sector: str,
    mapping_version: str,
    universe_symbols: Sequence[str],
    memberships: Sequence[SectorMembership],
    contexts: Mapping[str, MarketLeaderContext | None],
    *,
    timeframe: str,
    context_timestamp: datetime,
    universe_run_id: int,
    settings: Settings,
    processed_at: datetime,
) -> SectorContextSnapshot:
    """Compute one aggregate sector row; candidate-specific comparisons stay elsewhere."""
    if universe_run_id <= 0:
        raise ValueError("universe_run_id must be positive")
    context_timestamp = ensure_utc(context_timestamp)
    processed_at = ensure_utc(processed_at)
    universe = tuple(universe_symbols)
    if len(set(universe)) != len(universe):
        raise ValueError("universe must contain unique symbols")
    if set(contexts).difference(universe):
        raise ValueError("sector context contains symbols outside universe")
    mapped_members = []
    for symbol in universe:
        membership = resolve_sector(symbol, memberships, context_timestamp, mapping_version=mapping_version)
        if membership.sector == sector and membership.status.value == "AVAILABLE":
            mapped_members.append(symbol)
    member_count = len(mapped_members)
    if sector == "UNKNOWN" or member_count == 0:
        return SectorContextSnapshot(
            sector=sector,
            timeframe=timeframe,
            context_timestamp=context_timestamp,
            universe_run_id=universe_run_id,
            mapping_version=mapping_version,
            sector_return_pct=None,
            sector_positive_ratio=None,
            member_count=member_count,
            sample_size=member_count,
            available_count=0,
            missing_count=member_count,
            coverage_ratio=Decimal("0") if member_count else None,
            status=ContextStatus.NOT_AVAILABLE,
            processed_at=processed_at,
            missing_evidence=("unknown_sector",) if sector == "UNKNOWN" else ("no_sector_members",),
            reason_code=ReasonCode.UNKNOWN_SECTOR if sector == "UNKNOWN" else ReasonCode.NO_ELIGIBLE_MEMBERS,
            input_reference={"source": "phase5_sector_taxonomy", "mapping_version": mapping_version},
        )
    if member_count < settings.phase5_min_sector_members:
        return SectorContextSnapshot(
            sector=sector, timeframe=timeframe, context_timestamp=context_timestamp,
            universe_run_id=universe_run_id, mapping_version=mapping_version,
            sector_return_pct=None, sector_positive_ratio=None,
            member_count=member_count, sample_size=member_count, available_count=0,
            missing_count=member_count, coverage_ratio=Decimal("0"), status=ContextStatus.NOT_AVAILABLE,
            processed_at=processed_at, missing_evidence=("minimum_sector_members",),
            reason_code=ReasonCode.INSUFFICIENT_COVERAGE,
            input_reference={"source": "phase5_sector_taxonomy", "mapping_version": mapping_version},
        )
    available: list[MarketLeaderContext] = []
    missing: list[str] = []
    stale: list[str] = []
    error: list[str] = []
    for symbol in mapped_members:
        context = contexts.get(symbol)
        if context is not None and (
            context.symbol != symbol or context.timeframe != timeframe or context.context_timestamp != context_timestamp
        ):
            raise ValueError("sector context identity does not match context")
        status = _context_status(context)
        if status is ContextStatus.AVAILABLE and context is not None and context.return_pct is not None:
            available.append(context)
        elif status is ContextStatus.STALE:
            stale.append(symbol)
        elif status is ContextStatus.ERROR:
            error.append(symbol)
        else:
            missing.append(symbol)
    available_count = len(available)
    missing_count = member_count - available_count
    coverage_ratio = Decimal(available_count) / Decimal(member_count)
    evidence = tuple([f"missing:{symbol}" for symbol in missing] + [f"stale:{symbol}" for symbol in stale] + [f"error:{symbol}" for symbol in error])
    if stale:
        status = ContextStatus.STALE
        reason = ReasonCode.STALE_INPUT
    elif error:
        status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.SOURCE_UNAVAILABLE
    elif coverage_ratio < Decimal(str(settings.phase5_min_breadth_coverage)):
        status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.INSUFFICIENT_COVERAGE
    elif missing_count:
        status = ContextStatus.PARTIAL
        reason = None
    else:
        status = ContextStatus.AVAILABLE
        reason = None
    if status in {ContextStatus.NOT_AVAILABLE, ContextStatus.STALE}:
        sector_return = None
        positive_ratio = None
    else:
        sector_return = sum(context.return_pct for context in available if context.return_pct is not None) / Decimal(available_count)
        positive_ratio = Decimal(sum(context.return_pct > 0 for context in available if context.return_pct is not None)) / Decimal(available_count)
    return SectorContextSnapshot(
        sector=sector,
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        universe_run_id=universe_run_id,
        mapping_version=mapping_version,
        sector_return_pct=sector_return,
        sector_positive_ratio=positive_ratio,
        member_count=member_count,
        sample_size=member_count,
        available_count=available_count,
        missing_count=missing_count,
        coverage_ratio=coverage_ratio,
        status=status,
        processed_at=processed_at,
        missing_evidence=evidence,
        reason_code=reason,
        input_reference={"source": "phase5_sector_taxonomy", "mapping_version": mapping_version},
    )
