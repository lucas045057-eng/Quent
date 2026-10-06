"""Finite retry and bounded in-memory helpers for Phase 5 runtime work."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import sleep as default_sleep
from typing import Callable, Iterable, Iterator, TypeVar


T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.max_attempts <= 0 or self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid retry policy")


def run_with_retry(
    operation: Callable[[], R],
    policy: RetryPolicy = RetryPolicy(),
    *,
    sleep: Callable[[float], None] = default_sleep,
) -> R:
    """Run a finite number of attempts with capped exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(policy.max_attempts):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - caller owns retryable operation policy
            last_error = exc
            if attempt + 1 >= policy.max_attempts:
                break
            delay = min(policy.max_delay_seconds, policy.base_delay_seconds * (2**attempt))
            sleep(delay)
    assert last_error is not None
    raise last_error


def bounded_chunks(values: Iterable[T], size: int) -> Iterator[tuple[T, ...]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    chunk: list[T] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield tuple(chunk)
            chunk.clear()
    if chunk:
        yield tuple(chunk)


class BoundedContextCache:
    def __init__(self, *, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("cache capacity must be positive")
        self.capacity = capacity
        self._values: OrderedDict[str, object] = OrderedDict()

    def put(self, key: str, value: object) -> None:
        self._values.pop(key, None)
        self._values[key] = value
        while len(self._values) > self.capacity:
            self._values.popitem(last=False)

    def get(self, key: str) -> object | None:
        value = self._values.get(key)
        if value is not None:
            self._values.move_to_end(key)
        return value

    def __len__(self) -> int:
        return len(self._values)
