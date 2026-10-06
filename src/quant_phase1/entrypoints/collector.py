"""Long-running paper-only Bitget public market-data collector."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import signal

from ..adapters.bitget_v3.rest import BitgetV3UtaRestClient
from ..adapters.bitget_v3.websocket import (
    BitgetV3UtaWebSocket,
    MAX_SUBSCRIPTION_ARGS_PER_MESSAGE,
    TickerStreamWatchdog,
)
from ..config import Settings
from ..contracts import Candle, DataStatus
from ..freshness import (
    INTERVAL_SECONDS,
    PRICE_STAGE1_HARD_AGE_SECONDS,
    expected_latest_closed_open,
)
from ..gap_recovery import (
    GLOBAL_RECOVERY_CONCURRENCY_LIMIT,
    GapRecoveryCoordinator,
    RecoveryRange,
    RecoveryScan,
)
from ..logging import configure_logging
from ..pipeline import INTERVALS, MarketDataBatch, MarketDataCollector
from ..runtime import BoundedEventBuffer, PeriodicScheduler, RuntimeHealthTracker, WebSocketCanonicalStore, await_or_stop
from ..service import mark_degraded, mark_running, persist_health, persist_market_batch, write_health_file
from ..time import utc_now
from quant_data_layer.errors import normalize_error
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.db_admission import PostgresWriteAdmission
from quant_data_layer.observability import (
    DatabaseMetric,
    DatabaseSnapshot,
    Measurement,
    MeasurementReason,
    ProcessRole,
    RuntimeSnapshot,
    SourceId,
    SourceMetric,
    SourcePhase,
    SourceSnapshot,
    TransactionClass,
    TransactionClassSnapshot,
    WorkClass,
    capture_process_snapshot,
)
from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
from quant_phase3.cvd import BybitCVDBuilder
from quant_phase3.cross_exchange import build_cross_exchange_flow_snapshot
from quant_phase3.rollup import FlowRollupBuilder
from quant_phase3.runtime import Phase3PublicStreamRunner, Phase3TradeRuntime
from quant_phase3.subscriptions import (
    SubscriptionCandidate,
    TradeSubscriptionManager,
    select_candidates,
)
from quant_phase8.config import Phase8Settings
from quant_phase8.runtime import Phase8CollectorRuntime


LOGGER = logging.getLogger("quant_phase1")
WS_CHANNELS_PER_CONNECTION = MAX_SUBSCRIPTION_ARGS_PER_MESSAGE
WS_CANONICAL_KLINE_CAPACITY = 4
TICKER_STREAM_IDLE_RECONNECT_SECONDS = 30.0
DEFAULT_READINESS_TICKER_SYMBOLS = ("BTCUSDT", "ETHUSDT")


def _readiness_ticker_symbols() -> set[str]:
    configured = os.environ.get(
        "QUANT_COLLECTOR_READINESS_SYMBOLS",
        ",".join(DEFAULT_READINESS_TICKER_SYMBOLS),
    )
    return {symbol.strip().upper() for symbol in configured.split(",") if symbol.strip()}


def _ws_group_context(group: list[dict[str, str]]) -> str:
    normalized_args = sorted(
        (
            str(arg.get("topic") or "unknown").lower(),
            str(arg.get("interval") or "").lower(),
            str(arg.get("symbol") or "").upper(),
        )
        for arg in group
    )
    topics = {topic for topic, _, _ in normalized_args}
    intervals = sorted({interval for _, interval, _ in normalized_args if interval})
    symbols = sorted({symbol for _, _, symbol in normalized_args if symbol})
    if topics == {"ticker"}:
        scope = "readiness" if symbols and set(symbols).issubset(_readiness_ticker_symbols()) else "market"
        group_type = f"ticker:{scope}"
    elif topics == {"kline"}:
        group_type = f"kline:{'+'.join(intervals) or 'unknown'}"
    else:
        group_type = f"mixed:{'+'.join(sorted(topics))}" if topics else "empty"
    canonical = json.dumps(normalized_args, separators=(",", ":"))
    group_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    sample = ",".join(
        "".join(char for char in symbol if char.isalnum() or char in "_-.")[:40]
        for symbol in symbols[:3]
    ) or "none"
    return (
        f"group_id={group_id} group_type={group_type} "
        f"channels={len(normalized_args)} symbols={len(symbols)} sample={sample}"
    )


@contextmanager
def _phase4_repository_scope(settings: Settings):
    import psycopg

    from quant_phase4.persistence import Phase4Repository

    with psycopg.connect(settings.postgres_dsn) as connection:
        yield Phase4Repository(connection)


def initialize_phase4_repository(settings: Settings) -> tuple[str, ...]:
    """Check the preinstalled Phase 4 schema before collector startup."""

    import psycopg

    from ..db import assert_schema_ready

    with psycopg.connect(settings.postgres_dsn) as connection:
        assert_schema_ready(connection, required_version="009_phase4_metrics.sql")
        return ()


def partition_ws_args(args: list[dict[str, str]], *, size: int = WS_CHANNELS_PER_CONNECTION) -> list[list[dict[str, str]]]:
    if size <= 0:
        raise ValueError("size must be positive")
    return [args[start : start + size] for start in range(0, len(args), size)]


async def run_once(settings: Settings | None = None) -> dict[str, int]:
    settings = settings or Settings.from_env()
    async with BitgetV3UtaRestClient(settings) as client:
        batch = await MarketDataCollector(
            client, max_symbols=settings.universe_limit, kline_limit=settings.kline_fetch_limit
        ).collect_once()
    LOGGER.info(
        "collector_cycle_complete",
        extra={"event_id": "collector_cycle", "status": "AVAILABLE", "symbol": f"{len(batch.tickers)}_tickers"},
    )
    return {
        "instruments": len(batch.instruments),
        "tickers": len(batch.tickers),
        "symbols_with_candles": len(batch.candles_by_symbol),
    }


def _build_phase3_derived_outputs(
    rollup_builder: FlowRollupBuilder,
    history_rows: tuple[tuple, ...],
    windows: tuple,
    processed_at,
    min_directional_sources: int,
) -> tuple[list, list]:
    """Build immutable Phase 3 rollups and cross-exchange snapshots off-loop."""

    rollups = []
    for rows in history_rows:
        for timeframe in ("5m", "15m", "1H", "4H"):
            rollups.extend(rollup_builder.build(rows, timeframe=timeframe, processed_at=processed_at))
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for window in windows:
        grouped[(window.canonical_symbol, window.timeframe)].append(window)
    cross_snapshots = [
        build_cross_exchange_flow_snapshot(
            rows,
            snapshot_timestamp=max(row.window_close for row in rows),
            processed_at=processed_at,
            min_directional_sources=min_directional_sources,
        )
        for rows in grouped.values()
    ]
    return rollups, cross_snapshots


def _persist_phase3_outputs(
    settings: Settings,
    windows: tuple,
    rollups: tuple,
    cvd_points: tuple,
    cross_snapshots: tuple,
) -> None:
    """Perform synchronous Phase 3 PostgreSQL writes outside the asyncio loop."""

    import psycopg

    with psycopg.connect(settings.postgres_dsn) as connection:
        from ..db import assert_schema_ready
        from quant_phase3.persistence import Phase3Repository

        assert_schema_ready(connection, required_version="008_phase3_flow.sql")
        repository = Phase3Repository(connection)
        repository.insert_flow_windows(windows)
        repository.insert_flow_windows(rollups)
        repository.insert_cvd_snapshots(cvd_points)
        repository.insert_cross_exchange_snapshots(cross_snapshots)
        repository.cleanup_flow(
            retention_days=settings.phase3_flow_retention_days,
            cvd_retention_days=settings.phase3_cvd_retention_days,
            gap_retention_days=settings.phase3_gap_retention_days,
            cross_exchange_retention_days=settings.phase3_cross_exchange_retention_days,
            enrichment_retention_days=settings.phase3_enrichment_retention_days,
        )


class CollectorService:
    def __init__(
        self,
        settings: Settings,
        *,
        stop_event: asyncio.Event | None = None,
        db_admission: PostgresWriteAdmission | None = None,
    ) -> None:
        self.settings = settings
        self.stop_event = stop_event or asyncio.Event()
        self.component = "quant-collector"
        self.health = RuntimeHealthTracker(self.component)
        self.admission = WorkAdmissionController(role=ProcessRole.COLLECTOR)
        self.db_admission = db_admission or PostgresWriteAdmission()
        diagnostics_path = os.environ.get("PHASE7_ACCEPTANCE_DIAGNOSTICS_PATH", "").strip()
        self.diagnostics_path = Path(diagnostics_path) if diagnostics_path else None
        if self.diagnostics_path is not None and (
            not self.diagnostics_path.is_absolute()
            or self.diagnostics_path.parent != Path("/tmp")
        ):
            raise ValueError("acceptance diagnostics path must be a direct child of /tmp")
        self._db_writer_active_transactions = 0
        self._phase1_db_admission_deferred_count = 0
        self._phase1_db_admission_last_reason = "NONE"
        self._phase1_db_admission_last_stream = "NONE"
        # The canonical WS store only needs the recent closed-bar window used
        # for freshness/gap recovery.  Keep it bounded independently from the
        # event queue; using the queue capacity here multiplies memory by
        # symbols x intervals and can exhaust the small Candidate node.
        # REST bootstrap can fetch a larger backfill, but the live canonical
        # store only needs the latest few closed bars for freshness and the
        # next persistence cycle. Keeping this independent prevents
        # 200-symbol x 4-timeframe Candle retention from exhausting a small
        # runtime node.
        self.store = WebSocketCanonicalStore(capacity=min(settings.kline_fetch_limit, WS_CANONICAL_KLINE_CAPACITY))
        self.events = BoundedEventBuffer[dict](capacity=settings.event_buffer_capacity)
        self.selected_symbols: tuple[str, ...] = ()
        self.websockets: list[BitgetV3UtaWebSocket] = []
        self.ws_tasks: list[asyncio.Task[None]] = []
        self.gap_recovery: GapRecoveryCoordinator | None = None
        self._gap_recovery_start_lock = asyncio.Lock()
        self.ws_reconnect_count = 0
        self._ticker_source_lag_ms_last = 0
        self._ticker_source_lag_ms_max = 0
        self._ticker_ingest_duration_us_max = 0
        self._ticker_idle_symbols_last_group = 0
        self.ws_startup_retry_task: asyncio.Task[None] | None = None
        self.ws_refresh_pending = False
        self.phase3_runtime = None
        self.phase3_runner = None
        self.phase3_task: asyncio.Task[None] | None = None
        self.phase3_persist_task: asyncio.Task[None] | None = None
        self.phase4_runtime = None
        self.phase4_task: asyncio.Task[None] | None = None
        self._phase4_repository_initialized = False
        self.phase6_runtime = None
        self.phase6_task: asyncio.Task[None] | None = None
        from quant_phase7.runtime import Phase7CollectorRuntime

        self.phase7_runtime = Phase7CollectorRuntime(
            settings,
            admission=self.admission,
            db_admission=self.db_admission,
        )
        self.phase7_runtime.diagnostics_sink = (
            self._write_diagnostics_snapshot if self.diagnostics_path is not None else None
        )
        self.phase7_task: asyncio.Task[None] | None = None
        self.phase8_settings = Phase8Settings.from_env()
        self.phase8_runtime = (
            Phase8CollectorRuntime(
                settings, phase8_settings=self.phase8_settings, admission=self.admission
            )
            if self.phase8_settings.enabled else None
        )
        self.phase8_task: asyncio.Task[None] | None = None
        if settings.phase4_enabled:
            from quant_phase4.runtime import Phase4Runtime

            self.phase4_runtime = Phase4Runtime(
                settings,
                symbols_provider=lambda: self.selected_symbols,
                repository_provider=lambda: _phase4_repository_scope(settings),
                admission=self.admission,
            )
        if settings.phase6_enabled:
            from quant_phase6.runtime import Phase6CollectorRuntime

            self.phase6_runtime = Phase6CollectorRuntime(settings, admission=self.admission)
        self.phase3_stage1_ab_symbols: tuple[str, ...] = ()
        self.phase3_rollup = FlowRollupBuilder()
        self.phase3_cvd = BybitCVDBuilder(max_history_minutes=1440)
        self.phase3_history = defaultdict(lambda: deque(maxlen=300))
        if settings.phase3_enabled:
            self.phase3_runtime = Phase3TradeRuntime(
                {
                    "bitget": BitgetUTA3PublicTradeAdapter(),
                    "bybit": BybitPublicTradeAdapter(),
                    "hyperliquid": HyperliquidPublicTradeAdapter(),
                },
                queue_capacity=settings.max_trade_queue_size,
                dedup_max_entries_per_exchange=settings.trade_dedup_max_entries_per_exchange,
                dedup_ttl_seconds=settings.trade_dedup_ttl_seconds,
                allowed_lateness_seconds=settings.trade_allowed_lateness_seconds,
            )
            self.phase3_runner = Phase3PublicStreamRunner(
                self.phase3_runtime,
                reconnect_seconds=settings.ws_reconnect_seconds,
            )
        self.phase3_subscription_managers = {
            exchange: TradeSubscriptionManager(
                max_symbols=settings.max_trade_stream_symbols,
                min_subscription_seconds=settings.trade_min_subscription_seconds,
                cooldown_seconds=settings.trade_subscription_cooldown_seconds,
                capabilities=adapter.capabilities,
            )
            for exchange, adapter in (self.phase3_runtime.adapters.items() if self.phase3_runtime else ())
        }

    def _phase3_candidates(self) -> tuple[SubscriptionCandidate, ...]:
        universe = tuple(
            self._phase3_candidate(symbol, rank=10_000 + index, stage1_ab=False)
            for index, symbol in enumerate(self.selected_symbols)
        )
        stage1_ab = tuple(
            self._phase3_candidate(symbol, rank=index, stage1_ab=True)
            for index, symbol in enumerate(self.phase3_stage1_ab_symbols)
        )
        return select_candidates(
            universe,
            stage1_ab,
            max_symbols=self.settings.max_trade_stream_symbols,
        )

    def _phase3_hydration_routes(self) -> tuple[tuple[str, str], ...]:
        candidates = self._phase3_candidates()
        return tuple(
            sorted(
                (exchange, candidate.canonical_symbol)
                for exchange, manager in self.phase3_subscription_managers.items()
                if not (exchange == "bitget" and self.settings.bitget_sbe_flow_enabled)
                if manager.capabilities.supports_public_trade_stream
                for candidate in candidates
                if candidate.exchange_symbols.get(exchange)
            )
        )

    def phase3_symbols_by_exchange(self) -> dict[str, tuple[str, ...]]:
        candidates = self._phase3_candidates()
        result: dict[str, tuple[str, ...]] = {}
        now = utc_now()
        for exchange, manager in self.phase3_subscription_managers.items():
            if exchange == "bitget" and self.settings.bitget_sbe_flow_enabled:
                result[exchange] = ()
                continue
            manager.refresh(exchange, candidates, now=now)
            result[exchange] = manager.active_symbols(exchange)
        return result

    def observability_snapshot(self, *, sampled_at_utc: datetime | None = None) -> RuntimeSnapshot:
        """Return bounded process-local measurements without consuming runtime work."""

        sampled_at = sampled_at_utc or utc_now()
        sources: list[SourceSnapshot] = []

        recovery = self.gap_recovery.diagnostics_snapshot() if self.gap_recovery else None
        market_metrics = {
            SourceMetric.QUEUE_DEPTH: Measurement.available(self.events.depth),
            SourceMetric.QUEUE_CAPACITY: Measurement.available(self.events.capacity),
            SourceMetric.RECONNECT_COUNT: Measurement.available(self.ws_reconnect_count),
            SourceMetric.DROP_COUNT: Measurement.available(self.events.dropped_count),
        }
        if recovery is not None:
            market_metrics.update(
                {
                    SourceMetric.PENDING_WORK: Measurement.available(recovery["pending_count"]),
                    SourceMetric.ACTIVE_WORK: Measurement.available(recovery["active_count"]),
                    SourceMetric.BACKFILL_DEPTH: Measurement.available(recovery["queue_depth"]),
                    SourceMetric.RETRY_COUNT: Measurement.available(recovery["retry_count"]),
                }
            )
        sources.append(
            SourceSnapshot.not_exposed(
                source_id=SourceId.PHASE1_MARKET_DATA,
                phase=SourcePhase.PHASE1,
                configured=True,
                sampled_at_utc=sampled_at,
                measurements=market_metrics,
            )
        )

        for exchange, source_id in (
            ("bitget", SourceId.PHASE3_BITGET_TRADES),
            ("bybit", SourceId.PHASE3_BYBIT_TRADES),
            ("hyperliquid", SourceId.PHASE3_HYPERLIQUID_TRADES),
        ):
            configured = self.phase3_runtime is not None
            metrics = {}
            if configured:
                queues = tuple(
                    queue
                    for (queue_exchange, _), queue in self.phase3_runtime._queues.items()
                    if queue_exchange == exchange
                )
                metrics = {
                    SourceMetric.QUEUE_DEPTH: Measurement.available(sum(queue.depth for queue in queues)),
                    SourceMetric.QUEUE_CAPACITY: Measurement.available(sum(queue.capacity for queue in queues)),
                    SourceMetric.DROP_COUNT: Measurement.available(sum(queue.dropped_count for queue in queues)),
                }
                # Phase3TradeRuntime.health_snapshot() creates an empty registry row
                # when absent. Peek first so observation itself remains read-only.
                health_states = getattr(self.phase3_runtime.health, "_states", {})
                if exchange in health_states:
                    health = self.phase3_runtime.health_snapshot(exchange)
                    lifecycle = (
                        "AVAILABLE" if health.connected
                        else "INITIALIZING" if health.last_event_at is None
                        else "DEGRADED"
                    )
                    metrics.update(
                        {
                            SourceMetric.LIFECYCLE_STATE: Measurement.available(lifecycle),
                            SourceMetric.DATA_STATUS: Measurement.available(health.status.value),
                            SourceMetric.RECONNECT_COUNT: Measurement.available(health.reconnect_count),
                            SourceMetric.GAP_COUNT: Measurement.available(health.gap_count),
                            SourceMetric.DROP_COUNT: Measurement.available(health.dropped_trades),
                        }
                    )
            sources.append(
                SourceSnapshot.not_exposed(
                    source_id=source_id,
                    phase=SourcePhase.PHASE3,
                    configured=configured,
                    sampled_at_utc=sampled_at,
                    measurements=metrics,
                )
            )

        phase4_configured = self.phase4_runtime is not None
        phase4_metrics = {}
        if phase4_configured:
            phase4_metrics = {
                SourceMetric.PENDING_WORK: Measurement.available(self.phase4_runtime.pending_event_count),
                SourceMetric.ACTIVE_WORK: Measurement.available(
                    sum(not task.done() for task in self.phase4_runtime.tasks)
                ),
                SourceMetric.RECONNECT_COUNT: Measurement.available(self.phase4_runtime.resubscribe_count),
                SourceMetric.GAP_COUNT: Measurement.available(self.phase4_runtime.gap_count),
            }
        sources.append(
            SourceSnapshot.not_exposed(
                source_id=SourceId.PHASE4_LIQUIDATION,
                phase=SourcePhase.PHASE4,
                configured=phase4_configured,
                sampled_at_utc=sampled_at,
                measurements=phase4_metrics,
            )
        )

        for source_id in (SourceId.PHASE6_EXTERNAL_CONTEXT, SourceId.PHASE6_AI):
            sources.append(
                SourceSnapshot.not_exposed(
                    source_id=source_id,
                    phase=SourcePhase.PHASE6,
                    configured=self.phase6_runtime is not None,
                    sampled_at_utc=sampled_at,
                )
            )

        for source_id in (SourceId.PHASE7_ONCHAIN, SourceId.PHASE7_SPOT):
            sources.append(
                SourceSnapshot.not_exposed(
                    source_id=source_id,
                    phase=SourcePhase.PHASE7,
                    configured=self.settings.phase7_enabled or (
                        source_id is SourceId.PHASE7_SPOT and self.settings.bitget_sbe_flow_enabled
                    ),
                    sampled_at_utc=sampled_at,
                )
            )

        sources.append(
            SourceSnapshot.not_exposed(
                source_id=SourceId.PHASE8_OPTIONS_MARKET,
                phase=SourcePhase.PHASE8,
                configured=self.phase8_runtime is not None,
                sampled_at_utc=sampled_at,
            )
        )

        process = capture_process_snapshot(
            ProcessRole.COLLECTOR,
            sampled_at_utc=sampled_at,
            admission_snapshot=self.admission.snapshot(),
            shutdown_requested=self.stop_event.is_set(),
        )
        db_admission = self.db_admission.snapshot()
        transaction_class_map = {
            WorkClass.LIGHT: TransactionClass.HEALTH_STATUS,
            WorkClass.MEDIUM: TransactionClass.CANONICAL_BATCH,
            WorkClass.HEAVY: TransactionClass.LARGE_ATOMIC_SOURCE,
        }
        db_transaction_classes = []
        unmeasured = Measurement.not_exposed(MeasurementReason.NOT_INSTRUMENTED)
        for transaction_class in TransactionClass:
            work_class = next(
                (key for key, value in transaction_class_map.items() if value is transaction_class),
                None,
            )
            if work_class is None:
                db_transaction_classes.append(TransactionClassSnapshot.not_exposed(transaction_class))
                continue
            db_transaction_classes.append(TransactionClassSnapshot(
                transaction_class=transaction_class,
                pending=Measurement.available(db_admission.pending_by_class[work_class]),
                active=Measurement.available(db_admission.active_by_class[work_class]),
                wait_seconds=unmeasured,
                hold_seconds=unmeasured,
                timeouts=unmeasured,
            ))
        database = DatabaseSnapshot.not_exposed(
            sampled_at_utc=sampled_at,
            measurements={
                DatabaseMetric.PENDING_ADMISSIONS: Measurement.available(db_admission.pending_count),
                DatabaseMetric.ACTIVE_TRANSACTIONS: Measurement.available(db_admission.active_count),
                DatabaseMetric.APPLICATION_WRITER_ACTIVE_TRANSACTIONS: Measurement.available(
                    self._db_writer_active_transactions
                ),
                # The collector currently uses a synchronous writer with no pending-batch queue.
                DatabaseMetric.APPLICATION_WRITER_PENDING_BATCHES: Measurement.available(0),
            },
            transaction_classes=tuple(db_transaction_classes),
        )
        return RuntimeSnapshot(process=process, sources=tuple(sources), database=database)

    def diagnostics_snapshot(self) -> dict[str, int | str]:
        """Read scalar runtime counters only; never expose buffered payloads."""

        recovery = self.gap_recovery.diagnostics_snapshot() if self.gap_recovery else {
            "queue_depth": 0, "queue_capacity": 0, "pending_count": 0,
            "inflight_count": 0, "rerun_count": 0, "retry_count": 0,
            "failed_count": 0, "active_count": 0, "task_count": 0,
        }
        phase3_queues = tuple(self.phase3_runtime._queues.values()) if self.phase3_runtime else ()
        phase3_builders = tuple(self.phase3_runtime._builders.values()) if self.phase3_runtime else ()
        try:
            async_task_count = sum(not task.done() for task in asyncio.all_tasks())
        except RuntimeError:
            async_task_count = 0
        closed_klines = sum(
            len(values)
            for by_interval in self.store.closed_klines.values()
            for values in by_interval.values()
        )
        phase7 = self.phase7_runtime.diagnostics_snapshot() if self.phase7_runtime else {}
        snapshot: dict[str, int | str] = {
            "collector_asyncio_task_count": async_task_count,
            "collector_ws_task_count": sum(not task.done() for task in self.ws_tasks),
            "collector_ws_connection_count": len(self.websockets),
            "collector_event_buffer_depth": self.events.depth,
            "collector_event_buffer_capacity": self.events.capacity,
            "collector_event_buffer_dropped": self.events.dropped_count,
            "collector_ticker_store_count": len(self.store.tickers),
            "collector_ticker_source_lag_ms_last": self._ticker_source_lag_ms_last,
            "collector_ticker_source_lag_ms_max": self._ticker_source_lag_ms_max,
            "collector_ticker_ingest_duration_us_max": self._ticker_ingest_duration_us_max,
            "collector_ticker_idle_symbols_last_group": self._ticker_idle_symbols_last_group,
            "collector_ticker_idle_scope": "LAST_CONNECTION_SAMPLE_NOT_GLOBAL_COVERAGE",
            "collector_closed_kline_store_count": closed_klines,
            "collector_ws_application_queue_depth": self.events.depth,
            "collector_ws_transport_pending_visibility": "NOT_EXPOSED_BY_WEBSOCKETS_PUBLIC_API",
            "phase1_recovery_queue_depth": recovery["queue_depth"],
            "phase1_recovery_queue_capacity": recovery["queue_capacity"],
            "phase1_recovery_pending": recovery["pending_count"],
            "phase1_recovery_inflight": recovery["inflight_count"],
            "phase1_recovery_rerun": recovery["rerun_count"],
            "phase1_recovery_retry": recovery["retry_count"],
            "phase1_recovery_failed": recovery["failed_count"],
            "phase1_recovery_active": recovery["active_count"],
            "phase1_recovery_task_count": recovery["task_count"],
            "phase1_db_writer_pending_batches": 0,
            "phase1_db_writer_active_transactions": self._db_writer_active_transactions,
            "phase1_db_admission_deferred_count": self._phase1_db_admission_deferred_count,
            "phase1_db_admission_last_reason": self._phase1_db_admission_last_reason,
            "phase1_db_admission_last_stream": self._phase1_db_admission_last_stream,
            "phase1_db_writer_mode": "SYNCHRONOUS",
            "phase3_trade_queue_count": len(phase3_queues),
            "phase3_trade_queue_depth": sum(queue.depth for queue in phase3_queues),
            "phase3_trade_queue_capacity": sum(queue.capacity for queue in phase3_queues),
            "phase3_trade_aggregation_windows_pending": sum(
                len(builder._aggregates) for builder in phase3_builders
            ),
            "phase4_pending_events": self.phase4_runtime.pending_event_count if self.phase4_runtime else 0,
            "phase4_task_count": sum(
                not task.done() for task in getattr(self.phase4_runtime, "_tasks", ())
            ) if self.phase4_runtime else 0,
            "phase6_collector_task_count": sum(
                not task.done() for task in getattr(self.phase6_runtime, "_tasks", ())
            ) if self.phase6_runtime else 0,
        }
        snapshot.update(phase7)
        return snapshot

    async def _diagnostics_loop(self) -> None:
        if self.diagnostics_path is None:
            return
        while not self.stop_event.is_set():
            self._write_diagnostics_snapshot()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    def _write_diagnostics_snapshot(self) -> None:
        if self.diagnostics_path is None:
            return
        payload = {"sampled_at_utc": utc_now().isoformat(), **self.diagnostics_snapshot()}
        temporary_path = self.diagnostics_path.with_suffix(".tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_path, self.diagnostics_path)
        except OSError as exc:
            LOGGER.warning("acceptance_diagnostics_write_failed exception=%s", type(exc).__name__)

    @staticmethod
    def _phase3_candidate(symbol: str, *, rank: int, stage1_ab: bool) -> SubscriptionCandidate:
        normalized = symbol.strip().upper()
        if not normalized.endswith("USDT"):
            raise ValueError(f"Phase 3 requires a USDT perpetual symbol: {symbol}")
        canonical = f"{normalized[:-4]}-USDT-PERP"
        return SubscriptionCandidate(
            canonical_symbol=canonical,
            rank=rank,
            stage1_ab=stage1_ab,
            exchange_symbols={
                "bitget": normalized,
                "bybit": normalized,
                "hyperliquid": normalized[:-4],
            },
        )

    async def _load_phase3_stage1_ab_symbols(self) -> None:
        if self.phase3_runtime is None:
            return
        try:
            import psycopg

            with psycopg.connect(self.settings.postgres_dsn, autocommit=True) as connection:
                from ..db import assert_schema_ready
                from ..repositories import Phase1Repository

                assert_schema_ready(connection, required_version="008_phase3_flow.sql")
                self.phase3_stage1_ab_symbols = Phase1Repository(connection).load_latest_stage1_ab_symbols(
                    limit=self.settings.universe_limit
                )
        except Exception as exc:
            LOGGER.warning("phase3_stage1_candidates_unavailable error=%s", normalize_error(exc).safe_summary)

    async def _hydrate_phase3_state(self) -> None:
        if self.phase3_runtime is None:
            return
        try:
            import psycopg

            with psycopg.connect(self.settings.postgres_dsn, autocommit=True) as connection:
                from ..db import assert_schema_ready
                from quant_phase3.persistence import Phase3Repository

                assert_schema_ready(connection, required_version="008_phase3_flow.sql")
                routes = self._phase3_hydration_routes()
                minute_windows = Phase3Repository(connection).load_recent_available_minute_windows(
                    routes=routes,
                    per_route_limit=300,
                )
            for window in minute_windows:
                self.phase3_history[(window.exchange, window.canonical_symbol)].append(window)
            self.phase3_cvd.hydrate([window for window in minute_windows if window.exchange == "bybit"])
            LOGGER.info(
                "phase3_state_hydrated",
                extra={
                    "event_id": "phase3_hydration",
                    "status": "AVAILABLE",
                    "symbol": str(len({window.canonical_symbol for window in minute_windows})),
                },
            )
        except Exception as exc:
            LOGGER.warning("phase3_state_hydration_unavailable error=%s", normalize_error(exc).safe_summary)

    async def _persist_batch(self, batch: MarketDataBatch) -> bool:
        from ..pipeline import split_market_batch
        candle_count=sum(len(v) for by in batch.candles_by_symbol.values() for v in by.values())
        if candle_count>8000:
            succeeded=True
            for part in split_market_batch(batch, candle_budget=8000):
                ok=await self._persist_batch(part)
                succeeded=ok and succeeded
            return succeeded
        kline_count = sum(
            len(candles)
            for by_interval in batch.candles_by_symbol.values()
            for candles in by_interval.values()
        )
        ticker_count = len(batch.tickers)
        instrument_count = len(getattr(batch, "instruments", ()))
        if kline_count > 0:
            stream_id, replay_class = "phase1.closed_klines", ReplayClass.RECOVERABLE_REPLAYABLE
        elif instrument_count > 0:
            stream_id, replay_class = "phase1.instruments", ReplayClass.RECOVERABLE_REPLAYABLE
        else:
            stream_id, replay_class = "phase1.ticker_latest", ReplayClass.DERIVED_REPLACEABLE
        request = make_work_request(
            phase=SourcePhase.PHASE1,
            source_id=SourceId.PHASE1_MARKET_DATA,
            work_class=WorkClass.MEDIUM,
            estimated_items=max(1, ticker_count + instrument_count + kline_count),
            estimated_bytes=max(65_536, ticker_count * 4_096 + instrument_count * 2_048 + kline_count * 1_024),
            replay_class=replay_class,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id=stream_id,
            timeout_seconds=2.0,
        )
        try:
            async with self.admission.admit(request):
                return await self._persist_batch_admitted(batch)
        except AdmissionDeferred as exc:
            self._phase1_db_admission_deferred_count += 1
            self._phase1_db_admission_last_reason = exc.reason.value
            self._phase1_db_admission_last_stream = stream_id
            mark_degraded(self.health, self.component, exc)
            LOGGER.info(
                "collector_batch_admission_deferred stream=%s class=%s reason=%s",
                stream_id,
                replay_class.value,
                exc.reason.value,
            )
            return False

    async def _persist_batch_admitted(self, batch: MarketDataBatch) -> bool:
        self._db_writer_active_transactions += 1
        try:
            gap_recovery = self.gap_recovery
            recovery = (
                mark_running(self.health, self.component)
                if gap_recovery is None or gap_recovery.is_healthy
                else None
            )
            health_snapshot = copy.copy(self.health)
            return await asyncio.to_thread(
                self._persist_batch_transaction,
                batch,
                health_snapshot,
                gap_recovery,
                recovery,
            )
        except Exception as exc:
            mark_degraded(self.health, self.component, exc)
            LOGGER.warning("persistence_unavailable error=%s", normalize_error(exc).safe_summary)
            return False
        finally:
            self._db_writer_active_transactions = max(0, self._db_writer_active_transactions - 1)

    def _persist_batch_transaction(
        self,
        batch: MarketDataBatch,
        health_snapshot: RuntimeHealthTracker,
        gap_recovery: GapRecoveryCoordinator | None,
        recovery: RuntimeHealthSnapshot | None,
    ) -> bool:
        import psycopg

        with psycopg.connect(self.settings.postgres_dsn) as connection:
            from ..db import assert_schema_ready
            from ..repositories import Phase1Repository

            assert_schema_ready(connection)
            repository = Phase1Repository(connection)
            persisted_candles = persist_market_batch(
                repository,
                batch,
                retention_days=self.settings.kline_retention_days,
                market_snapshot_retention_days=self.settings.market_snapshot_retention_days,
            )
            persist_health(
                repository,
                self.component,
                health_snapshot,
                degraded_grace_seconds=self.settings.health_degraded_grace_seconds,
                details={
                    "heartbeat": True,
                    "symbols": len(batch.tickers),
                    "candles": persisted_candles,
                    "buffer_dropped": self.events.dropped_count,
                    "ws_messages": self.store.received_messages,
                    "ws_tickers": self.store.received_tickers,
                    "ws_klines": self.store.received_klines,
                    "ws_reconnects": self.ws_reconnect_count,
                    "kline_recovery_active": gap_recovery.active_count if gap_recovery else 0,
                    "kline_recovery_pending": gap_recovery.pending_count if gap_recovery else 0,
                    "kline_recovery_inflight": gap_recovery.inflight_count if gap_recovery else 0,
                    "kline_recovery_completed": gap_recovery.completed_count if gap_recovery else 0,
                    "kline_recovery_failed": gap_recovery.failed_count if gap_recovery else 0,
                    "kline_recovery_scanned": gap_recovery.candidate_scanned_total if gap_recovery else 0,
                    "kline_recovery_gaps": gap_recovery.actual_gap_total if gap_recovery else 0,
                    "kline_recovery_admitted": gap_recovery.admitted_count if gap_recovery else 0,
                    "kline_recovery_coalesced": gap_recovery.coalesced_count if gap_recovery else 0,
                    "kline_recovery_skipped": gap_recovery.skipped_count if gap_recovery else 0,
                    "kline_reconciliation_runs": gap_recovery.reconciliation_runs if gap_recovery else 0,
                    **self.diagnostics_snapshot(),
                },
                recovery=recovery,
            )
            if self.phase4_runtime is not None:
                self.phase4_runtime.persist_health(repository)
        return True

    def _store_batch(self) -> MarketDataBatch:
        return MarketDataBatch(
            collected_at=utc_now(),
            instruments=[],
            tickers=list(self.store.tickers.values()),
            selected_symbols=tuple(self.selected_symbols),
            candles_by_symbol={
                symbol: {interval: list(candles)[-2:] for interval, candles in intervals.items()}
                for symbol, intervals in self.store.closed_klines.items()
            },
        )

    def _ws_topic_groups(self) -> list[list[dict[str, str]]]:
        readiness_symbols = _readiness_ticker_symbols()
        ticker_args = [BitgetV3UtaWebSocket.ticker_arg(symbol) for symbol in self.selected_symbols]
        readiness_tickers = [arg for arg in ticker_args if arg["symbol"].upper() in readiness_symbols]
        other_tickers = [arg for arg in ticker_args if arg["symbol"].upper() not in readiness_symbols]
        topic_groups: list[list[dict[str, str]]] = [readiness_tickers, other_tickers]
        topic_groups.extend(
            [
                [BitgetV3UtaWebSocket.kline_arg(symbol, interval) for symbol in self.selected_symbols]
                for interval in INTERVALS
            ]
        )
        return [
            part
            for group in topic_groups
            if group
            for part in partition_ws_args(group, size=WS_CHANNELS_PER_CONNECTION)
        ]

    async def _start_ws_connections(self, client: BitgetV3UtaRestClient) -> None:
        topic_groups = self._ws_topic_groups()
        if not topic_groups:
            mark_degraded(self.health, self.component, "websocket: NO_SELECTED_SYMBOLS")
            LOGGER.info(
                "ws_subscriptions_unavailable",
                extra={"event_id": "ws_subscriptions", "status": "NOT_AVAILABLE", "symbol": "0"},
            )
            return
        channel_count = sum(len(group) for group in topic_groups)
        from quant_phase7.flow_scope import load_flow_scope
        sbe_connections=(len(load_flow_scope(self.settings.bitget_sbe_flow_scope_path).shards())
                         if self.settings.bitget_sbe_flow_enabled else 0)
        if len(topic_groups)+sbe_connections>90:
            raise ValueError("BITGET_PUBLIC_CONNECTION_BUDGET_EXCEEDED")
        # Keep ticker and each kline interval isolated, and cap each socket at
        # the stable per-connection channel count before opening subscriptions.
        failed_groups: list[list[dict[str, str]]] = []
        loop = asyncio.get_running_loop()
        for index, group in enumerate(topic_groups):
            group_context = _ws_group_context(group)
            connect_started = loop.time()
            websocket = BitgetV3UtaWebSocket(self.settings)
            try:
                await websocket.connect()
                await websocket.subscribe_many(group, batch_size=len(group))
            except asyncio.CancelledError:
                try:
                    await websocket.close()
                except Exception:
                    pass
                for task in self.ws_tasks:
                    task.cancel()
                await asyncio.gather(*self.ws_tasks,return_exceptions=True)
                self.ws_tasks.clear()
                for established in self.websockets:
                    try:
                        await established.close()
                    except Exception:
                        pass
                self.websockets.clear()
                raise
            except Exception as exc:
                try:
                    await websocket.close()
                except Exception:
                    pass
                failed_groups = topic_groups[index:]
                mark_degraded(self.health, self.component, "websocket: CONNECTIVITY_UNAVAILABLE")
                normalized = normalize_error(exc)
                LOGGER.warning(
                    "ws_initial_connection_degraded %s failure_type=%s code=%s "
                    "elapsed_ms=%.1f pending_groups=%d error=%s",
                    group_context,
                    normalized.exception_type,
                    normalized.code or "NONE",
                    (loop.time() - connect_started) * 1000,
                    len(failed_groups),
                    normalized.safe_summary,
                )
                break
            self.websockets.append(websocket)
            # Read this subscribed socket while later groups are connecting.
            # Waiting for every handshake first leaves early sockets buffered
            # for tens of seconds during full-market startup.
            self.ws_tasks.append(asyncio.create_task(
                self._receive_loop(websocket, client, group_context=group_context)
            ))
            self.ws_tasks.append(asyncio.create_task(
                self._ping_loop(websocket, client, group_context=group_context)
            ))
        if failed_groups:
            self.ws_startup_retry_task = asyncio.create_task(
                self._retry_ws_startup_connections(failed_groups, client),
                name="bitget-ws-startup-retry",
            )
            self.ws_tasks.append(self.ws_startup_retry_task)
        LOGGER.info(
            "ws_subscriptions_ready",
            extra={
                "event_id": "ws_subscriptions",
                "status": "DEGRADED" if failed_groups else "AVAILABLE",
                "symbol": str(channel_count),
                "connections": len(self.websockets),
                "pending_groups": len(failed_groups),
            },
        )

    async def _retry_ws_startup_connections(
        self,
        groups: list[list[dict[str, str]]],
        client: BitgetV3UtaRestClient,
    ) -> None:
        pending = list(groups)
        attempt = 0
        loop = asyncio.get_running_loop()
        try:
            while pending and not self.stop_event.is_set():
                if self.ws_refresh_pending:
                    self.ws_refresh_pending = False
                    await self._reset_ws_connections_for_retry()
                    pending = self._ws_topic_groups()
                failed: list[list[dict[str, str]]] = []
                for group in pending:
                    group_context = _ws_group_context(group)
                    connect_started = loop.time()
                    websocket = BitgetV3UtaWebSocket(self.settings)
                    try:
                        await websocket.connect()
                        await websocket.subscribe_many(group, batch_size=len(group))
                    except asyncio.CancelledError:
                        try:
                            await websocket.close()
                        except Exception:
                            pass
                        raise
                    except Exception as exc:
                        try:
                            await websocket.close()
                        except Exception:
                            pass
                        failed.append(group)
                        normalized = normalize_error(exc)
                        LOGGER.warning(
                            "ws_startup_retry_failed %s failure_type=%s code=%s "
                            "elapsed_ms=%.1f error=%s",
                            group_context,
                            normalized.exception_type,
                            normalized.code or "NONE",
                            (loop.time() - connect_started) * 1000,
                            normalized.safe_summary,
                        )
                        continue
                    self.websockets.append(websocket)
                    self.ws_tasks.append(asyncio.create_task(
                        self._receive_loop(websocket, client, group_context=group_context)
                    ))
                    self.ws_tasks.append(asyncio.create_task(
                        self._ping_loop(websocket, client, group_context=group_context)
                    ))
                pending = failed
                if self.ws_refresh_pending:
                    self.ws_refresh_pending = False
                    await self._reset_ws_connections_for_retry()
                    pending = self._ws_topic_groups()
                if pending:
                    attempt += 1
                    delay = min(
                        max(self.settings.ws_reconnect_seconds, 1.0) * (2 ** min(attempt - 1, 6)),
                        60.0,
                    )
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        continue

            if not self.stop_event.is_set() and self.ws_startup_retry_task is asyncio.current_task():
                LOGGER.info(
                    "ws_startup_retry_recovered",
                    extra={
                        "event_id": "ws_subscriptions",
                        "status": "AVAILABLE" if self.websockets else "NOT_AVAILABLE",
                        "connections": len(self.websockets),
                    },
                )
        finally:
            if self.ws_startup_retry_task is asyncio.current_task():
                self.ws_startup_retry_task = None

    async def _reset_ws_connections_for_retry(self) -> None:
        current = asyncio.current_task()
        owned = [task for task in self.ws_tasks if task is not current]
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
        for websocket in self.websockets:
            try:
                await websocket.close()
            except Exception:
                pass
        self.websockets.clear()
        self.ws_tasks = [current] if current is not None else []

    async def _restart_ws_connections(self, client: BitgetV3UtaRestClient) -> None:
        if self.ws_startup_retry_task is not None and not self.ws_startup_retry_task.done():
            self.ws_refresh_pending = True
            LOGGER.info(
                "ws_universe_refresh_deferred_during_startup_retry",
                extra={"event_id": "ws_subscriptions", "status": "DEGRADED"},
            )
            return
        for task in self.ws_tasks:
            task.cancel()
        if self.ws_tasks:
            await asyncio.gather(*self.ws_tasks, return_exceptions=True)
        for websocket in self.websockets:
            await websocket.close()
        self.ws_tasks.clear()
        self.websockets.clear()
        await self._start_ws_connections(client)

    def _latest_closed_open(self, symbol: str, interval: str):
        history = self.store.closed_klines.get(symbol, {}).get(interval, ())
        return max(
            (
                candle.bar_open_timestamp
                for candle in history
                if candle.is_closed and candle.status is DataStatus.AVAILABLE
            ),
            default=None,
        )

    def _scan_recovery_candidates(self) -> RecoveryScan:
        now = utc_now()
        candidates: list[RecoveryRange] = []
        scanned = 0
        for symbol in tuple(dict.fromkeys(self.selected_symbols)):
            for interval in INTERVALS:
                scanned += 1
                expected = expected_latest_closed_open(now, interval)
                latest = self._latest_closed_open(symbol, interval)
                if latest is not None and latest >= expected:
                    continue
                start = (
                    expected
                    if latest is None
                    else latest + timedelta(seconds=INTERVAL_SECONDS[interval])
                )
                candidates.append(RecoveryRange(symbol, interval, start, expected))
        return RecoveryScan(scanned_count=scanned, candidates=tuple(candidates))

    def _recovery_candidate_still_needed(self, candidate: RecoveryRange) -> bool:
        latest = self._latest_closed_open(candidate.symbol, candidate.interval)
        return latest is None or latest < candidate.end_open

    async def _admit_recovery_page(
        self,
        candidate: RecoveryRange,
        *,
        cursor: datetime,
        page_end: datetime,
        candles,
    ) -> list[Candle] | None:
        interval_seconds = INTERVAL_SECONDS[candidate.interval]
        expected_items = int((page_end - cursor).total_seconds() / interval_seconds) + 1
        request = make_work_request(
            phase=SourcePhase.PHASE1,
            source_id=SourceId.PHASE1_MARKET_DATA,
            work_class=WorkClass.HEAVY,
            estimated_items=expected_items,
            estimated_bytes=expected_items * 2_048,
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase1.closed_klines",
        )
        try:
            async with self.admission.admit(request):
                closed = [
                    candle
                    for candle in candles
                    if candle.is_closed
                    and candle.status is DataStatus.AVAILABLE
                    and cursor <= candle.bar_open_timestamp <= page_end
                ]
                observed = {candle.bar_open_timestamp for candle in closed}
                page_cursor = cursor
                while page_cursor <= page_end:
                    if page_cursor not in observed:
                        raise RuntimeError("Kline recovery page did not cover every expected closed bar")
                    page_cursor += timedelta(seconds=interval_seconds)
            return closed
        except AdmissionDeferred:
            return None

    async def _start_gap_recovery(self, client: BitgetV3UtaRestClient) -> None:
        async with self._gap_recovery_start_lock:
            if self.gap_recovery is not None:
                return

            async def recover_one(candidate: RecoveryRange):
                interval_seconds = INTERVAL_SECONDS[candidate.interval]
                latest = self._latest_closed_open(candidate.symbol, candidate.interval)
                cursor = candidate.start_open
                if latest is not None:
                    cursor = max(cursor, latest + timedelta(seconds=interval_seconds))
                page_limit = self.settings.kline_fetch_limit
                while cursor <= candidate.end_open:
                    page_end = min(
                        candidate.end_open,
                        cursor + timedelta(seconds=interval_seconds * (page_limit - 1)),
                    )
                    candles = await client.get_candles(
                        symbol=candidate.symbol,
                        interval=candidate.interval,
                        limit=page_limit,
                        start_time=cursor,
                        end_time=page_end,
                    )
                    closed = await self._admit_recovery_page(
                        candidate, cursor=cursor, page_end=page_end, candles=candles
                    )
                    if closed is None:
                        return
                    yield closed
                    cursor = page_end + timedelta(seconds=interval_seconds)

            def on_failure(symbol: str, interval: str, exc: Exception) -> None:
                mark_degraded(self.health, self.component, exc)

            coordinator = GapRecoveryCoordinator(
                recover_one,
                candidate_provider=self._scan_recovery_candidates,
                still_needed=self._recovery_candidate_still_needed,
                on_candles=lambda candles: self.store.add_closed_candles(list(candles)),
                concurrency=GLOBAL_RECOVERY_CONCURRENCY_LIMIT,
                max_work_items=max(1, self.settings.universe_limit * len(INTERVALS)),
                on_failure=on_failure,
            )
            await coordinator.start()
            self.gap_recovery = coordinator

    async def _recover_gaps(self, client: BitgetV3UtaRestClient) -> None:
        if self.gap_recovery is None:
            await self._start_gap_recovery(client)
        coordinator = self.gap_recovery
        if coordinator is None or not coordinator.request():
            if not self.stop_event.is_set():
                mark_degraded(self.health, self.component, "kline gap recovery scheduler unavailable")

    async def _kline_reconciliation_loop(self, client: BitgetV3UtaRestClient) -> None:
        async def reconcile():
            await self._recover_gaps(client)
        await PeriodicScheduler(30.0, reconcile).run(self.stop_event)

    async def _receive_loop(
        self,
        websocket: BitgetV3UtaWebSocket,
        client: BitgetV3UtaRestClient,
        *,
        group_context: str | None = None,
    ) -> None:
        generation = getattr(websocket, "connection_generation", None)
        group_context = group_context or _ws_group_context([])
        readiness_symbols = _readiness_ticker_symbols()
        watched_symbols = set(getattr(websocket, "ticker_symbols", ())).intersection(readiness_symbols)
        loop = asyncio.get_running_loop()
        ticker_watchdog = (
            TickerStreamWatchdog(
                watched_symbols,
                idle_timeout_seconds=TICKER_STREAM_IDLE_RECONNECT_SECONDS,
                started_at=loop.time(),
                max_source_age_seconds=PRICE_STAGE1_HARD_AGE_SECONDS,
            )
            if watched_symbols else None
        )
        if ticker_watchdog is not None:
            LOGGER.info(
                "ws_ticker_watchdog_configured symbols=%s idle_seconds=%.1f source_age_hard_seconds=%.1f",
                ",".join(sorted(watched_symbols)),
                ticker_watchdog.idle_timeout_seconds,
                ticker_watchdog.max_source_age_seconds,
            )
        next_idle_sample = loop.time()
        while not self.stop_event.is_set():
            phase = "receive"
            try:
                if generation is None:
                    receive = websocket.receive()
                else:
                    receive = websocket.receive(expected_generation=generation)
                if ticker_watchdog is None:
                    message = await receive
                else:
                    timeout = ticker_watchdog.seconds_until_stream_expiry(now=loop.time())
                    try:
                        # Keep each frame in the long-lived reader task. A
                        # child task per recv hands buffered frames through
                        # another scheduler cycle and can accumulate lag.
                        async with asyncio.timeout(timeout):
                            message = await receive
                    except asyncio.TimeoutError as exc:
                        stale = ticker_watchdog.expired_symbols(now=loop.time())
                        LOGGER.warning(
                            "ws_ticker_stream_idle_reconnecting symbols=%s idle_seconds=%.1f",
                            ",".join(stale),
                            ticker_watchdog.idle_timeout_seconds,
                        )
                        raise TimeoutError("required ticker stream idle") from exc
                phase = "process"
                self.events.append(message)
                for event in self.events.drain(max_items=64):
                    observed_at = utc_now()
                    event_arg = event.get("arg", {}) if isinstance(event, dict) else {}
                    event_symbol = str(event_arg.get("symbol") or "").upper() if isinstance(event_arg, dict) else ""
                    required_ticker = (
                        ticker_watchdog is not None
                        and event_arg.get("topic") == "ticker"
                        and event_symbol in watched_symbols
                    )
                    if required_ticker and not ticker_watchdog.observe(
                        event, now=loop.time(), wall_now=observed_at,
                    ):
                        LOGGER.debug("ws_ticker_source_stale_rejected symbol=%s", event_symbol)
                        continue
                    ingest_started = loop.time()
                    self.store.ingest(event, now=observed_at)
                    if required_ticker:
                        ticker = self.store.tickers.get(event_symbol)
                        if ticker is not None:
                            lag_ms = int((observed_at - ticker.exchange_timestamp).total_seconds() * 1000)
                            self._ticker_source_lag_ms_last = lag_ms
                            self._ticker_source_lag_ms_max = max(self._ticker_source_lag_ms_max, lag_ms)
                        self._ticker_ingest_duration_us_max = max(
                            self._ticker_ingest_duration_us_max,
                            int((loop.time() - ingest_started) * 1_000_000),
                        )
                        if loop.time() >= next_idle_sample:
                            self._ticker_idle_symbols_last_group = len(
                                ticker_watchdog.expired_symbols(now=loop.time())
                            )
                            next_idle_sample = loop.time() + min(1.0, ticker_watchdog.idle_timeout_seconds)
                # A hot socket can have frames buffered, making recv() complete
                # without yielding. Cooperate so other channel readers on this
                # event loop do not accumulate a transport backlog.
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                current_generation = getattr(websocket, "connection_generation", generation)
                if generation is not None and current_generation != generation:
                    generation = current_generation
                    continue
                self.ws_reconnect_count += 1
                normalized = normalize_error(exc)
                LOGGER.warning(
                    "ws_connection_failed loop=receive phase=%s %s generation=%s "
                    "error_type=%s code=%s error=%s",
                    phase,
                    group_context,
                    generation if generation is not None else "UNKNOWN",
                    normalized.exception_type,
                    normalized.code or "NONE",
                    normalized.safe_summary,
                )
                mark_degraded(self.health, self.component, exc)
                if self.stop_event.is_set():
                    return
                reconnect_started = loop.time()
                reconnect_phase = "connect"
                try:
                    await asyncio.sleep(self.settings.ws_reconnect_seconds)
                    if generation is None:
                        reconnected_generation = await websocket.reconnect()
                    else:
                        reconnected_generation = await websocket.reconnect(expected_generation=generation)
                        generation = reconnected_generation
                    LOGGER.info(
                        "ws_reconnect_succeeded loop=receive %s previous_generation=%s "
                        "generation=%s elapsed_ms=%.1f",
                        group_context,
                        current_generation if current_generation is not None else "UNKNOWN",
                        reconnected_generation,
                        (loop.time() - reconnect_started) * 1000,
                    )
                    if ticker_watchdog is not None:
                        ticker_watchdog.reset(now=loop.time())
                    if not getattr(websocket, "ticker_symbols", ()):
                        reconnect_phase = "gap_recovery"
                        await self._recover_gaps(client)
                except Exception as reconnect_exc:
                    normalized_reconnect = normalize_error(reconnect_exc)
                    failure_event = (
                        "ws_gap_recovery_failed"
                        if reconnect_phase == "gap_recovery"
                        else "ws_reconnect_failed"
                    )
                    LOGGER.warning(
                        "%s loop=receive phase=%s %s generation=%s "
                        "elapsed_ms=%.1f error_type=%s code=%s error=%s",
                        failure_event,
                        reconnect_phase,
                        group_context,
                        generation if generation is not None else "UNKNOWN",
                        (loop.time() - reconnect_started) * 1000,
                        normalized_reconnect.exception_type,
                        normalized_reconnect.code or "NONE",
                        normalized_reconnect.safe_summary,
                    )

    async def _persist_loop(self) -> None:
        scheduler = PeriodicScheduler(self.settings.ticker_persist_interval_seconds, self._persist_current_store)
        await scheduler.run(self.stop_event, run_immediately=False)

    async def _persist_current_store(self) -> None:
        if self.store.tickers:
            await self._persist_batch(self._store_batch())

    async def _persist_phase3_cycle(self) -> None:
        if self.phase3_runtime is None:
            return
        try:
            async with self.admission.admit(make_work_request(
                phase=SourcePhase.PHASE3,
                source_id=SourceId.PHASE3_FLOW_PROCESSING,
                work_class=WorkClass.HEAVY,
                estimated_items=max(1, self.settings.max_trade_queue_size),
                estimated_bytes=max(65_536, self.settings.max_trade_queue_size * 2_048),
                replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
                cancellation_owner=ProcessRole.COLLECTOR,
                stream_id="phase3.flow_windows",
            )):
                await self._persist_phase3_cycle_admitted()
        except AdmissionDeferred as exc:
            LOGGER.info("phase3_processing_deferred reason=%s", exc.reason.value)

    async def _persist_phase3_cycle_admitted(self) -> None:
        processed_at = utc_now()
        windows = await self.phase3_runtime.process_all_pending_async(
            watermark=processed_at,
            processed_at=processed_at,
        )
        if not windows:
            return
        for window in windows:
            self.phase3_history[(window.exchange, window.canonical_symbol)].append(window)
        cvd_points = [point for window in windows for point in self.phase3_cvd.ingest(window, processed_at=processed_at)]
        changed_history_keys = sorted({(window.exchange, window.canonical_symbol) for window in windows})
        history_rows = tuple(tuple(self.phase3_history[key]) for key in changed_history_keys)
        window_rows = tuple(windows)
        try:
            rollups, cross_snapshots = await asyncio.to_thread(
                _build_phase3_derived_outputs,
                self.phase3_rollup,
                history_rows,
                window_rows,
                processed_at,
                self.settings.phase3_min_directional_sources,
            )
            await asyncio.to_thread(
                _persist_phase3_outputs,
                self.settings,
                window_rows,
                tuple(rollups),
                tuple(cvd_points),
                tuple(cross_snapshots),
            )
            LOGGER.info(
                "phase3_flow_persisted",
                extra={
                    "event_id": "phase3_flow",
                    "status": "AVAILABLE",
                    "symbol": str(len({row.canonical_symbol for row in windows})),
                    "classification": {"minute_windows": len(windows), "rollups": len(rollups), "cvd": len(cvd_points)},
                },
            )
        except Exception as exc:
            mark_degraded(self.health, self.component, exc)
            LOGGER.warning("phase3_persistence_unavailable error=%s", normalize_error(exc).safe_summary)

    async def _ping_loop(
        self,
        websocket: BitgetV3UtaWebSocket,
        client: BitgetV3UtaRestClient | None = None,
        *,
        group_context: str | None = None,
    ) -> None:
        generation = getattr(websocket, "connection_generation", None)
        group_context = group_context or _ws_group_context([])
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                phase = "ping"
                try:
                    if generation is None:
                        await websocket.ping()
                    else:
                        new_generation = await websocket.ping(expected_generation=generation)
                        if new_generation != generation:
                            previous_generation = generation
                            generation = new_generation
                            LOGGER.info(
                                "ws_generation_advanced loop=ping %s previous_generation=%s "
                                "generation=%s",
                                group_context,
                                previous_generation,
                                generation,
                            )
                            if client is not None and not getattr(websocket, "ticker_symbols", ()):
                                phase = "gap_recovery"
                                await self._recover_gaps(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    normalized = normalize_error(exc)
                    LOGGER.warning(
                        "ws_connection_failed loop=ping phase=%s %s generation=%s "
                        "error_type=%s code=%s error=%s",
                        phase,
                        group_context,
                        generation if generation is not None else "UNKNOWN",
                        normalized.exception_type,
                        normalized.code or "NONE",
                        normalized.safe_summary,
                    )
                    mark_degraded(self.health, self.component, exc)
                    if self.stop_event.is_set():
                        return
                    reconnect_started = loop.time()
                    reconnect_phase = "connect"
                    previous_generation = generation
                    try:
                        if generation is None:
                            reconnected_generation = await websocket.reconnect()
                        else:
                            reconnected_generation = await websocket.reconnect(expected_generation=generation)
                            generation = reconnected_generation
                        LOGGER.info(
                            "ws_reconnect_succeeded loop=ping %s previous_generation=%s "
                            "generation=%s elapsed_ms=%.1f",
                            group_context,
                            previous_generation if previous_generation is not None else "UNKNOWN",
                            reconnected_generation,
                            (loop.time() - reconnect_started) * 1000,
                        )
                        if client is not None and not getattr(websocket, "ticker_symbols", ()):
                            reconnect_phase = "gap_recovery"
                            await self._recover_gaps(client)
                    except asyncio.CancelledError:
                        raise
                    except Exception as reconnect_exc:
                        normalized_reconnect = normalize_error(reconnect_exc)
                        failure_event = (
                            "ws_gap_recovery_failed"
                            if reconnect_phase == "gap_recovery"
                            else "ws_heartbeat_reconnect_failed"
                        )
                        LOGGER.warning(
                            "%s loop=ping phase=%s %s generation=%s "
                            "elapsed_ms=%.1f error_type=%s code=%s error=%s",
                            failure_event,
                            reconnect_phase,
                            group_context,
                            generation if generation is not None else "UNKNOWN",
                            (loop.time() - reconnect_started) * 1000,
                            normalized_reconnect.exception_type,
                            normalized_reconnect.code or "NONE",
                            normalized_reconnect.safe_summary,
                        )

    async def _universe_loop(self, client: BitgetV3UtaRestClient) -> None:
        async def refresh() -> None:
            try:
                batch = await MarketDataCollector(
                    client, max_symbols=self.settings.universe_limit, kline_limit=self.settings.kline_fetch_limit
                ).collect_once()
                self.selected_symbols = batch.selected_symbols
                await self._load_phase3_stage1_ab_symbols()
                selected = set(self.selected_symbols)
                self.store.tickers = {ticker.symbol: ticker for ticker in batch.tickers if ticker.symbol in selected}
                for intervals in batch.candles_by_symbol.values():
                    self.store.add_closed_candles([candle for values in intervals.values() for candle in values])
                await self._restart_ws_connections(client)
                await self._persist_batch(batch)
            except Exception as exc:
                mark_degraded(self.health, self.component, exc)
                LOGGER.warning("universe_refresh_failed error=%s", normalize_error(exc).safe_summary)

        await PeriodicScheduler(self.settings.universe_interval_seconds, refresh).run(self.stop_event)

    async def run(self) -> None:
        diagnostics_task = None
        if self.diagnostics_path is not None:
            diagnostics_task = asyncio.create_task(
                self._diagnostics_loop(), name="acceptance-diagnostics-sampler"
            )
        if self.settings.phase7_enabled or self.settings.bitget_sbe_flow_enabled:
            self.phase7_task = asyncio.create_task(
                self.phase7_runtime.run(self.stop_event),
                name="phase7-collector-supervisor",
            )
        if self.phase8_runtime is not None:
            self.phase8_task = asyncio.create_task(
                self.phase8_runtime.run(self.stop_event),
                name="phase8-collector-supervisor",
            )
        try:
            await self._run_market_data()
        finally:
            if diagnostics_task is not None:
                diagnostics_task.cancel()
                await asyncio.gather(diagnostics_task, return_exceptions=True)
            phase7_task = self.phase7_task
            if phase7_task is not None:
                if not phase7_task.done():
                    phase7_task.cancel()
                await asyncio.gather(phase7_task, return_exceptions=True)
            self.phase7_task = None
            phase8_task = self.phase8_task
            if phase8_task is not None:
                if not phase8_task.done():
                    phase8_task.cancel()
                await asyncio.gather(phase8_task, return_exceptions=True)
            self.phase8_task = None
            drained = await self.admission.shutdown(timeout_seconds=10.0)
            if not drained:
                LOGGER.error("collector_admission_shutdown_drain_timeout")
            db_drained = await asyncio.to_thread(self.db_admission.close, timeout_seconds=10.0)
            if not db_drained:
                LOGGER.error("collector_db_admission_shutdown_drain_timeout")

    async def _run_market_data(self) -> None:
        write_health_file(self.component, "RUNNING")
        async with BitgetV3UtaRestClient(self.settings) as client:
            if self.phase4_runtime is not None and not self._phase4_repository_initialized:
                try:
                    initialize_phase4_repository(self.settings)
                    self._phase4_repository_initialized = True
                except Exception as exc:
                    mark_degraded(self.health, self.component, exc)
                    LOGGER.warning("phase4_repository_initialization_unavailable error=%s", normalize_error(exc).safe_summary)
            bootstrap = await await_or_stop(
                MarketDataCollector(
                    client, max_symbols=self.settings.universe_limit, kline_limit=self.settings.kline_fetch_limit
                ).collect_once(),
                self.stop_event,
            )
            if bootstrap is None:
                write_health_file(self.component, "STOPPED")
                return
            self.selected_symbols = bootstrap.selected_symbols
            await self._load_phase3_stage1_ab_symbols()
            selected = set(self.selected_symbols)
            self.store.tickers = {ticker.symbol: ticker for ticker in bootstrap.tickers if ticker.symbol in selected}
            for intervals in bootstrap.candles_by_symbol.values():
                self.store.add_closed_candles([candle for values in intervals.values() for candle in values])
            await self._persist_batch(bootstrap)
            await self._hydrate_phase3_state()
            # Do not retain the full REST bootstrap graph for the lifetime of
            # the service; the bounded canonical store is the runtime state.
            del bootstrap

            persist_task: asyncio.Task[None] | None = None
            universe_task: asyncio.Task[None] | None = None
            kline_reconciliation_task: asyncio.Task[None] | None = None
            try:
                await await_or_stop(self._start_gap_recovery(client), self.stop_event)
                if self.stop_event.is_set():
                    return
                await await_or_stop(self._start_ws_connections(client), self.stop_event)
                if self.stop_event.is_set():
                    return
                persist_task = asyncio.create_task(self._persist_loop())
                universe_task = asyncio.create_task(self._universe_loop(client))
                # Healthy UTA sockets may push only the currently forming bar.
                # Reuse bounded REST gap recovery to confirm newly closed OHLCV.
                kline_reconciliation_task = asyncio.create_task(
                    self._kline_reconciliation_loop(client), name="closed-kline-reconciliation"
                )
                if self.phase3_runtime is not None and self.phase3_runner is not None:
                    await self.phase3_runtime.start()
                    self.phase3_task = asyncio.create_task(
                        self.phase3_runner.run_dynamic(
                            self.phase3_symbols_by_exchange,
                            stop_event=self.stop_event,
                            refresh_seconds=min(
                                self.settings.universe_interval_seconds,
                                self.settings.trade_subscription_cooldown_seconds,
                            ),
                        )
                    )
                    self.phase3_persist_task = asyncio.create_task(
                        PeriodicScheduler(5.0, self._persist_phase3_cycle).run(
                            self.stop_event, run_immediately=False
                        )
                    )
                if self.phase4_runtime is not None:
                    await self.phase4_runtime.start()
                if self.phase6_runtime is not None:
                    self.phase6_task = asyncio.create_task(
                        self.phase6_runtime.run(self.stop_event),
                        name="phase6-collector-supervisor",
                    )
                await self.stop_event.wait()
            finally:
                phase3_tasks = tuple(
                    task for task in (self.phase3_task, self.phase3_persist_task) if task is not None
                )
                phase6_tasks = (self.phase6_task,) if self.phase6_task is not None else ()
                managed_tasks = tuple(
                    task
                    for task in (
                        *self.ws_tasks, persist_task, universe_task, kline_reconciliation_task, *phase3_tasks, *phase6_tasks,
                    )
                    if task is not None
                )
                for task in managed_tasks:
                    if task is not self.phase7_task:
                        task.cancel()
                if managed_tasks:
                    await asyncio.gather(*managed_tasks, return_exceptions=True)
                if self.gap_recovery is not None:
                    await self.gap_recovery.stop()
                    self.gap_recovery = None
                if self.phase4_runtime is not None:
                    await self.phase4_runtime.stop()
                if self.phase3_runtime is not None:
                    await self.phase3_runtime.stop()
                self.phase3_task = None
                self.phase3_persist_task = None
                self.phase6_task = None
                close_results = await asyncio.gather(
                    *(websocket.close() for websocket in self.websockets),
                    return_exceptions=True,
                )
                for result in close_results:
                    if isinstance(result, BaseException):
                        LOGGER.warning(
                            "public_websocket_close_failed",
                            extra={
                                "event_id": "websocket_close_failed",
                                "exception_type": type(result).__name__,
                            },
                        )
                self.ws_tasks.clear()
                self.websockets.clear()
                write_health_file(self.component, "STOPPED")


async def run_service(settings: Settings | None = None, *, stop_event: asyncio.Event | None = None) -> None:
    await CollectorService(settings or Settings.from_env(), stop_event=stop_event).run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one REST cycle and exit")
    args = parser.parse_args(argv)
    configure_logging()
    settings = Settings.from_env()
    if args.once:
        asyncio.run(run_once(settings))
        return
    stop_event = asyncio.Event()

    def stop(*_: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, stop)
        except (NotImplementedError, RuntimeError):
            pass
    asyncio.run(run_service(settings, stop_event=stop_event))


if __name__ == "__main__":
    main()
