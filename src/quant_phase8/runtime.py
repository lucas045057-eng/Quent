"""Collector-owned, opt-in Phase 8 public options data lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
from typing import Any, Callable, Mapping, Sequence

from .adapters.deribit_rest import (
    DeribitPublicRestClient,
    parse_book_summary_response,
    parse_index_price_names_response,
    parse_instruments_response,
)
from .adapters.deribit_ws import DeribitPublicWebSocketClient
from .config import Phase8Settings
from .context import OptionsContextInputs, calculate_options_context
from .contracts import (
    DataStatus,
    ObservationKind,
    OptionInstrument,
    OptionInstrumentEvent,
    OptionMarketObservation,
    OptionMetricValue,
)
from .freshness import evaluate_catalog_freshness
from .universe import select_ticker_universe, validate_full_chain_snapshot
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.backpressure import decide_overload
from quant_data_layer.observability import DataState, ProcessRole, SourceId, SourcePhase, WorkClass


LOGGER = logging.getLogger("quant_phase8")
_CURRENCIES = ("BTC", "ETH")
_HEALTH_COMPONENT = "quant-phase8-options"
_WORKER_NAMES = frozenset({"instruments", "chain", "markprice", "ticker", "context", "health", "retention"})
_PERIODIC_STREAM_IDS = {
    "instruments": "phase8.option_instruments",
    "lifecycle": "phase8.option_lifecycle",
    "chain": "phase8.chain_summary",
    "markprice": "phase8.markprice_latest",
    "ticker": "phase8.bounded_ticker_state",
    "context": "phase8.options_context",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _PostgresPersistence:
    """Small synchronous persistence facade; each bounded operation owns a DB connection."""

    def __init__(self, settings: Any, phase8_settings: Phase8Settings) -> None:
        self.dsn = settings.postgres_dsn
        self.phase8_settings = phase8_settings

    def _connect(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def initialize(self) -> None:
        from quant_phase1.db import assert_schema_ready

        with self._connect() as connection:
            assert_schema_ready(connection, required_version="015_phase8_options_context.sql")

    def _phase8(self, name: str, *args: Any, **kwargs: Any) -> Any:
        from .persistence import Phase8Repository

        with self._connect() as connection:
            repository = Phase8Repository(connection, settings=self.phase8_settings)
            return getattr(repository, name)(*args, **kwargs)

    def upsert_instruments(self, instruments: Sequence[OptionInstrument]) -> int:
        return self._phase8("upsert_instruments", instruments)

    def retire_missing_instruments(self, **kwargs: Any) -> int:
        return self._phase8("retire_missing_instruments", **kwargs)

    def append_instrument_events(self, events: Sequence[OptionInstrumentEvent]) -> int:
        return self._phase8("append_instrument_events", events)

    def persist_observations(self, observations: Sequence[OptionMarketObservation]) -> int:
        return self._phase8("persist_observations", observations)

    def persist_full_chain_cycle(
        self, observations: Sequence[OptionMarketObservation], *, max_records: int
    ) -> int:
        return self._phase8("persist_full_chain_cycle", observations, max_records=max_records)

    def persist_context(self, snapshot: Any) -> bool:
        return self._phase8("persist_context", snapshot)

    def cleanup_retention(self, *, now: datetime) -> dict[str, int]:
        return self._phase8(
            "cleanup_retention", self.phase8_settings, now=now, max_batches=1
        )

    def set_health(self, status: str, details: Mapping[str, Any], checked_at: datetime) -> None:
        from quant_phase1.contracts import DataStatus as CoreDataStatus
        from quant_phase1.repositories import Phase1Repository

        # system_health uses the Phase 1 DataStatus contract, which has no
        # PARTIAL value. Keep the richer Phase 8 status in details and persist
        # transitional/degraded states as NOT_AVAILABLE instead of failing the
        # health write before it reaches PostgreSQL.
        mapped = {
            "ERROR": CoreDataStatus.ERROR,
            "STALE": CoreDataStatus.STALE,
            "PARTIAL": CoreDataStatus.NOT_AVAILABLE,
            "DEGRADED": CoreDataStatus.NOT_AVAILABLE,
            "INITIALIZING": CoreDataStatus.NOT_AVAILABLE,
            "STOPPING": CoreDataStatus.NOT_AVAILABLE,
            "STOPPED": CoreDataStatus.NOT_AVAILABLE,
            "AVAILABLE": CoreDataStatus.AVAILABLE,
        }.get(status, CoreDataStatus.NOT_AVAILABLE)
        with self._connect() as connection:
            Phase1Repository(connection).upsert_system_health(
                _HEALTH_COMPONENT, mapped, checked_at, dict(details)
            )


class Phase8CollectorRuntime:
    """Bounded public Deribit options ingestion and context lifecycle.

    The runtime is deliberately opt-in and has no engine, Stage1, risk, order,
    or private-API dependencies. Adapter state owns sparse WS merges; this
    runtime periodically snapshots the already-merged canonical state.
    """

    def __init__(
        self,
        settings: Any,
        *,
        phase8_settings: Phase8Settings | None = None,
        rest_factory: Callable[..., Any] = DeribitPublicRestClient,
        websocket_factory: Callable[..., Any] = DeribitPublicWebSocketClient,
        persistence_factory: Callable[..., Any] = _PostgresPersistence,
        clock: Callable[[], datetime] = _utc_now,
        admission: WorkAdmissionController | None = None,
    ) -> None:
        self.settings = settings
        self.admission = admission or WorkAdmissionController(role=ProcessRole.COLLECTOR)
        if self.admission.role is not ProcessRole.COLLECTOR:
            raise ValueError("Phase 8 collector runtime requires Collector-owned admission")
        self.phase8_settings = phase8_settings or Phase8Settings.from_env()
        self._rest_factory = rest_factory
        self._websocket_factory = websocket_factory
        self._persistence_factory = persistence_factory
        self._clock = clock
        self.state = "NOT_STARTED" if self.phase8_settings.enabled else "DISABLED"
        self.reason: str | None = None
        self.started = asyncio.Event()
        self.catalog: dict[str, OptionInstrument] = {}
        self.supported_index_names: frozenset[str] = frozenset()
        self.ticker_symbols: tuple[str, ...] = ()
        self.universe_status = DataStatus.NOT_AVAILABLE
        self._summary_observations: dict[str, tuple[OptionMarketObservation, ...]] = {}
        self._underlying_prices: dict[str, OptionMetricValue] = {}
        self._catalog_fetched_at: datetime | None = None
        self._summary_fetched_at: dict[str, datetime] = {}
        self._lifecycle_received_at: datetime | None = None
        self._persistence: Any | None = None
        self._rest: Any | None = None
        self._websocket: Any | None = None
        self._stop_event: asyncio.Event | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._catalog_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self._initial_ready = False
        self._reconcile_task: asyncio.Task[None] | None = None
        self._skipped_cycles: dict[str, int] = {}
        self._last_skipped_cycle: str | None = None
        self._last_skipped_cycle_reason: str | None = None
        self._runtime_stage = "NOT_STARTED"
        self._worker_health: dict[str, dict[str, Any]] = {}
        self._last_context_timestamp: datetime | None = None
        self._last_context_persisted_at: datetime | None = None
        self._last_failure_type: str | None = None
        self._ws_connection_status = "NOT_STARTED"
        self._lifecycle_status = "NOT_AVAILABLE"
        self._markprice_status = "NOT_AVAILABLE"
        self._ticker_status = "NOT_AVAILABLE"
        self._context_status = "NOT_AVAILABLE"
        self._last_ws_success: datetime | None = None
        self._last_rest_success: datetime | None = None
        self._last_error_category: str | None = None

    @property
    def owned_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        tasks = {task for task in self._tasks if not task.done()}
        if self._reconcile_task is not None and not self._reconcile_task.done():
            tasks.add(self._reconcile_task)
        if self._websocket is not None:
            tasks.update(getattr(self._websocket, "owned_tasks", ()))
        return tuple(tasks)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.phase8_settings.enabled:
            self.state = "DISABLED"
            self._runtime_stage = "DISABLED"
            self.started.set()
            return
        self._stop_event = stop_event
        self.state = "INITIALIZING"
        try:
            self._persistence = self._persistence_factory(self.settings, self.phase8_settings)
            await self._db("initialize")
            self._runtime_stage = "PERSISTENCE_READY"
            self.reason = "STARTUP_IN_PROGRESS"
            await self._write_health()
            async with self._rest_factory(self.phase8_settings) as rest:
                self._rest = rest
                self._runtime_stage = "REST_READY"
                await self._refresh_catalog()
                self._runtime_stage = "CATALOG_RECONCILED"
                try:
                    await self._refresh_summaries()
                except AdmissionDeferred as exc:
                    self._record_admission_skip("chain", exc)
                self._runtime_stage = "CHAIN_SUMMARY_RECONCILED"
                self._refresh_universe()
                self._websocket = self._websocket_factory(
                    self.phase8_settings,
                    instrument_catalog=(),
                    supported_index_names=set(),
                    ticker_symbols=(),
                    on_invalidate=self._on_invalidate,
                    on_lifecycle=self._on_lifecycle,
                    on_markprice_batch=self._on_markprice_batch,
                    on_ticker=self._on_ticker,
                    defer_market_subscriptions=True,
                )
                await self._configure_tickers()
                self._runtime_stage = "BOUNDED_TICKERS_READY"
                self._initial_ready = True
                self._start_workers()
                self._ws_connection_status = "CONNECTING"
                supervisor = asyncio.create_task(
                    self._run_websocket_supervisor(), name="phase8-websocket-supervisor"
                )
                self._tasks.add(supervisor)
                supervisor.add_done_callback(self._tasks.discard)
                try:
                    await self._persist_ticker_snapshot()
                except AdmissionDeferred as exc:
                    self._record_admission_skip("ticker", exc)
                try:
                    await self._persist_contexts()
                except AdmissionDeferred as exc:
                    self._record_admission_skip("context", exc)
                self._refresh_health_state()
                self._runtime_stage = "RUNNING"
                await self._write_health()
                self.started.set()
                try:
                    await stop_event.wait()
                finally:
                    self.state = "STOPPING"
                    self._runtime_stage = "STOPPING"
                    await self._write_health()
                    await self._stop_workers()
                    await self._close_websocket()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = "ERROR"
            self._runtime_stage = "STARTUP_FAILED"
            self._last_failure_type = type(exc).__name__
            self.reason = "STARTUP_FAILED"
            LOGGER.warning("phase8_runtime_startup_failed exception_type=%s", type(exc).__name__)
            await self._write_health()
            self.started.set()
        finally:
            terminal_error = self.state == "ERROR"
            self.state = "STOPPING"
            await self._stop_workers()
            await self._close_websocket()
            self._rest = None
            self._websocket = None
            self._tasks.clear()
            self._reconcile_task = None
            self.state = "ERROR" if terminal_error else "STOPPED"
            self._runtime_stage = "ERROR" if terminal_error else "STOPPED"
            await self._write_health()

    def _cancel_owned_tasks(self) -> None:
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()

    async def _stop_workers(self) -> None:
        self._cancel_owned_tasks()
        if self._reconcile_task is not None and not self._reconcile_task.done():
            self._reconcile_task.cancel()
        pending = [task for task in self._tasks if not task.done()]
        if self._reconcile_task is not None and not self._reconcile_task.done():
            pending.append(self._reconcile_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for worker in self._worker_health.values():
            worker["status"] = "STOPPED"

    async def _close_websocket(self) -> None:
        if self._websocket is None:
            return
        websocket, self._websocket = self._websocket, None
        try:
            await websocket.close()
        except Exception as exc:
            LOGGER.warning("phase8_websocket_close_failed exception_type=%s", type(exc).__name__)

    async def _db(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._persistence is None:
            return None
        async with self._db_lock:
            operation = getattr(self._persistence, method)
            return await asyncio.to_thread(operation, *args, **kwargs)

    def _readiness_timeout(self) -> float:
        return max(5.0, min(60.0, self.phase8_settings.shutdown_timeout_seconds * 2.0))

    async def _run_websocket_supervisor(self) -> None:
        websocket = self._websocket
        if websocket is None or self._stop_event is None:
            return
        try:
            await websocket.start()
            while not self._stop_event.is_set():
                if websocket.ready:
                    if self._ws_connection_status != "AVAILABLE":
                        await self._mark_websocket_ready()
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                    except TimeoutError:
                        continue
                    return
                try:
                    await websocket.wait_ready(timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not getattr(websocket, "owned_tasks", ()):
                        await self._record_websocket_failure(exc, terminal=True)
                        return
                else:
                    await self._mark_websocket_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                await self._record_websocket_failure(exc, terminal=True)

    async def _record_websocket_failure(self, exc: Exception, *, terminal: bool) -> None:
        self._ws_connection_status = "ERROR" if terminal else "DEGRADED"
        self._lifecycle_status = "NOT_AVAILABLE"
        self._markprice_status = "NOT_AVAILABLE"
        self._ticker_status = "NOT_AVAILABLE"
        self._last_error_category = self._websocket_error_category(exc)
        self.reason = "WEBSOCKET_UNAVAILABLE"
        self._refresh_health_state()
        await self._write_health()

    def _websocket_error_category(self, exc: Exception) -> str:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, OSError)):
            return "NETWORK"
        try:
            import aiohttp
        except ImportError:
            return "CONTRACT"
        if isinstance(exc, aiohttp.ClientError):
            return "NETWORK"
        last_error = str(getattr(self._websocket, "last_error", "")).lower()
        if any(marker in last_error for marker in ("timeout", "connection", "transport", "reset")):
            return "NETWORK"
        return "CONTRACT"

    async def _mark_websocket_ready(self) -> None:
        websocket = self._websocket
        if websocket is None or not websocket.ready:
            return
        already_ready = self._ws_connection_status == "AVAILABLE"
        self._ws_connection_status = "AVAILABLE"
        self._lifecycle_status = "AVAILABLE"
        self._markprice_status = "AVAILABLE" if websocket.ready_markprice_indexes else "NOT_AVAILABLE"
        expected_tickers = set(self.ticker_symbols)
        ready_tickers = set(websocket.ready_ticker_symbols)
        if expected_tickers and expected_tickers <= ready_tickers:
            self._ticker_status = "AVAILABLE"
        elif expected_tickers & ready_tickers:
            self._ticker_status = "PARTIAL"
        else:
            self._ticker_status = "NOT_AVAILABLE"
        self._last_ws_success = self._clock().astimezone(timezone.utc)
        self._last_error_category = None
        if not already_ready:
            try:
                await self._persist_contexts()
            except AdmissionDeferred as exc:
                self._record_admission_skip("context", exc)
        self._refresh_health_state()
        await self._write_health()

    async def _refresh_catalog(self) -> None:
        assert self._rest is not None
        index_response = await self._rest.call(
            "public/get_index_price_names", {"extended": False}
        )
        instrument_payloads: list[tuple[str, Any, datetime]] = []
        for currency in _CURRENCIES:
            payload = await self._rest.call(
                "public/get_instruments",
                {"currency": currency, "kind": "option", "expired": False},
            )
            instrument_payloads.append((currency, payload, self._clock().astimezone(timezone.utc)))
        now = self._clock().astimezone(timezone.utc)
        request = make_work_request(
            phase=SourcePhase.PHASE8,
            source_id=SourceId.PHASE8_OPTIONS_MARKET,
            work_class=WorkClass.HEAVY,
            estimated_items=self.phase8_settings.max_full_chain_records_total,
            estimated_bytes=self.phase8_settings.max_full_chain_records_total * 2_048,
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase8.option_instruments",
        )
        async with self.admission.admit(request):
            indexes = parse_index_price_names_response(index_response)
            instruments: list[OptionInstrument] = []
            for currency, payload, fetched_at in instrument_payloads:
                instruments.extend(parse_instruments_response(
                    payload,
                    requested_currency=currency,
                    supported_index_names=indexes,
                    fetched_at=fetched_at,
                    processed_at=now,
                    max_records=self.phase8_settings.max_full_chain_records_per_underlying,
                ))
            validation = validate_full_chain_snapshot(instruments, self.phase8_settings)
            if validation.status is not DataStatus.AVAILABLE or not validation.records:
                raise ValueError("full-chain instrument catalog failed bounded contract validation")
            async with self._catalog_lock:
                self.catalog = {item.symbol: item for item in validation.records}
                self.supported_index_names = indexes
                self._catalog_fetched_at = max(fetched for _, _, fetched in instrument_payloads)
            await self._db("upsert_instruments", validation.records)
            for currency in _CURRENCIES:
                symbols = tuple(item.symbol for item in validation.records if item.underlying == currency)
                if symbols:
                    await self._db(
                        "retire_missing_instruments",
                        exchange="DERIBIT",
                        underlying=currency,
                        active_symbols=symbols,
                        processed_at=now,
                    )
            self._last_rest_success = self._catalog_fetched_at

    async def _refresh_summaries(self) -> None:
        assert self._rest is not None
        payloads: list[tuple[str, Any, datetime]] = []
        for currency in _CURRENCIES:
            payload = await self._rest.call(
                "public/get_book_summary_by_currency",
                {"currency": currency, "kind": "option"},
            )
            payloads.append((currency, payload, self._clock().astimezone(timezone.utc)))
        request = make_work_request(
            phase=SourcePhase.PHASE8,
            source_id=SourceId.PHASE8_OPTIONS_MARKET,
            work_class=WorkClass.HEAVY,
            estimated_items=self.phase8_settings.max_full_chain_records_total,
            estimated_bytes=self.phase8_settings.max_full_chain_records_total * 2_048,
            replay_class=ReplayClass.DERIVED_REPLACEABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase8.chain_summary",
        )
        async with self.admission.admit(request):
            for currency, payload, fetched_at in payloads:
                processed_at = self._clock().astimezone(timezone.utc)
                instruments = tuple(item for item in self.catalog.values() if item.underlying == currency)
                observations = parse_book_summary_response(
                    payload,
                    requested_currency=currency,
                    instrument_catalog=instruments,
                    fetched_at=fetched_at,
                    processed_at=processed_at,
                    max_records=self.phase8_settings.max_full_chain_records_per_underlying,
                )
                if not observations:
                    raise ValueError("empty full-chain summary rejected")
                await self._db(
                    "persist_full_chain_cycle",
                    observations,
                    max_records=self.phase8_settings.max_full_chain_records_per_underlying,
                )
                self._summary_observations[currency] = observations
                self._summary_fetched_at[currency] = fetched_at
                price = next(
                    (
                        observation.metrics.get("underlying_price")
                        for observation in observations
                        if observation.metrics.get("underlying_price") is not None
                        and observation.metrics["underlying_price"].value is not None
                    ),
                    None,
                )
                if price is not None:
                    self._underlying_prices[currency] = price
        if self._summary_fetched_at:
            self._last_rest_success = max(
                value for value in (self._last_rest_success, *self._summary_fetched_at.values())
                if value is not None
            )

    def _refresh_universe(self) -> None:
        selection = select_ticker_universe(
            tuple(self.catalog.values()),
            self._underlying_prices,
            self._clock().astimezone(timezone.utc),
            self.phase8_settings,
        )
        self.universe_status = selection.status
        selected = tuple(
            instrument.symbol
            for currency in _CURRENCIES
            for instrument in selection.selected[currency]
        )
        if len(selected) > self.phase8_settings.max_ticker_subscriptions_total:
            self.ticker_symbols = ()
            self.universe_status = DataStatus.PARTIAL
            self.reason = "TICKER_SUBSCRIPTION_TOTAL_CAP"
            return
        self.ticker_symbols = selected

    async def _configure_tickers(self, *, force_reseed: bool = False) -> None:
        if self._websocket is None:
            return
        try:
            await self._websocket.configure_market_subscriptions(
                tuple(self.catalog.values()), self.supported_index_names,
                ticker_symbols=self.ticker_symbols,
                force_reseed=force_reseed,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_websocket_failure(exc, terminal=False)

    async def _on_lifecycle(
        self,
        instrument: OptionInstrument | None,
        event: OptionInstrumentEvent,
    ) -> None:
        candidate = None
        if instrument is not None:
            candidate = dict(self.catalog)
            candidate[instrument.symbol] = instrument
            validation = validate_full_chain_snapshot(tuple(candidate.values()), self.phase8_settings)
            if validation.status is not DataStatus.AVAILABLE:
                self.state = "DEGRADED"
                self.reason = "LIFECYCLE_CATALOG_CAP"
                await self._write_health()
                return
        request = make_work_request(
            phase=SourcePhase.PHASE8,
            source_id=SourceId.PHASE8_OPTIONS_MARKET,
            work_class=WorkClass.MEDIUM,
            estimated_items=2 if instrument is not None else 1,
            estimated_bytes=8_192,
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase8.option_lifecycle",
            timeout_seconds=0.5,
        )
        try:
            async with self.admission.admit(request):
                if instrument is not None:
                    await self._db("upsert_instruments", (instrument,))
                await self._db("append_instrument_events", (event,))
        except AdmissionDeferred as exc:
            self._record_admission_skip("lifecycle", exc)
            return
        self._lifecycle_received_at = event.received_at
        if candidate is not None:
            self.catalog = candidate
            if self._initial_ready and self._websocket is not None:
                self._refresh_universe()
                await self._configure_tickers()
        self._refresh_health_state()
        if self.state == "DEGRADED":
            await self._write_health()

    async def _on_markprice_batch(self, observations: Sequence[OptionMarketObservation]) -> None:
        if not observations:
            return
        if all(
            observation.observation_kind is ObservationKind.WS_MARKPRICE_SNAPSHOT
            for observation in observations
        ):
            request = self._replaceable_snapshot_request(
                "phase8.markprice_latest", len(observations), timeout_seconds=0.25
            )
            try:
                async with self.admission.admit(request):
                    await self._db("persist_observations", tuple(observations))
            except AdmissionDeferred as exc:
                self._record_admission_skip("markprice", exc)
                return
        if (
            self._websocket is not None
            and self._websocket.ready
            and self._ws_connection_status != "AVAILABLE"
        ):
            await self._mark_websocket_ready()

    async def _on_ticker(
        self, _symbol: str, _metrics: Mapping[str, OptionMetricValue]
    ) -> None:
        if (
            self._websocket is not None
            and self._websocket.ready
            and self._ws_connection_status != "AVAILABLE"
        ):
            await self._mark_websocket_ready()

    async def _on_invalidate(self, reason: str) -> None:
        if reason == "WEBSOCKET_STARTUP":
            self._ws_connection_status = "CONNECTING"
            return
        if reason == "WEBSOCKET_STOPPED" or (
            self._stop_event is not None and self._stop_event.is_set()
        ):
            return
        self._ws_connection_status = "DEGRADED"
        self._lifecycle_status = "NOT_AVAILABLE"
        self._markprice_status = "NOT_AVAILABLE"
        self._ticker_status = "NOT_AVAILABLE"
        self._last_error_category = self._websocket_error_category(
            RuntimeError(reason)
        )
        self.reason = "WEBSOCKET_STATE_INVALIDATED"
        self._refresh_health_state()
        await self._write_health()
        if (
            self._initial_ready
            and self._stop_event is not None
            and not self._stop_event.is_set()
            and (self._reconcile_task is None or self._reconcile_task.done())
        ):
            self._reconcile_task = asyncio.create_task(
                self._reconcile_after_outage(), name="phase8-reconcile-after-outage"
            )

    async def _reconcile_after_outage(self) -> None:
        assert self._websocket is not None and self._rest is not None
        try:
            await self._websocket.wait_ready(
                timeout=float(self.phase8_settings.reseed_after_outage_seconds)
            )
            await self._mark_websocket_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._record_websocket_failure(exc, terminal=False)
            LOGGER.warning("phase8_reconciliation_failed exception_type=%s", type(exc).__name__)

    async def _persist_markprice_snapshot(self) -> None:
        if self._websocket is None:
            return
        processed_at = self._clock().astimezone(timezone.utc)
        rows = tuple(
            replace(
                observation,
                observation_kind=ObservationKind.WS_MARKPRICE_SNAPSHOT,
                processed_at=processed_at,
            )
            for by_symbol in self._websocket.markprice_state.values()
            for observation in by_symbol.values()
        )
        if rows:
            request = self._replaceable_snapshot_request("phase8.markprice_latest", len(rows))
            async with self.admission.admit(request):
                await self._db("persist_observations", rows)

    def _ticker_observations(self) -> tuple[OptionMarketObservation, ...]:
        if self._websocket is None:
            return ()
        ready_symbols = set(self._websocket.ready_ticker_symbols)
        processed_at = self._clock().astimezone(timezone.utc)
        result = []
        for symbol in sorted(ready_symbols):
            metrics = self._websocket.ticker_state.get(symbol)
            instrument = self.catalog.get(symbol)
            if not metrics or instrument is None:
                continue
            source_times = [metric.exchange_timestamp for metric in metrics.values() if metric.exchange_timestamp]
            received_times = [metric.received_at for metric in metrics.values() if metric.received_at]
            statuses = {metric.status for metric in metrics.values()}
            result.append(OptionMarketObservation(
                exchange=instrument.exchange,
                source=instrument.source,
                symbol=symbol,
                underlying=instrument.underlying,
                observation_kind=ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT,
                metrics=metrics,
                exchange_timestamp=max(source_times) if source_times else None,
                fetched_at=None,
                received_at=max(received_times) if received_times else None,
                processed_at=processed_at,
                status=DataStatus.AVAILABLE if statuses == {DataStatus.AVAILABLE} else DataStatus.PARTIAL,
                price_index=instrument.price_index,
                quote_currency=instrument.quote_currency,
            ))
        return tuple(result)

    async def _persist_ticker_snapshot(self) -> None:
        observations = self._ticker_observations()
        if observations:
            request = self._replaceable_snapshot_request("phase8.bounded_ticker_state", len(observations))
            async with self.admission.admit(request):
                await self._db("persist_observations", observations)

    def _replaceable_snapshot_request(
        self, stream_id: str, item_count: int, *, timeout_seconds: float = 2.0
    ):
        return make_work_request(
            phase=SourcePhase.PHASE8,
            source_id=SourceId.PHASE8_OPTIONS_MARKET,
            work_class=WorkClass.MEDIUM,
            estimated_items=max(1, item_count),
            estimated_bytes=max(65_536, item_count * 2_048),
            replay_class=ReplayClass.DERIVED_REPLACEABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id=stream_id,
            timeout_seconds=timeout_seconds,
        )

    async def _persist_contexts(self) -> None:
        now = self._clock().astimezone(timezone.utc)
        observations: list[OptionMarketObservation] = [
            item for currency in _CURRENCIES
            for item in self._summary_observations.get(currency, ())
        ]
        if self._websocket is not None:
            observations.extend(
                item for group in self._websocket.markprice_state.values()
                for item in group.values()
            )
            observations.extend(self._ticker_observations())
        work_items = max(1, len(self.catalog) + len(observations))
        request = make_work_request(
            phase=SourcePhase.PHASE8,
            source_id=SourceId.PHASE8_OPTIONS_CONTEXT,
            work_class=WorkClass.HEAVY,
            estimated_items=work_items,
            estimated_bytes=max(65_536, work_items * 2_048),
            replay_class=ReplayClass.DERIVED_REPLACEABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase8.options_context",
        )
        async with self.admission.admit(request):
            await self._persist_contexts_admitted(now, observations)

    async def _persist_contexts_admitted(
        self, now: datetime, observations: list[OptionMarketObservation]
    ) -> None:
        context_statuses: list[DataStatus] = []
        for currency in _CURRENCIES:
            currency_instruments = tuple(
                item for item in self.catalog.values() if item.underlying == currency
            )
            ticker_universe = tuple(
                symbol for symbol in self.ticker_symbols
                if self.catalog.get(symbol) is not None
                and self.catalog[symbol].underlying == currency
            )
            snapshot = calculate_options_context(
                OptionsContextInputs(
                    underlying=currency,
                    instruments=currency_instruments,
                    observations=tuple(
                        item for item in observations if item.underlying == currency
                    ),
                    ticker_universe_symbols=ticker_universe,
                ),
                as_of=now,
                settings=self.phase8_settings,
            )
            await self._db("persist_context", snapshot)
            metrics = getattr(snapshot, "metrics", {})
            context_statuses.extend(
                metric.status for metric in metrics.values() if getattr(metric, "status", None) is not None
            )
            snapshot_status = getattr(snapshot, "status", None)
            if snapshot_status is not None:
                context_statuses.append(snapshot_status)
            self._last_context_timestamp = max(
                self._last_context_timestamp or snapshot.context_timestamp,
                snapshot.context_timestamp,
            )
            self._last_context_persisted_at = now
        if not context_statuses:
            context_statuses.append(DataStatus.NOT_AVAILABLE)
        if any(status is DataStatus.ERROR for status in context_statuses):
            self._context_status = "ERROR"
        elif any(status is DataStatus.STALE for status in context_statuses):
            self._context_status = "STALE"
        elif any(status in {DataStatus.PARTIAL, DataStatus.NOT_AVAILABLE} for status in context_statuses):
            self._context_status = "PARTIAL"
        else:
            self._context_status = "AVAILABLE"

    async def _refresh_chain_cycle(self) -> None:
        await self._refresh_summaries()
        self._refresh_universe()
        await self._configure_tickers()
        await self._persist_contexts()

    async def _refresh_instrument_cycle(self) -> None:
        await self._refresh_catalog()
        if self._summary_observations:
            self._refresh_universe()
        await self._configure_tickers(force_reseed=True)
        if self._summary_observations:
            await self._persist_contexts()

    async def _run_periodic(
        self,
        name: str,
        interval_seconds: int,
        callback: Callable[[], Any],
        *,
        immediate: bool = False,
    ) -> None:
        if immediate:
            await self._run_cycle(name, callback)
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                await self._run_cycle(name, callback)

    async def _run_cycle(self, name: str, callback: Callable[[], Any]) -> None:
        worker = self._worker_health.get(name)
        attempted_at = self._clock().astimezone(timezone.utc)
        if worker is not None:
            worker["status"] = "RUNNING"
            worker["last_attempt_at"] = attempted_at.isoformat()
            worker["reason"] = None
        try:
            await callback()
            if worker is not None:
                worker["status"] = "AVAILABLE"
                worker["last_success_at"] = self._clock().astimezone(timezone.utc).isoformat()
                worker["consecutive_failures"] = 0
                worker["error_type"] = None
            self._refresh_health_state()
            await self._write_health()
        except asyncio.CancelledError:
            raise
        except AdmissionDeferred as exc:
            self._record_admission_skip(name, exc)
            if worker is not None:
                worker["status"] = "PARTIAL"
                worker["reason"] = self._last_skipped_cycle_reason
            await self._write_health()
        except Exception as exc:
            self.state = "DEGRADED"
            self.reason = f"{name.upper()}_CYCLE_FAILED"
            if worker is not None:
                worker["status"] = "DEGRADED"
                worker["last_error_at"] = self._clock().astimezone(timezone.utc).isoformat()
                worker["consecutive_failures"] = int(worker.get("consecutive_failures", 0)) + 1
                worker["error_type"] = type(exc).__name__
                worker["reason"] = self.reason
            LOGGER.warning("phase8_periodic_cycle_failed cycle=%s exception_type=%s", name, type(exc).__name__)
            await self._write_health()

    def _record_admission_skip(self, name: str, exc: AdmissionDeferred) -> None:
        stream_id = _PERIODIC_STREAM_IDS.get(name)
        if stream_id is None:
            self.state = "DEGRADED"
            self.reason = "UNCLASSIFIED_ADMISSION_DEFERRED"
            self._last_skipped_cycle = name
            self._last_skipped_cycle_reason = self.reason
            return
        data_age_seconds = None
        max_age_seconds = None
        if name == "chain" and self._summary_fetched_at:
            now = self._clock().astimezone(timezone.utc)
            data_age_seconds = max(
                max(0.0, (now - timestamp).total_seconds())
                for timestamp in self._summary_fetched_at.values()
            )
            max_age_seconds = self.phase8_settings.chain_stale_after_seconds
        decision = decide_overload(
            stream_id,
            data_age_seconds=data_age_seconds,
            max_age_seconds=max_age_seconds,
        )
        self._skipped_cycles[name] = self._skipped_cycles.get(name, 0) + 1
        self._last_skipped_cycle = name
        self._last_skipped_cycle_reason = f"{decision.reason}:{exc.reason.value}"
        self.state = "STALE" if decision.status is DataState.STALE else "DEGRADED"
        self.reason = self._last_skipped_cycle_reason

    def _phase8_component_statuses(self) -> dict[str, str]:
        now = self._clock().astimezone(timezone.utc)
        catalog = evaluate_catalog_freshness(
            self._catalog_fetched_at, as_of=now, settings=self.phase8_settings
        )
        catalog_status = catalog.status.value
        present_summaries = [
            self._summary_fetched_at[currency]
            for currency in _CURRENCIES
            if currency in self._summary_fetched_at
        ]
        if not present_summaries:
            summary_status = "NOT_AVAILABLE"
        elif any(
            (now - fetched_at).total_seconds() < 0
            or (now - fetched_at).total_seconds() > self.phase8_settings.chain_stale_after_seconds
            for fetched_at in present_summaries
        ):
            summary_status = "STALE"
        elif len(present_summaries) == len(_CURRENCIES):
            summary_status = "AVAILABLE"
        else:
            summary_status = "PARTIAL"
        return {
            "catalog_status": catalog_status,
            "summary_status": summary_status,
            "ws_connection_status": self._ws_connection_status,
            "lifecycle_status": self._lifecycle_status,
            "markprice_status": self._markprice_status,
            "ticker_status": self._ticker_status,
            "context_status": self._context_status,
        }

    def _refresh_health_state(self) -> None:
        if self.state in {"STOPPING", "STOPPED", "ERROR", "DISABLED"}:
            return
        now = self._clock().astimezone(timezone.utc)
        components = self._phase8_component_statuses()
        if components["catalog_status"] == DataStatus.STALE.value:
            self.state = "STALE"
            self.reason = "CATALOG_STALE"
            return
        if components["catalog_status"] != DataStatus.AVAILABLE.value:
            self.state = "DEGRADED"
            self.reason = "CATALOG_NOT_AVAILABLE"
            return
        if components["summary_status"] == "STALE":
            self.state = "STALE"
            self.reason = "CHAIN_SUMMARY_STALE"
            return
        if components["summary_status"] != "AVAILABLE":
            self.state = "DEGRADED"
            self.reason = "CHAIN_SUMMARY_NOT_AVAILABLE"
            return
        if (
            self._websocket is None
            or not self._websocket.ready
            or self._ws_connection_status != "AVAILABLE"
        ):
            self.state = "DEGRADED"
            self.reason = "WEBSOCKET_UNAVAILABLE"
            return
        if any(
            components[name] != "AVAILABLE"
            for name in ("lifecycle_status", "markprice_status", "ticker_status")
        ):
            self.state = "DEGRADED"
            self.reason = "WEBSOCKET_DATA_COVERAGE_PARTIAL"
            return
        if self._context_status != "AVAILABLE":
            self.state = "DEGRADED"
            self.reason = "OPTIONS_CONTEXT_PARTIAL"
            return
        unhealthy_workers = [
            (name, item)
            for name, item in self._worker_health.items()
            if item.get("status") in {"DEGRADED", "PARTIAL"}
        ]
        if unhealthy_workers:
            name, item = sorted(unhealthy_workers)[0]
            self.state = "DEGRADED"
            self.reason = item.get("reason") or f"{name.upper()}_WORKER_DEGRADED"
            return
        self.state = "AVAILABLE" if self.universe_status is DataStatus.AVAILABLE else "DEGRADED"
        self.reason = None if self.state == "AVAILABLE" else "UNIVERSE_COVERAGE_PARTIAL"

    def _health_details(self) -> dict[str, Any]:
        component_statuses = self._phase8_component_statuses()
        phase8_status = {
            "AVAILABLE": "AVAILABLE",
            "DEGRADED": "PARTIAL",
            "STALE": "STALE",
            "ERROR": "ERROR",
            "DISABLED": "NOT_CONFIGURED",
        }.get(self.state, "NOT_AVAILABLE")
        data_quality = {
            "AVAILABLE": "AVAILABLE",
            "PARTIAL": "PARTIAL",
            "STALE": "STALE",
            "ERROR": "ERROR",
        }.get(phase8_status, "NOT_AVAILABLE")
        successful_workers = [
            datetime.fromisoformat(item["last_success_at"])
            for item in self._worker_health.values()
            if item.get("last_success_at")
        ]
        successful_sources = [
            value for value in (
                self._catalog_fetched_at,
                *self._summary_fetched_at.values(),
                self._last_context_persisted_at,
                *successful_workers,
            ) if value is not None
        ]
        return {
            "phase": 8,
            "configured": bool(self.phase8_settings.enabled),
            "phase8_status": phase8_status,
            "data_quality": data_quality,
            "runtime_state": self.state,
            "runtime_stage": self._runtime_stage,
            "reason": self.reason or ("STARTUP_IN_PROGRESS" if self.state == "INITIALIZING" else None),
            "failure_type": self._last_failure_type,
            **component_statuses,
            "last_rest_success": self._last_rest_success.isoformat() if self._last_rest_success else None,
            "last_ws_success": self._last_ws_success.isoformat() if self._last_ws_success else None,
            "last_error_category": self._last_error_category,
            "workers_started": bool(self._tasks),
            "worker_count": len(self._worker_health),
            "worker_health": {name: dict(value) for name, value in sorted(self._worker_health.items())},
            "last_success_at": max(successful_sources).isoformat() if successful_sources else None,
            "context_timestamp": self._last_context_timestamp.isoformat() if self._last_context_timestamp else None,
            "last_persisted_at": self._last_context_persisted_at.isoformat() if self._last_context_persisted_at else None,
            "catalog_count": len(self.catalog),
            "catalog_fetched_at": self._catalog_fetched_at.isoformat() if self._catalog_fetched_at else None,
            "summary_fetched_at": {
                currency: value.isoformat() for currency, value in sorted(self._summary_fetched_at.items())
            },
            "ticker_subscription_count": len(self.ticker_symbols),
            "universe_status": self.universe_status.value,
            "websocket_ready": bool(self._websocket and self._websocket.ready),
            "retention_enforcement": self.phase8_settings.retention_enforcement,
            "context_only": True,
            "skipped_cycles": dict(sorted(self._skipped_cycles.items())),
            "last_skipped_cycle": self._last_skipped_cycle,
            "last_skipped_cycle_reason": self._last_skipped_cycle_reason,
        }

    async def _write_health(self) -> None:
        if self._persistence is None:
            return
        try:
            await self._db("set_health", self.state, self._health_details(), self._clock().astimezone(timezone.utc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("phase8_health_persistence_failed exception_type=%s", type(exc).__name__)

    def _start_workers(self) -> None:
        assert self._stop_event is not None
        settings = self.phase8_settings
        jobs: list[tuple[str, int, Callable[[], Any]]] = [
            ("instruments", settings.instrument_refresh_seconds, self._refresh_instrument_cycle),
            ("chain", settings.chain_snapshot_interval_seconds, self._refresh_chain_cycle),
            ("markprice", settings.markprice_snapshot_interval_seconds, self._persist_markprice_snapshot),
            ("ticker", settings.ticker_snapshot_interval_seconds, self._persist_ticker_snapshot),
            ("context", settings.context_interval_seconds, self._persist_contexts),
            ("health", 60, self._write_health),
        ]
        if settings.retention_enforcement:
            jobs.append(("retention", 86_400, lambda: self._db("cleanup_retention", now=self._clock().astimezone(timezone.utc))))
        for name, interval, callback in jobs:
            if name not in _WORKER_NAMES:
                raise RuntimeError("unrecognized Phase 8 worker")
            self._worker_health[name] = {
                "status": "SCHEDULED",
                "interval_seconds": interval,
                "started_at": self._clock().astimezone(timezone.utc).isoformat(),
                "last_attempt_at": None,
                "last_success_at": None,
                "last_error_at": None,
                "consecutive_failures": 0,
                "reason": None,
                "error_type": None,
            }
            task = asyncio.create_task(
                self._run_periodic(name, interval, callback), name=f"phase8-{name}-scheduler"
            )
            self._tasks.add(task)
