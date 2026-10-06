"""Bounded identity-based deduplication for WS and REST trade events."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Callable

from .contracts import CanonicalTrade


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


class BoundedTradeDeduplicator:
    def __init__(
        self,
        *,
        identity_key: Callable[[CanonicalTrade], tuple[str, ...]],
        max_entries_per_exchange: int = 10_000,
        ttl_seconds: int = 300,
    ) -> None:
        if max_entries_per_exchange <= 0 or ttl_seconds <= 0:
            raise ValueError("dedup limits must be positive")
        self.identity_key = identity_key
        self.max_entries_per_exchange = max_entries_per_exchange
        self.ttl_seconds = ttl_seconds
        # Keep LRU/capacity order separate from first-seen expiry order. Scanning
        # the full LRU bucket for expired keys on every trade made ingestion O(n)
        # per event once the high-volume exchange buckets filled.
        self._entries: dict[str, OrderedDict[tuple[str, ...], datetime]] = defaultdict(OrderedDict)
        self._expiry_entries: dict[str, OrderedDict[tuple[str, ...], datetime]] = defaultdict(OrderedDict)
        self.duplicate_count = 0

    def add(self, trade: CanonicalTrade, *, now: datetime | None = None) -> bool:
        current = _utc(now or datetime.now(timezone.utc), "now")
        bucket = self._entries[trade.exchange]
        expiry_bucket = self._expiry_entries[trade.exchange]
        expiry = current.timestamp() - self.ttl_seconds
        while expiry_bucket:
            oldest_key, seen_at = next(iter(expiry_bucket.items()))
            if seen_at.timestamp() > expiry:
                break
            expiry_bucket.popitem(last=False)
            bucket.pop(oldest_key, None)
        key = self.identity_key(trade)
        if key in bucket:
            self.duplicate_count += 1
            bucket.move_to_end(key)
            return False
        bucket[key] = current
        bucket.move_to_end(key)
        expiry_bucket[key] = current
        while len(bucket) > self.max_entries_per_exchange:
            evicted_key, _ = bucket.popitem(last=False)
            expiry_bucket.pop(evicted_key, None)
        return True

    def size(self, exchange: str | None = None) -> int:
        if exchange is not None:
            return len(self._entries.get(exchange, ()))
        return self.total_size

    @property
    def total_size(self) -> int:
        return sum(len(bucket) for bucket in self._entries.values())
