import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
import pytest

from quant_phase1.config import Settings
from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    ObservationKind,
    OptionInstrument,
    OptionMarketObservation,
    OptionMetricValue,
    loads_decimal_json,
)
from quant_phase8.adapters.deribit_ws import (
    DeribitPublicWebSocketClient,
    parse_instrument_creation_notification,
)
from quant_data_layer.admission import AdmissionDeferred, AdmissionReason, ReplayClass
from quant_data_layer.observability import WorkClass


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)


def _runtime_class():
    from quant_phase8.runtime import Phase8CollectorRuntime

    return Phase8CollectorRuntime


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"), parse_float=str)


def _markprice_observation():
    return OptionMarketObservation(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-25SEP26-100000-C",
        underlying="BTC",
        observation_kind=ObservationKind.WS_MARKPRICE_SNAPSHOT,
        metrics={"mark_iv": OptionMetricValue(metric="mark_iv", value=Decimal("0.5"), processed_at=T0)},
        exchange_timestamp=T0,
        fetched_at=None,
        received_at=T0,
        processed_at=T0,
        status=DataStatus.AVAILABLE,
    )


def _phase8_settings(**overrides):
    env = {
        "TRADING_MODE": "paper",
        "PHASE8_OPTIONS_ENABLED": "1",
        "PHASE8_OPTIONS_INSTRUMENT_REFRESH_SECONDS": "3600",
        "PHASE8_OPTIONS_CHAIN_SNAPSHOT_INTERVAL_SECONDS": "3600",
        "PHASE8_OPTIONS_MARKPRICE_SNAPSHOT_INTERVAL_SECONDS": "3600",
        "PHASE8_OPTIONS_TICKER_SNAPSHOT_INTERVAL_SECONDS": "3600",
        "PHASE8_OPTIONS_CONTEXT_INTERVAL_SECONDS": "3600",
        "PHASE8_OPTIONS_RETENTION_ENFORCEMENT": "0",
    }
    env.update(overrides)
    return Phase8Settings.from_env(env)


def test_phase8_health_persistence_uses_system_health_supported_status_and_keeps_phase_status(monkeypatch):
    from quant_phase1 import repositories
    from quant_phase1.contracts import DataStatus as CoreDataStatus
    from quant_phase8.runtime import _PostgresPersistence

    writes = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeRepository:
        def __init__(self, _connection):
            pass

        def upsert_system_health(self, component, status, checked_at, details):
            writes.append((component, status, checked_at, dict(details)))

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda _dsn: FakeConnection())
    monkeypatch.setattr(repositories, "Phase1Repository", FakeRepository)
    persistence = _PostgresPersistence(
        Settings.from_env({"POSTGRES_DSN": "postgresql://unused/quant"}),
        _phase8_settings(),
    )

    persistence.set_health(
        "INITIALIZING",
        {"configured": True, "phase8_status": "NOT_AVAILABLE", "data_quality": "PARTIAL"},
        T0,
    )

    assert writes == [(
        "quant-phase8-options",
        CoreDataStatus.NOT_AVAILABLE,
        T0,
        {"configured": True, "phase8_status": "NOT_AVAILABLE", "data_quality": "PARTIAL"},
    )]


