import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from types import SimpleNamespace

import pytest

from quant_phase1.config import Settings
from quant_phase1.entrypoints.collector import CollectorService
from quant_phase4.aggregation import LiquidationWindow
from quant_phase4.contracts import (
    BasisObservation,
    BasisType,
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LiquidationSide,
    LongShortMetricType,
    LongShortObservation,
    LongShortPopulationSemantics,
    QuantityUnit,
    SourceGranularity,
)
from quant_phase4.health import Phase4HealthRegistry, Phase4HealthState
from quant_phase4.adapters.base import AdapterTimeoutError
from quant_phase4.runtime import (
    HYDRATED_HIGH_WATERMARK_REASON,
    Phase4Runtime,
    _estimated_event_bytes,
)
from quant_phase4.runtime import Phase4PublicLiquidationRunner
from quant_data_layer.admission import ReplayClass
from quant_data_layer.observability import WorkClass


NOW = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)


def settings(**overrides):
    values = {"TRADING_MODE": "paper", **overrides}
    return Settings.from_env(values)


def liquidation(event_id="event-1"):
    return {
        "arg": {"instType": "usdt-futures", "topic": "liquidation", "symbol": "BTCUSDT"},
        "data": [{"id": event_id, "symbol": "BTCUSDT", "side": "buy", "price": "100", "amount": "1", "ts": "1789952400000"}],
    }


class FakeLiquidationAdapter:
    exchange = "fake"
    public_ws_url = "wss://public.example/liquidation"

    def parse_ws_message(self, payload, received_at):
        row = CanonicalLiquidation(
            event_id=payload["data"][0].get("id", "event-1"),
            exchange="fake",
            exchange_symbol="BTCUSDT",
            canonical_symbol="BTC-USDT-PERP",
            event_timestamp=NOW,
            received_at=received_at,
            processed_at=received_at,
            side=LiquidationSide.LIQUIDATED_LONG,
            raw_side="buy",
            raw_side_semantics="PUBLIC",
            price=100,
            raw_quantity=1,
            quantity_unit=QuantityUnit.QUOTE_COIN,
            quantity_base=None,
            notional_usd=None,
            source_endpoint=self.public_ws_url,
            source_channel="liquidation",
            source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
            coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
            status=DataStatus.AVAILABLE,
            raw_reference=None,
            raw_payload={"private": "must not persist"},
        )
        return (row,)


class FakeRestAdapter:
    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def fetch(self, *args, **kwargs):
        self.calls += 1
        return self.row


def long_short(status=DataStatus.AVAILABLE):
    return LongShortObservation(
        exchange="fake",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        population_semantics=LongShortPopulationSemantics.HOLDER_COUNT_RATIO,
        period="5m",
        long_value=0.6 if status is DataStatus.AVAILABLE else None,
        short_value=0.4 if status is DataStatus.AVAILABLE else None,
        ratio=1.5 if status is DataStatus.AVAILABLE else None,
        exchange_timestamp=NOW,
        fetched_at=NOW,
        received_at=NOW,
        processed_at=NOW,
        source_endpoint="/public",
        status=status,
        raw_reference=None,
        raw_payload=None,
    )


def basis(status=DataStatus.AVAILABLE):
    return BasisObservation(
        exchange="fake",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        basis_type=BasisType.MARK_INDEX,
        perpetual_price=101 if status is DataStatus.AVAILABLE else None,
        reference_price=100 if status is DataStatus.AVAILABLE else None,
        absolute_basis=1 if status is DataStatus.AVAILABLE else None,
        basis_bps=100 if status is DataStatus.AVAILABLE else None,
        basis_pct=1 if status is DataStatus.AVAILABLE else None,
        exchange_timestamp=NOW,
        fetched_at=NOW,
        received_at=NOW,
        processed_at=NOW,
        max_timestamp_skew=timedelta(seconds=5),
        timestamp_skew=timedelta(0),
        source_endpoint="/public",
        status=status,
        raw_reference=None,
        raw_payload=None,
    )


def test_phase4_health_transitions_are_component_level_and_recoverable():
    registry = Phase4HealthRegistry()
    registry.mark_started(NOW)
    registry.mark_disconnected("liquidation", NOW, reason="WS_DISCONNECTED")
    assert registry.snapshot("liquidation").state is Phase4HealthState.DEGRADED
    registry.mark_reconnected("liquidation", NOW)
    assert registry.snapshot("liquidation").state is Phase4HealthState.RECOVERED
    registry.mark_stale("long_short", NOW, reason="STALE_REST")
    registry.mark_error("basis", NOW, reason="DB_OUTAGE")
    assert set(registry.snapshots()) == {"liquidation", "long_short", "basis"}


def test_runtime_is_disabled_by_default_and_collector_does_not_import_or_start_phase4():
    service = CollectorService(settings())
    assert service.phase4_runtime is None
    assert service.phase4_task is None


def test_enabled_collector_supplies_repository_provider_for_runtime_persistence():
    service = CollectorService(settings(PHASE4_ENABLED="1"))
    assert service.phase4_runtime is not None
    assert service.phase4_runtime.repository is None
    assert service.phase4_runtime.repository_provider is not None


def test_phase4_event_persistence_scope_does_not_run_migrations_per_event(monkeypatch):
    """Startup checks schema while event persistence does not run migrations."""

    import quant_phase1.db as db
    import quant_phase1.entrypoints.collector as collector_module

    migration_calls = []
    readiness_checks = []
    monkeypatch.setattr(
        db, "apply_migrations", lambda *args, **kwargs: migration_calls.append(args) or []
    )
    monkeypatch.setattr(
        db, "assert_schema_ready",
        lambda connection, *, required_version: readiness_checks.append(
            (connection, required_version)
        ),
    )

    class Cursor:
        def executemany(self, *_args):
            return None

    class Connection:
        scopes = 0

        def __enter__(self):
            type(self).scopes += 1
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    connection = Connection()
    fake_psycopg = SimpleNamespace(connect=lambda _dsn: connection)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    assert collector_module.initialize_phase4_repository(settings(PHASE4_ENABLED="1")) == ()

    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1"),
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
        repository_provider=lambda: collector_module._phase4_repository_scope(settings(PHASE4_ENABLED="1")),
    )
    assert runtime.ingest_liquidation("fake", liquidation("event-1"), NOW) == 1
    assert runtime.ingest_liquidation("fake", liquidation("event-2"), NOW + timedelta(seconds=1)) == 1

    assert connection.scopes == 3
    assert migration_calls == []
    assert readiness_checks == [(connection, "009_phase4_metrics.sql")]

    calls = []

    class Repository:
        def insert_long_short(self, rows):
            calls.append(("long_short", tuple(rows)))

        def insert_basis(self, rows):
            calls.append(("basis", tuple(rows)))

        def insert_liquidation_windows(self, rows):
            calls.append(("windows", tuple(rows)))

    repository = Repository()

    @contextmanager
    def provider():
        calls.append("open")
        yield repository
        calls.append("close")

    async def scenario():
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            long_short_adapters={"fake": FakeRestAdapter(long_short())},
            basis_adapters={"fake": FakeRestAdapter(basis())},
            repository_provider=provider,
        )
        await runtime.start()
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        runtime._persist_windows((SimpleNamespace(
            exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m", window_open=NOW,
        ),))
        await runtime.stop()

    asyncio.run(scenario())
    assert calls[0] == "open"
    assert {item[0] for item in calls if isinstance(item, tuple)} == {"long_short", "basis", "windows"}
    assert calls[-1] == "close"


