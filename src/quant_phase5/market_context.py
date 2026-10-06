"""Deterministic BTC/ETH market-leader context over canonical closed bars."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Mapping, Sequence

from quant_phase1.config import Settings
from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.freshness import INTERVAL_SECONDS, evaluate_kline_freshness, expected_latest_closed_open
from quant_phase1.market.closed_bars import accept_closed_candle
from quant_phase1.market.structure import classify_structure
from quant_phase1.time import ensure_utc

from .contracts import (
    _decimal,
    ContextStatus,
    DirectionState,
    MarketLeaderContext,
    ReasonCode,
    StructureState,
    VolatilityState,
    VolumeState,
)


LEADERS = frozenset({"BTCUSDT", "ETHUSDT"})
CALCULATION_VERSION = "phase5-v1"


def _finite(value: Decimal, field_name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return value


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    rounded_seconds = round(max(0.0, (end - start).total_seconds()), 3)
    return _decimal(rounded_seconds, "elapsed_seconds")


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    if not values or period <= 0:
        raise ValueError("EMA requires values and a positive period")
    alpha = Decimal(2) / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal(1) - alpha) * result
    return _finite(result, "ema")


def classify_volatility(interval: str, value: Decimal, settings: Settings) -> VolatilityState:
    """Apply the configured per-bar volatility thresholds with exact boundaries."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval}")
    value = _finite(value, "realized_volatility")
    low, high, extreme = (Decimal(str(item)) for item in settings.phase5_volatility_thresholds[interval])
    if value < low:
        return VolatilityState.LOW
    if value < high:
        return VolatilityState.NORMAL
    if value < extreme:
        return VolatilityState.HIGH
    return VolatilityState.EXTREME


def _classify_volume(ratio: Decimal, settings: Settings) -> VolumeState:
    ratio = _finite(ratio, "volume_ratio")
    if ratio < Decimal(str(settings.phase5_volume_below_ratio)):
        return VolumeState.BELOW_BASELINE
    if ratio >= Decimal(str(settings.phase5_volume_elevated_ratio)):
        return VolumeState.ELEVATED
    return VolumeState.NORMAL


def _structure_state(candles: Sequence[Candle]) -> StructureState:
    structure = classify_structure(candles)
    if structure.trend == "BULLISH":
        return StructureState.HIGHER_HIGH_HIGHER_LOW
    if structure.trend == "BEARISH":
        return StructureState.LOWER_HIGH_LOWER_LOW
    return StructureState.RANGE


def _direction_state(
    candles: Sequence[Candle],
    settings: Settings,
    structure_state: StructureState,
) -> tuple[DirectionState, tuple[str, ...], tuple[str, ...]]:
    closes = [candle.close for candle in candles]
    ema_fast = _ema(closes, settings.phase5_ema_fast_period)
    ema_slow = _ema(closes, settings.phase5_ema_slow_period)
    first_close = closes[0]
    if first_close <= 0:
        raise ValueError("close must be positive")
    slope_pct = (closes[-1] / first_close - Decimal("1")) * Decimal("100")
    threshold = Decimal(str(settings.phase5_slope_threshold_pct))
    ema_up = ema_fast > ema_slow
    ema_down = ema_fast < ema_slow
    slope_up = slope_pct >= threshold
    slope_down = slope_pct <= -threshold
    structure_up = structure_state is StructureState.HIGHER_HIGH_HIGHER_LOW
    structure_down = structure_state is StructureState.LOWER_HIGH_LOWER_LOW
    up_count = sum((ema_up, slope_up, structure_up))
    down_count = sum((ema_down, slope_down, structure_down))
    support: list[str] = []
    conflict: list[str] = []
    if ema_up:
        support.append("ema_fast_above_ema_slow")
    elif ema_down:
        conflict.append("ema_fast_below_ema_slow")
    if slope_up:
        support.append("close_slope_above_threshold")
    elif slope_down:
        conflict.append("close_slope_below_threshold")
    if structure_up:
        support.append("higher_high_higher_low_structure")
    elif structure_down:
        conflict.append("lower_high_lower_low_structure")
    if up_count >= 2 and down_count == 0:
        state = DirectionState.TREND_UP
    elif down_count >= 2 and up_count == 0:
        state = DirectionState.TREND_DOWN
    elif not ema_up and not ema_down and not slope_up and not slope_down and structure_state is StructureState.RANGE:
        state = DirectionState.RANGE
    else:
        state = DirectionState.MIXED
    return state, tuple(support), tuple(conflict)


