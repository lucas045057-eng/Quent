"""Bounded, opt-in orchestration for Phase 4 public market context."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import aiohttp
from websockets.asyncio.client import connect

from quant_phase1.config import PUBLIC_WS_CLOSE_TIMEOUT_SECONDS
from quant_phase1.adapters.bitget_v3.rate_limit import TokenBucket
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.observability import ProcessRole, SourceId, SourcePhase, WorkClass

from .aggregation import LiquidationWindow, LiquidationWindowBuilder, rollup_liquidation_windows
from .contracts import BasisObservation, CanonicalLiquidation, DataStatus, LongShortObservation
from .health import Phase4HealthRegistry, Phase4HealthSnapshot, Phase4HealthState
from .liquidation import BoundedLiquidationDeduplicator, BoundedLiquidationQueue, liquidation_identity_key
from .persistence import Phase4Retention
from .adapters.base import AdapterError, SharedPublicRESTTransport


HYDRATED_HIGH_WATERMARK_STATUS = DataStatus.STALE
HYDRATED_HIGH_WATERMARK_REASON = "HYDRATED_HIGH_WATERMARK"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC-aware")
    return value.astimezone(timezone.utc)


def _long_short_endpoint_id(adapter: Any, exchange: str) -> str:
    endpoint = getattr(adapter, "endpoint_id", None)
    return endpoint if isinstance(endpoint, str) and endpoint else f"{exchange}_long_short_public"


def _phase4_adapter_failure(exchange: str, adapter: Any, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AdapterError):
        category = exc.category
    elif isinstance(exc, TimeoutError):
        category = "TIMEOUT"
    elif isinstance(exc, (aiohttp.ClientError, OSError)):
        category = "NETWORK_ERROR"
    else:
        category = "INTERNAL_ERROR"
    provider_code = getattr(exc, "provider_code", None)
    http_status = getattr(exc, "http_status", None)
    return {
        "provider": exchange,
        "endpoint": _long_short_endpoint_id(adapter, exchange),
        "error_category": category,
        "error_code": provider_code or (f"HTTP_{http_status}" if http_status is not None else None),
        "error_type": type(exc).__name__,
        "http_status": http_status,
        "schema_stage": getattr(exc, "schema_stage", None),
        "schema_error_summary": (
            getattr(exc, "schema_field", None)
            or ("RESPONSE_VALIDATION" if category == "SCHEMA_ERROR" else None)
        ),
    }


def _is_external_transient_failure(failure: Mapping[str, Any]) -> bool:
    category = failure.get("error_category")
    if category in {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT"}:
        return True
    return category == "HTTP_ERROR" and isinstance(failure.get("http_status"), int) and failure["http_status"] >= 500


@dataclass(frozen=True, slots=True)
class Phase4RestCycleResult:
    long_short: tuple[LongShortObservation, ...]
    basis: tuple[BasisObservation, ...]


@dataclass(frozen=True, slots=True)
class _LiquidationGapWatermark:
    """Transport-recovery boundary, using receipt then source event time."""

    received_at: datetime
    event_timestamp: datetime

    @classmethod
    def from_disconnect(cls, now: datetime) -> "_LiquidationGapWatermark":
        normalized = _utc(now)
        return cls(received_at=normalized, event_timestamp=normalized)

    @classmethod
    def from_event(cls, event: CanonicalLiquidation) -> "_LiquidationGapWatermark":
        return cls(received_at=_utc(event.received_at), event_timestamp=_utc(event.event_timestamp))

    def is_newer(self, other: "_LiquidationGapWatermark") -> bool:
        return (self.received_at, self.event_timestamp) > (other.received_at, other.event_timestamp)


@dataclass(frozen=True, slots=True)
class _RollupRebuildMarker:
    """Bounded durable-recovery range for one scope and timeframe."""

    earliest_window_open: datetime
    latest_window_open: datetime

    def extend(self, window_open: datetime) -> "_RollupRebuildMarker":
        opened = _utc(window_open)
        return _RollupRebuildMarker(
            earliest_window_open=min(self.earliest_window_open, opened),
            latest_window_open=max(self.latest_window_open, opened),
        )


@dataclass(frozen=True, slots=True)
class _GlobalRollupRebuildMarker:
    """Durable cursor for bounded recovery across source scopes."""

    earliest_window_open: datetime
    latest_window_open: datetime
    rebuild_earliest_window_open: datetime
    rebuild_latest_window_open: datetime
    cursor_exchange: str | None = None
    cursor_symbol: str | None = None
    cursor_timeframe: str | None = None

    @classmethod
    def from_range(cls, marker: _RollupRebuildMarker) -> "_GlobalRollupRebuildMarker":
        return cls(
            earliest_window_open=marker.earliest_window_open,
            latest_window_open=marker.latest_window_open,
            rebuild_earliest_window_open=marker.earliest_window_open,
            rebuild_latest_window_open=marker.latest_window_open,
        )

    def extend(self, marker: _RollupRebuildMarker) -> "_GlobalRollupRebuildMarker":
        return _GlobalRollupRebuildMarker(
            earliest_window_open=min(self.earliest_window_open, marker.earliest_window_open),
            latest_window_open=max(self.latest_window_open, marker.latest_window_open),
            rebuild_earliest_window_open=min(self.rebuild_earliest_window_open, marker.earliest_window_open),
            rebuild_latest_window_open=max(self.rebuild_latest_window_open, marker.latest_window_open),
            cursor_exchange=self.cursor_exchange,
            cursor_symbol=self.cursor_symbol,
            cursor_timeframe=self.cursor_timeframe,
        )

    def at_cursor(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
    ) -> "_GlobalRollupRebuildMarker":
        return _GlobalRollupRebuildMarker(
            earliest_window_open=self.rebuild_earliest_window_open,
            latest_window_open=self.rebuild_latest_window_open,
            rebuild_earliest_window_open=self.rebuild_earliest_window_open,
            rebuild_latest_window_open=self.rebuild_latest_window_open,
            cursor_exchange=exchange,
            cursor_symbol=symbol,
            cursor_timeframe=timeframe,
        )

    def with_progress(
        self,
        marker: _RollupRebuildMarker,
    ) -> "_GlobalRollupRebuildMarker":
        return _GlobalRollupRebuildMarker(
            earliest_window_open=marker.earliest_window_open,
            latest_window_open=marker.latest_window_open,
            rebuild_earliest_window_open=self.rebuild_earliest_window_open,
            rebuild_latest_window_open=self.rebuild_latest_window_open,
            cursor_exchange=self.cursor_exchange,
            cursor_symbol=self.cursor_symbol,
            cursor_timeframe=self.cursor_timeframe,
        )


def _bounded_symbols(symbols: Iterable[str], limit: int) -> tuple[str, ...]:
    selected = []
    seen = set()
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        if not normalized or normalized in seen:
            continue
        selected.append(normalized)
        seen.add(normalized)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _row_reason(row: Any, fallback: str) -> str:
    return getattr(getattr(row, "reason_code", None), "value", None) or fallback


def _estimated_event_bytes(event: CanonicalLiquidation) -> int:
    """Conservatively charge bounded in-memory state for one canonical event."""

    text_fields = (
        event.event_id,
        event.exchange,
        event.exchange_symbol,
        event.canonical_symbol,
        event.source_endpoint,
        event.source_channel,
        event.raw_side,
        event.raw_side_semantics,
        event.raw_reference,
    )
    # The fixed charge covers the dataclass, deque/dict references, Decimal
    # objects, and allocator overhead; text is charged again by UTF-8 bytes.
    payload_bytes = len(repr(event.raw_payload).encode("utf-8")) if event.raw_payload is not None else 0
    return 768 + payload_bytes + sum(len(value.encode("utf-8")) for value in text_fields if value)


def _estimated_window_bytes(window: LiquidationWindow) -> int:
    return 512 + len(window.exchange) + len(window.canonical_symbol) + len(window.timeframe) + len(window.reason or "")


def _minute_open(value: datetime) -> datetime:
    normalized = _utc(value)
    epoch = int(normalized.timestamp())
    return datetime.fromtimestamp(epoch - epoch % 60, tz=timezone.utc)


class Phase4PublicLiquidationRunner:
    """Reconnectable public-only transport for the two liquidation streams."""

    def __init__(self, runtime: "Phase4Runtime", *, connect_factory=connect, reconnect_seconds: float = 5.0) -> None:
        self.runtime = runtime
        self.connect_factory = connect_factory
        self.reconnect_seconds = reconnect_seconds

    async def run_exchange(self, exchange: str, symbols: tuple[str, ...], *, stop_event: asyncio.Event) -> None:
        adapter = self.runtime.liquidation_adapters[exchange]
        while not stop_event.is_set():
            try:
                async with self.connect_factory(
                    adapter.public_ws_url,
                    open_timeout=15,
                    ping_interval=20,
                    close_timeout=PUBLIC_WS_CLOSE_TIMEOUT_SECONDS,
                ) as socket:
                    for symbol in symbols:
                        await socket.send(json.dumps(adapter.subscription(symbol), separators=(",", ":")))
                    while not stop_event.is_set():
                        raw = await socket.recv()
                        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                        if self._is_control(payload):
                            continue
                        self.runtime.ingest_liquidation(exchange, payload, datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A worker stop is used for both planned universe refreshes
                # and runtime shutdown.  Its socket may raise while the
                # context manager closes, but that is not a transport gap.
                if stop_event.is_set():
                    return
                now = datetime.now(timezone.utc)
                await self.runtime.handle_disconnect(exchange, now)
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.reconnect_seconds)
                except asyncio.TimeoutError:
                    await self.runtime.handle_reconnect(exchange, datetime.now(timezone.utc))
                if isinstance(exc, StopAsyncIteration):
                    continue
        if not stop_event.is_set():
            await self.runtime.handle_disconnect(exchange, datetime.now(timezone.utc))

    async def run_dynamic(self, symbols_provider: Any, *, stop_event: asyncio.Event, max_symbols: int) -> None:
        worker_stop = asyncio.Event()
        workers: tuple[asyncio.Task[Any], ...] = ()
        try:
            while not stop_event.is_set():
                symbols = _bounded_symbols(symbols_provider(), max_symbols)
                if workers:
                    worker_stop.set()
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                worker_stop = asyncio.Event()
                workers = tuple(
                    asyncio.create_task(self.run_exchange(exchange, symbols, stop_event=worker_stop))
                    for exchange in sorted(self.runtime.liquidation_adapters)
                    if symbols
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max(1.0, self.runtime.settings.phase4_rest_cycle_seconds))
                except asyncio.TimeoutError:
                    continue
        finally:
            worker_stop.set()
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)

    @staticmethod
    def _is_control(payload: Any) -> bool:
        return not isinstance(payload, Mapping) or payload.get("event") in {"subscribe", "unsubscribe", "error"}


class Phase4Runtime:
    """Own only bounded Phase 4 state; persistence remains idempotent."""

    def __init__(
        self,
        settings: Any,
        *,
        liquidation_adapters: Mapping[str, Any] | None = None,
        long_short_adapters: Mapping[str, Any] | None = None,
        basis_adapters: Mapping[str, Any] | None = None,
        repository: Any | None = None,
        repository_provider: Any | None = None,
        symbols_provider: Any | None = None,
        connect_factory: Any = connect,
        session_factory: Any = aiohttp.ClientSession,
        reconnect_seconds: float | None = None,
        admission: WorkAdmissionController | None = None,
    ) -> None:
        self.settings = settings
        self.admission = admission or WorkAdmissionController(role=ProcessRole.COLLECTOR)
        if self.admission.role is not ProcessRole.COLLECTOR:
            raise ValueError("Phase 4 collector runtime requires Collector-owned admission")
        self.repository = repository
        self.repository_provider = repository_provider
        if liquidation_adapters is None or long_short_adapters is None or basis_adapters is None:
            from .adapters.bitget_classic_v2 import BitgetClassicV2LongShortAdapter
            from .adapters.bitget_uta_v3 import BitgetUTA3BasisAdapter, BitgetUTA3LiquidationAdapter
            from .adapters.bybit_v5 import BybitV5BasisAdapter, BybitV5LiquidationAdapter, BybitV5LongShortAdapter
            from .adapters.hyperliquid_public import HyperliquidPublicLongShortAdapter

        self.liquidation_adapters = dict({
            "bitget": BitgetUTA3LiquidationAdapter(),
            "bybit": BybitV5LiquidationAdapter(),
        } if liquidation_adapters is None else liquidation_adapters)
        if "bitget" in self.liquidation_adapters:
            self.liquidation_adapters["bitget"].public_ws_url = settings.phase4_bitget_uta_ws_public_url
        if "bybit" in self.liquidation_adapters:
            self.liquidation_adapters["bybit"].public_ws_url = settings.phase4_bybit_ws_public_linear_url
        self.long_short_adapters = dict({
            "bitget": BitgetClassicV2LongShortAdapter(settings=settings),
            "bybit": BybitV5LongShortAdapter(settings=settings),
            "hyperliquid": HyperliquidPublicLongShortAdapter(),
        } if long_short_adapters is None else long_short_adapters)
        self.basis_adapters = dict({
            "bitget": BitgetUTA3BasisAdapter(settings=settings),
            "bybit": BybitV5BasisAdapter(settings=settings),
        } if basis_adapters is None else basis_adapters)
        self.symbols_provider = symbols_provider
        self.health = Phase4HealthRegistry()
        self._queues: dict[tuple[str, str], BoundedLiquidationQueue] = {}
        self._dedup = BoundedLiquidationDeduplicator(
            max_entries_per_exchange=min(settings.phase4_queue_capacity * 5, 10_000), ttl_seconds=300
        )
        self._builders: dict[tuple[str, str], LiquidationWindowBuilder] = {}
        self._tasks: tuple[asyncio.Task[Any], ...] = ()
        self._stop_event = asyncio.Event()
        self._connect_factory = connect_factory
        self._session_factory = session_factory
        self._reconnect_seconds = settings.ws_reconnect_seconds if reconnect_seconds is None else reconnect_seconds
        self._rest_session: Any | None = None
        self._rate_limiter = TokenBucket(settings.rest_requests_per_second, capacity=20)
        self._running = False
        self._gap_count = 0
        self._resubscribe_count = 0
        self.resubscriptions: tuple[str, ...] = ()
        self._persisted_window_keys: dict[tuple[str, str, str, datetime], datetime] = {}
        self._pending_event_recovery: dict[tuple[str, ...], CanonicalLiquidation] = {}
        self._pending_event_recovery_bytes = 0
        self._pending_window_rows: dict[tuple[str, str, str, datetime], LiquidationWindow] = {}
        self._pending_rollup_rows: dict[tuple[str, str, str, datetime], LiquidationWindow] = {}
        self._pending_rollup_rebuilds: dict[tuple[str, str, str, datetime], None] = {}
        self._rollup_rebuild_needed: dict[tuple[str, str, str], _RollupRebuildMarker] = {}
        self._rollup_rebuild_all: _GlobalRollupRebuildMarker | None = None
        exchange_count = max(2, len(self.liquidation_adapters))
        self._rollup_rebuild_marker_budget = max(
            1, min(4_096, getattr(settings, "universe_limit", 200) * exchange_count * 4)
        )
        self._rollup_rebuild_overflow_count = 0
        self._window_persistence_overflow_count = 0
        self._queued_event_count = 0
        self._queued_event_bytes = 0
        self._event_budget = getattr(settings, "phase4_event_budget", 20_000)
        self._event_bytes_budget = getattr(settings, "phase4_event_bytes_budget", 32 * 1024 * 1024)
        self._window_retry_budget = max(1, min(4_096, self._event_budget))
        self._builder_window_budget = getattr(settings, "phase4_builder_window_budget", self._event_budget)
        self._finalized_window_budget = getattr(settings, "phase4_finalized_window_budget", self._event_budget * 2)
        self._rollup_window_budget = getattr(settings, "phase4_rollup_window_budget", self._event_budget * 5)
        self._rollup_retry_budget = max(1, min(4_096, self._rollup_window_budget))
        self._hydration_windows_per_key = getattr(settings, "phase4_hydration_windows_per_key", 240)
        self.windows: tuple[LiquidationWindow, ...] = ()
        self.retention = Phase4Retention(
            liquidation_event_hours=settings.phase4_liquidation_event_retention_hours,
            liquidation_window_days=settings.phase4_liquidation_retention_days,
            long_short_days=settings.phase4_long_short_retention_days,
            basis_days=settings.phase4_basis_retention_days,
            cross_exchange_days=settings.phase4_cross_exchange_retention_days,
            enrichment_days=settings.phase4_enrichment_retention_days,
        )
        self._liquidation_gap_pending: set[str] = set()
        self._liquidation_gap_watermarks: dict[str, _LiquidationGapWatermark] = {}
        self._hydrated_window_high_watermarks: dict[tuple[str, str], datetime] = {}
        self._rollup_scope_iteration_failed = False

    async def _open_rest_transport(self) -> None:
        if self._rest_session is None:
            self._rest_session = self._session_factory(timeout=aiohttp.ClientTimeout(total=15))
            if hasattr(self._rest_session, "__aenter__"):
                entered = await self._rest_session.__aenter__()
                if entered is not None:
                    self._rest_session = entered
        for adapter in (*self.long_short_adapters.values(), *self.basis_adapters.values()):
            if hasattr(adapter, "configure_transport"):
                base_url = getattr(adapter, "base_url", "https://api.example.invalid")
                adapter.configure_transport(SharedPublicRESTTransport(base_url, session=self._rest_session, rate_limiter=self._rate_limiter))

    async def _close_rest_transport(self) -> None:
        session, self._rest_session = self._rest_session, None
        if session is not None and hasattr(session, "close"):
            result = session.close()
            if asyncio.iscoroutine(result):
                await result

    @property
    def running(self) -> bool:
        return self._running

    @property
    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return self._tasks

    @property
    def gap_count(self) -> int:
        return self._gap_count

    @property
    def resubscribe_count(self) -> int:
        return self._resubscribe_count

    @property
    def pending_event_count(self) -> int:
        return sum(queue.depth for queue in self._queues.values()) + len(self._pending_event_recovery)

    def health_snapshot(self, component: str) -> Phase4HealthSnapshot:
        return self.health.snapshot(component)

    def persist_health(self, repository: Any) -> None:
        """Write one bounded row per metric component through the existing health table."""
        from quant_phase1.contracts import DataStatus as Phase1DataStatus

        status = {
            Phase4HealthState.RUNNING: Phase1DataStatus.AVAILABLE,
            Phase4HealthState.RECOVERED: Phase1DataStatus.AVAILABLE,
            Phase4HealthState.STALE: Phase1DataStatus.STALE,
            Phase4HealthState.DEGRADED: Phase1DataStatus.ERROR,
            Phase4HealthState.ERROR: Phase1DataStatus.ERROR,
        }
        for component, snapshot in self.health.snapshots().items():
            historical_gap = component == "liquidation" and snapshot.gap_count > 0
            phase_status = (
                "ERROR" if snapshot.state is Phase4HealthState.ERROR else
                "STALE" if snapshot.state is Phase4HealthState.STALE else
                "PARTIAL" if historical_gap or snapshot.state is Phase4HealthState.DEGRADED else
                "AVAILABLE"
            )
            data_quality = snapshot.diagnostics.get("data_quality")
            if data_quality is None and historical_gap:
                data_quality = "PARTIAL"
            persisted_status = (
                Phase1DataStatus.NOT_AVAILABLE
                if phase_status == "PARTIAL"
                else status[snapshot.state]
            )
            watermark = self._liquidation_gap_watermarks.get(component)
            repository.upsert_system_health(
                f"phase4-{component}",
                persisted_status,
                snapshot.checked_at,
                {
                    "runtime_state": snapshot.state.value,
                    "phase4_status": phase_status,
                    "data_quality": data_quality,
                    "reason": snapshot.reason,
                    "reconnect_count": snapshot.reconnect_count,
                    "dropped_count": snapshot.dropped_count,
                    "rejected_count": snapshot.rejected_count,
                    "gap_count": snapshot.gap_count,
                    "gap_detected": historical_gap,
                    "gap_reason": snapshot.gap_reason,
                    "gap_detected_at": snapshot.gap_detected_at.isoformat() if snapshot.gap_detected_at else None,
                    "gap_watermark_received_at": watermark.received_at.isoformat() if watermark else (
                        snapshot.gap_watermark_received_at.isoformat()
                        if snapshot.gap_watermark_received_at else None
                    ),
                    "gap_watermark_event_timestamp": watermark.event_timestamp.isoformat() if watermark else (
                        snapshot.gap_watermark_event_timestamp.isoformat()
                        if snapshot.gap_watermark_event_timestamp else None
                    ),
                    "last_exchange_at": snapshot.last_exchange_at.isoformat() if snapshot.last_exchange_at else None,
                    "last_fetched_at": snapshot.last_fetched_at.isoformat() if snapshot.last_fetched_at else None,
                    "last_processed_at": snapshot.last_processed_at.isoformat() if snapshot.last_processed_at else None,
                    **snapshot.diagnostics,
                },
            )

    async def start(self) -> None:
        if not getattr(self.settings, "phase4_enabled", False) or self._running:
            return
        self._running = True
        self._stop_event = asyncio.Event()
        now = datetime.now(timezone.utc)
        self.health.mark_started(now)
        await self._open_rest_transport()
        self._hydrate_persisted_windows(now)
        worker = asyncio.create_task(self._liquidation_worker(), name="phase4-liquidation-worker")
        rest = asyncio.create_task(self._rest_scheduler(), name="phase4-rest-scheduler")
        transport = asyncio.create_task(
            Phase4PublicLiquidationRunner(
                self, connect_factory=self._connect_factory, reconnect_seconds=self._reconnect_seconds
            ).run_dynamic(self.symbols_provider or (lambda: ()), stop_event=self._stop_event, max_symbols=self.settings.universe_limit),
            name="phase4-liquidation-transport",
        )
        self._tasks = (worker, rest, transport)

    async def stop(self) -> None:
        if not self._running and not self._tasks:
            return
        self._running = False
        self._stop_event.set()
        tasks = self._tasks
        self._tasks = ()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_rest_transport()

    def _queue_for(self, exchange: str, symbol: str, now: datetime) -> BoundedLiquidationQueue | None:
        key = (exchange, symbol)
        queue = self._queues.get(key)
        if queue is not None:
            return queue
        exchange_count = max(2, len(self.liquidation_adapters))
        max_queues = max(1, getattr(self.settings, "universe_limit", 200)) * exchange_count
        if len(self._queues) >= max_queues:
            self.health.mark_backpressure("liquidation", now, dropped=1)
            return None
        queue = BoundedLiquidationQueue(exchange=exchange, symbol=symbol, capacity=self.settings.phase4_queue_capacity)
        self._queues[key] = queue
        self._builders[key] = LiquidationWindowBuilder(max_open_windows=self.settings.phase4_queue_capacity)
        return queue

    @property
    def _persistence_configured(self) -> bool:
        return self.repository is not None or self.repository_provider is not None

    def _queue_event(self, queue: BoundedLiquidationQueue, event: CanonicalLiquidation, now: datetime) -> bool:
        event_bytes = _estimated_event_bytes(event)
        if (
            self._state_count() + 1 > self._event_budget + self._finalized_window_budget + self._rollup_window_budget
            or self._state_bytes() + event_bytes > self._event_bytes_budget
        ):
            return False
        if not queue.put_nowait(event, now=now):
            return False
        self._queued_event_count += 1
        self._queued_event_bytes += event_bytes
        return True

    def _dequeue_event(self, event: CanonicalLiquidation) -> None:
        self._queued_event_count = max(0, self._queued_event_count - 1)
        self._queued_event_bytes = max(0, self._queued_event_bytes - _estimated_event_bytes(event))

    def _retain_event_for_retry(self, event: CanonicalLiquidation) -> bool:
        key = liquidation_identity_key(event)
        if key in self._pending_event_recovery:
            return True
        event_bytes = _estimated_event_bytes(event)
        if (
            self._state_count() + 1 > self._event_budget + self._finalized_window_budget + self._rollup_window_budget
            or self._state_bytes() + event_bytes > self._event_bytes_budget
        ):
            return False
        self._pending_event_recovery[key] = event
        self._pending_event_recovery_bytes += event_bytes
        self._queued_event_count += 1
        self._queued_event_bytes += event_bytes
        return True

    def _release_recovery_event(self, key: tuple[str, ...], event: CanonicalLiquidation) -> None:
        self._pending_event_recovery.pop(key, None)
        event_bytes = _estimated_event_bytes(event)
        self._pending_event_recovery_bytes = max(0, self._pending_event_recovery_bytes - event_bytes)
        self._queued_event_count = max(0, self._queued_event_count - 1)
        self._queued_event_bytes = max(0, self._queued_event_bytes - event_bytes)

    def _persist_event(self, event: CanonicalLiquidation) -> None:
        with self._repository_scope() as repository:
            if repository is None:
                raise RuntimeError("Phase 4 repository is unavailable")
            repository.insert_liquidation_events([event])

    def ingest_liquidation(self, exchange: str, payload: Mapping[str, Any], received_at: datetime) -> int:
        if not getattr(self.settings, "phase4_enabled", False):
            return 0
        received_at = _utc(received_at)
        adapter = self.liquidation_adapters[exchange]
        events = adapter.parse_ws_message(payload, received_at)
        accepted = 0
        accepted_timestamps: list[datetime] = []
        for event in events:
            if not self._dedup.add(event, now=received_at):
                continue
            if self._reject_hydrated_closed_minute(event):
                # This is an intentional stale-source rejection.  It occurs
                # before event persistence and queue admission, so a late
                # event cannot be stored as if it updated a window that the
                # hydrated watermark has already closed.  It is not a
                # capacity drop and therefore does not degrade health.
                self.health.mark_rejected(
                    "liquidation", received_at, reason=HYDRATED_HIGH_WATERMARK_REASON
                )
                continue
            persisted = not self._persistence_configured
            if self._persistence_configured:
                try:
                    self._persist_event(event)
                    persisted = True
                except Exception:
                    self.health.mark_db_outage("liquidation", received_at)
                    if self._retain_event_for_retry(event):
                        accepted += 1
                        accepted_timestamps.append(event.event_timestamp)
                    else:
                        self.health.mark_backpressure("liquidation", received_at, dropped=1)
                    continue
            queue = self._queue_for(exchange, event.canonical_symbol, received_at)
            if queue is None or not self._queue_event(queue, event, received_at):
                self.health.mark_backpressure("liquidation", received_at, dropped=0)
                if persisted and self._persistence_configured and self._retain_event_for_retry(event):
                    accepted += 1
                    accepted_timestamps.append(event.event_timestamp)
                else:
                    self.health.mark_backpressure("liquidation", received_at, dropped=1)
                continue
            accepted += 1
            accepted_timestamps.append(event.event_timestamp)
            if persisted:
                self._maybe_recover_liquidation(exchange, event, received_at)
        if accepted:
            self.health.update_timestamps("liquidation", exchange=max(accepted_timestamps), processed=received_at)
        return accepted

    async def handle_disconnect(self, exchange: str, now: datetime) -> None:
        now = _utc(now)
        for (source, symbol), queue in self._queues.items():
            if source == exchange:
                queue.record_disconnect_without_backfill(now=now)
                builder = self._builders[(source, symbol)]
                if builder.contains_window(source, symbol, now) or self._builder_window_count() < self._builder_window_budget:
                    builder.mark_gap(source, symbol, now, reason="LIQUIDATION_GAP_NO_BACKFILL")
                else:
                    # The queue and component health remain explicit even when
                    # the bounded builder cannot admit another minute identity.
                    self.health.mark_backpressure("liquidation", now, dropped=0)
        self._gap_count += 1
        self._liquidation_gap_pending.add(exchange)
        watermark = _LiquidationGapWatermark.from_disconnect(now)
        previous = self._liquidation_gap_watermarks.get(exchange)
        if previous is None or watermark.is_newer(previous):
            self._liquidation_gap_watermarks[exchange] = watermark
        self.health.mark_gap(
            "liquidation", now, reason="LIQUIDATION_GAP_NO_BACKFILL",
            watermark_received_at=watermark.received_at,
            watermark_event_timestamp=watermark.event_timestamp,
        )

    async def handle_reconnect(self, exchange: str, now: datetime) -> None:
        now = _utc(now)
        self._resubscribe_count += 1
        adapter = self.liquidation_adapters.get(exchange)
        subscriptions = []
        if adapter is not None and hasattr(adapter, "subscription"):
            for source, symbol in sorted(self._queues):
                if source == exchange:
                    adapter.subscription(symbol)
                    subscriptions.append(f"{exchange}:{symbol}")
        else:
            subscriptions = [f"{exchange}:{symbol}" for source, symbol in sorted(self._queues) if source == exchange]
        self.resubscriptions = tuple(subscriptions)
        self.health.mark_gap(
            "liquidation", now, reason="LIQUIDATION_GAP_AWAITING_FRESH_EVENT", count=False,
        )

    def queue_depth(self, exchange: str, symbol: str) -> int:
        queue = self._queues.get((exchange, symbol))
        return queue.depth if queue else 0

    async def run_rest_cycle(self, symbols: Iterable[str], processed_at: datetime) -> Phase4RestCycleResult:
        processed_at = _utc(processed_at)
        long_short_rows: list[LongShortObservation] = []
        basis_rows: list[BasisObservation] = []
        long_short_failures: list[dict[str, Any]] = []
        for symbol in _bounded_symbols(symbols, getattr(self.settings, "universe_limit", 200)):
            for exchange, adapter in tuple(self.long_short_adapters.items()):
                try:
                    if hasattr(adapter, "fetch"):
                        row = await adapter.fetch(symbol, "5m", received_at=processed_at)
                    elif hasattr(adapter, "unavailable"):
                        row = adapter.unavailable(symbol, "5m", received_at=processed_at)
                    else:
                        continue
                    long_short_rows.append(row)
                except Exception as exc:
                    long_short_failures.append(_phase4_adapter_failure(exchange, adapter, exc))
            for exchange, adapter in tuple(self.basis_adapters.items()):
                try:
                    if hasattr(adapter, "fetch"):
                        row = await adapter.fetch(symbol, received_at=processed_at)
                    else:
                        row = None
                    if row is not None:
                        basis_rows.append(row)
                        self._mark_metric("basis", row, processed_at)
                except Exception as exc:
                    self.health.mark_error("basis", processed_at, reason=f"REST_ERROR:{type(exc).__name__}")
        available_rows = [row for row in long_short_rows if row.status is DataStatus.AVAILABLE]
        if long_short_failures:
            critical = next(
                (
                    failure for failure in long_short_failures
                    if not _is_external_transient_failure(failure)
                ),
                None,
            )
            failure = critical or long_short_failures[-1]
            details = dict(failure)
            details["source_failure_count"] = len(long_short_failures)
            details["usable_source_count"] = len({row.exchange for row in available_rows})
            if available_rows:
                details["last_success_at"] = max(row.fetched_at for row in available_rows).isoformat()
            if critical is None and available_rows:
                details["data_quality"] = "PARTIAL"
                self.health.mark_degraded(
                    "long_short", processed_at, reason="LONG_SHORT_PROVIDER_DEGRADED", diagnostics=details
                )
            else:
                reason = (
                    "LONG_SHORT_SCHEMA_ERROR"
                    if failure.get("error_category") in {"SCHEMA_ERROR", "PARSE_ERROR"}
                    else "LONG_SHORT_SOURCES_UNAVAILABLE"
                )
                self.health.mark_error("long_short", processed_at, reason=reason, diagnostics=details)
        elif available_rows:
            selected = max(available_rows, key=lambda row: row.fetched_at)
            endpoint = _long_short_endpoint_id(self.long_short_adapters.get(selected.exchange), selected.exchange)
            self._mark_metric("long_short", selected, processed_at)
            self.health.mark_success(
                "long_short",
                processed_at,
                provider=selected.exchange,
                endpoint=endpoint,
                exchange=selected.exchange_timestamp,
                fetched=selected.fetched_at,
                processed=selected.processed_at,
            )
        else:
            for row in long_short_rows:
                self._mark_metric("long_short", row, processed_at)
        self._persist_rest(long_short_rows, basis_rows, processed_at)
        return Phase4RestCycleResult(tuple(long_short_rows), tuple(basis_rows))

    def _mark_metric(self, component: str, row: Any, now: datetime) -> None:
        self.health.update_timestamps(component, exchange=row.exchange_timestamp, fetched=row.fetched_at, processed=now)
        current = self.health.snapshot(component)
        is_current_cycle = getattr(row, "processed_at", None) == now
        if row.status is DataStatus.AVAILABLE and is_current_cycle:
            if self.health.snapshot(component).state is not Phase4HealthState.RUNNING:
                self.health.mark_recovered(component, now)
        elif row.status is DataStatus.STALE:
            if current.state is not Phase4HealthState.STALE:
                self.health.mark_stale(component, now, reason=_row_reason(row, "STALE_REST"))
        elif row.status is DataStatus.ERROR:
            if current.state is not Phase4HealthState.ERROR:
                self.health.mark_error(component, now, reason=_row_reason(row, "REST_ERROR"))
        elif row.status is DataStatus.NOT_AVAILABLE and current.state in {
            Phase4HealthState.RUNNING,
            Phase4HealthState.RECOVERED,
        }:
            self.health.mark_stale(component, now, reason=_row_reason(row, "NOT_AVAILABLE"))

    def _persist_rest(self, long_short_rows: list[LongShortObservation], basis_rows: list[BasisObservation], now: datetime) -> None:
        try:
            with self._repository_scope() as repository:
                if repository is None:
                    return
                if long_short_rows:
                    repository.insert_long_short(long_short_rows)
                if basis_rows:
                    repository.insert_basis(basis_rows)
        except Exception:
            if long_short_rows:
                self.health.mark_db_outage("long_short", now)
            if basis_rows:
                self.health.mark_db_outage("basis", now)

    def _cleanup(self) -> None:
        try:
            with self._repository_scope() as repository:
                if repository is not None:
                    repository.cleanup(self.retention)
        except Exception:
            # Retention is bounded housekeeping; a cleanup outage must not stop ingestion.
            return

    async def _liquidation_worker(self) -> None:
        try:
            while self._running:
                now = datetime.now(timezone.utc)
                event_batch_limit = min(256, max(1, self.settings.phase4_queue_capacity))
                request = make_work_request(
                    phase=SourcePhase.PHASE4,
                    source_id=SourceId.PHASE4_LIQUIDATION,
                    work_class=WorkClass.HEAVY,
                    estimated_items=event_batch_limit,
                    estimated_bytes=max(65_536, min(8 * 1024 * 1024, self._event_bytes_budget)),
                    replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
                    cancellation_owner=ProcessRole.COLLECTOR,
                    stream_id="phase4.liquidation_events",
                    timeout_seconds=2.0,
                )
                try:
                    async with self.admission.admit(request):
                        self._process_liquidation_batch(now, event_batch_limit)
                except AdmissionDeferred:
                    # Queue contents are untouched until admission succeeds.
                    pass
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise

    def _process_liquidation_batch(self, now: datetime, event_batch_limit: int) -> None:
        self._retry_event_recovery(now)
        self._retry_window_persistence(now)
        completed: list[LiquidationWindow] = []
        drained = 0
        while drained < event_batch_limit:
            progressed = False
            for key, queue in tuple(sorted(self._queues.items())):
                if not queue.depth or drained >= event_batch_limit:
                    continue
                event = queue.get_nowait()
                self._dequeue_event(event)
                builder = self._builders[key]
                if not builder.contains(event) and self._builder_window_count() >= self._builder_window_budget:
                    self.health.mark_backpressure("liquidation", now, dropped=1)
                    continue
                if not builder.add(event):
                    continue
                drained += 1
                progressed = True
            if not progressed:
                break
        pending_limit = max(0, self._window_retry_budget - len(self._pending_window_rows))
        if pending_limit:
            for key in sorted(self._builders):
                if pending_limit <= 0:
                    break
                builder = self._builders[key]
                rows = builder.finalize_pending(now, processed_at=now, limit=pending_limit)
                completed.extend(rows)
                pending_limit -= len(rows)
        if completed:
            self.windows = tuple((self.windows + tuple(completed))[-self._rollup_window_budget :])
            persisted = self._persist_windows(completed)
            persisted_minutes = tuple(
                row for row in completed
                if (row.exchange, row.canonical_symbol, row.timeframe, row.window_open) in persisted
            )
            if persisted_minutes:
                self._persist_windows(self._rollups_for(persisted_minutes, now))
            self._compact_state()

    def _persist_windows(
        self, windows: Iterable[LiquidationWindow], *, startup_hydration: bool = False
    ) -> tuple[tuple[str, str, str, datetime], ...]:
        if startup_hydration:
            return self._persist_hydrated_rollups(windows)
        incoming: dict[str, list[LiquidationWindow]] = {"1m": [], "rollup": []}
        seen: set[tuple[str, str, str, datetime]] = set()
        for row in windows:
            key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
            if key in seen:
                continue
            seen.add(key)
            if row.timeframe == "1m":
                incoming["1m"].append(row)
            else:
                incoming["rollup"].append(row)

        persisted_keys: list[tuple[str, str, str, datetime]] = []
        for row in incoming["rollup"]:
            key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
            if key not in self._persisted_window_keys:
                if len(self._pending_rollup_rebuilds) < self._rollup_window_budget:
                    self._pending_rollup_rebuilds[key] = None
                else:
                    self._mark_rollup_rebuild_needed(row, datetime.now(timezone.utc))
                    self._record_window_persistence_overflow(datetime.now(timezone.utc))
        persisted_keys.extend(self._persist_window_class(
            incoming["1m"], self._pending_window_rows, self._window_retry_budget
        ))
        persisted_keys.extend(self._persist_window_class(
            incoming["rollup"], self._pending_rollup_rows, self._rollup_retry_budget
        ))
        self._compact_state()
        return tuple(persisted_keys)

    def _persist_window_class(
        self,
        incoming: list[LiquidationWindow],
        pending: dict[tuple[str, str, str, datetime], LiquidationWindow],
        budget: int,
    ) -> list[tuple[str, str, str, datetime]]:
        """Persist one priority class without admitting unbounded retry state.

        1m source rows and derived rollups have independent retry budgets.  A
        full retry class is flushed before another row is admitted; when the
        repository is unavailable, the bounded class remains retryable and
        the overflow is visible as health degradation rather than a silent
        drop.  The worker only creates rollups after their source minutes are
        acknowledged, so a database outage cannot strand an untracked set of
        derived rows.
        """

        persisted: list[tuple[str, str, str, datetime]] = []
        now = datetime.now(timezone.utc)
        blocked = False
        if pending:
            ok, flushed = self._flush_pending_window_class(pending)
            persisted.extend(flushed)
            blocked = not ok

        for row in incoming:
            key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
            if key in self._persisted_window_keys or key in pending:
                continue
            if blocked:
                self._record_window_persistence_overflow(now)
                continue
            if len(pending) >= budget:
                ok, flushed = self._flush_pending_window_class(pending)
                persisted.extend(flushed)
                if not ok:
                    blocked = True
                    self._record_window_persistence_overflow(now)
                    continue
            pending[key] = row

        if pending and not blocked:
            ok, flushed = self._flush_pending_window_class(pending)
            persisted.extend(flushed)
            if not ok:
                blocked = True
        if blocked and incoming:
            self.health.mark_backpressure("liquidation", now, dropped=0)
        return persisted

    def _flush_pending_window_class(
        self, pending: dict[tuple[str, str, str, datetime], LiquidationWindow]
    ) -> tuple[bool, list[tuple[str, str, str, datetime]]]:
        if not pending:
            return True, []
        batch = tuple(pending.values())[:256]
        try:
            with self._repository_scope() as repository:
                if repository is None:
                    raise RuntimeError("Phase 4 repository is unavailable")
                repository.insert_liquidation_windows(batch)
        except Exception:
            self.health.mark_db_outage("liquidation", datetime.now(timezone.utc))
            return False, []
        now = datetime.now(timezone.utc)
        persisted: list[tuple[str, str, str, datetime]] = []
        for row in batch:
            key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
            pending.pop(key, None)
            if row.timeframe != "1m":
                self._pending_rollup_rebuilds.pop(key, None)
            self._remember_window_key(key, now)
            persisted.append(key)
            if row.timeframe == "1m":
                builder = self._builders.get((row.exchange, row.canonical_symbol))
                if builder is not None:
                    builder.acknowledge((row,))
        return True, persisted

    def _record_window_persistence_overflow(self, now: datetime) -> None:
        self._window_persistence_overflow_count += 1
        self.health.mark_backpressure("liquidation", now, dropped=0)

    def _mark_rollup_rebuild_needed(self, row: LiquidationWindow, now: datetime) -> None:
        """Account for an overflow without retaining every rollup identity.

        The marker range is sufficient to enumerate the affected rollup
        identities from durable 1m rows.  Per-scope markers are bounded by the
        configured exchange/universe shape; an additional single global marker
        covers adversarial or externally supplied rows without unbounded
        memory growth.
        """

        scope = (row.exchange, row.canonical_symbol, row.timeframe)
        marker = self._rollup_rebuild_needed.get(scope)
        pending_opens = tuple(
            key[3] for key in self._pending_rollup_rebuilds
            if key[:3] == scope
        )
        earliest = min((row.window_open, *pending_opens))
        if marker is None and len(self._rollup_rebuild_needed) >= self._rollup_rebuild_marker_budget:
            marker = self._rollup_rebuild_all
            self._rollup_rebuild_all = (
                _GlobalRollupRebuildMarker.from_range(_RollupRebuildMarker(earliest, row.window_open))
                if marker is None else marker.extend(_RollupRebuildMarker(earliest, row.window_open))
            )
            self._persist_rollup_rebuild_marker(("*", "*", "*"), self._rollup_rebuild_all, now)
            self._rollup_rebuild_overflow_count += 1
            return
        marker = _RollupRebuildMarker(earliest, row.window_open) if marker is None else marker.extend(earliest).extend(row.window_open)
        self._rollup_rebuild_needed[scope] = marker
        self._persist_rollup_rebuild_marker(scope, marker, now)
        self._rollup_rebuild_overflow_count += 1

    def _persist_rollup_rebuild_marker(
        self,
        scope: tuple[str, str, str],
        marker: _RollupRebuildMarker | _GlobalRollupRebuildMarker,
        now: datetime,
    ) -> bool:
        try:
            with self._repository_scope() as repository:
                if repository is None:
                    raise RuntimeError("Phase 4 repository is unavailable")
                method = getattr(repository, "upsert_liquidation_rollup_rebuild_marker", None)
                if method is not None:
                    if isinstance(marker, _GlobalRollupRebuildMarker):
                        try:
                            method(
                                *scope,
                                marker.earliest_window_open,
                                marker.latest_window_open,
                                now,
                                cursor_exchange=marker.cursor_exchange,
                                cursor_symbol=marker.cursor_symbol,
                                cursor_timeframe=marker.cursor_timeframe,
                                rebuild_earliest_window_open=marker.rebuild_earliest_window_open,
                                rebuild_latest_window_open=marker.rebuild_latest_window_open,
                            )
                        except TypeError:
                            # Keep compatibility with test/during-upgrade repositories;
                            # the active process still retains the full cursor state.
                            method(*scope, marker.earliest_window_open, marker.latest_window_open, now)
                    else:
                        method(*scope, marker.earliest_window_open, marker.latest_window_open, now)
                elif hasattr(repository, "upsert_system_health"):
                    details = {
                        "kind": "PHASE4_ROLLUP_REBUILD",
                        "active": True,
                        "exchange": scope[0],
                        "canonical_symbol": scope[1],
                        "timeframe": scope[2],
                        "earliest_window_open": marker.earliest_window_open.isoformat(),
                        "latest_window_open": marker.latest_window_open.isoformat(),
                    }
                    if isinstance(marker, _GlobalRollupRebuildMarker):
                        details.update({
                            "cursor_exchange": marker.cursor_exchange,
                            "cursor_symbol": marker.cursor_symbol,
                            "cursor_timeframe": marker.cursor_timeframe,
                            "rebuild_earliest_window_open": marker.rebuild_earliest_window_open.isoformat(),
                            "rebuild_latest_window_open": marker.rebuild_latest_window_open.isoformat(),
                        })
                    repository.upsert_system_health(
                        f"phase4-rollup-rebuild:{scope[0]}:{scope[1]}:{scope[2]}",
                        DataStatus.STALE,
                        now,
                        details,
                    )
                else:
                    return False
        except Exception:
            self.health.mark_db_outage("liquidation", now)
            return False
        return True

    def _clear_rollup_rebuild_marker(self, scope: tuple[str, str, str], now: datetime) -> bool:
        try:
            with self._repository_scope() as repository:
                if repository is None:
                    return False
                method = getattr(repository, "clear_liquidation_rollup_rebuild_marker", None)
                if method is not None:
                    method(*scope, now)
                elif hasattr(repository, "upsert_system_health"):
                    repository.upsert_system_health(
                        f"phase4-rollup-rebuild:{scope[0]}:{scope[1]}:{scope[2]}",
                        DataStatus.AVAILABLE,
                        now,
                        {"kind": "PHASE4_ROLLUP_REBUILD", "active": False},
                    )
                else:
                    return True
        except Exception:
            self.health.mark_db_outage("liquidation", now)
            return False
        return True

    def _persist_hydrated_rollups(
        self, windows: Iterable[LiquidationWindow]
    ) -> tuple[tuple[str, str, str, datetime], ...]:
        """Upsert restart rollups in bounded batches without retry-queue admission.

        The in-memory key index is only a write-reduction optimization.  The
        repository's durable unique identity and upsert are the correctness
        boundary, so cache eviction may cause a harmless upsert but must not
        turn a hydrated restart set into bounded pending-row backpressure.
        """

        candidates = []
        seen: set[tuple[str, str, str, datetime]] = set()
        for row in windows:
            key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
            if key in seen or key in self._persisted_window_keys or key in self._pending_window_rows or key in self._pending_rollup_rows:
                continue
            seen.add(key)
            candidates.append(row)
        if not candidates:
            return ()

        persisted_keys: list[tuple[str, str, str, datetime]] = []
        now = datetime.now(timezone.utc)
        for start in range(0, len(candidates), 256):
            batch = tuple(candidates[start : start + 256])
            try:
                with self._repository_scope() as repository:
                    if repository is None:
                        raise RuntimeError("Phase 4 repository is unavailable")
                    repository.insert_liquidation_windows(batch)
            except Exception:
                self.health.mark_db_outage("liquidation", now)
                # The normalized 1m source rows remain durable and will
                # deterministically rebuild this batch on the next startup;
                # do not manufacture a queue-capacity-sized retry storm here.
                return tuple(persisted_keys)
            for row in batch:
                key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
                self._remember_window_key(key, now)
                persisted_keys.append(key)
        self._compact_state()
        return tuple(persisted_keys)

    def _retry_window_persistence(self, now: datetime) -> None:
        persisted = self._persist_windows(())
        persisted_minutes = {
            key for key in persisted if key[2] == "1m"
        }
        if persisted_minutes:
            rows = tuple(
                row for row in self.windows
                if (row.exchange, row.canonical_symbol, row.timeframe, row.window_open) in persisted_minutes
            )
            if rows:
                self._persist_windows(self._rollups_for(rows, now))
        if self._pending_rollup_rebuilds:
            rebuilds = self._pending_rollup_rebuilds.keys()
            rows_by_key = {
                (row.exchange, row.canonical_symbol, row.timeframe, row.window_open): row
                for row in self._rollups_for(self.windows, now)
            }
            self._persist_windows(
                rows_by_key[key] for key in tuple(rebuilds) if key in rows_by_key
            )
        self._retry_rollup_rebuilds(now)

    def _load_rollup_source_range(
        self,
        exchange: str,
        canonical_symbol: str,
        start: datetime,
        end: datetime,
    ) -> tuple[LiquidationWindow, ...] | None:
        """Read one bounded durable 1m range, with an in-memory test fallback."""

        try:
            with self._repository_scope() as repository:
                if repository is not None and hasattr(repository, "load_liquidation_windows_range"):
                    return tuple(repository.load_liquidation_windows_range(
                        exchange,
                        canonical_symbol,
                        window_open_from=start,
                        window_open_to=end,
                        limit=256,
                    ))
                if repository is not None and hasattr(repository, "load_recent_liquidation_windows"):
                    return tuple(
                        row for row in repository.load_recent_liquidation_windows(
                            per_key_limit=self._hydration_windows_per_key,
                            max_rows=self._rollup_window_budget,
                        )
                        if row.exchange == exchange
                        and row.canonical_symbol == canonical_symbol
                        and row.timeframe == "1m"
                        and start <= row.window_open < end
                    )
        except Exception:
            self.health.mark_db_outage("liquidation", datetime.now(timezone.utc))
            return None
        return tuple(
            row for row in self.windows
            if row.exchange == exchange
            and row.canonical_symbol == canonical_symbol
            and row.timeframe == "1m"
            and start <= row.window_open < end
        )

    def _retry_one_rollup_marker(
        self,
        scope: tuple[str, str, str],
        marker: _RollupRebuildMarker,
        now: datetime,
        *,
        max_groups: int,
        retain_marker: bool = True,
    ) -> _RollupRebuildMarker | None:
        exchange, canonical_symbol, timeframe = scope
        seconds = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}.get(timeframe)
        if seconds is None:
            return None
        current = datetime.fromtimestamp(
            int(marker.earliest_window_open.timestamp())
            - int(marker.earliest_window_open.timestamp()) % seconds,
            tz=timezone.utc,
        )
        latest = datetime.fromtimestamp(
            int(marker.latest_window_open.timestamp())
            - int(marker.latest_window_open.timestamp()) % seconds,
            tz=timezone.utc,
        )
        processed = 0
        while current <= latest and processed < max_groups:
            key = (exchange, canonical_symbol, timeframe, current)
            if key in self._pending_rollup_rows:
                return marker
            rows = self._load_rollup_source_range(
                exchange,
                canonical_symbol,
                current,
                current + timedelta(seconds=seconds),
            )
            if rows is None:
                return marker
            generated = tuple(
                row for row in rollup_liquidation_windows(rows, timeframe)
                if row.window_open == current
            )
            if generated:
                persisted = self._persist_hydrated_rollups(generated)
                if key not in persisted and key not in self._persisted_window_keys:
                    return marker
            current += timedelta(seconds=seconds)
            processed += 1
        if current <= latest:
            updated = _RollupRebuildMarker(current, latest)
            if retain_marker:
                self._rollup_rebuild_needed[scope] = updated
                self._persist_rollup_rebuild_marker(scope, updated, now)
            return updated
        if not retain_marker or self._clear_rollup_rebuild_marker(scope, now):
            return None
        return marker

    def _iter_rollup_rebuild_scopes(self) -> Iterable[tuple[str, str]]:
        """Stream durable source scopes without retaining the scope universe."""

        try:
            with self._repository_scope() as repository:
                if repository is not None and hasattr(repository, "iter_liquidation_window_scopes"):
                    iterator = repository.iter_liquidation_window_scopes(page_size=128)
                    for exchange, canonical_symbol in iterator:
                        yield str(exchange), str(canonical_symbol)
                    return
                if repository is not None and hasattr(repository, "load_liquidation_window_scopes"):
                    scopes = repository.load_liquidation_window_scopes()
                    for exchange, canonical_symbol in scopes:
                        yield str(exchange), str(canonical_symbol)
                    return
                if repository is not None and hasattr(repository, "load_recent_liquidation_windows"):
                    seen: set[tuple[str, str]] = set()
                    rows = repository.load_recent_liquidation_windows(
                        per_key_limit=self._hydration_windows_per_key,
                        max_rows=self._rollup_window_budget,
                    )
                    for row in rows:
                        if row.timeframe != "1m":
                            continue
                        scope = (row.exchange, row.canonical_symbol)
                        if scope not in seen:
                            seen.add(scope)
                            yield scope
                    return
        except Exception:
            self.health.mark_db_outage("liquidation", datetime.now(timezone.utc))
            self._rollup_scope_iteration_failed = True
            return

        seen: set[tuple[str, str]] = set()
        for row in self.windows:
            if row.timeframe != "1m":
                continue
            scope = (row.exchange, row.canonical_symbol)
            if scope not in seen:
                seen.add(scope)
                yield scope

    def _global_rebuild_target(
        self,
        marker: _GlobalRollupRebuildMarker,
        *,
        after_current: bool = False,
    ) -> tuple[tuple[str, str], str] | None:
        timeframes = ("5m", "15m", "1H", "4H")
        cursor_scope = None
        cursor_timeframe = None
        if marker.cursor_exchange is not None and marker.cursor_symbol is not None:
            cursor_scope = (marker.cursor_exchange, marker.cursor_symbol)
            cursor_timeframe = marker.cursor_timeframe
        for scope in self._iter_rollup_rebuild_scopes():
            if cursor_scope is None:
                return scope, timeframes[0]
            if scope < cursor_scope:
                continue
            if scope == cursor_scope:
                if cursor_timeframe in timeframes:
                    index = timeframes.index(cursor_timeframe)
                    if not after_current:
                        return scope, cursor_timeframe
                    if index + 1 < len(timeframes):
                        return scope, timeframes[index + 1]
                    continue
                if not after_current:
                    return scope, timeframes[0]
                continue
            return scope, timeframes[0]
        return None

    def _retry_rollup_rebuilds(self, now: datetime) -> None:
        """Recompute overflowed rollups from durable 1m windows in bounded steps."""

        max_groups = max(1, self._rollup_retry_budget)
        for scope, marker in tuple(sorted(self._rollup_rebuild_needed.items())):
            updated = self._retry_one_rollup_marker(scope, marker, now, max_groups=max_groups)
            if updated is None:
                self._rollup_rebuild_needed.pop(scope, None)

        # A single global marker is the bounded escape hatch when the scope
        # marker table itself is full. Durable source scopes are streamed one
        # page at a time; the marker retains the active scope/timeframe and
        # the helper's advanced range so retries cannot restart at chunk zero.
        if self._rollup_rebuild_all is not None:
            marker = self._rollup_rebuild_all
            self._rollup_scope_iteration_failed = False
            target = self._global_rebuild_target(marker)
            if target is None:
                if self._rollup_scope_iteration_failed:
                    return
                if self._clear_rollup_rebuild_marker(("*", "*", "*"), now):
                    self._rollup_rebuild_all = None
                return
            (exchange, canonical_symbol), timeframe = target
            active = marker if (
                marker.cursor_exchange,
                marker.cursor_symbol,
                marker.cursor_timeframe,
            ) == (exchange, canonical_symbol, timeframe) else marker.at_cursor(
                exchange, canonical_symbol, timeframe
            )
            updated = self._retry_one_rollup_marker(
                (exchange, canonical_symbol, timeframe),
                _RollupRebuildMarker(active.earliest_window_open, active.latest_window_open),
                now,
                max_groups=max_groups,
                retain_marker=False,
            )
            if updated is not None:
                self._rollup_rebuild_all = active.with_progress(updated)
                self._persist_rollup_rebuild_marker(("*", "*", "*"), self._rollup_rebuild_all, now)
                return
            next_target = self._global_rebuild_target(active, after_current=True)
            if next_target is None:
                if self._clear_rollup_rebuild_marker(("*", "*", "*"), now):
                    self._rollup_rebuild_all = None
                return
            (next_exchange, next_symbol), next_timeframe = next_target
            self._rollup_rebuild_all = marker.at_cursor(
                next_exchange, next_symbol, next_timeframe
            )
            self._persist_rollup_rebuild_marker(("*", "*", "*"), self._rollup_rebuild_all, now)

    def _retry_event_recovery(self, now: datetime) -> None:
        for key, event in tuple(self._pending_event_recovery.items())[:256]:
            try:
                self._persist_event(event)
            except Exception:
                self.health.mark_db_outage("liquidation", now)
                continue
            queue = self._queue_for(event.exchange, event.canonical_symbol, now)
            if queue is None:
                self.health.mark_backpressure("liquidation", now, dropped=0)
                continue
            self._release_recovery_event(key, event)
            if not self._queue_event(queue, event, now):
                self.health.mark_backpressure("liquidation", now, dropped=0)
                self._retain_event_for_retry(event)
                continue
            self._maybe_recover_liquidation(event.exchange, event, now)

    def _maybe_recover_liquidation(self, exchange: str, event: CanonicalLiquidation, now: datetime) -> None:
        if exchange not in self._liquidation_gap_pending:
            return
        watermark = self._liquidation_gap_watermarks.get(exchange)
        if watermark is None or not _LiquidationGapWatermark.from_event(event).is_newer(watermark):
            return
        self._liquidation_gap_pending.remove(exchange)
        self.health.mark_recovered("liquidation", now)

    def _rollups_for(self, completed: Iterable[LiquidationWindow], now: datetime) -> tuple[LiquidationWindow, ...]:
        seconds_by_timeframe = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}
        touched: dict[str, set[tuple[str, str, datetime]]] = {timeframe: set() for timeframe in seconds_by_timeframe}
        for row in completed:
            for timeframe, seconds in seconds_by_timeframe.items():
                epoch = int(row.window_open.timestamp())
                opened = datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)
                touched[timeframe].add((row.exchange, row.canonical_symbol, opened))
        rows_by_rollup: dict[str, dict[tuple[str, str, datetime], list[LiquidationWindow]]] = {
            timeframe: {} for timeframe in seconds_by_timeframe
        }
        # Index each hydrated minute once per required timeframe.  The prior
        # implementation scanned all hydrated minutes for every touched key,
        # which made a dual-exchange 200-symbol restart quadratic in the
        # number of durable windows.
        for row in self.windows:
            if row.timeframe != "1m":
                continue
            epoch = int(row.window_open.timestamp())
            for timeframe, seconds in seconds_by_timeframe.items():
                opened = datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)
                key = (row.exchange, row.canonical_symbol, opened)
                if key not in touched[timeframe] or opened + timedelta(seconds=seconds) > now:
                    continue
                rows_by_rollup[timeframe].setdefault(key, []).append(row)

        rollups: list[LiquidationWindow] = []
        for timeframe in seconds_by_timeframe:
            for key in sorted(rows_by_rollup[timeframe]):
                generated = rollup_liquidation_windows(rows_by_rollup[timeframe][key], timeframe)
                for row in generated:
                    if row.status is DataStatus.ERROR:
                        self.health.mark_error(
                            "liquidation",
                            now,
                            reason=row.reason or "LIQUIDATION_ROLLUP_ERROR",
                        )
                rollups.extend(generated)
        return tuple(rollups)

    def _builder_window_count(self) -> int:
        return sum(builder.active_count for builder in self._builders.values())

    def _state_count(self) -> int:
        return (
            self._queued_event_count
            + self._builder_window_count()
            + len(self.windows)
            + len(self._pending_window_rows)
            + len(self._pending_rollup_rows)
            + len(self._pending_rollup_rebuilds)
            + len(self._rollup_rebuild_needed)
            + (1 if self._rollup_rebuild_all is not None else 0)
            + len(self._persisted_window_keys)
        )

    def _state_bytes(self) -> int:
        return (
            self._queued_event_bytes
            + sum(builder.state_bytes for builder in self._builders.values())
            + sum(_estimated_window_bytes(row) for row in self.windows)
            + sum(_estimated_window_bytes(row) for row in self._pending_window_rows.values())
            + sum(_estimated_window_bytes(row) for row in self._pending_rollup_rows.values())
            + len(self._pending_rollup_rebuilds) * 96
            + len(self._rollup_rebuild_needed) * 128
            + (128 if self._rollup_rebuild_all is not None else 0)
            + len(self._persisted_window_keys) * 96
        )

    def _compact_state(self) -> None:
        if len(self.windows) > self._rollup_window_budget:
            self.windows = self.windows[-self._rollup_window_budget :]
        while len(self._persisted_window_keys) > self._finalized_window_budget:
            self._persisted_window_keys.pop(next(iter(self._persisted_window_keys)))
        remaining = self._finalized_window_budget
        for builder in self._builders.values():
            builder.compact_finalized(max(0, remaining))
            remaining = max(0, remaining - builder.state_count)
        current_bytes = self._state_bytes()
        if current_bytes > self._event_bytes_budget and self.windows:
            window_bytes = sum(_estimated_window_bytes(row) for row in self.windows)
            non_window_bytes = current_bytes - window_bytes
            available_window_bytes = self._event_bytes_budget - non_window_bytes
            if available_window_bytes <= 0:
                self.windows = ()
            else:
                retained_bytes = 0
                first_retained = len(self.windows)
                for index in range(len(self.windows) - 1, -1, -1):
                    row_bytes = _estimated_window_bytes(self.windows[index])
                    if retained_bytes + row_bytes > available_window_bytes:
                        break
                    retained_bytes += row_bytes
                    first_retained = index
                self.windows = self.windows[first_retained:]

    def _hydrate_persisted_windows(self, now: datetime) -> None:
        if not self._persistence_configured:
            return
        try:
            with self._repository_scope() as repository:
                if repository is None or not hasattr(repository, "load_recent_liquidation_windows"):
                    return
                rows = tuple(repository.load_recent_liquidation_windows(
                    per_key_limit=self._hydration_windows_per_key,
                    max_rows=self._rollup_window_budget,
                ))
                if hasattr(repository, "load_recent_liquidation_rollup_keys"):
                    for exchange, symbol, timeframe, window_open in repository.load_recent_liquidation_rollup_keys(
                        max_rows=self._rollup_window_budget,
                    ):
                        self._remember_window_key((exchange, symbol, timeframe, _utc(window_open)), now)
                if hasattr(repository, "load_liquidation_rollup_rebuild_markers"):
                    for marker_row in repository.load_liquidation_rollup_rebuild_markers():
                        exchange, symbol, timeframe, earliest, latest, *cursor_fields = marker_row
                        marker = _RollupRebuildMarker(_utc(earliest), _utc(latest))
                        if (exchange, symbol, timeframe) == ("*", "*", "*"):
                            cursor_exchange = cursor_fields[0] if len(cursor_fields) > 0 else None
                            cursor_symbol = cursor_fields[1] if len(cursor_fields) > 1 else None
                            cursor_timeframe = cursor_fields[2] if len(cursor_fields) > 2 else None
                            rebuild_earliest = (
                                _utc(cursor_fields[3])
                                if len(cursor_fields) > 3 and cursor_fields[3] is not None
                                else marker.earliest_window_open
                            )
                            rebuild_latest = (
                                _utc(cursor_fields[4])
                                if len(cursor_fields) > 4 and cursor_fields[4] is not None
                                else marker.latest_window_open
                            )
                            self._rollup_rebuild_all = _GlobalRollupRebuildMarker(
                                marker.earliest_window_open,
                                marker.latest_window_open,
                                rebuild_earliest,
                                rebuild_latest,
                                cursor_exchange,
                                cursor_symbol,
                                cursor_timeframe,
                            )
                        elif len(self._rollup_rebuild_needed) < self._rollup_rebuild_marker_budget:
                            self._rollup_rebuild_needed[(exchange, symbol, timeframe)] = marker
                        else:
                            self._rollup_rebuild_all = (
                                _GlobalRollupRebuildMarker.from_range(marker)
                                if self._rollup_rebuild_all is None
                                else self._rollup_rebuild_all.extend(marker)
                            )
        except Exception:
            self.health.mark_db_outage("liquidation", now)
            return
        for row in rows:
            key = (row.exchange, row.canonical_symbol)
            queue = self._queue_for(row.exchange, row.canonical_symbol, now)
            if queue is None:
                continue
            self._builders[key].hydrate((row,))
            self._remember_window_key((row.exchange, row.canonical_symbol, "1m", row.window_open), now)
            stream = (row.exchange, row.canonical_symbol)
            high_watermark = self._hydrated_window_high_watermarks.get(stream)
            if high_watermark is None or row.window_open > high_watermark:
                self._hydrated_window_high_watermarks[stream] = row.window_open
        self.windows = tuple(rows[-self._rollup_window_budget :])
        # A restart has only bounded normalized 1m rows available. Rebuild
        # closed rollups from those rows and persist only missing timeframe
        # identities; the 1m rows themselves are already durable.
        rollups = self._rollups_for(self.windows, now)
        if rollups:
            self._persist_windows(rollups, startup_hydration=True)
            self._compact_state()
        self._retry_rollup_rebuilds(now)

    @contextmanager
    def _repository_scope(self):
        if self.repository_provider is not None:
            with self.repository_provider() as repository:
                yield repository
            return
        yield self.repository

    def _remember_window_key(self, key: tuple[str, str, str, datetime], now: datetime) -> bool:
        expiry = now - timedelta(seconds=300)
        for old_key, seen_at in list(self._persisted_window_keys.items()):
            if seen_at <= expiry:
                del self._persisted_window_keys[old_key]
        if key in self._persisted_window_keys:
            seen_at = self._persisted_window_keys.pop(key)
            self._persisted_window_keys[key] = seen_at
            return False
        self._persisted_window_keys[key] = now
        # This is only a short duplicate-suppression index; durable restart
        # continuity lives in the bounded builder/window hydration state.
        key_budget = min(self._finalized_window_budget, max(1, self.settings.phase4_queue_capacity))
        while len(self._persisted_window_keys) > key_budget:
            self._persisted_window_keys.pop(next(iter(self._persisted_window_keys)))
        return True

    def _reject_hydrated_closed_minute(self, event: CanonicalLiquidation) -> bool:
        """Reject late minutes already covered by durable hydration.

        The finalized-key cache is intentionally compacted.  Its eviction
        must not make a durable closed minute look like a new aggregate.  The
        per-stream hydrated high-watermark is authoritative even while the
        corresponding finalized key remains in builder memory.  New minutes
        strictly above it continue through normal aggregation.
        """

        stream = (event.exchange, event.canonical_symbol)
        high_watermark = self._hydrated_window_high_watermarks.get(stream)
        return high_watermark is not None and _minute_open(event.event_timestamp) <= high_watermark

    async def _rest_scheduler(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.settings.phase4_rest_cycle_seconds
                )
            except asyncio.TimeoutError:
                if self.symbols_provider is not None:
                    symbols = _bounded_symbols(self.symbols_provider(), getattr(self.settings, "universe_limit", 200))
                    if symbols:
                        await self.run_rest_cycle(symbols, datetime.now(timezone.utc))
                self._cleanup()