def test_invalid_phase4_enable_value_is_rejected():
    with pytest.raises(ValueError):
        settings(PHASE4_ENABLED="maybe")


def test_runtime_enforces_bounded_ingestion_and_records_backpressure():
    async def scenario():
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1", PHASE4_QUEUE_CAPACITY="1"),
            liquidation_adapters={"fake": FakeLiquidationAdapter()},
        )
        await runtime.start()
        assert runtime.ingest_liquidation("fake", liquidation(), NOW) == 1
        assert runtime.ingest_liquidation("fake", liquidation("event-2"), NOW) == 0
        assert runtime.queue_depth("fake", "BTC-USDT-PERP") == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        await runtime.stop()

    asyncio.run(scenario())


def test_runtime_reconnect_marks_gap_and_resubscribes_without_fabricating_events():
    async def scenario():
        persisted = []
        repository = SimpleNamespace(insert_liquidation_events=lambda rows: persisted.extend(rows))
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            liquidation_adapters={"fake": FakeLiquidationAdapter()},
            repository=repository,
        )
        await runtime.start()
        assert runtime.ingest_liquidation("fake", liquidation(), NOW) == 1
        await runtime.handle_disconnect("fake", NOW)
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        assert runtime.gap_count == 1
        await runtime.handle_reconnect("fake", NOW)
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        assert runtime.resubscribe_count == 1
        assert runtime.resubscriptions == ("fake:BTC-USDT-PERP",)
        assert runtime.pending_event_count == 1
        assert runtime.ingest_liquidation("fake", liquidation("fresh-event"), NOW + timedelta(seconds=1)) == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.RECOVERED
        assert [row.event_id for row in persisted] == ["event-1", "fresh-event"]
        await runtime.stop()

    asyncio.run(scenario())


def test_liquidation_gap_remains_explicit_partial_after_stream_recovers():
    async def scenario():
        persisted = []
        rows = {}

        class Repository:
            def insert_liquidation_events(self, events):
                persisted.extend(events)
                return len(events)

            def upsert_system_health(self, component, status, checked_at, details):
                rows[component] = (status.value, checked_at, details)

        repository = Repository()
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            liquidation_adapters={"fake": FakeLiquidationAdapter()},
            repository=repository,
        )
        await runtime.start()
        gap_at = NOW + timedelta(seconds=1)
        await runtime.handle_disconnect("fake", gap_at)
        await runtime.handle_reconnect("fake", gap_at + timedelta(seconds=1))
        assert runtime.ingest_liquidation(
            "fake", liquidation("post-gap"), gap_at + timedelta(seconds=2),
        ) == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.RECOVERED

        runtime.persist_health(repository)
        await runtime.stop()

        status, checked_at, details = rows["phase4-liquidation"]
        assert status == "NOT_AVAILABLE"
        assert checked_at == gap_at + timedelta(seconds=2)
        assert details["runtime_state"] == "RECOVERED"
        assert details["phase4_status"] == "PARTIAL"
        assert details["data_quality"] == "PARTIAL"
        assert details["gap_detected"] is True
        assert details["gap_count"] >= 1
        assert details["gap_reason"] == "LIQUIDATION_GAP_NO_BACKFILL"
        assert details["gap_watermark_received_at"] == gap_at.isoformat()
        assert details["gap_watermark_event_timestamp"] == gap_at.isoformat()
        assert [event.event_id for event in persisted] == ["post-gap"]

    asyncio.run(scenario())


def test_pre_gap_pending_event_retry_cannot_recover_after_disconnect_watermark():
    class Repository:
        def __init__(self):
            self.fail = True
            self.persisted = []

        def insert_liquidation_events(self, rows):
            if self.fail:
                raise ConnectionError("db down")
            self.persisted.extend(rows)

    async def scenario():
        repository = Repository()
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            liquidation_adapters={"fake": FakeLiquidationAdapter()},
            repository=repository,
        )
        assert runtime.ingest_liquidation("fake", liquidation("pre-gap"), NOW) == 1
        await runtime.handle_disconnect("fake", NOW + timedelta(seconds=1))
        repository.fail = False

        runtime._retry_event_recovery(NOW + timedelta(seconds=2))
        assert runtime.pending_event_count == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        assert [row.event_id for row in repository.persisted] == ["pre-gap"]

        assert runtime.ingest_liquidation("fake", liquidation("post-gap"), NOW + timedelta(seconds=3)) == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.RECOVERED

    asyncio.run(scenario())


def test_runtime_rest_stale_and_database_outage_recover_without_crashing():
    async def scenario():
        repository = SimpleNamespace(
            insert_long_short=lambda rows: (_ for _ in ()).throw(ConnectionError("db down")),
            insert_basis=lambda rows: 1,
        )
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            long_short_adapters={"fake": FakeRestAdapter(long_short(DataStatus.STALE))},
            basis_adapters={"fake": FakeRestAdapter(basis())},
            repository=repository,
        )
        await runtime.start()
        result = await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        assert result.long_short[0].status is DataStatus.STALE
        assert runtime.health_snapshot("long_short").state is Phase4HealthState.DEGRADED
        runtime.repository = SimpleNamespace(insert_long_short=lambda rows: 1, insert_basis=lambda rows: 1)
        runtime.long_short_adapters["fake"].row = long_short()
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        assert runtime.health_snapshot("long_short").state is Phase4HealthState.RECOVERED
        await runtime.stop()

    asyncio.run(scenario())


