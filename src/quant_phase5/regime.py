"""Independent market regime dimensions for Phase 5 context reporting."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from quant_phase1.config import Settings
from quant_phase1.time import ensure_utc

from .contracts import (
    BreadthState,
    ContextStatus,
    DirectionState,
    MarketBreadthSnapshot,
    MarketLeaderContext,
    MarketRegimeSnapshot,
    ReasonCode,
    VolatilityState,
)


_VOLATILITY_RANK = {
    VolatilityState.LOW: 0,
    VolatilityState.NORMAL: 1,
    VolatilityState.HIGH: 2,
    VolatilityState.EXTREME: 3,
}


def _leader_status(context: MarketLeaderContext | None) -> ContextStatus:
    if context is None:
        return ContextStatus.NOT_AVAILABLE
    if context.status is ContextStatus.ERROR or context.freshness_status is ContextStatus.ERROR:
        return ContextStatus.ERROR
    if context.status is ContextStatus.STALE or context.freshness_status is ContextStatus.STALE:
        return ContextStatus.STALE
    if context.status is ContextStatus.AVAILABLE and context.freshness_status is ContextStatus.AVAILABLE:
        return ContextStatus.AVAILABLE
    return ContextStatus.NOT_AVAILABLE


def _direction(
    leaders: Mapping[str, MarketLeaderContext],
    breadth: MarketBreadthSnapshot | None,
) -> tuple[DirectionState, ContextStatus, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    statuses = {symbol: _leader_status(leaders.get(symbol)) for symbol in ("BTCUSDT", "ETHUSDT")}
    missing: list[str] = [f"{symbol}_context" for symbol, status in statuses.items() if status is ContextStatus.NOT_AVAILABLE]
    support: list[str] = []
    conflict: list[str] = []
    if ContextStatus.ERROR in statuses.values():
        return DirectionState.NOT_AVAILABLE, ContextStatus.ERROR, tuple(support), tuple(conflict), tuple(missing + ["leader_error"])
    if ContextStatus.STALE in statuses.values():
        return DirectionState.NOT_AVAILABLE, ContextStatus.STALE, tuple(support), tuple(conflict), tuple(missing + ["leader_stale"])
    if breadth is not None and breadth.status is ContextStatus.ERROR:
        return DirectionState.NOT_AVAILABLE, ContextStatus.ERROR, tuple(support), tuple(conflict), ("breadth_error",)
    if breadth is not None and breadth.status is ContextStatus.STALE:
        return DirectionState.NOT_AVAILABLE, ContextStatus.STALE, tuple(support), tuple(conflict), ("breadth_stale",)
    usable = [leaders[symbol] for symbol, status in statuses.items() if status is ContextStatus.AVAILABLE]
    if len(usable) < 2:
        status = ContextStatus.PARTIAL if usable else ContextStatus.NOT_AVAILABLE
        return DirectionState.NOT_AVAILABLE, status, tuple(support), tuple(conflict), tuple(missing)
    if breadth is None:
        return DirectionState.NOT_AVAILABLE, ContextStatus.PARTIAL, tuple(support), tuple(conflict), ("breadth_context",)
    if breadth.status not in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL}:
        return DirectionState.NOT_AVAILABLE, ContextStatus.PARTIAL, tuple(support), tuple(conflict), ("breadth_unavailable",)
    leader_states = [context.trend_state for context in usable]
    if all(state is DirectionState.TREND_UP for state in leader_states) and breadth.breadth_state in {
        BreadthState.BROAD_STRENGTH, BreadthState.NARROW_STRENGTH
    }:
        label = DirectionState.TREND_UP
        support.extend(("btc_trend_up", "eth_trend_up", "breadth_strength"))
    elif all(state is DirectionState.TREND_DOWN for state in leader_states) and breadth.breadth_state in {
        BreadthState.BROAD_WEAKNESS, BreadthState.NARROW_WEAKNESS
    }:
        label = DirectionState.TREND_DOWN
        support.extend(("btc_trend_down", "eth_trend_down", "breadth_weakness"))
    elif all(state is DirectionState.RANGE for state in leader_states) and breadth.breadth_state is BreadthState.MIXED:
        label = DirectionState.RANGE
        support.extend(("btc_range", "eth_range", "breadth_mixed"))
    else:
        label = DirectionState.MIXED
        conflict.append("leader_breadth_conflict")
    return label, breadth.status, tuple(support), tuple(conflict), tuple(missing)


def _volatility(
    leaders: Mapping[str, MarketLeaderContext],
) -> tuple[VolatilityState, ContextStatus, tuple[str, ...]]:
    statuses = {symbol: _leader_status(leaders.get(symbol)) for symbol in ("BTCUSDT", "ETHUSDT")}
    missing = tuple(f"{symbol}_volatility" for symbol, status in statuses.items() if status is ContextStatus.NOT_AVAILABLE)
    if ContextStatus.ERROR in statuses.values():
        return VolatilityState.NOT_AVAILABLE, ContextStatus.ERROR, missing + ("leader_error",)
    if ContextStatus.STALE in statuses.values():
        return VolatilityState.NOT_AVAILABLE, ContextStatus.STALE, missing + ("leader_stale",)
    usable = [leaders[symbol] for symbol, status in statuses.items() if status is ContextStatus.AVAILABLE]
    if not usable:
        return VolatilityState.NOT_AVAILABLE, ContextStatus.NOT_AVAILABLE, missing
    state = max((context.volatility_state for context in usable), key=_VOLATILITY_RANK.__getitem__)
    status = ContextStatus.AVAILABLE if len(usable) == 2 else ContextStatus.PARTIAL
    return state, status, missing


def compute_market_regime(
    leaders: Mapping[str, MarketLeaderContext],
    breadth: MarketBreadthSnapshot | None,
    *,
    timeframe: str,
    context_timestamp: datetime,
    settings: Settings,
    processed_at: datetime,
) -> MarketRegimeSnapshot:
    """Combine independent context dimensions without creating a scalar score."""
    del settings  # thresholds are applied by the dimension producers.
    context_timestamp = ensure_utc(context_timestamp)
    processed_at = ensure_utc(processed_at)
    if set(leaders).difference({"BTCUSDT", "ETHUSDT"}):
        raise ValueError("regime accepts BTCUSDT and ETHUSDT leaders only")
    for symbol, context in leaders.items():
        if context.symbol != symbol or context.timeframe != timeframe or context.context_timestamp != context_timestamp:
            raise ValueError("leader context identity does not match regime")
    if breadth is not None and (breadth.timeframe != timeframe or breadth.context_timestamp != context_timestamp):
        raise ValueError("breadth identity does not match regime")
    direction, direction_status, support, conflict, missing = _direction(leaders, breadth)
    volatility, volatility_status, volatility_missing = _volatility(leaders)
    if breadth is None:
        breadth_regime = BreadthState.NOT_AVAILABLE
        breadth_status = ContextStatus.NOT_AVAILABLE
        breadth_missing = ("breadth_context",)
    else:
        breadth_status = breadth.status
        breadth_regime = breadth.breadth_state if breadth_status in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL} else BreadthState.NOT_AVAILABLE
        if breadth_status is ContextStatus.AVAILABLE:
            breadth_missing = ()
        elif breadth_status is ContextStatus.PARTIAL:
            breadth_missing = breadth.missing_evidence or ("breadth_partial",)
        else:
            breadth_missing = ("breadth_unavailable",)
    dimension_statuses = (direction_status, volatility_status, breadth_status)
    if ContextStatus.ERROR in dimension_statuses:
        status = ContextStatus.ERROR
        reason = ReasonCode.CALCULATION_ERROR
    elif ContextStatus.STALE in dimension_statuses:
        status = ContextStatus.STALE
        reason = ReasonCode.STALE_INPUT
    elif all(item is ContextStatus.AVAILABLE for item in dimension_statuses):
        status = ContextStatus.AVAILABLE
        reason = None
    elif any(item in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL} for item in dimension_statuses):
        status = ContextStatus.PARTIAL
        reason = None
    else:
        status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.MISSING_INPUT
    all_missing = tuple(dict.fromkeys(missing + volatility_missing + breadth_missing))
    if status in {ContextStatus.ERROR, ContextStatus.STALE, ContextStatus.NOT_AVAILABLE} and not all_missing:
        all_missing = ("regime_dimension",)
    return MarketRegimeSnapshot(
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        universe_run_id=breadth.universe_run_id if breadth is not None else None,
        direction_regime=direction,
        volatility_regime=volatility,
        breadth_regime=breadth_regime,
        direction_status=direction_status,
        volatility_status=volatility_status,
        breadth_status=breadth_status,
        status=status,
        support_evidence=support,
        conflict_evidence=conflict,
        missing_evidence=all_missing,
        processed_at=processed_at,
        sample_size=breadth.sample_size if breadth is not None else 0,
        available_count=breadth.available_count if breadth is not None else 0,
        missing_count=breadth.missing_count if breadth is not None else 0,
        coverage_ratio=breadth.coverage_ratio if breadth is not None else None,
        reason_code=reason,
    )
