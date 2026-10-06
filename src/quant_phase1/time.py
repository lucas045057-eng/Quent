"""UTC-only time helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UtcClock:
    def __init__(self, provider: Callable[[], datetime] | None = None) -> None:
        self._provider = provider or utc_now

    def now(self) -> datetime:
        return ensure_utc(self._provider())