def test_runtime_start_stop_are_idempotent_and_tasks_are_bounded():
    async def scenario():
        runtime = Phase4Runtime(settings(PHASE4_ENABLED="1"), liquidation_adapters={"fake": FakeLiquidationAdapter()})
        await runtime.start()
        tasks = runtime.tasks
        await runtime.start()
        assert runtime.tasks is tasks
        await runtime.stop()
        await runtime.stop()
        assert runtime.tasks == ()

    asyncio.run(scenario())


def test_runtime_does_not_persist_the_same_window_again_after_restart():
    class Repository:
        def __init__(self):
            self.calls = []

        def insert_liquidation_windows(self, rows):
            self.calls.append(tuple(rows))
            return len(tuple(rows))

    async def scenario():
        repository = Repository()
        runtime = Phase4Runtime(settings(PHASE4_ENABLED="1"), repository=repository)
        row = SimpleNamespace(
            exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m", window_open=NOW,
        )
        runtime._persist_windows((row,))
        await runtime.start()
        await runtime.stop()
        await runtime.start()
        runtime._persist_windows((row,))
        await runtime.stop()
        assert len(repository.calls) == 1

    asyncio.run(scenario())


def test_runtime_cleanup_uses_configured_phase4_retention_and_provider():
    calls = []

    @contextmanager
    def provider():
        yield SimpleNamespace(cleanup=lambda retention: calls.append(retention))

    runtime = Phase4Runtime(settings(PHASE4_ENABLED="1", PHASE4_CROSS_EXCHANGE_RETENTION_DAYS="12"), repository_provider=provider)
    runtime._cleanup()
    assert len(calls) == 1
    assert calls[0].cross_exchange_days == 12


def test_engine_phase4_enrichment_is_context_only():
    from quant_phase1.stage1 import Stage1Result
    from quant_phase4.cross_exchange import build_phase4_context
    from quant_phase4.enrichment import enrich_stage1_phase4

    result = Stage1Result("BTCUSDT", "A", "test", DataStatus.AVAILABLE, (), {}, None)
    context = build_phase4_context((), (long_short(DataStatus.NOT_AVAILABLE),), (basis(),), NOW)
    enriched = enrich_stage1_phase4(result, context, NOW)
    assert enriched.phase1_result == result
    assert enriched.classification == result.classification
    assert enriched.phase1_result.status is result.status
    assert enriched.context_only is True


def test_phase4_runtime_source_is_public_only_and_has_no_execution_routes():
    source = __import__("pathlib").Path("src/quant_phase4/runtime.py").read_text(encoding="utf-8")
    for forbidden in ("private", "api_key", "order", "position", "executor"):
        assert forbidden not in source.lower()


def test_phase4_transport_subscribes_both_public_liquidation_streams_and_reconnects():
    class Socket:
        def __init__(self, messages):
            self.messages = iter(messages)
            self.sent = []

        async def __aenter__(self):
            sockets.append(self)
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            value = next(self.messages)
            if isinstance(value, BaseException):
                raise value
            return value

    sockets = []
    messages = [
        '{"arg":{"instType":"usdt-futures","topic":"liquidation","symbol":"BTCUSDT"},"data":[{"symbol":"BTCUSDT","side":"buy","price":"100","amount":"2","ts":1789992000000}]}',
        '{"topic":"allLiquidation.BTCUSDT","type":"snapshot","ts":1789992000100,"data":[{"T":1789992000000,"s":"BTCUSDT","S":"Buy","v":"2","p":"99"}]}',
        ConnectionError("disconnect"),
    ]

    def connect_factory(*args, **kwargs):
        return Socket(messages)

    async def scenario():
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1", UNIVERSE_LIMIT="1"),
            symbols_provider=lambda: ("BTCUSDT",),
            connect_factory=connect_factory,
            reconnect_seconds=0,
        )
        await runtime.start()
        await asyncio.sleep(0.05)
        await runtime.stop()
        return runtime

    runtime = asyncio.run(scenario())
    sent = [payload for socket in sockets for payload in socket.sent]
    assert len(sockets) >= 2
    assert any('"topic":"liquidation"' in payload for payload in sent)
    assert any('allLiquidation.BTCUSDT' in payload for payload in sent)
    assert runtime.gap_count >= 1
    assert runtime.resubscribe_count >= 1


def test_planned_worker_stop_does_not_fabricate_disconnect_gap():
    class RuntimeStub:
        liquidation_adapters = {"fake": FakeLiquidationAdapter()}

        def __init__(self):
            self.disconnects = []
            self.liquidation_adapters["fake"].subscription = lambda symbol: {"symbol": symbol}

        def ingest_liquidation(self, *args):
            return 0

        async def handle_disconnect(self, *args):
            self.disconnects.append(args)

        async def handle_reconnect(self, *args):
            raise AssertionError("planned stop must not reconnect")

    stop_event = asyncio.Event()

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, payload):
            return None

        async def recv(self):
            stop_event.set()
            raise ConnectionError("socket closed by planned refresh")

    async def scenario():
        runtime = RuntimeStub()
        runner = Phase4PublicLiquidationRunner(runtime, connect_factory=lambda *args, **kwargs: Socket())
        await runner.run_exchange("fake", ("BTCUSDT",), stop_event=stop_event)
        return runtime

    runtime = asyncio.run(scenario())
    assert runtime.disconnects == []


@pytest.mark.asyncio
async def test_liquidation_worker_processes_bounded_batch_inside_collector_admission():
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1"),
        liquidation_adapters={},
        long_short_adapters={},
        basis_adapters={},
    )
    runtime._running = True
    state = {"active": False, "requests": [], "inside_recovery": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    runtime.admission = AdmissionSpy()

    def stop_after_one_batch(_now):
        state["inside_recovery"].append(state["active"])
        runtime._running = False

    runtime._retry_event_recovery = stop_after_one_batch
    runtime._retry_window_persistence = lambda _now: None
    await runtime._liquidation_worker()

    assert state["inside_recovery"] == [True]
    assert state["requests"][0].work_class is WorkClass.HEAVY
    assert state["requests"][0].replay_class is ReplayClass.CANONICAL_UNRECOVERABLE
    assert state["requests"][0].estimated_items <= 256


def test_new_runtime_hydrates_persisted_minutes_for_rollup_continuity():
    rows = []
    for index in range(5):
        rows.append(LiquidationWindow(
            exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
            window_open=NOW + timedelta(minutes=index), window_close=NOW + timedelta(minutes=index + 1),
            observed_event_count=1, observed_liquidated_long_count=1,
            observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
            largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
            source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
            coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
            status=DataStatus.AVAILABLE, reason=None, processed_at=NOW + timedelta(minutes=index + 1),
        ))

    class Repository:
        def __init__(self):
            self.calls = 0

        def load_recent_liquidation_windows(self, **kwargs):
            self.calls += 1
            assert kwargs["per_key_limit"] == 240
            return tuple(rows)

    async def scenario():
        repository = Repository()
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"), repository=repository,
            liquidation_adapters={"fake": FakeLiquidationAdapter()}, symbols_provider=lambda: (),
        )
        await runtime.start()
        await runtime.stop()
        return runtime, repository

    runtime, repository = asyncio.run(scenario())
    assert repository.calls == 1
    assert len(runtime.windows) == 5
    assert runtime._builders[("fake", "BTC-USDT-PERP")].state_count == 5
    rollups = runtime._rollups_for(runtime.windows, NOW + timedelta(minutes=5))
    assert len(rollups) == 1
    assert rollups[0].timeframe == "5m"


