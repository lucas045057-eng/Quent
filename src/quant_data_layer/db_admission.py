"""Cross-process admission for bounded PostgreSQL write transactions.

Each admitted write owns a session advisory lock on a dedicated autocommit
connection and a distinct transaction-scoped fence on its business connection.
The fence is non-blocking: if the admission connection disappears while the
business transaction is still alive, a successor defers instead of overlapping.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Any, Callable, Iterator


class DbWorkClass(str, Enum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


class DbAdmissionDeferredReason(str, Enum):
    SLOT_TIMEOUT = "SLOT_TIMEOUT"
    WAITER_LIMIT = "WAITER_LIMIT"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    FENCE_BUSY = "FENCE_BUSY"
    CONTROLLER_CLOSED = "CONTROLLER_CLOSED"


class DbAdmissionDeferred(RuntimeError):
    """Safe, bounded admission outcome; never retains a DSN or identity."""

    def __init__(self, reason: DbAdmissionDeferredReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class DbAdmissionLimits:
    """Provisional local waiter bounds and shared PostgreSQL slot count."""

    slot_count: int = 3
    max_waiters: int = 8
    poll_interval_seconds: float = 0.05
    connect_timeout_seconds: int = 2

    def __post_init__(self) -> None:
        if (
            isinstance(self.slot_count, bool)
            or not isinstance(self.slot_count, int)
            or self.slot_count < 2
        ):
            raise ValueError("slot_count must be at least two; a global single-writer gate is forbidden")
        if isinstance(self.max_waiters, bool) or not isinstance(self.max_waiters, int) or self.max_waiters < 1:
            raise ValueError("max_waiters must be positive")
        if (
            isinstance(self.poll_interval_seconds, bool)
            or not isinstance(self.poll_interval_seconds, (int, float))
            or not math.isfinite(self.poll_interval_seconds)
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive")
        if (
            isinstance(self.connect_timeout_seconds, bool)
            or not isinstance(self.connect_timeout_seconds, int)
            or self.connect_timeout_seconds < 1
        ):
            raise ValueError("connect_timeout_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class DbAdmissionSnapshot:
    slot_count: int
    pending_by_class: dict[DbWorkClass, int]
    active_by_class: dict[DbWorkClass, int]
    accepted_count: int
    deferred_count: int
    closed: bool

    @property
    def pending_count(self) -> int:
        return sum(self.pending_by_class.values())

    @property
    def active_count(self) -> int:
        return sum(self.active_by_class.values())


ConnectionFactory = Callable[..., Any]


class PostgresWriteAdmission:
    """A process-local waiter bound over a cross-process PostgreSQL slot pool.

    All instances that coordinate the same database must use the same
    ``lock_namespace``, ``fence_namespace``, and ``slot_count``. Slot count is
    intentionally provisional until replay/resource evidence freezes it.
    """

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory | None = None,
        limits: DbAdmissionLimits | None = None,
        lock_namespace: int = 1_570_926_001,
        fence_namespace: int = -1_570_926_001,
    ) -> None:
        self.limits = limits or DbAdmissionLimits()
        self._validate_key(lock_namespace, "lock_namespace")
        self._validate_key(fence_namespace, "fence_namespace")
        if lock_namespace == fence_namespace:
            raise ValueError("lock and fence namespaces must be distinct")
        self.lock_namespace = lock_namespace
        self.fence_namespace = fence_namespace
        self._connection_factory = connection_factory
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._pending: Counter[DbWorkClass] = Counter()
        self._active: Counter[DbWorkClass] = Counter()
        self._accepted_count = 0
        self._deferred_count = 0
        self._next_slot = 0
        self._closed = False

    @staticmethod
    def _validate_key(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31:
            raise ValueError(f"{name} must be a signed PostgreSQL int32")

    def _factory(self) -> ConnectionFactory:
        if self._connection_factory is not None:
            return self._connection_factory
        import psycopg

        return psycopg.connect

    @contextmanager
    def transaction(
        self,
        dsn: str,
        *,
        work_class: DbWorkClass,
        timeout_seconds: float,
        identity: str,
        business_connection: Any | None = None,
    ) -> Iterator[Any]:
        """Yield a business connection only after slot and transaction fence.

        ``identity`` is deliberately accepted only as request metadata for
        callers; it is never stored, included in metrics, or included in errors.
        It must be bounded so callers cannot accidentally pass an untrusted
        payload here.
        """
        if not isinstance(work_class, DbWorkClass):
            raise TypeError("work_class must be a DbWorkClass")
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise ValueError("identity must be a non-empty string of at most 128 characters")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if business_connection is not None:
            transaction_status = getattr(
                getattr(getattr(business_connection, "info", None), "transaction_status", None),
                "name",
                None,
            )
            if transaction_status not in (None, "IDLE"):
                raise ValueError("business_connection must be idle before admission")
        provided_business_connection = business_connection

        self._enter_wait_queue(work_class)
        deadline = time.monotonic() + timeout_seconds
        admission_connection = None
        business_connection = provided_business_connection
        owns_business_connection = False
        slot: int | None = None
        waiting = True
        active = False
        try:
            admission_connection, slot = self._acquire_slot(dsn, deadline)
            if business_connection is None:
                try:
                    business_connection = self._factory()(
                        dsn,
                        autocommit=False,
                        connect_timeout=self.limits.connect_timeout_seconds,
                    )
                    owns_business_connection = True
                except Exception:  # noqa: BLE001 - never retain driver messages or DSNs
                    raise DbAdmissionDeferred(DbAdmissionDeferredReason.DATABASE_UNAVAILABLE) from None

            with business_connection.transaction():
                try:
                    row = business_connection.execute(
                        "SELECT pg_try_advisory_xact_lock(%s, %s)",
                        (self.fence_namespace, slot),
                    ).fetchone()
                except Exception:  # noqa: BLE001 - fail closed on fence errors
                    raise DbAdmissionDeferred(DbAdmissionDeferredReason.DATABASE_UNAVAILABLE) from None
                if not row or row[0] is not True:
                    raise DbAdmissionDeferred(DbAdmissionDeferredReason.FENCE_BUSY)
                self._promote_waiter(work_class)
                waiting = False
                active = True
                yield business_connection
        except DbAdmissionDeferred:
            with self._lock:
                self._deferred_count += 1
            raise
        finally:
            if active:
                self._set_active(work_class, -1)
            if business_connection is not None and owns_business_connection:
                try:
                    business_connection.close()
                except Exception:  # noqa: BLE001 - preserve the original outcome
                    pass
            if admission_connection is not None:
                if slot is not None:
                    try:
                        admission_connection.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            (self.lock_namespace, slot),
                        )
                    except Exception:  # noqa: BLE001 - session close is the fallback release
                        pass
                try:
                    admission_connection.close()
                except Exception:  # noqa: BLE001 - preserve the original outcome
                    pass
            if waiting:
                self._leave_wait_queue(work_class)

    def _enter_wait_queue(self, work_class: DbWorkClass) -> None:
        with self._lock:
            if self._closed:
                self._deferred_count += 1
                raise DbAdmissionDeferred(DbAdmissionDeferredReason.CONTROLLER_CLOSED)
            if sum(self._pending.values()) >= self.limits.max_waiters:
                self._deferred_count += 1
                raise DbAdmissionDeferred(DbAdmissionDeferredReason.WAITER_LIMIT)
            self._pending[work_class] += 1

    def _leave_wait_queue(self, work_class: DbWorkClass) -> None:
        with self._changed:
            if self._pending[work_class] > 0:
                self._pending[work_class] -= 1
            if self._pending[work_class] <= 0:
                self._pending.pop(work_class, None)
            self._changed.notify_all()

    def _set_active(self, work_class: DbWorkClass, delta: int) -> None:
        with self._changed:
            self._active[work_class] += delta
            if self._active[work_class] <= 0:
                self._active.pop(work_class, None)
            self._changed.notify_all()

    def _promote_waiter(self, work_class: DbWorkClass) -> None:
        """Atomically move a successfully fenced request from pending to active."""
        with self._changed:
            if self._pending[work_class] > 0:
                self._pending[work_class] -= 1
            if self._pending[work_class] <= 0:
                self._pending.pop(work_class, None)
            self._active[work_class] += 1
            self._accepted_count += 1
            self._changed.notify_all()

    def _acquire_slot(self, dsn: str, deadline: float) -> tuple[Any, int]:
        with self._lock:
            if self._closed:
                raise DbAdmissionDeferred(DbAdmissionDeferredReason.CONTROLLER_CLOSED)
        try:
            admission_connection = self._factory()(
                dsn,
                autocommit=True,
                connect_timeout=min(
                    self.limits.connect_timeout_seconds,
                    max(1, math.ceil(max(0.0, deadline - time.monotonic()))),
                ),
            )
        except Exception:  # noqa: BLE001 - do not echo connection/credential details
            raise DbAdmissionDeferred(DbAdmissionDeferredReason.DATABASE_UNAVAILABLE) from None

        try:
            while True:
                if time.monotonic() >= deadline:
                    raise DbAdmissionDeferred(DbAdmissionDeferredReason.SLOT_TIMEOUT)
                with self._lock:
                    if self._closed:
                        raise DbAdmissionDeferred(DbAdmissionDeferredReason.CONTROLLER_CLOSED)
                    start = self._next_slot
                    self._next_slot = (self._next_slot + 1) % self.limits.slot_count
                for offset in range(self.limits.slot_count):
                    slot = (start + offset) % self.limits.slot_count
                    try:
                        row = admission_connection.execute(
                            "SELECT pg_try_advisory_lock(%s, %s)",
                            (self.lock_namespace, slot),
                        ).fetchone()
                    except Exception:  # noqa: BLE001 - do not reuse a broken admission session
                        raise DbAdmissionDeferred(DbAdmissionDeferredReason.DATABASE_UNAVAILABLE) from None
                    if row and row[0] is True:
                        return admission_connection, slot
                if time.monotonic() >= deadline:
                    raise DbAdmissionDeferred(DbAdmissionDeferredReason.SLOT_TIMEOUT)
                with self._changed:
                    if self._closed:
                        raise DbAdmissionDeferred(DbAdmissionDeferredReason.CONTROLLER_CLOSED)
                    self._changed.wait(
                        min(self.limits.poll_interval_seconds, max(0.0, deadline - time.monotonic()))
                    )
        except DbAdmissionDeferred:
            try:
                admission_connection.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    def snapshot(self) -> DbAdmissionSnapshot:
        with self._lock:
            pending = {work_class: self._pending[work_class] for work_class in DbWorkClass}
            active = {work_class: self._active[work_class] for work_class in DbWorkClass}
            return DbAdmissionSnapshot(
                slot_count=self.limits.slot_count,
                pending_by_class=pending,
                active_by_class=active,
                accepted_count=self._accepted_count,
                deferred_count=self._deferred_count,
                closed=self._closed,
            )

    def close(self, *, timeout_seconds: float = 5.0) -> bool:
        """Reject new work, cancel waiters, then wait for active transactions."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds cannot be negative")
        deadline = time.monotonic() + timeout_seconds
        with self._changed:
            self._closed = True
            self._changed.notify_all()
            while self._active or self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True
