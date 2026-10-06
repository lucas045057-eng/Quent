"""Bounded, deduplicated Kline gap recovery for one collector lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
import inspect


RecoveryKey = tuple[str, str]
RecoveryFetch = Callable[..., Awaitable[Sequence[object]] | AsyncIterator[Sequence[object]]]
CandlesHandler = Callable[[Sequence[object]], None]
FailureHandler = Callable[[str, str, Exception], None]


@dataclass(frozen=True)
class RecoveryRange:
    """One contiguous missing closed-bar range for a symbol/timeframe."""

    symbol: str
    interval: str
    start_open: datetime
    end_open: datetime

    def __post_init__(self) -> None:
        if self.start_open.tzinfo is None or self.end_open.tzinfo is None:
            raise ValueError("recovery range timestamps must be timezone-aware")
        if self.start_open > self.end_open:
            raise ValueError("recovery range start must not follow end")

    @property
    def key(self) -> RecoveryKey:
        return (self.symbol, self.interval)

    def merge(self, other: RecoveryRange) -> RecoveryRange:
        if self.key != other.key:
            raise ValueError("only ranges with the same symbol and interval can merge")
        return RecoveryRange(
            self.symbol,
            self.interval,
            min(self.start_open, other.start_open),
            max(self.end_open, other.end_open),
        )


@dataclass(frozen=True)
class RecoveryScan:
    """Cheap universe scan result; only candidates become recovery work."""

    scanned_count: int
    candidates: tuple[RecoveryRange, ...]

    def __post_init__(self) -> None:
        if self.scanned_count < 0:
            raise ValueError("scanned_count cannot be negative")
        if len(self.candidates) > self.scanned_count:
            raise ValueError("candidate count cannot exceed scanned count")


RecoveryCandidateProvider = Callable[[], RecoveryScan | Awaitable[RecoveryScan]]

LOGGER = logging.getLogger("quant_phase1")
GLOBAL_RECOVERY_CONCURRENCY_LIMIT = 8


class GapRecoveryCoordinator:
    """Own one bounded worker pool and coalesce reconnect recovery requests.

    A request represents refreshing the latest closed Kline window for every
    currently selected symbol/interval. Pending identities are deduplicated;
    a request arriving during a fetch sets one rerun bit for that identity so
    an intervening gap is not silently lost.
    """

    def __init__(
        self,
        recover_one: RecoveryFetch,
        *,
        symbols_provider: Callable[[], Sequence[str]] | None = None,
        candidate_provider: RecoveryCandidateProvider | None = None,
        still_needed: Callable[[RecoveryRange], bool] | None = None,
        on_candles: CandlesHandler,
        intervals: Sequence[str] = ("5m", "15m", "1H", "4H"),
        concurrency: int = GLOBAL_RECOVERY_CONCURRENCY_LIMIT,
        max_work_items: int,
        on_failure: FailureHandler | None = None,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 60.0,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if max_work_items <= 0:
            raise ValueError("max_work_items must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry bounds must be positive and ordered")
        if not intervals:
            raise ValueError("at least one Kline interval is required")
        if (symbols_provider is None) == (candidate_provider is None):
            raise ValueError("provide exactly one of symbols_provider or candidate_provider")

        self._recover_one = recover_one
        self._symbols_provider = symbols_provider
        self._candidate_provider = candidate_provider
        self._still_needed = still_needed
        self._on_candles = on_candles
        self._intervals = tuple(intervals)
        self._concurrency = concurrency
        self.max_work_items = max_work_items
        self._on_failure = on_failure
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

        self._queue: asyncio.Queue[RecoveryKey] = asyncio.Queue(maxsize=max_work_items)
        self._pending: set[RecoveryKey] = set()
        self._inflight: set[RecoveryKey] = set()
        self._rerun: set[RecoveryKey] = set()
        self._failed: set[RecoveryKey] = set()
        self._retry_due: dict[RecoveryKey, float] = {}
        self._attempts: dict[RecoveryKey, int] = {}
        self._ranges: dict[RecoveryKey, RecoveryRange] = {}
        self._capacity = asyncio.Condition()
        self._requested = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._changed = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._scheduling = False
        self._active_count = 0
        self.completed_count = 0
        self.failure_attempt_count = 0
        self.request_generation = 0
        self.last_reconciled_generation = 0
        self.reconciliation_runs = 0
        self.candidate_scanned_total = 0
        self.actual_gap_total = 0
        self.admitted_count = 0
        self.coalesced_count = 0
        self.skipped_count = 0

    @property
    def active_count(self) -> int:
        """Current REST calls; always bounded by the fixed worker count."""

        return self._active_count

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._retry_due)

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    @property
    def outstanding_count(self) -> int:
        return self.pending_count + self.inflight_count

    @property
    def task_count(self) -> int:
        return sum(
            task is not None and not task.done()
            for task in (self._scheduler_task, *self._worker_tasks)
        )

    def diagnostics_snapshot(self) -> dict[str, int]:
        """Expose bounded queue/work counts without mutating recovery state."""

        return {
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "pending_count": self.pending_count,
            "inflight_count": self.inflight_count,
            "rerun_count": len(self._rerun),
            "retry_count": len(self._retry_due),
            "failed_count": self.failed_count,
            "active_count": self.active_count,
            "task_count": self.task_count,
        }

    @property
    def is_idle(self) -> bool:
        return not (
            self._requested.is_set()
            or self._scheduling
            or self._pending
            or self._inflight
            or self._rerun
            or self._retry_due
        )

    @property
    def is_healthy(self) -> bool:
        return self.is_idle and not self._failed and not self._closed

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("a stopped gap recovery coordinator cannot be restarted")
        if self._scheduler_task is not None:
            return
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(), name=f"kline-gap-worker-{index}")
            for index in range(self._concurrency)
        ]
        self._scheduler_task = asyncio.create_task(
            self._schedule_loop(), name="kline-gap-scheduler"
        )
        self._changed.set()

    def request(self) -> bool:
        """Signal a full selected-universe recovery without spawning a task."""

        if self._closed or self._scheduler_task is None:
            return False
        self.request_generation += 1
        self._requested.set()
        self._wakeup.set()
        self._changed.set()
        return True

    async def wait_idle(self) -> None:
        """Wait until all coalesced recovery work has completed (primarily tests)."""

        while not self.is_idle:
            self._changed.clear()
            if self.is_idle:
                return
            await self._changed.wait()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._requested.set()
        self._wakeup.set()
        tasks = [task for task in (self._scheduler_task, *self._worker_tasks) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._capacity:
            self._pending.clear()
            self._inflight.clear()
            self._rerun.clear()
            self._failed.clear()
            self._retry_due.clear()
            self._attempts.clear()
            self._ranges.clear()
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._capacity.notify_all()
        self._active_count = 0
        self._scheduler_task = None
        self._worker_tasks.clear()
        self._changed.set()

    async def _schedule_loop(self) -> None:
        while not self._closed:
            timeout = None
            if self._retry_due:
                earliest = min(self._retry_due.values())
                timeout = max(0.0, earliest - asyncio.get_running_loop().time())
            try:
                if timeout is None:
                    await self._wakeup.wait()
                else:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            if self._closed:
                return
            self._wakeup.clear()
            if self._requested.is_set():
                self._requested.clear()
                generation = self.request_generation
                self._scheduling = True
                self.reconciliation_runs += 1
                self._changed.set()
                try:
                    if self._candidate_provider is not None:
                        scan = self._candidate_provider()
                        if inspect.isawaitable(scan):
                            scan = await scan
                        if not isinstance(scan, RecoveryScan):
                            raise TypeError("candidate_provider must return RecoveryScan")
                        self.candidate_scanned_total += scan.scanned_count
                        self.actual_gap_total += len(scan.candidates)
                        for candidate in scan.candidates:
                            await self._enqueue(candidate)
                    else:
                        assert self._symbols_provider is not None
                        symbols = tuple(dict.fromkeys(self._symbols_provider()))
                        for symbol in symbols:
                            for interval in self._intervals:
                                await self._enqueue((symbol, interval))
                    self.last_reconciled_generation = generation
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._report_failure("*", "*", exc)
                    if not self._closed:
                        await asyncio.sleep(self._retry_base_seconds)
                        self._requested.set()
                        self._wakeup.set()
                finally:
                    self._scheduling = False
                    self._changed.set()
                if self.request_generation != generation and not self._closed:
                    self._requested.set()
                    self._wakeup.set()
            await self._enqueue_due_retries()

    async def _enqueue_due_retries(self) -> None:
        now = asyncio.get_running_loop().time()
        async with self._capacity:
            due = [key for key, deadline in self._retry_due.items() if deadline <= now]
            for key in due:
                self._retry_due.pop(key, None)
                self._pending.add(key)
                self._queue.put_nowait(key)
            if due:
                self._changed.set()

    def _merge_candidate(self, candidate: RecoveryRange) -> bool:
        previous = self._ranges.get(candidate.key)
        merged = candidate if previous is None else previous.merge(candidate)
        changed = merged != previous
        if changed:
            self._ranges[candidate.key] = merged
        return changed

    async def _enqueue(self, item: RecoveryKey | RecoveryRange) -> None:
        candidate = item if isinstance(item, RecoveryRange) else None
        key = candidate.key if candidate is not None else item
        async with self._capacity:
            if key in self._pending:
                if candidate is not None:
                    self._merge_candidate(candidate)
                    self.coalesced_count += 1
                return
            if key in self._retry_due:
                if candidate is not None:
                    self._merge_candidate(candidate)
                    self.coalesced_count += 1
                return
            if key in self._inflight:
                if candidate is None or self._merge_candidate(candidate):
                    self._rerun.add(key)
                if candidate is not None:
                    self.coalesced_count += 1
                self._changed.set()
                return
            while not self._closed and self.outstanding_count >= self.max_work_items:
                await self._capacity.wait()
            if self._closed:
                return
            self._pending.add(key)
            if candidate is not None:
                self._ranges[key] = candidate
            self.admitted_count += 1
            self._queue.put_nowait(key)
            self._changed.set()

    async def _worker_loop(self) -> None:
        while not self._closed:
            key = await self._queue.get()
            self._pending.discard(key)
            self._inflight.add(key)
            self._changed.set()
            attempt = 0
            try:
                while not self._closed:
                    try:
                        candidate = self._ranges.get(key)
                        skipped = (
                            candidate is not None
                            and self._still_needed is not None
                            and not self._still_needed(candidate)
                        )
                        if skipped:
                            self.skipped_count += 1
                        else:
                            self._active_count += 1
                            self._changed.set()
                            try:
                                recovery = (
                                    self._recover_one(candidate)
                                    if candidate is not None
                                    else self._recover_one(*key)
                                )
                                if inspect.isawaitable(recovery):
                                    recovery = await recovery
                                if hasattr(recovery, "__aiter__"):
                                    async for candles in recovery:
                                        self._on_candles(candles)
                                else:
                                    self._on_candles(recovery)
                                if (
                                    candidate is not None
                                    and self._still_needed is not None
                                    and self._still_needed(candidate)
                                ):
                                    raise RuntimeError(
                                        "recovery completed without closing its expected-bar gap"
                                    )
                            finally:
                                self._active_count -= 1
                                self._changed.set()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._failed.add(key)
                        self.failure_attempt_count += 1
                        self._report_failure(*key, exc)
                        attempt = self._attempts.get(key, 0) + 1
                        self._attempts[key] = attempt
                        async with self._capacity:
                            self._inflight.discard(key)
                            self._rerun.discard(key)
                            self._retry_due[key] = (
                                asyncio.get_running_loop().time() + self._retry_delay(attempt)
                            )
                            self._capacity.notify_all()
                        self._wakeup.set()
                        self._changed.set()
                        break

                    self._failed.discard(key)
                    self._attempts.pop(key, None)
                    if not skipped:
                        self.completed_count += 1
                    async with self._capacity:
                        if key in self._rerun:
                            self._rerun.discard(key)
                            self._changed.set()
                            continue
                        self._inflight.discard(key)
                        self._ranges.pop(key, None)
                        self._capacity.notify_all()
                    self._changed.set()
                    break
            finally:
                if key in self._inflight:
                    async with self._capacity:
                        self._inflight.discard(key)
                        self._ranges.pop(key, None)
                        self._rerun.discard(key)
                        self._capacity.notify_all()
                    self._changed.set()
                self._queue.task_done()

    def _retry_delay(self, attempt: int) -> float:
        return min(self._retry_base_seconds * (2 ** max(attempt - 1, 0)), self._retry_max_seconds)

    def _report_failure(self, symbol: str, interval: str, exc: Exception) -> None:
        if self._on_failure is not None:
            try:
                self._on_failure(symbol, interval, exc)
                return
            except Exception:
                LOGGER.exception("kline_gap_recovery_failure_handler_failed")
        LOGGER.warning(
            "kline_gap_recovery_failed symbol=%s interval=%s error_type=%s",
            symbol,
            interval,
            type(exc).__name__,
        )