def test_new_runtime_hydrates_repository_minutes_and_persists_each_startup_rollup_once():
    aligned = NOW.replace(hour=4, minute=0, second=0, microsecond=0)
    minutes = tuple(LiquidationWindow(
        exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
        window_open=aligned + timedelta(minutes=index),
        window_close=aligned + timedelta(minutes=index + 1),
        observed_event_count=1, observed_liquidated_long_count=1,
        observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
        largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE, reason=None,
        processed_at=aligned + timedelta(minutes=index + 1),
    ) for index in range(240))

    class Repository:
        def __init__(self):
            self.persisted = {(row.exchange, row.canonical_symbol, row.timeframe, row.window_open): row for row in minutes}
            self.writes = []

        def load_recent_liquidation_windows(self, **kwargs):
            assert kwargs["per_key_limit"] == 240
            return minutes

        def load_recent_liquidation_rollup_keys(self, **kwargs):
            return tuple(key for key in self.persisted if key[2] != "1m")

        def insert_liquidation_windows(self, rows):
            for row in rows:
                key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
                self.writes.append(row)
                self.persisted[key] = row

    async def scenario():
        repository = Repository()
        for _ in range(2):
            runtime = Phase4Runtime(
                settings(PHASE4_ENABLED="1"), repository=repository,
                liquidation_adapters={"fake": FakeLiquidationAdapter()}, symbols_provider=lambda: (),
            )
            await runtime.start()
            assert len(runtime.windows) == 240
            await runtime.stop()
        return repository

    repository = asyncio.run(scenario())
    assert {row.timeframe for row in repository.writes} == {"5m", "15m", "1H", "4H"}
    assert len(repository.writes) == 48 + 16 + 4 + 1
    assert len({(row.exchange, row.canonical_symbol, row.timeframe, row.window_open) for row in repository.writes}) == len(repository.writes)