@pytest.mark.asyncio
async def test_phase8_context_reconciliation_uses_replaceable_collector_admission(monkeypatch):
    from quant_phase8 import runtime as phase8_runtime_module

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    state = {"active": False, "requests": [], "calculated": [], "persisted": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Persistence:
        def persist_context(self, snapshot):
            state["persisted"].append((snapshot, state["active"]))

    runtime.admission = AdmissionSpy()
    runtime._persistence = Persistence()

    def calculate(inputs, **_kwargs):
        state["calculated"].append((inputs.underlying, state["active"]))
        return SimpleNamespace(
            underlying=inputs.underlying, context_timestamp=T0, status=DataStatus.AVAILABLE,
        )

    monkeypatch.setattr(phase8_runtime_module, "calculate_options_context", calculate)
    await runtime._persist_contexts()

    assert state["calculated"] == [("BTC", True), ("ETH", True)]
    assert [active for _, active in state["persisted"]] == [True, True]
    assert all(request.work_class is WorkClass.HEAVY for request in state["requests"])
    assert all(request.replay_class is ReplayClass.DERIVED_REPLACEABLE for request in state["requests"])


@pytest.mark.asyncio
async def test_phase8_catalog_rest_stays_outside_admission_but_reconciliation_is_inside(monkeypatch):
    from quant_phase8 import runtime as phase8_runtime_module

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    state = {"active": False, "rest_active": [], "contract_active": [], "persist_active": [], "requests": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Rest:
        async def call(self, method, params):
            state["rest_active"].append(state["active"])
            if method == "public/get_index_price_names":
                return ["BTC_USD", "ETH_USD"]
            return [params["currency"]]

    records = tuple(SimpleNamespace(symbol=f"{currency}-C", underlying=currency) for currency in ("BTC", "ETH"))
    runtime.admission = AdmissionSpy()
    runtime._rest = Rest()

    def parse_indexes(_payload):
        state["contract_active"].append(state["active"])
        return frozenset({"BTC_USD", "ETH_USD"})

    def parse_instruments(payload, **_kwargs):
        state["contract_active"].append(state["active"])
        return tuple(item for item in records if item.underlying == payload[0])

    def validate(_instruments, _settings):
        state["contract_active"].append(state["active"])
        return SimpleNamespace(status=DataStatus.AVAILABLE, records=records)

    async def persist(method, *_args, **_kwargs):
        state["persist_active"].append((method, state["active"]))

    monkeypatch.setattr(phase8_runtime_module, "parse_index_price_names_response", parse_indexes)
    monkeypatch.setattr(phase8_runtime_module, "parse_instruments_response", parse_instruments)
    monkeypatch.setattr(phase8_runtime_module, "validate_full_chain_snapshot", validate)
    monkeypatch.setattr(runtime, "_db", persist)
    await runtime._refresh_catalog()

    assert state["rest_active"] == [False, False, False]
    assert state["contract_active"] == [True, True, True, True]
    assert all(active for _, active in state["persist_active"])
    assert state["requests"][0].replay_class is ReplayClass.RECOVERABLE_REPLAYABLE
    assert state["requests"][0].estimated_items == 4096


@pytest.mark.asyncio
async def test_phase8_summary_rest_stays_outside_admission_but_snapshot_persistence_is_inside(monkeypatch):
    from quant_phase8 import runtime as phase8_runtime_module

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    state = {"active": False, "rest_active": [], "parser_active": [], "persist_active": [], "requests": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Rest:
        async def call(self, _method, params):
            state["rest_active"].append(state["active"])
            return {"currency": params["currency"]}

    observation = SimpleNamespace(metrics={})
    runtime.admission = AdmissionSpy()
    runtime._rest = Rest()

    def parse_summary(payload, **_kwargs):
        state["parser_active"].append(state["active"])
        return (observation,)

    async def persist(method, *_args, **_kwargs):
        state["persist_active"].append((method, state["active"]))

    monkeypatch.setattr(phase8_runtime_module, "parse_book_summary_response", parse_summary)
    monkeypatch.setattr(runtime, "_db", persist)
    await runtime._refresh_summaries()

    assert state["rest_active"] == [False, False]
    assert state["parser_active"] == [True, True]
    assert [active for _, active in state["persist_active"]] == [True, True]
    assert state["requests"][0].replay_class is ReplayClass.DERIVED_REPLACEABLE


@pytest.mark.asyncio
async def test_phase8_replaceable_cycle_admission_deferral_preserves_prior_snapshot_and_marks_skip():
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    runtime._summary_fetched_at = {"BTC": T0, "ETH": T0}
    runtime._clock = lambda: T0 + timedelta(minutes=1)
    prior = {"BTC": ("existing-summary",)}
    runtime._summary_observations = prior
    health_writes = []

    async def write_health():
        health_writes.append(runtime._health_details())

    async def deferred_cycle():
        raise AdmissionDeferred(AdmissionReason.CAPACITY)

    runtime._write_health = write_health
    await runtime._run_cycle("chain", deferred_cycle)

    assert runtime._summary_observations is prior
    assert runtime.state == "DEGRADED"
    assert runtime.reason == "REPLACEABLE_CYCLE_SKIPPED:CAPACITY"
    assert runtime._health_details()["skipped_cycles"] == {"chain": 1}
    assert health_writes[-1]["last_skipped_cycle"] == "chain"


@pytest.mark.asyncio
async def test_phase8_markprice_and_ticker_snapshot_writes_are_classified_as_replaceable(monkeypatch):
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    requests = []
    persisted = []

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            requests.append(request)
            yield None

    async def persist(method, rows):
        persisted.append((method, tuple(rows)))

    observation = _markprice_observation()
    runtime.admission = AdmissionSpy()
    runtime._db = persist
    runtime._websocket = SimpleNamespace(
        markprice_state={"BTC": {"mark_iv": observation}},
        ready_ticker_symbols=(),
        ticker_state={},
    )
    await runtime._persist_markprice_snapshot()
    runtime._ticker_observations = lambda: (observation,)
    await runtime._persist_ticker_snapshot()

    assert [request.stream_id for request in requests] == [
        "phase8.markprice_latest",
        "phase8.bounded_ticker_state",
    ]
    assert all(request.replay_class is ReplayClass.DERIVED_REPLACEABLE for request in requests)
    assert [method for method, _ in persisted] == ["persist_observations", "persist_observations"]


@pytest.mark.asyncio
async def test_phase8_live_markprice_admission_defer_skips_persistence_and_records_reason():
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    persisted = []

    class FullAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            raise AdmissionDeferred(AdmissionReason.CAPACITY)
            yield None

    async def persist(*args, **kwargs):
        persisted.append((args, kwargs))

    runtime.admission = FullAdmission()
    runtime._db = persist
    runtime._websocket = SimpleNamespace(ready=True)
    await runtime._on_markprice_batch((_markprice_observation(),))

    assert persisted == []
    assert runtime.state == "DEGRADED"
    assert runtime.reason == "REPLACEABLE_CYCLE_SKIPPED:CAPACITY"
    assert runtime._health_details()["skipped_cycles"] == {"markprice": 1}


@pytest.mark.asyncio
async def test_phase8_lifecycle_persistence_uses_replayable_admission_before_recording():
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    active = False
    requests = []
    writes = []

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            nonlocal active
            requests.append(request)
            active = True
            try:
                yield None
            finally:
                active = False

    async def persist(method, rows):
        writes.append((method, tuple(rows), active))

    runtime.admission = AdmissionSpy()
    runtime._db = persist
    event = SimpleNamespace(received_at=T0)
    await runtime._on_lifecycle(None, event)

    assert requests[0].stream_id == "phase8.option_lifecycle"
    assert requests[0].replay_class is ReplayClass.RECOVERABLE_REPLAYABLE
    assert writes == [("append_instrument_events", (event,), True)]


@pytest.mark.asyncio
async def test_phase8_lifecycle_admission_defer_does_not_mutate_or_persist_state():
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"}),
        phase8_settings=_phase8_settings(),
    )
    original_catalog = runtime.catalog
    writes = []

    class FullAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            raise AdmissionDeferred(AdmissionReason.CAPACITY)
            yield None

    async def persist(*args, **kwargs):
        writes.append((args, kwargs))

    runtime.admission = FullAdmission()
    runtime._db = persist
    event = SimpleNamespace(received_at=T0)
    await runtime._on_lifecycle(None, event)

    assert runtime.catalog is original_catalog
    assert runtime._lifecycle_received_at is None
    assert writes == []
    assert runtime._last_skipped_cycle_reason == "DURABLE_REPLAY_PENDING:CAPACITY"


def _ws_catalog():
    return tuple(
        OptionInstrument(
            exchange="DERIBIT",
            source="deribit",
            symbol=f"BTC-30OCT26-100000-{side}",
            underlying="BTC",
            option_type="call" if side == "C" else "put",
            strike=Decimal("100000"),
            expires_at=datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc),
            instrument_created_at=None,
            instrument_state="open",
            is_active=True,
            price_index="btc_usd",
            base_currency="BTC",
            quote_currency="BTC",
            settlement_currency="BTC",
            exchange_timestamp=None,
            fetched_at=T0,
            processed_at=T0,
        )
        for side in ("C", "P")
    )


class FakePersistence:
    def __init__(self, events):
        self.events = events
        self.cleanups = 0
        self.contexts = []
        self.health_rows = []
        self.summary_observations = []
        self.market_observations = []

    def initialize(self):
        self.events.append("migration")

    def upsert_instruments(self, instruments):
        self.events.append("catalog_persisted")
        return len(instruments)

    def retire_missing_instruments(self, **_kwargs):
        return 0

    def append_instrument_events(self, events):
        return len(events)

    def persist_observations(self, observations):
        self.market_observations.extend(observations)
        return len(observations)

    def persist_full_chain_cycle(self, observations, *, max_records):
        self.summary_observations.extend(observations)
        return len(observations)

    def persist_context(self, snapshot):
        self.contexts.append(snapshot)
        self.events.append("context_persisted")
        return True

    def set_health(self, status, details, checked_at):
        self.health_rows.append((status, dict(details), checked_at))
        self.events.append(f"health:{status}")

    def cleanup_retention(self, *, now):
        self.cleanups += 1


class FakeRestClient:
    def __init__(self, events):
        self.events = events
        self.summary_calls = 0
        self.summary_progress = asyncio.Event()

    async def __aenter__(self):
        self.events.append("rest_open")
        return self

    async def __aexit__(self, *_args):
        self.events.append("rest_close")

    async def call(self, method, params):
        self.events.append(f"rest:{method}:{params.get('currency', '')}")
        if method == "public/get_index_price_names":
            return _fixture("rest_index_names.json")
        if method == "public/get_instruments":
            return _fixture(f"rest_instruments_{params['currency'].lower()}.json")
        if method == "public/get_book_summary_by_currency":
            self.summary_calls += 1
            if self.summary_calls >= 4:
                self.summary_progress.set()
            return _fixture(f"rest_book_summary_{params['currency'].lower()}.json")
        raise AssertionError(f"unexpected public method: {method}")


class FakeWebSocketClient:
    def __init__(
        self,
        events,
        *,
        instrument_catalog,
        supported_index_names,
        ticker_symbols,
        on_invalidate,
        on_lifecycle,
        on_markprice_batch,
        on_ticker,
        defer_market_subscriptions,
        start_error=None,
        start_gate=None,
        **_kwargs,
    ):
        assert defer_market_subscriptions is True
        assert tuple(instrument_catalog) == ()
        assert not supported_index_names
        assert tuple(ticker_symbols) == ()
        self.events = events
        self.reseeded = asyncio.Event()
        self.on_invalidate = on_invalidate
        self.on_lifecycle = on_lifecycle
        self.on_markprice_batch = on_markprice_batch
        self.on_ticker = on_ticker
        self.ready = False
        self.markprice_state = {}
        self.ticker_state = {}
        self._configured_tickers = ()
        self._configured_indexes = set()
        self.start_error = start_error
        self.start_gate = start_gate

    @property
    def ready_ticker_symbols(self):
        return set(self._configured_tickers)

    @property
    def ready_markprice_indexes(self):
        return set(self._configured_indexes) if self.ready else set()

    async def start(self):
        self.events.append("ws_lifecycle_started")
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            raise self.start_error

    async def configure_market_subscriptions(self, catalog, indexes, *, ticker_symbols, force_reseed=False):
        self._configured_tickers = tuple(ticker_symbols)
        self._configured_indexes = set(indexes)
        self.ready = False
        self.events.append("ws_markprice_configured" if not ticker_symbols else "ws_ticker_configured")
        if force_reseed:
            self.events.append("ws_force_reseed")
            self.reseeded.set()

    async def wait_ready(self, *, timeout):
        self.ready = True
        self.reseeded.set()
        self.events.append("ws_markprice_ready" if not self._configured_tickers else "ws_ticker_ready")

    async def invalidate(self, reason):
        self.ready = False
        self.reseeded.clear()
        self.markprice_state.clear()
        self.ticker_state.clear()
        await self.on_invalidate(reason)

    async def close(self):
        if self.start_gate is not None:
            self.start_gate.set()
        self.events.append("ws_closed")


async def _wait_for(predicate, *, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached before test deadline")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_websocket_start_failure_does_not_block_rest_catalog_or_initial_summary():
    events = []
    persistence = FakePersistence(events)
    rest = FakeRestClient(events)
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(),
        rest_factory=lambda *_args: rest,
        websocket_factory=lambda _settings, *args, **kwargs: FakeWebSocketClient(
            events, *args, start_error=OSError("offline"), **kwargs
        ),
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event))
    await asyncio.wait_for(runtime.started.wait(), timeout=2)
    await _wait_for(lambda: runtime._health_details()["ws_connection_status"] == "ERROR")

    assert len(runtime.catalog) == 4
    assert set(runtime._summary_observations) == {"BTC", "ETH"}
    assert len(persistence.summary_observations) > 0
    assert runtime._health_details()["catalog_status"] == "AVAILABLE"
    assert runtime._health_details()["summary_status"] == "AVAILABLE"
    assert runtime._last_failure_type is None, (
        runtime._last_failure_type, runtime._runtime_stage, events[-10:],
    )
    assert runtime.state == "DEGRADED"
    assert runtime._health_details()["phase8_status"] == "PARTIAL"
    assert runtime._health_details()["markprice_status"] == "NOT_AVAILABLE"
    assert runtime._health_details()["ticker_status"] == "NOT_AVAILABLE"
    assert runtime._health_details()["last_error_category"] == "NETWORK"
    assert not any(
        observation.observation_kind in {
            ObservationKind.WS_MARKPRICE_SNAPSHOT,
            ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT,
        }
        for observation in persistence.market_observations
    )
    assert any(
        observation.status is DataStatus.AVAILABLE
        and observation.metrics.get("open_interest") is not None
        and observation.metrics["open_interest"].value is not None
        for observation in persistence.summary_observations
    )
    assert any(
        observation.status is DataStatus.AVAILABLE
        and observation.metrics.get("volume_24h") is not None
        and observation.metrics["volume_24h"].value is not None
        for observation in persistence.summary_observations
    )
    assert persistence.contexts
    for snapshot in persistence.contexts:
        for metric_name in (
            "atm_iv",
            "iv_term_structure",
            "iv_skew",
            "risk_reversal_25d",
            "butterfly_25d",
        ):
            metric = snapshot.metrics[metric_name]
            assert metric.status is DataStatus.NOT_AVAILABLE
            assert metric.value is None
        assert all(
            metric.status is not DataStatus.ERROR
            for metric in snapshot.metrics.values()
        )
    health = persistence.health_rows[-1][1]
    assert health["phase8_status"] == "PARTIAL"
    assert health["reason"]
    assert health["summary_status"] == "AVAILABLE"
    assert runtime._tasks

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert runtime.owned_tasks == ()


