"""Small async token bucket for public Bitget endpoint limits."""

from __future__ import annotations

import asyncio
import time
from weakref import WeakKeyDictionary
_candle_gates=WeakKeyDictionary()

async def acquire_public_candle_slot():
    """Shared endpoint allowance for REST backfill and SBE reconciliation."""
    loop=asyncio.get_running_loop()
    gate=_candle_gates.setdefault(loop,[asyncio.Lock(),0.0])
    async with gate[0]:
        await asyncio.sleep(max(0.0,gate[1]-loop.time()))
        gate[1]=loop.time()+1/18



class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: int) -> None:
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("rate_per_second and capacity must be positive")
        self.rate_per_second = rate_per_second
        self.capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_second)
        self._updated = now

    async def delay_until_available(self) -> float:
        async with self._lock:
            self._refill()
            return max(0.0, (1.0 - self._tokens) / self.rate_per_second)

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                delay = (1.0 - self._tokens) / self.rate_per_second
            await asyncio.sleep(delay)