def test_large_restart_hydration_upserts_all_rollups_without_queue_backpressure():
    """Durable upsert, rather than the tiny key cache, protects restart scale."""

    base = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = []
    for exchange in ("bitget", "bybit"):
        for symbol_index in range(200):
            symbol = f"COIN{symbol_index}-USDT-PERP"
            for minute in range(240):
                opened = base + timedelta(minutes=minute)
                minutes.append(LiquidationWindow(
                    exchange=exchange, canonical_symbol=symbol, timeframe="1m",
                    window_open=opened, window_close=opened + timedelta(minutes=1),
                    observed_event_count=1, observed_liquidated_long_count=1,
                    observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
                    largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
                    source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
                    coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
                    status=DataStatus.AVAILABLE, reason=None, processed_at=opened + timedelta(minutes=1),
                ))

    class Repository:
        def __init__(self):
            self.persisted = {
                (row.exchange, row.canonical_symbol, row.timeframe, row.window_open): row
                for row in minutes
            }
            self.batches = []
            self.events = []

        def load_recent_liquidation_windows(self, **kwargs):
            assert kwargs["per_key_limit"] == 240
            assert kwargs["max_rows"] >= len(minutes)
            return tuple(minutes)

        def load_recent_liquidation_rollup_keys(self, **kwargs):
            assert kwargs["max_rows"] >= 27_600
            return tuple(key for key in self.persisted if key[2] != "1m")

        def insert_liquidation_windows(self, rows):
            rows = tuple(rows)
            self.batches.append(rows)
            self.persisted.update({
                (row.exchange, row.canonical_symbol, row.timeframe, row.window_open): row
                for row in rows
            })

        def insert_liquidation_events(self, rows):
            self.events.extend(rows)

    async def scenario():
        repository = Repository()
        runtime_settings = settings(
            PHASE4_ENABLED="1", PHASE4_QUEUE_CAPACITY="2", UNIVERSE_LIMIT="200"
        )
        adapters = {
            "bitget": FakeLiquidationAdapter(),
            "bybit": FakeLiquidationAdapter(),
        }
        for restart_index in range(2):
            runtime = Phase4Runtime(
                runtime_settings, repository=repository, liquidation_adapters=adapters,
                symbols_provider=lambda: (),
            )
            await runtime.start()
            assert not runtime._pending_window_rows
            assert runtime.health_snapshot("liquidation").dropped_count == 0
            if restart_index == 0:
                stream = ("bitget", "COIN0-USDT-PERP")
                runtime._builders[stream].compact_finalized(0)
                durable_key = ("bitget", "COIN0-USDT-PERP", "1m", base)
                late = replace(
                    FakeLiquidationAdapter().parse_ws_message(liquidation("late-duplicate"), NOW)[0],
                    exchange="bitget", canonical_symbol="COIN0-USDT-PERP",
                    exchange_symbol="COIN0USDT", event_timestamp=base,
                )

                class LateAdapter(FakeLiquidationAdapter):
                    def parse_ws_message(self, payload, received_at):
                        return (replace(late, received_at=received_at, processed_at=received_at),)

                runtime.liquidation_adapters["bitget"] = LateAdapter()
                assert runtime.ingest_liquidation("bitget", liquidation("late-duplicate"), NOW) == 0
                assert repository.persisted[durable_key] is minutes[0]
                assert repository.events == []
                assert not runtime._reject_hydrated_closed_minute(
                    replace(late, event_id="new-minute", event_timestamp=base + timedelta(minutes=240))
                )
            await runtime.stop()
        return repository

    repository = asyncio.run(scenario())
    expected_rollups = 400 * (48 + 16 + 4 + 1)
    assert len(repository.persisted) == len(minutes) + expected_rollups
    assert len({
        (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
        for row in repository.persisted.values()
    }) == len(repository.persisted)
    assert max(len(batch) for batch in repository.batches) <= 256


def test_hydrated_high_watermark_rejects_retained_and_evicted_late_events_before_persistence():
    class Repository:
        def __init__(self):
            self.events = []

        def insert_liquidation_events(self, rows):
            self.events.extend(rows)

    repository = Repository()
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1"),
        repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    runtime.health.mark_started(NOW)
    stream = ("fake", "BTC-USDT-PERP")
    runtime._queue_for(*stream, NOW)
    hydrated = LiquidationWindow(
        exchange=stream[0], canonical_symbol=stream[1], timeframe="1m",
        window_open=NOW, window_close=NOW + timedelta(minutes=1),
        observed_event_count=1, observed_liquidated_long_count=1,
        observed_liquidated_short_count=0, observed_notional_usd=None,
        largest_observed_notional_usd=None, source_exchange_count=1,
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE, reason=None, processed_at=NOW + timedelta(minutes=1),
    )
    runtime._builders[stream].hydrate((hydrated,))
    runtime._hydrated_window_high_watermarks[stream] = NOW

    assert runtime.ingest_liquidation("fake", liquidation("late-retained"), NOW) == 0
    runtime._builders[stream].compact_finalized(0)
    assert runtime.ingest_liquidation("fake", liquidation("late-evicted"), NOW) == 0

    assert repository.events == []
    assert runtime.queue_depth(*stream) == 0
    snapshot = runtime.health_snapshot("liquidation")
    assert snapshot.state is Phase4HealthState.RUNNING
    assert snapshot.reason == HYDRATED_HIGH_WATERMARK_REASON
    assert snapshot.rejected_count == 2
    assert snapshot.dropped_count == 0


def test_rollup_overflow_marker_rebuilds_all_rollups_after_restart_from_durable_minutes():
    class Repository:
        def __init__(self, minutes):
            self.minutes = tuple(minutes)
            self.rollups = {}
            self.markers = {}
            self.allow_rollups = False

        def load_recent_liquidation_windows(self, **kwargs):
            limit = kwargs["max_rows"]
            return self.minutes[-limit:]

        def load_recent_liquidation_rollup_keys(self, **kwargs):
            return tuple(self.rollups)

        def load_liquidation_windows_range(self, exchange, canonical_symbol, *, window_open_from, window_open_to, limit):
            return tuple(
                row for row in self.minutes
                if row.exchange == exchange and row.canonical_symbol == canonical_symbol
                and window_open_from <= row.window_open < window_open_to
            )[:limit]

        def load_liquidation_rollup_rebuild_markers(self):
            return tuple((*scope, marker[0], marker[1]) for scope, marker in self.markers.items())

        def upsert_liquidation_rollup_rebuild_marker(
            self, exchange, canonical_symbol, timeframe, earliest_window_open, latest_window_open, checked_at
        ):
            scope = (exchange, canonical_symbol, timeframe)
            previous = self.markers.get(scope)
            self.markers[scope] = (
                min(earliest_window_open, previous[0]) if previous else earliest_window_open,
                max(latest_window_open, previous[1]) if previous else latest_window_open,
            )

        def clear_liquidation_rollup_rebuild_marker(self, exchange, canonical_symbol, timeframe, checked_at):
            self.markers.pop((exchange, canonical_symbol, timeframe), None)

        def insert_liquidation_windows(self, rows):
            rows = tuple(rows)
            if any(row.timeframe != "1m" for row in rows) and not self.allow_rollups:
                raise ConnectionError("rollup database outage")
            for row in rows:
                key = (row.exchange, row.canonical_symbol, row.timeframe, row.window_open)
                if row.timeframe == "1m":
                    continue
                self.rollups[key] = row

    aligned = NOW.replace(minute=0, second=0, microsecond=0)
    minutes = tuple(
        LiquidationWindow(
            exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
            window_open=aligned + timedelta(minutes=index),
            window_close=aligned + timedelta(minutes=index + 1),
            observed_event_count=1, observed_liquidated_long_count=1,
            observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
            largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
            source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
            coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
            status=DataStatus.AVAILABLE, reason=None,
            processed_at=aligned + timedelta(minutes=index + 1),
        )
        for index in range(240)
    )
    repository = Repository(minutes)
    runtime_settings = settings(
        PHASE4_ENABLED="1", PHASE4_ROLLUP_WINDOW_BUDGET="1", PHASE4_EVENT_BUDGET="1",
    )
    first = Phase4Runtime(
        runtime_settings, repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    first.windows = minutes
    rollups = first._rollups_for(minutes, aligned + timedelta(hours=4))
    first._persist_windows((*minutes, *rollups))

    assert first._rollup_rebuild_overflow_count == len(rollups) - 1
    assert repository.markers
    assert not repository.rollups

    repository.allow_rollups = True
    second = Phase4Runtime(
        runtime_settings, repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    second._hydrate_persisted_windows(NOW)
    for _ in range(100):
        if not second._rollup_rebuild_needed:
            break
        second._retry_window_persistence(NOW)

    expected = len(rollups)
    assert len(repository.rollups) == expected, sorted(
        set((row.exchange, row.canonical_symbol, row.timeframe, row.window_open) for row in rollups)
        - set(repository.rollups)
    )
    assert not second._rollup_rebuild_needed
    assert not repository.markers


def test_global_rollup_marker_advances_scope_and_timeframe_chunks_across_bounded_retries():
    class Repository:
        def __init__(self, minutes):
            self.minutes = tuple(minutes)
            self.rollups = {}
            self.range_starts = []
            self.clear_count = 0

        def iter_liquidation_window_scopes(self, *, page_size):
            assert page_size == 128
            for scope in sorted({(row.exchange, row.canonical_symbol) for row in self.minutes}):
                yield scope

        def load_liquidation_windows_range(
            self, exchange, canonical_symbol, *, window_open_from, window_open_to, limit
        ):
            self.range_starts.append((exchange, canonical_symbol, window_open_from))
            return tuple(
                row for row in self.minutes
                if row.exchange == exchange and row.canonical_symbol == canonical_symbol
                and window_open_from <= row.window_open < window_open_to
            )[:limit]

        def insert_liquidation_windows(self, rows):
            for row in rows:
                if row.timeframe != "1m":
                    self.rollups[(row.exchange, row.canonical_symbol, row.timeframe, row.window_open)] = row

        def upsert_liquidation_rollup_rebuild_marker(self, *args, **kwargs):
            return None

        def clear_liquidation_rollup_rebuild_marker(self, exchange, canonical_symbol, timeframe, checked_at):
            if (exchange, canonical_symbol, timeframe) == ("*", "*", "*"):
                self.clear_count += 1

    aligned = NOW.replace(minute=0, second=0, microsecond=0)
    minutes = []
    for exchange, symbol in (("fake-a", "BTC-USDT-PERP"), ("fake-b", "ETH-USDT-PERP")):
        minutes.extend(
            LiquidationWindow(
                exchange=exchange, canonical_symbol=symbol, timeframe="1m",
                window_open=aligned + timedelta(minutes=index),
                window_close=aligned + timedelta(minutes=index + 1),
                observed_event_count=1, observed_liquidated_long_count=1,
                observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
                largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
                source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
                coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
                status=DataStatus.AVAILABLE, reason=None,
                processed_at=aligned + timedelta(minutes=index + 1),
            )
            for index in range(10)
        )
    repository = Repository(minutes)
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1", PHASE4_ROLLUP_WINDOW_BUDGET="1"),
        repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    runtime._rollup_retry_budget = 1
    runtime._rollup_rebuild_marker_budget = 1
    runtime.windows = tuple(minutes)
    rollups = runtime._rollups_for(tuple(minutes), aligned + timedelta(hours=4))
    fake_a_rollup = next(row for row in rollups if row.exchange == "fake-a")
    fake_b_rollups = tuple(row for row in rollups if row.exchange == "fake-b")
    runtime._mark_rollup_rebuild_needed(fake_a_rollup, NOW)
    for row in fake_b_rollups:
        if row.timeframe != "4H":
            runtime._mark_rollup_rebuild_needed(row, NOW)
    runtime._rollup_rebuild_needed.clear()

    observed_progress = []
    for _ in range(100):
        marker = runtime._rollup_rebuild_all
        if marker is None:
            break
        observed_progress.append((marker.cursor_exchange, marker.cursor_symbol, marker.cursor_timeframe, marker.earliest_window_open))
        runtime._retry_rollup_rebuilds(NOW)

    assert runtime._rollup_rebuild_all is None
    assert repository.clear_count == 1
    assert len(repository.rollups) == len(rollups), sorted(
        set((row.exchange, row.canonical_symbol, row.timeframe, row.window_open) for row in rollups)
        - set(repository.rollups)
    )
    fake_a_starts = [start for exchange, symbol, start in repository.range_starts if exchange == "fake-a"]
    assert fake_a_starts[:2] == [aligned, aligned + timedelta(minutes=5)]
    assert observed_progress[1][:3] == ("fake-a", "BTC-USDT-PERP", "5m")
    assert observed_progress[1][3:4] == (aligned + timedelta(minutes=5),)
    assert observed_progress[2][:3] == ("fake-a", "BTC-USDT-PERP", "15m")
    assert observed_progress[2][3:4] == (aligned,)
    assert any(item[:3] == ("fake-a", "BTC-USDT-PERP", "15m") for item in observed_progress)
    assert any(item[:3] == ("fake-b", "ETH-USDT-PERP", "5m") for item in observed_progress)


def test_runtime_rest_cycle_consumes_only_bounded_prefix_of_lazy_symbols():
    seen = []

    class Adapter(FakeRestAdapter):
        async def fetch(self, symbol, *args, **kwargs):
            seen.append(symbol)
            return long_short()

    async def scenario():
        runtime = Phase4Runtime(
            replace(settings(PHASE4_ENABLED="1"), universe_limit=2),
            long_short_adapters={"fake": Adapter(long_short())},
            basis_adapters={},
        )
        await runtime.start()
        await runtime.run_rest_cycle((f"COIN{index}USDT" for index in range(1000)), NOW)
        await runtime.stop()

    asyncio.run(scenario())
    assert seen == ["COIN0USDT", "COIN1USDT"]


def test_successful_fresh_rest_data_recovers_stale_health():
    async def scenario():
        adapter = FakeRestAdapter(long_short(DataStatus.STALE))
        runtime = Phase4Runtime(settings(PHASE4_ENABLED="1"), long_short_adapters={"fake": adapter})
        await runtime.start()
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        assert runtime.health_snapshot("long_short").state is Phase4HealthState.STALE
        adapter.row = long_short(DataStatus.AVAILABLE)
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        assert runtime.health_snapshot("long_short").state is Phase4HealthState.RECOVERED
        await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [DataStatus.STALE, DataStatus.NOT_AVAILABLE, DataStatus.ERROR])
def test_non_available_rest_rows_never_recover_or_lose_health_reason(status):
    async def scenario():
        row = long_short(status)
        persisted = []

        @contextmanager
        def provider():
            yield SimpleNamespace(insert_long_short=lambda rows: persisted.extend(rows))

        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            long_short_adapters={"fake": FakeRestAdapter(row)},
            basis_adapters={},
            repository_provider=provider,
        )
        await runtime.start()
        runtime.health.mark_stale("long_short", NOW, reason="prior-stale")
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        snapshot = runtime.health_snapshot("long_short")
        assert snapshot.state is not Phase4HealthState.RECOVERED
        expected_reason = {
            DataStatus.STALE: "prior-stale",
            DataStatus.NOT_AVAILABLE: "prior-stale",
            DataStatus.ERROR: "REST_ERROR",
        }[status]
        assert snapshot.reason == expected_reason
        assert persisted == [row]
        await runtime.stop()

    asyncio.run(scenario())


def test_long_short_health_persists_safe_provider_error_taxonomy_and_timestamps():
    writes = []

    class FailingAdapter:
        exchange = "bitget"
        path = "/api/v2/mix/market/long-short"
        endpoint_id = "bitget_classic_v2_long_short"

        async def fetch(self, *_args, **_kwargs):
            raise AdapterTimeoutError("public request timed out", endpoint=self.path)

    class HealthRepository:
        def upsert_system_health(self, component, status, checked_at, details):
            writes.append((component, status, checked_at, dict(details)))

    async def scenario():
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            long_short_adapters={"bitget": FailingAdapter()},
            basis_adapters={},
        )
        await runtime.start()
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        runtime.persist_health(HealthRepository())
        await runtime.stop()

    asyncio.run(scenario())
    details = next(row[3] for row in writes if row[0] == "phase4-long_short")

    assert details["error_category"] == "TIMEOUT"
    assert details["error_type"] == "AdapterTimeoutError"
    assert details["provider"] == "bitget"
    assert details["endpoint"] == "bitget_classic_v2_long_short"
    assert details["consecutive_failures"] == 1
    assert details["last_attempt_at"] == NOW.isoformat()
    assert details["last_error_at"] == NOW.isoformat()
    assert "public request timed out" not in repr(details)