@pytest.mark.asyncio
async def test_rest_summary_worker_continues_while_websocket_start_is_failing():
    events = []
    persistence = FakePersistence(events)
    rest = FakeRestClient(events)
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(PHASE8_OPTIONS_CHAIN_SNAPSHOT_INTERVAL_SECONDS="1"),
        rest_factory=lambda *_args: rest,
        websocket_factory=lambda _settings, *args, **kwargs: FakeWebSocketClient(
            events, *args, start_error=OSError("offline"), **kwargs
        ),
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event))
    await asyncio.wait_for(runtime.started.wait(), timeout=2)
    await asyncio.wait_for(rest.summary_progress.wait(), timeout=2)
    await _wait_for(lambda: runtime._worker_health["chain"]["last_success_at"] is not None)

    assert rest.summary_calls >= 4
    assert runtime._worker_health["chain"]["last_success_at"] == T0.isoformat()
    assert runtime._health_details()["summary_status"] == "AVAILABLE"
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert runtime.owned_tasks == ()


@pytest.mark.asyncio
async def test_phase8_websocket_recovery_restores_health_without_fabricating_sparse_ticker_fields():
    events = []
    persistence = FakePersistence(events)
    rest = FakeRestClient(events)
    websocket = None

    def make_websocket(_settings, *args, **kwargs):
        nonlocal websocket
        websocket = FakeWebSocketClient(events, *args, **kwargs)
        return websocket

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(),
        rest_factory=lambda *_args: rest,
        websocket_factory=make_websocket,
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event))
    await asyncio.wait_for(runtime.started.wait(), timeout=2)
    await _wait_for(lambda: runtime._health_details()["ws_connection_status"] == "AVAILABLE")
    assert runtime.state == (
        "AVAILABLE" if runtime._health_details()["context_status"] == "AVAILABLE" else "DEGRADED"
    )

    symbol = runtime.ticker_symbols[0]
    sparse_metric = OptionMetricValue(
        metric="delta", value=Decimal("0.42"), exchange_timestamp=T0,
        received_at=T0, processed_at=T0,
    )
    await websocket.invalidate("WEBSOCKET_RECONNECT")
    assert runtime.state == "DEGRADED"
    assert runtime._health_details()["phase8_status"] == "PARTIAL"
    websocket.ticker_state[symbol] = {"delta": sparse_metric}
    await asyncio.wait_for(runtime._reconcile_task, timeout=2)
    assert runtime._health_details()["ws_connection_status"] == "AVAILABLE"
    assert runtime.state == (
        "AVAILABLE" if runtime._health_details()["context_status"] == "AVAILABLE" else "DEGRADED"
    )

    observations = runtime._ticker_observations()
    assert len(observations) == 1
    assert set(observations[0].metrics) == {"delta"}
    assert observations[0].metrics["delta"].value == Decimal("0.42")
    assert all(metric.value is not None for metric in observations[0].metrics.values())
    assert "mark_iv" not in observations[0].metrics
    assert "gamma" not in observations[0].metrics

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert runtime.owned_tasks == ()


