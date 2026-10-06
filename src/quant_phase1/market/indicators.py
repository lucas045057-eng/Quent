"""Deterministic local indicators over canonical closed candles."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..contracts import Candle


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    if not values:
        raise ValueError("at least one value is required")
    alpha = Decimal(2) / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal(1) - alpha) * result
    return result


def compute_indicators(candles: Sequence[Candle]) -> dict[str, Decimal]:
    if not candles:
        raise ValueError("at least one candle is required")
    ordered = sorted(candles, key=lambda candle: candle.bar_open_timestamp)
    closes = [candle.close for candle in ordered]
    true_ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in ordered:
        if previous_close is None:
            true_ranges.append(candle.high - candle.low)
        else:
            true_ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
        previous_close = candle.close
    atr_window = true_ranges[-min(14, len(true_ranges)):]
    atr = sum(atr_window, Decimal("0")) / Decimal(len(atr_window))
    return {
        "last_close": closes[-1],
        "ema_fast": _ema(closes, 9),
        "ema_slow": _ema(closes, 21),
        "atr": atr,
        "range_high": max(candle.high for candle in ordered),
        "range_low": min(candle.low for candle in ordered),
    }