def test_long_short_schema_error_remains_internal_contract_failure_with_safe_field_evidence():
    from quant_phase4.adapters.base import AdapterSchemaError

    writes = []

    class SchemaFailingAdapter:
        exchange = "bitget"
        path = "/api/v2/mix/market/long-short"
        endpoint_id = "bitget_classic_v2_long_short"

        async def fetch(self, *_args, **_kwargs):
            raise AdapterSchemaError(
                "provider response did not match contract",
                field="data",
                stage="response_validation",
            )

    class HealthRepository:
        def upsert_system_health(self, component, status, checked_at, details):
            writes.append((component, status, checked_at, dict(details)))

    async def scenario():
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            long_short_adapters={"bitget": SchemaFailingAdapter()},
            basis_adapters={},
        )
        await runtime.start()
        await runtime.run_rest_cycle(("BTCUSDT",), NOW)
        runtime.persist_health(HealthRepository())
        await runtime.stop()

    asyncio.run(scenario())
    details = next(row[3] for row in writes if row[0] == "phase4-long_short")

    assert details["phase4_status"] == "ERROR"
    assert details["error_category"] == "SCHEMA_ERROR"
    assert details["error_type"] == "AdapterSchemaError"
    assert details["schema_stage"] == "response_validation"
    assert details["schema_error_summary"] == "data"
    assert details["provider"] == "bitget"
    assert details["endpoint"] == "bitget_classic_v2_long_short"
    assert details["last_attempt_at"] == NOW.isoformat()
    assert details["last_error_at"] == NOW.isoformat()
    assert "provider response did not match contract" not in repr(details)