@pytest.mark.asyncio
async def test_phase8_shutdown_is_clean_while_websocket_supervisor_is_retrying():
    events = []
    persistence = FakePersistence(events)
    rest = FakeRestClient(events)
    retry_gate = asyncio.Event()
    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(),
        rest_factory=lambda *_args: rest,
        websocket_factory=lambda _settings, *args, **kwargs: FakeWebSocketClient(
            events, *args, start_gate=retry_gate, **kwargs
        ),
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event))
    await asyncio.wait_for(runtime.started.wait(), timeout=2)
    await _wait_for(lambda: "ws_lifecycle_started" in events)
    assert runtime._health_details()["ws_connection_status"] in {"CONNECTING", "DEGRADED"}

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert runtime.state == "STOPPED"
    assert events.count("ws_closed") == 1
    assert runtime.owned_tasks == ()


@pytest.mark.asyncio
async def test_disabled_runtime_constructs_no_phase8_clients_or_tasks():
    constructions = []
    runtime = _runtime_class()(
        Settings.from_env({}),
        phase8_settings=Phase8Settings.from_env({"TRADING_MODE": "paper"}),
        rest_factory=lambda *_a, **_k: constructions.append("rest"),
        websocket_factory=lambda *_a, **_k: constructions.append("ws"),
        persistence_factory=lambda *_a, **_k: constructions.append("persistence"),
    )

    await runtime.run(asyncio.Event())

    assert constructions == []
    assert runtime.owned_tasks == ()
    assert runtime.state == "DISABLED"


