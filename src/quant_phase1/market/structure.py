"""Basic HH/HL/LH/LL market structure classification."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from ..contracts import Candle


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    trend: str
    higher_highs: int
    higher_lows: int
    lower_highs: int
    lower_lows: int
    range_center: Decimal


def classify_structure(candles: Sequence[Candle]) -> StructureSnapshot:
    if len(candles) < 2:
        raise ValueError("at least two candles are required")
    ordered = sorted(candles, key=lambda candle: candle.bar_open_timestamp)
    higher_highs = higher_lows = lower_highs = lower_lows = 0
    for previous, current in zip(ordered, ordered[1:]):
        higher_highs += int(current.high > previous.high)
        higher_lows += int(current.low > previous.low)
        lower_highs += int(current.high < previous.high)
        lower_lows += int(current.low < previous.low)
    if higher_highs and higher_lows and ordered[-1].close > ordered[0].close:
        trend = "BULLISH"
    elif lower_highs and lower_lows and ordered[-1].close < ordered[0].close:
        trend = "BEARISH"
    else:
        trend = "RANGE"
    highest = max(candle.high for candle in ordered)
    lowest = min(candle.low for candle in ordered)
    return StructureSnapshot(trend, higher_highs, higher_lows, lower_highs, lower_lows, (highest + lowest) / 2)