def test_persisted_window_recent_keys_remain_bounded():
    async def scenario():
        runtime = Phase4Runtime(settings(PHASE4_ENABLED="1", PHASE4_QUEUE_CAPACITY="2"))
        for index in range(20):
            row = SimpleNamespace(
                exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
                window_open=NOW + timedelta(minutes=index),
            )
            runtime._remember_window_key((row.exchange, row.canonical_symbol, row.timeframe, row.window_open), NOW)
        assert len(runtime._persisted_window_keys) <= 2

    asyncio.run(scenario())


def test_liquidation_event_db_outage_retains_bounded_recovery_event_and_retries():
    class Repository:
        def __init__(self):
            self.fail = True
            self.events = []

        def insert_liquidation_events(self, rows):
            if self.fail:
                raise ConnectionError("db down")
            self.events.extend(rows)

    repository = Repository()
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1", PHASE4_EVENT_BUDGET="1"),
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
        repository=repository,
    )

    assert runtime.ingest_liquidation("fake", liquidation(), NOW) == 1
    assert runtime.pending_event_count == 1
    assert runtime.health_snapshot("liquidation").reason == "DB_OUTAGE"

    repository.fail = False
    runtime._retry_event_recovery(NOW + timedelta(seconds=1))
    assert runtime.pending_event_count == 1  # now admitted to the processing queue
    assert runtime.queue_depth("fake", "BTC-USDT-PERP") == 1
    assert [row.event_id for row in repository.events] == ["event-1"]


def test_failed_window_write_is_retryable_and_acknowledges_only_after_success():
    class Repository:
        def __init__(self):
            self.fail = True
            self.calls = []

        def insert_liquidation_windows(self, rows):
            if self.fail:
                raise ConnectionError("db down")
            self.calls.append(tuple(rows))

    repository = Repository()
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1"),
        repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    key = ("fake", "BTC-USDT-PERP")
    from quant_phase4.aggregation import LiquidationWindowBuilder

    builder = LiquidationWindowBuilder(max_open_windows=4)
    runtime._builders[key] = builder
    builder.add(runtime.liquidation_adapters["fake"].parse_ws_message(liquidation(), NOW)[0])
    rows = builder.finalize_pending(NOW + timedelta(minutes=2), processed_at=NOW + timedelta(minutes=2), limit=1)

    runtime._persist_windows(rows)
    assert builder.pending_count == 1
    assert runtime._pending_window_rows

    repository.fail = False
    runtime._retry_window_persistence(NOW + timedelta(seconds=1))
    assert builder.pending_count == 0
    assert not runtime._pending_window_rows
    assert len(repository.calls) == 1


def test_tiny_window_retry_budget_flushes_minutes_and_all_rollups_without_loss():
    class Repository:
        def __init__(self):
            self.rows = []

        def insert_liquidation_windows(self, rows):
            self.rows.extend(rows)

    repository = Repository()
    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1", PHASE4_EVENT_BUDGET="1"),
        repository=repository,
        liquidation_adapters={"fake": FakeLiquidationAdapter()},
    )
    runtime._rollup_retry_budget = 1
    aligned = NOW.replace(minute=0, second=0, microsecond=0)
    minutes = tuple(
        LiquidationWindow(
            exchange="fake", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
            window_open=aligned + timedelta(minutes=index),
            window_close=aligned + timedelta(minutes=index + 1),
            observed_event_count=1, observed_liquidated_long_count=1,
            observed_liquidated_short_count=0, observed_notional_usd=Decimal("100"),
            largest_observed_notional_usd=Decimal("100"), source_exchange_count=1,
            source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
            coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
            status=DataStatus.AVAILABLE, reason=None,
            processed_at=aligned + timedelta(minutes=index + 1),
        )
        for index in range(240)
    )
    runtime.windows = minutes
    rollups = runtime._rollups_for(minutes, aligned + timedelta(hours=4))
    runtime._persist_windows((*minutes, *rollups))

    assert len(repository.rows) == len(minutes) + len(rollups)
    assert {row.timeframe for row in repository.rows} == {"1m", "5m", "15m", "1H", "4H"}
    assert not runtime._pending_window_rows
    assert not runtime._pending_rollup_rows
    assert runtime._window_persistence_overflow_count == 0
    assert runtime.health_snapshot("liquidation").dropped_count == 0