@pytest.mark.asyncio
async def test_startup_failure_remains_visible_in_phase8_health():
    events = []
    persistence = FakePersistence(events)

    class FailingRestClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def call(self, *_args):
            events.append("rest:public/get_index_price_names:")
            raise OSError("offline")

    def make_websocket(_settings, *args, **kwargs):
        return FakeWebSocketClient(events, *args, **kwargs)

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(),
        rest_factory=lambda *_args: FailingRestClient(),
        websocket_factory=make_websocket,
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )

    await runtime.run(asyncio.Event())

    assert runtime.state == "ERROR"
    assert events[-1] == "health:ERROR"
    initial = persistence.health_rows[0]
    assert initial[0] == "INITIALIZING"
    assert initial[1]["configured"] is True
    assert initial[1]["phase8_status"] == "NOT_AVAILABLE"
    assert initial[1]["runtime_stage"] == "PERSISTENCE_READY"
    assert initial[1]["reason"] == "STARTUP_IN_PROGRESS"
    assert events.index("health:INITIALIZING") < next(
        index for index, event in enumerate(events) if event.startswith("rest:public/")
    )


@pytest.mark.asyncio
async def test_runtime_orders_lifecycle_catalog_markprice_summary_universe_ticker_and_shutdown():
    events = []
    persistence = FakePersistence(events)
    rest = FakeRestClient(events)
    websocket = None

    def make_websocket(_settings, *args, **kwargs):
        nonlocal websocket
        websocket = FakeWebSocketClient(events, *args, **kwargs)
        return websocket

    runtime = _runtime_class()(
        Settings.from_env({"TRADING_MODE": "paper"}),
        phase8_settings=_phase8_settings(),
        rest_factory=lambda *_args: rest,
        websocket_factory=make_websocket,
        persistence_factory=lambda *_args: persistence,
        clock=lambda: T0,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop_event))
    await asyncio.wait_for(runtime.started.wait(), timeout=2)

    await _wait_for(lambda: runtime._health_details()["ws_connection_status"] == "AVAILABLE")
    assert events.index("rest:public/get_index_price_names:") < events.index("ws_ticker_configured")
    assert events.index("rest:public/get_book_summary_by_currency:ETH") < events.index("ws_ticker_configured")
    assert events.index("ws_ticker_configured") < events.index("ws_lifecycle_started")
    assert events.index("ws_ticker_configured") < events.index("ws_ticker_ready")
    assert "context_persisted" in events
    assert len(runtime.catalog) == 4
    assert len(runtime.ticker_symbols) <= 128
    assert runtime.state == (
        "AVAILABLE" if runtime._health_details()["context_status"] == "AVAILABLE" else "DEGRADED"
    )
    startup_health = persistence.health_rows[-1][1]
    assert startup_health["configured"] is True
    assert startup_health["phase8_status"] == runtime._health_details()["phase8_status"]
    assert startup_health["runtime_stage"] == "RUNNING"
    assert startup_health["workers_started"] is True
    assert startup_health["worker_count"] == 6
    assert set(startup_health["worker_health"]) == {
        "instruments", "chain", "markprice", "ticker", "context", "health",
    }
    assert all(worker["status"] == "SCHEDULED" for worker in startup_health["worker_health"].values())
    assert persistence.cleanups == 0
    assert runtime.owned_tasks

    creation_payload = loads_decimal_json(
        (FIXTURES / "ws_instrument_creation.json").read_text(encoding="utf-8")
    )
    creation_payload["params"]["data"]["instrument_name"] = "BTC-30OCT26-105000-C"
    creation_payload["params"]["data"]["strike"] = Decimal("105000")
    created_instrument, created_event = parse_instrument_creation_notification(
        creation_payload,
        supported_index_names={"btc_usd", "eth_usd", "btc_usdc", "eth_usdc"},
        received_at=T0,
        processed_at=T0,
    )
    await websocket.on_lifecycle(created_instrument, created_event)
    assert "BTC-30OCT26-105000-C" in runtime.catalog
    assert "BTC-30OCT26-105000-C" in runtime.ticker_symbols

    await websocket.invalidate("WEBSOCKET_RECONNECT")
    assert runtime.state in {"DEGRADED", "STALE"}
    await asyncio.wait_for(websocket.reseeded.wait(), timeout=2)
    assert "ws_force_reseed" not in events
    assert runtime._reconcile_task is not None
    await asyncio.wait_for(runtime._reconcile_task, timeout=2)
    assert runtime._health_details()["ws_connection_status"] == "AVAILABLE"
    assert runtime.state in {"AVAILABLE", "DEGRADED"}
    assert events[-1] == f"health:{runtime.state}"

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)
    assert runtime.state == "STOPPED"
    assert events.count("ws_closed") == 1
    assert events.index("ws_closed") < events.index("rest_close")
    assert runtime.owned_tasks == ()
    assert persistence.cleanups == 0


