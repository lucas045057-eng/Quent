"""Deterministic Phase 1 Stage1 basic screening."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from .contracts import Candle, DataStatus, RawReference, Ticker
from .freshness import evaluate_kline_freshness
from quant_data_layer.freshness import (
    FRESHNESS_POLICY, bitget_price_source_clock_skew_tolerance,
)
from .market.indicators import compute_indicators
from .market.structure import classify_structure


DEFAULT_GRACE_SECONDS = {"5m": 30, "15m": 60, "1H": 120, "4H": 180}
REQUIRED_INTERVALS = ("5m", "15m", "1H", "4H")


@dataclass(frozen=True, slots=True)
class Stage1Result:
    symbol: str
    category: str
    reason: str
    status: DataStatus
    inputs_used: tuple[str, ...]
    indicators: Mapping[str, Decimal]
    structure: str | None
    reason_codes: tuple[str, ...] = ()
    key_metrics: Mapping[str, Decimal | str] = field(default_factory=dict)
    data_snapshot_reference: RawReference | None = None
    timestamp: datetime | None = None

    strategy_version: str | None = field(default=None, metadata={"omit_if_none": True})

    @property
    def classification(self) -> str:
        return {"A": "DEEP_ANALYSIS", "B": "WAIT_TRIGGER", "C": "NO_EDGE", "D": "REJECT"}[self.category]


def _not_ready(
    symbol: str,
    reason: str,
    *,
    status: DataStatus = DataStatus.NOT_AVAILABLE,
    now: datetime | None = None,
) -> Stage1Result:
    return Stage1Result(
        symbol, "D", reason, status, (), {}, None, (reason,), {},
        RawReference(provider="phase1", endpoint="stage1", reason=reason), now,
    )


def evaluate_stage1(
    symbol: str,
    ticker: Ticker,
    candles_by_interval: Mapping[str, Sequence[Candle]],
    *,
    now: datetime,
    grace_seconds: Mapping[str, int] | None = None,
    ticker_max_age_seconds: float = FRESHNESS_POLICY["PRICE_STAGE1"].hard_seconds,
) -> Stage1Result:
    if ticker_max_age_seconds < 0:
        raise ValueError("ticker_max_age_seconds must be non-negative")
    ticker_max_age_seconds = min(
        float(ticker_max_age_seconds), FRESHNESS_POLICY["PRICE_STAGE1"].hard_seconds,
    )
    if ticker.status is not DataStatus.AVAILABLE:
        return _not_ready(symbol, "REQUIRED_DATA_NOT_FRESH", status=ticker.status, now=now)
    ticker_age = (now - ticker.exchange_timestamp).total_seconds()
    future_skew_tolerance = bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", ticker.exchange, ticker.source,
    )
    if ticker_age < -future_skew_tolerance or ticker_age > ticker_max_age_seconds:
        return _not_ready(symbol, "REQUIRED_DATA_NOT_FRESH", status=DataStatus.STALE, now=now)

    grace = grace_seconds or DEFAULT_GRACE_SECONDS
    latest_by_interval: dict[str, Candle] = {}
    for interval in REQUIRED_INTERVALS:
        candles = sorted(candles_by_interval.get(interval, ()), key=lambda candle: candle.bar_open_timestamp)
        if not candles:
            return _not_ready(symbol, "REQUIRED_DATA_NOT_FRESH", now=now)
        latest = candles[-1]
        latest_by_interval[interval] = latest
        if latest.status is not DataStatus.AVAILABLE or not latest.is_closed:
            return _not_ready(symbol, "REQUIRED_DATA_NOT_FRESH", status=latest.status, now=now)
        freshness = evaluate_kline_freshness(now, interval, latest.bar_open_timestamp, grace_seconds=grace[interval])
        if freshness is not DataStatus.AVAILABLE:
            return _not_ready(symbol, "REQUIRED_DATA_NOT_FRESH", status=DataStatus.STALE, now=now)

    if len(candles_by_interval["5m"]) < 2 or len(candles_by_interval["1H"]) < 2:
        return _not_ready(symbol, "INSUFFICIENT_CLOSED_BARS", now=now)

    indicators = compute_indicators(candles_by_interval["5m"])
    structure = classify_structure(candles_by_interval["1H"])
    structure_4h = classify_structure(candles_by_interval["4H"])
    spread_ratio = (ticker.ask_price - ticker.bid_price) / ticker.last_price
    atr = indicators["atr"]
    range_to_atr = (indicators["range_high"] - indicators["range_low"]) / max(atr, Decimal("0.00000001"))
    aligned = structure.trend == structure_4h.trend and structure.trend in {"BULLISH", "BEARISH"}
    ema_aligned = (
        structure.trend == "BULLISH" and indicators["ema_fast"] > indicators["ema_slow"]
    ) or (
        structure.trend == "BEARISH" and indicators["ema_fast"] < indicators["ema_slow"]
    )
    if ticker.turnover24h <= 0:
        category, reason = "D", "INVALID_LIQUIDITY"
        reason_codes = ("INVALID_LIQUIDITY",)
    elif spread_ratio > Decimal("0.002"):
        category, reason = "C", "WIDE_SPREAD"
        reason_codes = ("WIDE_SPREAD",)
    elif structure.trend == "RANGE" or structure_4h.trend == "RANGE":
        category, reason = "C", "NO_DIRECTIONAL_STRUCTURE"
        reason_codes = ("NO_DIRECTIONAL_STRUCTURE",)
    elif not aligned:
        category, reason = "C", "MULTI_TIMEFRAME_CONFLICT"
        reason_codes = ("MULTI_TIMEFRAME_CONFLICT",)
    elif spread_ratio <= Decimal("0.0015") and range_to_atr >= Decimal("2") and ema_aligned:
        category, reason = "A", "HIGH_CONFIDENCE_ALIGNED_TREND"
        reason_codes = ("STRUCTURE_ALIGNED", "EMA_ALIGNED", "RANGE_EXPANSION")
    else:
        category, reason = "B", "WAIT_FOR_TRIGGER"
        reason_codes = ("DIRECTIONAL_BIAS", "TRIGGER_NOT_CONFIRMED")
    return Stage1Result(
        symbol=symbol,
        category=category,
        reason=reason,
        status=DataStatus.AVAILABLE,
        inputs_used=("price", "volume", "turnover", "bid_ask", "closed_5m", "closed_15m", "closed_1H", "closed_4H", "local_indicators", "market_structure"),
        indicators=indicators,
        structure=structure.trend,
        reason_codes=reason_codes,
        key_metrics={
            "spread_ratio": spread_ratio,
            "range_to_atr": range_to_atr,
            "turnover24h": ticker.turnover24h,
            "multi_timeframe_aligned": "true" if aligned else "false",
        },
        data_snapshot_reference=RawReference(provider="phase1", endpoint="canonical_market_snapshot", reason="stage1_inputs"),
        timestamp=now,
    )
