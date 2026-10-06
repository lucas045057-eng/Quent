"""Freshness rules for ticker age and interval-aware Kline availability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .contracts import DataStatus
from .time import ensure_utc


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}

# Matches the accepted PRICE_STAGE1 hard freshness limit; recovery does not widen it.
PRICE_STAGE1_HARD_AGE_SECONDS = 60.0


def expected_latest_closed_open(now: datetime, interval: str) -> datetime:
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval}")
    now = ensure_utc(now)
    seconds = INTERVAL_SECONDS[interval]
    boundary = int(now.timestamp()) // seconds * seconds
    return datetime.fromtimestamp(boundary - seconds, tz=timezone.utc)


def evaluate_kline_freshness(
    now: datetime,
    interval: str,
    latest_closed_open: datetime | None,
    *,
    grace_seconds: int,
) -> DataStatus:
    if grace_seconds < 0:
        raise ValueError("grace_seconds cannot be negative")
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported interval {interval}")
    now = ensure_utc(now)
    if latest_closed_open is not None:
        latest_closed_open = ensure_utc(latest_closed_open)
        if latest_closed_open > now:
            return DataStatus.ERROR

    seconds = INTERVAL_SECONDS[interval]
    boundary = int(now.timestamp()) // seconds * seconds
    boundary_time = datetime.fromtimestamp(boundary, tz=timezone.utc)
    if now < boundary_time + timedelta(seconds=grace_seconds):
        return DataStatus.NOT_AVAILABLE if latest_closed_open is None else DataStatus.AVAILABLE

    expected = expected_latest_closed_open(now, interval)
    if latest_closed_open is None or latest_closed_open < expected:
        return DataStatus.STALE
    return DataStatus.AVAILABLE