@pytest.mark.asyncio
async def test_deferred_websocket_subscribes_lifecycle_then_markprice_then_bounded_ticker():
    requests = []
    lifecycle_events = []
    lifecycle_subscribed = asyncio.Event()
    market_ready = asyncio.Event()

    async def handler(request):
        socket = web.WebSocketResponse(autoping=True)
        await socket.prepare(request)
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            requests.append(payload)
            await socket.send_json({"jsonrpc": "2.0", "id": payload["id"], "result": []})
            channels = payload["params"]["channels"]
            if len(requests) == 1:
                lifecycle_subscribed.set()
                creation = (FIXTURES / "ws_instrument_creation.json").read_text(encoding="utf-8")
                await socket.send_str(creation.replace('"strike": 100000,', '"strike": 100000.5,'))
            elif any(channel.startswith("markprice.options.") for channel in channels):
                await socket.send_str((FIXTURES / "ws_markprice_seed.json").read_text(encoding="utf-8"))
            elif any(channel.startswith("incremental_ticker.") for channel in channels):
                await socket.send_str((FIXTURES / "ws_ticker_snapshot.json").read_text(encoding="utf-8"))
                market_ready.set()
        return socket

    server = TestServer(web.Application())
    server.app.router.add_get("/ws/api/v2", handler)
    await server.start_server()
    url = str(server.make_url("/ws/api/v2")).replace("http://", "ws://", 1)
    catalog = _ws_catalog()
    client = DeribitPublicWebSocketClient(
        _phase8_settings(),
        instrument_catalog=(),
        supported_index_names=set(),
        ticker_symbols=(),
        defer_market_subscriptions=True,
        on_lifecycle=lambda instrument, event: lifecycle_events.append((instrument, event)),
        _url=url,
        _sleep=lambda _seconds: asyncio.sleep(0),
    )
    try:
        await client.start()
        await asyncio.wait_for(lifecycle_subscribed.wait(), timeout=2)
        assert [channel for request in requests for channel in request["params"]["channels"]] == [
            "instrument.creation.option.BTC",
            "instrument.state.option.BTC",
            "instrument.creation.option.ETH",
            "instrument.state.option.ETH",
        ]
        assert not client.ready
        async def wait_for_deferred_lifecycle():
            while not client._deferred_lifecycle and not client.last_error:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_deferred_lifecycle(), timeout=2)
        assert len(client._deferred_lifecycle) == 1, client.last_error
        await client.configure_market_subscriptions(catalog, {"btc_usd"}, ticker_symbols=())
        assert len(lifecycle_events) == 1, client.last_error
        assert lifecycle_events[0][0].symbol == catalog[0].symbol
        await client.wait_ready(timeout=2)
        await client.configure_market_subscriptions(
            catalog, {"btc_usd"}, ticker_symbols=(catalog[0].symbol,)
        )
        await asyncio.wait_for(market_ready.wait(), timeout=2)
        await client.wait_ready(timeout=2)
        channel_groups = [request["params"]["channels"] for request in requests]
        assert channel_groups[1] == ["markprice.options.btc_usd"]
        assert channel_groups[2] == [f"incremental_ticker.{catalog[0].symbol}"]
        assert client.ready
    finally:
        await client.close()
        await server.close()