def _realized_volatility(candles: Sequence[Candle]) -> Decimal:
    returns: list[Decimal] = []
    for previous, current in zip(candles, candles[1:]):
        if previous.close <= 0 or current.close <= 0:
            raise ValueError("close must be positive")
        try:
            returns.append(_finite((current.close / previous.close).ln(), "log_return"))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid log return") from exc
    if not returns:
        raise ValueError("volatility window is empty")
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum((value - mean) ** 2 for value in returns) / Decimal(len(returns))
    return _finite(variance.sqrt(), "realized_volatility")


def _unavailable(
    *,
    symbol: str,
    timeframe: str,
    processed_at: datetime,
    status: ContextStatus,
    freshness_status: ContextStatus,
    reason_code: ReasonCode,
    evidence: tuple[str, ...],
    closed_bar_count: int,
    minimum_closed_bars: int,
    freshness_details: Mapping[str, Any] | None = None,
) -> MarketLeaderContext:
    return MarketLeaderContext(
        symbol=symbol,
        timeframe=timeframe,
        context_timestamp=processed_at,
        input_window_start=None,
        input_window_end=None,
        return_pct=None,
        trend_state=DirectionState.NOT_AVAILABLE,
        structure_state=StructureState.NOT_AVAILABLE,
        volatility_state=VolatilityState.NOT_AVAILABLE,
        volume_state=VolumeState.NOT_AVAILABLE,
        volatility_value=None,
        volume_ratio=None,
        freshness_status=freshness_status,
        data_quality={
            "closed_bar_count": closed_bar_count,
            "minimum_closed_bars": minimum_closed_bars,
            "status": status.value,
            **dict(freshness_details or {}),
        },
        source_count=0,
        missing_count=1,
        status=status,
        processed_at=processed_at,
        reason_code=reason_code,
        missing_evidence=evidence,
        calculation_version=CALCULATION_VERSION,
        input_reference={"source": "phase1_canonical_closed_klines", "symbol": symbol, "timeframe": timeframe},
    )


