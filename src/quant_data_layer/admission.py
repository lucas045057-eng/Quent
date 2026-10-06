"""Process-local bounded admission for replaceable and replayable work.

Limits in ``PROVISIONAL_DEFAULT_LIMITS`` are conservative starting points for
tests and local development. They are not accepted capacity values; replay and
resource evidence must establish production values.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import AsyncIterator, Mapping
import time
from uuid import uuid4

from .observability import ProcessRole, SourceId, SourcePhase, WorkClass, require_utc


class ReplayClass(StrEnum):
    CANONICAL_UNRECOVERABLE = "A"
    RECOVERABLE_REPLAYABLE = "B"
    DERIVED_REPLACEABLE = "C"


class AdmissionReason(StrEnum):
    CAPACITY = "CAPACITY"
    BACKPRESSURE = "BACKPRESSURE"
    DEADLINE = "DEADLINE"
    SHUTDOWN = "SHUTDOWN"
    OWNER_MISMATCH = "OWNER_MISMATCH"


class AdmissionDeferred(RuntimeError):
    """Safe bounded outcome; deliberately contains no request or payload data."""

    def __init__(self, reason: AdmissionReason) -> None:
        if not isinstance(reason, AdmissionReason):
            raise TypeError("admission reason must be registered")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    max_active: int = 4
    max_active_items: int = 16_384
    max_active_bytes: int = 64 * 1024 * 1024
    max_pending_requests: int = 64
    max_pending_items: int = 65_536
    max_pending_bytes: int = 128 * 1024 * 1024
    max_active_by_class: Mapping[WorkClass, int] = field(
        default_factory=lambda: {
            WorkClass.LIGHT: 2,
            WorkClass.MEDIUM: 2,
            WorkClass.HEAVY: 2,
        }
    )
    weights: Mapping[WorkClass, int] = field(
        default_factory=lambda: {
            WorkClass.LIGHT: 4,
            WorkClass.MEDIUM: 2,
            WorkClass.HEAVY: 1,
        }
    )
    aging_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in (
            "max_active",
            "max_active_items",
            "max_active_bytes",
            "max_pending_requests",
            "max_pending_items",
            "max_pending_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.aging_interval_seconds) or self.aging_interval_seconds <= 0:
            raise ValueError("aging_interval_seconds must be finite and positive")
        for name in ("max_active_by_class", "weights"):
            supplied = getattr(self, name)
            if not isinstance(supplied, Mapping) or set(supplied) != set(WorkClass):
                raise ValueError(f"{name} must define every registered work class exactly")
            normalized: dict[WorkClass, int] = {}
            for work_class, value in supplied.items():
                if not isinstance(work_class, WorkClass):
                    raise TypeError(f"{name} keys must be WorkClass values")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ValueError(f"{name} values must be positive integers")
                normalized[work_class] = value
            object.__setattr__(self, name, MappingProxyType(normalized))


PROVISIONAL_DEFAULT_LIMITS = AdmissionLimits()
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


@dataclass(frozen=True, slots=True)
class WorkAdmissionRequest:
    request_id: str = field(repr=False)
    phase: SourcePhase
    source_id: SourceId
    work_class: WorkClass
    estimated_items: int
    estimated_bytes: int
    deadline_utc: datetime
    replay_class: ReplayClass
    cancellation_owner: ProcessRole
    stream_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or _SAFE_REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError("request_id must be a bounded safe identifier")
        if not isinstance(self.phase, SourcePhase) or not isinstance(self.source_id, SourceId):
            raise TypeError("phase and source_id must be registered values")
        if not isinstance(self.work_class, WorkClass) or not isinstance(self.replay_class, ReplayClass):
            raise TypeError("work and replay classes must be registered values")
        if not isinstance(self.cancellation_owner, ProcessRole):
            raise TypeError("cancellation_owner must be a registered process role")
        if self.stream_id is not None:
            from .backpressure import get_stream_contract

            contract = get_stream_contract(self.stream_id)
            if self.replay_class is not contract.replay_class:
                raise ValueError("replay_class does not match stream registry")
            if contract.phase is not None and self.phase is not contract.phase:
                raise ValueError("phase does not match stream registry")
            if contract.source_id is not None and self.source_id is not contract.source_id:
                raise ValueError("source_id does not match stream registry")
        if not isinstance(self.estimated_items, int) or isinstance(self.estimated_items, bool) or self.estimated_items <= 0:
            raise ValueError("estimated_items must be a positive integer")
        if not isinstance(self.estimated_bytes, int) or isinstance(self.estimated_bytes, bool) or self.estimated_bytes <= 0:
            raise ValueError("estimated_bytes must be a positive integer")
        require_utc(self.deadline_utc)


def make_work_request(
    *,
    phase: SourcePhase,
    source_id: SourceId,
    work_class: WorkClass,
    estimated_items: int,
    estimated_bytes: int,
    replay_class: ReplayClass,
    cancellation_owner: ProcessRole,
    stream_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> WorkAdmissionRequest:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return WorkAdmissionRequest(
        request_id=uuid4().hex,
        phase=phase,
        source_id=source_id,
        work_class=work_class,
        estimated_items=estimated_items,
        estimated_bytes=estimated_bytes,
        deadline_utc=datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds),
        replay_class=replay_class,
        cancellation_owner=cancellation_owner,
        stream_id=stream_id,
    )


@dataclass(frozen=True, slots=True)
class AdmissionSnapshot:
    role: ProcessRole
    accepting: bool
    active: int
    pending: int
    active_items: int
    pending_items: int
    active_bytes: int
    pending_bytes: int
    active_by_class: Mapping[WorkClass, int]
    pending_by_class: Mapping[WorkClass, int]
    oldest_pending_wait_seconds_by_class: Mapping[WorkClass, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "accepting": self.accepting,
            "active": self.active,
            "pending": self.pending,
            "active_items": self.active_items,
            "pending_items": self.pending_items,
            "active_bytes": self.active_bytes,
            "pending_bytes": self.pending_bytes,
            "active_by_class": {key.value: self.active_by_class[key] for key in WorkClass},
            "pending_by_class": {key.value: self.pending_by_class[key] for key in WorkClass},
            "oldest_pending_wait_seconds_by_class": {
                key.value: self.oldest_pending_wait_seconds_by_class[key] for key in WorkClass
            },
        }


@dataclass(frozen=True, slots=True)
class AdmissionPermit:
    work_class: WorkClass
    estimated_items: int
    estimated_bytes: int
    wait_seconds: float
    owner_task: asyncio.Task[object] = field(repr=False, compare=False)


@dataclass(slots=True)
class _Waiter:
    request: WorkAdmissionRequest
    owner_task: asyncio.Task[object]
    enqueued_at: float
    sequence: int
    future: asyncio.Future[AdmissionPermit]
    permit: AdmissionPermit | None = None


class WorkAdmissionController:
    """Bound active and queued work within one process; creates no worker tasks."""

    def __init__(self, *, role: ProcessRole, limits: AdmissionLimits = PROVISIONAL_DEFAULT_LIMITS) -> None:
        if not isinstance(role, ProcessRole):
            raise TypeError("role must be a registered ProcessRole")
        if not isinstance(limits, AdmissionLimits):
            raise TypeError("limits must be AdmissionLimits")
        self.role = role
        self.limits = limits
        self._condition = asyncio.Condition()
        self._queues: dict[WorkClass, list[_Waiter]] = {work_class: [] for work_class in WorkClass}
        self._active_by_class = {work_class: 0 for work_class in WorkClass}
        self._pending_by_class = {work_class: 0 for work_class in WorkClass}
        self._active = 0
        self._pending = 0
        self._active_items = 0
        self._pending_items = 0
        self._active_bytes = 0
        self._pending_bytes = 0
        self._next_sequence = 0
        self._closed = False

    @asynccontextmanager
    async def admit(self, request: WorkAdmissionRequest) -> AsyncIterator[AdmissionPermit]:
        permit = await self._acquire(request)
        try:
            yield permit
        finally:
            await self._release(permit)

    def snapshot(self) -> AdmissionSnapshot:
        return AdmissionSnapshot(
            role=self.role,
            accepting=not self._closed,
            active=self._active,
            pending=self._pending,
            active_items=self._active_items,
            pending_items=self._pending_items,
            active_bytes=self._active_bytes,
            pending_bytes=self._pending_bytes,
            active_by_class=MappingProxyType(dict(self._active_by_class)),
            pending_by_class=MappingProxyType(dict(self._pending_by_class)),
            oldest_pending_wait_seconds_by_class=MappingProxyType({
                work_class: max(
                    (time.monotonic() - waiter.enqueued_at for waiter in queue),
                    default=0.0,
                )
                for work_class, queue in self._queues.items()
            }),
        )

    async def shutdown(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be finite and non-negative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds if timeout_seconds is not None else None
        async with self._condition:
            self._closed = True
            for queue in self._queues.values():
                while queue:
                    waiter = queue.pop(0)
                    self._pending -= 1
                    self._pending_items -= waiter.request.estimated_items
                    self._pending_bytes -= waiter.request.estimated_bytes
                    self._pending_by_class[waiter.request.work_class] -= 1
                    if not waiter.future.done():
                        waiter.future.set_exception(AdmissionDeferred(AdmissionReason.SHUTDOWN))
            self._condition.notify_all()
            while self._active:
                if deadline is None:
                    await self._condition.wait()
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return False
            return True

    async def _acquire(self, request: WorkAdmissionRequest) -> AdmissionPermit:
        if not isinstance(request, WorkAdmissionRequest):
            raise TypeError("request must be WorkAdmissionRequest")
        if request.cancellation_owner is not self.role:
            raise AdmissionDeferred(AdmissionReason.OWNER_MISMATCH)
        if (
            request.estimated_items > self.limits.max_active_items
            or request.estimated_bytes > self.limits.max_active_bytes
        ):
            raise AdmissionDeferred(AdmissionReason.CAPACITY)
        if datetime.now(timezone.utc) >= request.deadline_utc:
            raise AdmissionDeferred(AdmissionReason.DEADLINE)

        loop = asyncio.get_running_loop()
        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("admission requires an owned asyncio task")
        async with self._condition:
            if self._closed:
                raise AdmissionDeferred(AdmissionReason.SHUTDOWN)
            if (
                self._pending + 1 > self.limits.max_pending_requests
                or self._pending_items + request.estimated_items > self.limits.max_pending_items
                or self._pending_bytes + request.estimated_bytes > self.limits.max_pending_bytes
            ):
                raise AdmissionDeferred(AdmissionReason.BACKPRESSURE)
            self._next_sequence += 1
            waiter = _Waiter(
                request=request,
                owner_task=owner_task,
                enqueued_at=loop.time(),
                sequence=self._next_sequence,
                future=loop.create_future(),
            )
            self._queues[request.work_class].append(waiter)
            self._pending += 1
            self._pending_items += request.estimated_items
            self._pending_bytes += request.estimated_bytes
            self._pending_by_class[request.work_class] += 1
            self._dispatch_locked(loop.time())

        timeout = max(0.0, (request.deadline_utc - datetime.now(timezone.utc)).total_seconds())
        try:
            return await asyncio.wait_for(asyncio.shield(waiter.future), timeout=timeout)
        except TimeoutError:
            async with self._condition:
                if waiter.permit is None:
                    self._remove_waiter_locked(waiter)
                else:
                    self._release_locked(waiter.permit)
                self._dispatch_locked(loop.time())
            raise AdmissionDeferred(AdmissionReason.DEADLINE) from None
        except asyncio.CancelledError:
            async with self._condition:
                if waiter.permit is None:
                    self._remove_waiter_locked(waiter)
                else:
                    self._release_locked(waiter.permit)
                self._dispatch_locked(loop.time())
            raise

    async def _release(self, permit: AdmissionPermit) -> None:
        async with self._condition:
            self._release_locked(permit)
            self._dispatch_locked(asyncio.get_running_loop().time())

    def _remove_waiter_locked(self, waiter: _Waiter) -> None:
        queue = self._queues[waiter.request.work_class]
        try:
            queue.remove(waiter)
        except ValueError:
            return
        self._pending -= 1
        self._pending_items -= waiter.request.estimated_items
        self._pending_bytes -= waiter.request.estimated_bytes
        self._pending_by_class[waiter.request.work_class] -= 1
        if not waiter.future.done():
            waiter.future.cancel()

    def _release_locked(self, permit: AdmissionPermit) -> None:
        self._active -= 1
        self._active_by_class[permit.work_class] -= 1
        self._active_items -= permit.estimated_items
        self._active_bytes -= permit.estimated_bytes
        self._condition.notify_all()

    def _dispatch_locked(self, now: float) -> None:
        while self._active < self.limits.max_active:
            candidates = [
                queue[0]
                for work_class, queue in self._queues.items()
                if queue
                and self._active_by_class[work_class] < self.limits.max_active_by_class[work_class]
                and self._active_items + queue[0].request.estimated_items <= self.limits.max_active_items
                and self._active_bytes + queue[0].request.estimated_bytes <= self.limits.max_active_bytes
            ]
            if not candidates:
                return
            waiter = max(
                candidates,
                key=lambda item: (
                    self.limits.weights[item.request.work_class]
                    + (now - item.enqueued_at) / self.limits.aging_interval_seconds,
                    -item.sequence,
                ),
            )
            self._queues[waiter.request.work_class].pop(0)
            self._pending -= 1
            self._pending_items -= waiter.request.estimated_items
            self._pending_bytes -= waiter.request.estimated_bytes
            self._pending_by_class[waiter.request.work_class] -= 1
            request = waiter.request
            permit = AdmissionPermit(
                work_class=request.work_class,
                estimated_items=request.estimated_items,
                estimated_bytes=request.estimated_bytes,
                wait_seconds=max(0.0, now - waiter.enqueued_at),
                owner_task=waiter.owner_task,
            )
            waiter.permit = permit
            self._active += 1
            self._active_by_class[request.work_class] += 1
            self._active_items += request.estimated_items
            self._active_bytes += request.estimated_bytes
            if not waiter.future.done():
                waiter.future.set_result(permit)