def test_phase8_compose_environment_is_only_on_existing_collector():
    compose_path = Path(__file__).parents[1] / "docker-compose.local.yml"
    parsed = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
            str(compose_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    compose = json.loads(parsed.stdout)
    assert set(compose["services"]) == {"postgres", "quant-collector", "quant-engine", "quant-migrate"}
    assert compose["services"]["quant-migrate"]["profiles"] == ["maintenance"]
    collector_env = compose["services"]["quant-collector"]["environment"]
    engine_env = compose["services"]["quant-engine"]["environment"]
    assert collector_env["PHASE8_OPTIONS_ENABLED"] == "${PHASE8_OPTIONS_ENABLED:-0}"
    assert not any(name.startswith("PHASE8_OPTIONS_") for name in engine_env)
    assert compose["services"]["quant-collector"]["mem_limit"] == "768m"
    assert compose["services"]["quant-engine"]["mem_limit"] == "512m"
    assert compose["services"]["postgres"]["mem_limit"] == "768m"


@pytest.mark.asyncio
async def test_collector_owns_and_awaits_optional_phase8_runtime(monkeypatch):
    import quant_phase1.entrypoints.collector as collector_entrypoint

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE2_ENABLED": "0",
        "PHASE3_ENABLED": "0",
        "PHASE4_ENABLED": "0",
        "PHASE5_ENABLED": "0",
        "PHASE6_ENABLED": "0",
        "PHASE7_ENABLED": "0",
    })
    phase8_settings = _phase8_settings()
    monkeypatch.setattr(Phase8Settings, "from_env", classmethod(lambda cls, _env=None: phase8_settings))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakePhase8Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, stop_event):
            started.set()
            try:
                await stop_event.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(collector_entrypoint, "Phase8CollectorRuntime", FakePhase8Runtime, raising=False)

    class FakeRestClient:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeMarketDataCollector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect_once(self):
            return SimpleNamespace(selected_symbols=(), tickers=(), candles_by_symbol={})

    async def noop(*_args, **_kwargs):
        return None

    async def idle(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def stop_after_phase8(*_args, **_kwargs):
        await asyncio.wait_for(started.wait(), timeout=1)
        service.stop_event.set()

    monkeypatch.setattr(collector_entrypoint, "BitgetV3UtaRestClient", FakeRestClient)
    monkeypatch.setattr(collector_entrypoint, "MarketDataCollector", FakeMarketDataCollector)
    monkeypatch.setattr(collector_entrypoint, "write_health_file", lambda *_a, **_kw: None)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_persist_batch", noop)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_load_phase3_stage1_ab_symbols", noop)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_hydrate_phase3_state", noop)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_start_gap_recovery", noop)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_start_ws_connections", stop_after_phase8)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_persist_loop", idle)
    monkeypatch.setattr(collector_entrypoint.CollectorService, "_universe_loop", idle)

    service = collector_entrypoint.CollectorService(settings, stop_event=asyncio.Event())
    assert service.phase8_runtime is not None
    await asyncio.wait_for(service.run(), timeout=2)

    assert started.is_set()
    assert cancelled.is_set()
    assert service.phase8_task is None
