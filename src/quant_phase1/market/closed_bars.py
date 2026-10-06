"""Closed-bar gate independent of exchange-specific response formats."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..contracts import Candle, DataStatus
from ..time import ensure_utc


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


def accept_closed_candle(candle: Candle, now: datetime) -> bool:
    if candle.status is not DataStatus.AVAILABLE or not candle.is_closed:
        return False
    if candle.interval not in INTERVAL_SECONDS:
        return False
    return candle.bar_open_timestamp + timedelta(seconds=INTERVAL_SECONDS[candle.interval]) <= ensure_utc(now)
