"""Centralized relative-strength calculations for Phase 5."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from quant_phase1.config import Settings
from quant_phase1.time import ensure_utc

from .contracts import (
    ContextStatus,
    MarketLeaderContext,
    ReasonCode,
    RelativeStrengthClass,
    RelativeStrengthSnapshot,
)


def _status(context: MarketLeaderContext | None) -> ContextStatus:
    if context is None:
        return ContextStatus.NOT_AVAILABLE
    if context.status is ContextStatus.ERROR or context.freshness_status is ContextStatus.ERROR:
        return ContextStatus.ERROR
    if context.status is ContextStatus.STALE or context.freshness_status is ContextStatus.STALE:
        return ContextStatus.STALE
    if context.status is ContextStatus.AVAILABLE and context.freshness_status is ContextStatus.AVAILABLE:
        return ContextStatus.AVAILABLE
    return ContextStatus.NOT_AVAILABLE


def _empty(
    *,
    symbol: str,
    benchmark: str,
    timeframe: str,
    context_timestamp: datetime,
    processed_at: datetime,
    universe_run_id: int | None,
    sample_size: int,
    available_count: int,
    missing_count: int,
    reason: ReasonCode,
    status: ContextStatus = ContextStatus.NOT_AVAILABLE,
    missing_evidence: tuple[str, ...] = (),
) -> RelativeStrengthSnapshot:
    coverage = Decimal(available_count) / Decimal(sample_size) if sample_size else None
    return RelativeStrengthSnapshot(
        symbol=symbol,
        benchmark=benchmark,
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        candidate_return_pct=None,
        benchmark_return_pct=None,
        relative_return_pct=None,
        relative_class=RelativeStrengthClass.NOT_AVAILABLE,
        universe_run_id=universe_run_id,
        sample_size=sample_size,
        available_count=available_count,
        missing_count=missing_count,
        coverage_ratio=coverage,
        status=status,
        processed_at=processed_at,
        missing_evidence=missing_evidence or ("relative_strength_input",),
        input_reference={"source": "phase5_market_leader_context", "benchmark": benchmark},
        reason_code=reason,
    )


def compute_relative_strength(
    symbol: str,
    candidate_context: MarketLeaderContext | None,
    benchmark: str,
    benchmark_context: MarketLeaderContext | None,
    *,
    timeframe: str,
    context_timestamp: datetime,
    processed_at: datetime,
    settings: Settings,
    universe_run_id: int | None = None,
    universe_contexts: Mapping[str, MarketLeaderContext | None] | None = None,
) -> RelativeStrengthSnapshot:
    """Return candidate minus benchmark in percentage points.

    The market benchmark is the equal-weight arithmetic mean of available,
    point-in-time universe members after excluding the candidate symbol.
    """
    if benchmark not in {"BTCUSDT", "ETHUSDT", "MARKET_UNIVERSE_EQUAL_WEIGHT"}:
        raise ValueError("unsupported relative-strength benchmark")
    if timeframe not in {"15m", "1H", "4H"}:
        raise ValueError("relative strength supports 15m, 1H, and 4H only")
    context_timestamp = ensure_utc(context_timestamp)
    processed_at = ensure_utc(processed_at)
    if candidate_context is not None and (
        candidate_context.symbol != symbol
        or candidate_context.timeframe != timeframe
        or candidate_context.context_timestamp != context_timestamp
    ):
        raise ValueError("candidate context identity does not match relative strength")
    candidate_status = _status(candidate_context)
    if symbol == benchmark and benchmark != "MARKET_UNIVERSE_EQUAL_WEIGHT":
        return _empty(
            symbol=symbol, benchmark=benchmark, timeframe=timeframe,
            context_timestamp=context_timestamp, processed_at=processed_at,
            universe_run_id=None, sample_size=0, available_count=0, missing_count=0,
            reason=ReasonCode.MISSING_INPUT,
        )
    if candidate_status is ContextStatus.STALE:
        return _empty(
            symbol=symbol, benchmark=benchmark, timeframe=timeframe,
            context_timestamp=context_timestamp, processed_at=processed_at,
            universe_run_id=universe_run_id if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else None,
            sample_size=1, available_count=0, missing_count=1, reason=ReasonCode.STALE_INPUT,
            status=ContextStatus.STALE, missing_evidence=("candidate_stale",),
        )
    if candidate_status in {ContextStatus.ERROR, ContextStatus.NOT_AVAILABLE} or candidate_context is None:
        return _empty(
            symbol=symbol, benchmark=benchmark, timeframe=timeframe,
            context_timestamp=context_timestamp, processed_at=processed_at,
            universe_run_id=universe_run_id if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else None,
            sample_size=0, available_count=0, missing_count=0,
            reason=ReasonCode.SOURCE_UNAVAILABLE if candidate_status is ContextStatus.ERROR else ReasonCode.MISSING_INPUT,
            missing_evidence=("candidate_context",),
        )
    if benchmark != "MARKET_UNIVERSE_EQUAL_WEIGHT":
        if benchmark_context is None or benchmark_context.symbol != benchmark:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=None, sample_size=1, available_count=0, missing_count=1,
                reason=ReasonCode.MISSING_INPUT, missing_evidence=("benchmark_context",),
            )
        if benchmark_context.timeframe != timeframe or benchmark_context.context_timestamp != context_timestamp:
            raise ValueError("benchmark context identity does not match relative strength")
        benchmark_status = _status(benchmark_context)
        if benchmark_status is ContextStatus.STALE:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=None, sample_size=1, available_count=0, missing_count=1,
                reason=ReasonCode.STALE_INPUT, status=ContextStatus.STALE,
                missing_evidence=("benchmark_stale",),
            )
        if benchmark_status is not ContextStatus.AVAILABLE or benchmark_context.return_pct is None:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=None, sample_size=1, available_count=0, missing_count=1,
                reason=ReasonCode.SOURCE_UNAVAILABLE if benchmark_status is ContextStatus.ERROR else ReasonCode.MISSING_INPUT,
                missing_evidence=("benchmark_context",),
            )
        benchmark_return = benchmark_context.return_pct
        sample_size = available_count = 1
        missing_count = 0
        universe_identity = None
    else:
        if universe_run_id is None or universe_run_id <= 0:
            raise ValueError("market benchmark requires positive universe_run_id")
        if universe_contexts is None or symbol not in universe_contexts:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=universe_run_id, sample_size=0, available_count=0, missing_count=0,
                reason=ReasonCode.MISSING_INPUT, missing_evidence=("universe_snapshot",),
            )
        sample_contexts = []
        stale = False
        error = False
        missing = 0
        missing_symbols: list[str] = []
        for member_symbol, member_context in universe_contexts.items():
            if member_context is not None and member_context.symbol != member_symbol:
                raise ValueError("universe context symbol does not match key")
            if member_context is not None and (
                member_context.timeframe != timeframe or member_context.context_timestamp != context_timestamp
            ):
                raise ValueError("universe context identity does not match relative strength")
            if member_symbol == symbol:
                continue
            sample_contexts.append(member_context)
            if member_context is None or _status(member_context) is ContextStatus.NOT_AVAILABLE:
                missing += 1
                missing_symbols.append(member_symbol)
                continue
            member_status = _status(member_context)
            if member_status is ContextStatus.STALE:
                stale = True
            elif member_status is ContextStatus.ERROR:
                error = True
            elif member_status is ContextStatus.AVAILABLE and member_context.return_pct is not None:
                continue
            else:
                missing += 1
        sample_size = len(sample_contexts)
        available_count = sum(
            1 for item in sample_contexts
            if item is not None and _status(item) is ContextStatus.AVAILABLE and item.return_pct is not None
        )
        missing_count = sample_size - available_count
        if stale:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=universe_run_id, sample_size=sample_size,
                available_count=available_count, missing_count=missing_count,
                reason=ReasonCode.STALE_INPUT, status=ContextStatus.STALE,
                missing_evidence=("benchmark_stale",),
            )
        if error:
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=universe_run_id, sample_size=sample_size,
                available_count=available_count, missing_count=missing_count,
                reason=ReasonCode.SOURCE_UNAVAILABLE, missing_evidence=("benchmark_error",),
            )
        coverage = Decimal(available_count) / Decimal(sample_size) if sample_size else None
        if sample_size < settings.phase5_min_market_benchmark_sample or coverage is None or coverage < Decimal(str(settings.phase5_min_breadth_coverage)):
            return _empty(
                symbol=symbol, benchmark=benchmark, timeframe=timeframe,
                context_timestamp=context_timestamp, processed_at=processed_at,
                universe_run_id=universe_run_id, sample_size=sample_size,
                available_count=available_count, missing_count=missing_count,
                reason=ReasonCode.INSUFFICIENT_COVERAGE, missing_evidence=("benchmark_coverage",),
            )
        benchmark_return = sum(item.return_pct for item in sample_contexts if item is not None and _status(item) is ContextStatus.AVAILABLE and item.return_pct is not None) / Decimal(available_count)
        universe_identity = universe_run_id
        market_status = ContextStatus.PARTIAL if missing_count else ContextStatus.AVAILABLE
        market_missing_evidence = tuple(f"missing:{member_symbol}" for member_symbol in missing_symbols)
    relative = candidate_context.return_pct - benchmark_return
    negative, positive = (Decimal(str(value)) for value in settings.phase5_relative_strength_thresholds_pct[timeframe])
    if relative >= positive:
        relative_class = RelativeStrengthClass.STRONG
    elif relative <= negative:
        relative_class = RelativeStrengthClass.WEAK
    else:
        relative_class = RelativeStrengthClass.NEUTRAL
    return RelativeStrengthSnapshot(
        symbol=symbol,
        benchmark=benchmark,
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        candidate_return_pct=candidate_context.return_pct,
        benchmark_return_pct=benchmark_return,
        relative_return_pct=relative,
        relative_class=relative_class,
        universe_run_id=universe_identity,
        sample_size=sample_size,
        available_count=available_count,
        missing_count=missing_count,
        coverage_ratio=Decimal(available_count) / Decimal(sample_size),
        status=market_status if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else ContextStatus.AVAILABLE,
        processed_at=processed_at,
        missing_evidence=market_missing_evidence if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else (),
        input_reference={"source": "phase5_market_leader_context", "benchmark": benchmark},
    )