def test_disconnect_marks_existing_builder_gap_and_reconnect_stays_degraded_until_fresh_persisted_event():
    class Repository:
        def __init__(self):
            self.fail = False

        def insert_liquidation_events(self, rows):
            if self.fail:
                raise ConnectionError("db down")

    async def scenario():
        repository = Repository()
        runtime = Phase4Runtime(
            settings(PHASE4_ENABLED="1"),
            liquidation_adapters={"fake": FakeLiquidationAdapter()},
            repository=repository,
        )
        assert runtime.ingest_liquidation("fake", liquidation(), NOW) == 1
        await runtime.handle_disconnect("fake", NOW + timedelta(seconds=1))
        builder = runtime._builders[("fake", "BTC-USDT-PERP")]
        builder.add(runtime.liquidation_adapters["fake"].parse_ws_message(liquidation("queued"), NOW)[0])
        gap_window = builder.finalize(NOW + timedelta(minutes=2), processed_at=NOW + timedelta(minutes=2))[0]
        assert gap_window.status is DataStatus.STALE
        assert gap_window.reason == "LIQUIDATION_GAP_NO_BACKFILL"

        await runtime.handle_reconnect("fake", NOW + timedelta(seconds=2))
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        repository.fail = True
        assert runtime.ingest_liquidation("fake", liquidation("fresh-event"), NOW + timedelta(seconds=3)) == 1
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.DEGRADED
        repository.fail = False
        runtime._retry_event_recovery(NOW + timedelta(seconds=4))
        assert runtime.health_snapshot("liquidation").state is Phase4HealthState.RECOVERED

    asyncio.run(scenario())


def test_runtime_persists_required_liquidation_rollups_from_finalized_minutes():
    class Repository:
        def __init__(self):
            self.rows = []

        def insert_liquidation_windows(self, rows):
            self.rows.extend(rows)

    repository = Repository()
    runtime = Phase4Runtime(settings(PHASE4_ENABLED="1"), repository=repository)
    aligned = NOW.replace(minute=0, second=0, microsecond=0)
    minutes = []
    for index in range(240):
        opened = aligned + timedelta(minutes=index)
        minutes.append(LiquidationWindow(
            exchange="bybit", canonical_symbol="BTC-USDT-PERP", timeframe="1m",
            window_open=opened, window_close=opened + timedelta(minutes=1),
            observed_event_count=1, observed_liquidated_long_count=1,
            observed_liquidated_short_count=0, observed_notional_usd=100,
            largest_observed_notional_usd=100, source_exchange_count=1,
            source_granularity=SourceGranularity.ALL_LIQUIDATIONS_STREAM,
            coverage_semantics=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
            status=DataStatus.AVAILABLE, reason=None, processed_at=opened + timedelta(minutes=1),
        ))
    runtime.windows = tuple(minutes)
    rollups = runtime._rollups_for(minutes, aligned + timedelta(hours=4))
    runtime._persist_windows((*minutes, *rollups))
    while runtime._pending_window_rows:
        runtime._retry_window_persistence(aligned + timedelta(hours=4))

    assert {row.timeframe for row in repository.rows} == {"1m", "5m", "15m", "1H", "4H"}
    assert len([row for row in repository.rows if row.timeframe == "5m"]) == 48
    assert len([row for row in repository.rows if row.timeframe == "15m"]) == 16
    assert len([row for row in repository.rows if row.timeframe == "1H"]) == 4
    assert len([row for row in repository.rows if row.timeframe == "4H"]) == 1


def test_global_event_budget_admits_both_exchanges_without_unbounded_queue_growth():
    class Adapter(FakeLiquidationAdapter):
        def __init__(self, exchange):
            self.exchange = exchange
            self.public_ws_url = f"wss://{exchange}.example/liquidation"

        def parse_ws_message(self, payload, received_at):
            row = super().parse_ws_message(payload, received_at)
            return (replace(row[0], exchange=self.exchange, source_endpoint=self.public_ws_url),)

    runtime = Phase4Runtime(
        settings(PHASE4_ENABLED="1", UNIVERSE_LIMIT="1", PHASE4_EVENT_BUDGET="2"),
        liquidation_adapters={"bitget": Adapter("bitget"), "bybit": Adapter("bybit")},
    )
    assert runtime.ingest_liquidation("bitget", liquidation("bitget-event"), NOW) == 1
    assert runtime.ingest_liquidation("bybit", liquidation("bybit-event"), NOW) == 1
    assert runtime.queue_depth("bitget", "BTC-USDT-PERP") == 1
    assert runtime.queue_depth("bybit", "BTC-USDT-PERP") == 1
    assert runtime._queued_event_count == 2
    assert runtime._queued_event_count <= runtime._event_budget


def test_global_event_bytes_budget_applies_across_exchanges():
    class Adapter(FakeLiquidationAdapter):
        def __init__(self, exchange):
            self.exchange = exchange
            self.public_ws_url = f"wss://{exchange}.example/liquidation"

        def parse_ws_message(self, payload, received_at):
            row = super().parse_ws_message(payload, received_at)
            return (replace(row[0], exchange=self.exchange, source_endpoint=self.public_ws_url),)

    probe = replace(
        Adapter("bitget").parse_ws_message(liquidation("bitget-event"), NOW)[0], exchange="bitget",
        source_endpoint="wss://bitget.example/liquidation",
    )
    one_event_bytes = _estimated_event_bytes(probe)
    runtime = Phase4Runtime(
        settings(
            PHASE4_ENABLED="1", UNIVERSE_LIMIT="1", PHASE4_EVENT_BUDGET="10",
            PHASE4_EVENT_BYTES_BUDGET=str(one_event_bytes),
        ),
        liquidation_adapters={"bitget": Adapter("bitget"), "bybit": Adapter("bybit")},
    )
    assert runtime.ingest_liquidation("bitget", liquidation("bitget-event"), NOW) == 1
    assert runtime.ingest_liquidation("bybit", liquidation("bybit-event"), NOW) == 0
    assert runtime._queued_event_bytes <= runtime._event_bytes_budget


def test_repeated_dual_exchange_disconnects_bound_global_gap_builder_state_and_keep_health_visible():
    runtime = Phase4Runtime(
        settings(
            PHASE4_ENABLED="1", UNIVERSE_LIMIT="200", PHASE4_BUILDER_WINDOW_BUDGET="7",
        ),
        liquidation_adapters={
            "bitget": SimpleNamespace(public_ws_url="wss://bitget.example/liquidation"),
            "bybit": SimpleNamespace(public_ws_url="wss://bybit.example/liquidation"),
        },
    )
    for exchange in ("bitget", "bybit"):
        for index in range(200):
            assert runtime._queue_for(exchange, f"COIN{index}-USDT-PERP", NOW) is not None

    async def scenario():
        for cycle in range(5):
            moment = NOW + timedelta(minutes=cycle)
            await runtime.handle_disconnect("bitget", moment)
            await runtime.handle_disconnect("bybit", moment)

    asyncio.run(scenario())
    assert runtime._builder_window_count() <= runtime._builder_window_budget
    assert all(queue.gap_state is not None and queue.gap_state.status is DataStatus.STALE for queue in runtime._queues.values())
    snapshot = runtime.health_snapshot("liquidation")
    assert snapshot.state is Phase4HealthState.DEGRADED
    assert snapshot.gap_count == 10
