"""Causal, closed-candle breakout retest confirmation for V2 evidence."""
from datetime import timedelta
from decimal import Decimal as D


INTERVALS = {"15m": 900, "1H": 3600, "4H": 14400}
BREAKOUT_BUFFER_ATR = D("0.15")
RETEST_BAND_ATR = D("0.20")
RECLAIM_BUFFER_ATR = D("0.05")
INVALIDATION_BUFFER_ATR = D("0.20")
MIN_VOLUME_RATIO = D("1.25")
RETEST_TIMEOUT_BARS = 4


def _closed_contiguous(candles, *, timeframe, as_of):
    seconds = INTERVALS[timeframe]
    rows = sorted((bar for bar in candles if bar.is_closed and bar.status.value == "AVAILABLE"
        and bar.bar_open_timestamp + timedelta(seconds=seconds) <= as_of),
        key=lambda bar: bar.bar_open_timestamp)
    if len(rows) < 22:
        return None
    rows = rows[-26:]
    if any(right.bar_open_timestamp - left.bar_open_timestamp != timedelta(seconds=seconds)
        for left, right in zip(rows, rows[1:])):
        return None
    latest_close = rows[-1].bar_open_timestamp + timedelta(seconds=seconds)
    if latest_close != as_of.replace(second=0, microsecond=0) and (as_of - latest_close).total_seconds() > seconds:
        return None
    return rows


def retest_confirmation(candles, side, timeframe, *, as_of):
    """Return True/False when evidence is complete, or None when it is incomplete.

    A positive result needs a 20-bar boundary break with ≥0.15 ATR clearance,
    ≥1.25 volume, then a reclaiming retest within four closed bars. Missing,
    gapped, stale, wrong-interval, or forming candles never become a negative.
    """
    if side not in {"LONG", "SHORT"} or timeframe not in INTERVALS:
        return None
    rows = _closed_contiguous(candles, timeframe=timeframe, as_of=as_of)
    if rows is None:
        return None
    sign = D(1) if side == "LONG" else D(-1)
    seconds = INTERVALS[timeframe]
    for index in range(max(20, len(rows) - (RETEST_TIMEOUT_BARS + 2)), len(rows) - 1):
        prior = rows[index - 20:index]
        if len(prior) != 20:
            continue
        level = max(bar.high for bar in prior) if sign > 0 else min(bar.low for bar in prior)
        ranges = [max(bar.high - bar.low, abs(bar.high - rows[i-1].close), abs(bar.low - rows[i-1].close))
            for i, bar in enumerate(rows[index - 14:index], start=index - 14)]
        if len(ranges) != 14:
            continue
        atr = sum(ranges, D(0)) / D(14)
        baseline_volume = sum((bar.volume for bar in prior), D(0)) / D(20)
        if atr <= 0 or baseline_volume <= 0:
            continue
        trigger = rows[index]
        if sign * (trigger.close - level) < BREAKOUT_BUFFER_ATR * atr:
            continue
        if trigger.volume / baseline_volume < MIN_VOLUME_RATIO:
            continue
        invalidated = False
        retest_seen = False
        for retest_index in range(index + 1, min(len(rows), index + RETEST_TIMEOUT_BARS + 1)):
            bar = rows[retest_index]
            distance = retest_index - index
            if distance > RETEST_TIMEOUT_BARS:
                break
            if sign * (bar.close - level) < -INVALIDATION_BUFFER_ATR * atr:
                invalidated = True
                break
            touches_level_band = bar.low <= level + RETEST_BAND_ATR * atr and bar.high >= level - RETEST_BAND_ATR * atr
            if not retest_seen and touches_level_band and sign * (bar.close - level) >= RECLAIM_BUFFER_ATR * atr:
                retest_seen = True
        if retest_seen and not invalidated:
            return True
    return False