def build_market_leader_context(
    candles: Sequence[Candle],
    settings: Settings,
    *,
    now: datetime,
    processed_at: datetime,
    symbol: str | None = None,
    timeframe: str | None = None,
    allow_non_leader: bool = False,
) -> MarketLeaderContext:
    """Build one context row from canonical, same-symbol, closed candles only.

    The public/default contract remains BTCUSDT and ETHUSDT leaders.  The
    explicit ``allow_non_leader`` path is used only by the breadth/relative
    strength/sector runtime, which reuses the same closed-bar calculation for
    point-in-time universe members without introducing a second formula.
    """
    if symbol is None:
        symbol = candles[0].symbol if candles else "BTCUSDT"
    if timeframe is None:
        timeframe = candles[0].interval if candles else "5m"
    if symbol not in LEADERS and not allow_non_leader:
        raise ValueError("market leader must be BTCUSDT or ETHUSDT")
    if timeframe not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {timeframe}")
    now = ensure_utc(now)
    processed_at = ensure_utc(processed_at)
    interval_seconds = INTERVAL_SECONDS[timeframe]
    minimum_closed_bars = max(
        settings.phase5_min_closed_bars,
        settings.phase5_return_lookback_bars + 1,
        settings.phase5_vol_window_bars + 1,
        settings.phase5_volume_baseline_bars + 1,
        settings.phase5_ema_slow_period,
    )
    matching = [
        candle
        for candle in candles
        if candle.symbol == symbol and candle.interval == timeframe and candle.status is DataStatus.AVAILABLE
    ]
    misaligned = [
        candle
        for candle in matching
        if int(candle.bar_open_timestamp.timestamp()) % interval_seconds != 0
    ]
    if misaligned:
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.NOT_AVAILABLE,
            freshness_status=ContextStatus.NOT_AVAILABLE,
            reason_code=ReasonCode.TIMESTAMP_SKEW,
            evidence=("bar_open_timestamp_alignment",),
            closed_bar_count=0,
            minimum_closed_bars=minimum_closed_bars,
        )
    closed = sorted(
        (candle for candle in matching if accept_closed_candle(candle, now)),
        key=lambda candle: candle.bar_open_timestamp,
    )
    latest_open = closed[-1].bar_open_timestamp if closed else None
    freshness = ContextStatus(
        evaluate_kline_freshness(
            now,
            timeframe,
            latest_open,
            grace_seconds=settings.kline_ingestion_grace_seconds[timeframe],
        ).value
    )
    latest_candle = closed[-1] if closed else None
    source_timestamp = (
        latest_open + timedelta(seconds=interval_seconds) if latest_open is not None else None
    )
    expected_timestamp = expected_latest_closed_open(now, timeframe) + timedelta(seconds=interval_seconds)
    freshness_reason = (
        "NO_RECENT_SOURCE_DATA" if latest_candle is None else
        "SOURCE_NOT_ADVANCING" if freshness is ContextStatus.STALE else None
    )
    freshness_age = (
        _elapsed_seconds(source_timestamp, processed_at)
        if source_timestamp is not None else None
    )
    freshness_details = {
        "source_timestamp": source_timestamp.isoformat() if source_timestamp else None,
        "exchange_timestamp": latest_candle.exchange_timestamp.isoformat() if latest_candle else None,
        "fetched_at": latest_candle.fetched_at.isoformat() if latest_candle else None,
        "checked_at": processed_at.isoformat(),
        "freshness_age_seconds": round(freshness_age, 3) if freshness_age is not None else None,
        "freshness_threshold_seconds": interval_seconds + settings.kline_ingestion_grace_seconds[timeframe],
        "expected_latest_closed_at": expected_timestamp.isoformat(),
        "reason": freshness_reason,
        "context_timestamp": None,
        "context_age_seconds": None,
    }
    if freshness is ContextStatus.STALE:
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.STALE,
            freshness_status=ContextStatus.STALE,
            reason_code=ReasonCode.STALE_INPUT,
            evidence=("latest_closed_bar",),
            closed_bar_count=len(closed),
            minimum_closed_bars=minimum_closed_bars,
            freshness_details=freshness_details,
        )
    if freshness in {ContextStatus.ERROR, ContextStatus.NOT_AVAILABLE}:
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.ERROR if freshness is ContextStatus.ERROR else ContextStatus.NOT_AVAILABLE,
            freshness_status=freshness,
            reason_code=ReasonCode.CALCULATION_ERROR if freshness is ContextStatus.ERROR else ReasonCode.MISSING_INPUT,
            evidence=("latest_closed_bar",),
            closed_bar_count=len(closed),
            minimum_closed_bars=minimum_closed_bars,
            freshness_details=freshness_details,
        )
    if len(closed) < minimum_closed_bars:
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.NOT_AVAILABLE,
            freshness_status=ContextStatus.AVAILABLE,
            reason_code=ReasonCode.MISSING_INPUT,
            evidence=("minimum_closed_bar_window",),
            closed_bar_count=len(closed),
            minimum_closed_bars=minimum_closed_bars,
            freshness_details={**freshness_details, "reason": "INSUFFICIENT_CLOSED_BARS"},
        )
    window = closed[-minimum_closed_bars:]
    if any(
        current.bar_open_timestamp - previous.bar_open_timestamp != timedelta(seconds=interval_seconds)
        for previous, current in zip(window, window[1:])
    ):
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.NOT_AVAILABLE,
            freshness_status=ContextStatus.AVAILABLE,
            reason_code=ReasonCode.MISSING_INPUT,
            evidence=("contiguous_closed_bar_window",),
            closed_bar_count=len(closed),
            minimum_closed_bars=minimum_closed_bars,
            freshness_details={**freshness_details, "reason": "CLOSED_BAR_GAP"},
        )
    try:
        closes = [candle.close for candle in window]
        if any(value <= 0 or not value.is_finite() for value in closes):
            raise ValueError("close must be positive and finite")
        current_close = closes[-1]
        prior_close = closes[-1 - settings.phase5_return_lookback_bars]
        return_pct = _finite((current_close / prior_close - Decimal("1")) * Decimal("100"), "return_pct")
        structure_state = _structure_state(window)
        trend_state, support, conflict = _direction_state(window, settings, structure_state)
        volatility_window = window[-(settings.phase5_vol_window_bars + 1):]
        volatility_value = _realized_volatility(volatility_window)
        volatility_state = classify_volatility(timeframe, volatility_value, settings)
        if window[-1].volume < 0 or not window[-1].volume.is_finite():
            raise ValueError("latest volume must be finite and non-negative")
        baseline = [candle.volume for candle in window[-(settings.phase5_volume_baseline_bars + 1):-1]]
        if len(baseline) < settings.phase5_volume_baseline_bars or any(value <= 0 or not value.is_finite() for value in baseline):
            raise ValueError("volume baseline is invalid")
        volume_ratio = _finite(window[-1].volume / Decimal(str(median(baseline))), "volume_ratio")
        volume_state = _classify_volume(volume_ratio, settings)
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        return _unavailable(
            symbol=symbol,
            timeframe=timeframe,
            processed_at=processed_at,
            status=ContextStatus.ERROR,
            freshness_status=ContextStatus.ERROR,
            reason_code=ReasonCode.CALCULATION_ERROR,
            evidence=("canonical_input_values",),
            closed_bar_count=len(closed),
            minimum_closed_bars=minimum_closed_bars,
            freshness_details={**freshness_details, "reason": "CANONICAL_INPUT_INVALID"},
        )
    context_timestamp = window[-1].bar_open_timestamp + timedelta(seconds=interval_seconds)
    freshness_details["context_timestamp"] = context_timestamp.isoformat()
    freshness_details["context_age_seconds"] = _elapsed_seconds(context_timestamp, processed_at)
    return MarketLeaderContext(
        symbol=symbol,
        timeframe=timeframe,
        context_timestamp=context_timestamp,
        input_window_start=window[0].bar_open_timestamp,
        input_window_end=context_timestamp,
        return_pct=return_pct,
        trend_state=trend_state,
        structure_state=structure_state,
        volatility_state=volatility_state,
        volume_state=volume_state,
        volatility_value=volatility_value,
        volume_ratio=volume_ratio,
        freshness_status=ContextStatus.AVAILABLE,
        data_quality={
            "closed_bar_count": len(window),
            "minimum_closed_bars": minimum_closed_bars,
            "volatility_window_bars": settings.phase5_vol_window_bars,
            "volume_baseline_bars": settings.phase5_volume_baseline_bars,
            "source_status": "AVAILABLE",
            **freshness_details,
        },
        source_count=1,
        missing_count=0,
        status=ContextStatus.AVAILABLE,
        processed_at=processed_at,
        support_evidence=support,
        conflict_evidence=conflict,
        calculation_version=CALCULATION_VERSION,
        input_reference={"source": "phase1_canonical_closed_klines", "symbol": symbol, "timeframe": timeframe},
    )
