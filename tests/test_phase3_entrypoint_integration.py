import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quant_phase1.config import Settings
from quant_phase1.entrypoints.collector import CollectorService
from quant_data_layer.observability import (
    DatabaseMetric,
    MeasurementState,
    ProcessRole,
    TransactionClass,
    WorkClass,
)
from quant_data_layer.admission import AdmissionDeferred, AdmissionReason, ReplayClass
from quant_phase1.contracts import DataStatus
from quant_phase1.gap_recovery import RecoveryRange


def _settings(enabled: bool) -> Settings:
    return Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
            "PHASE3_ENABLED": "1" if enabled else "0",
        }
    )


def test_collector_keeps_phase3_opt_in_and_bounded():
    disabled = CollectorService(_settings(False))
    assert disabled.phase3_runtime is None

    enabled = CollectorService(_settings(True))
    assert enabled.phase3_runtime is not None
    assert enabled.phase3_runner is not None
    assert enabled.settings.max_trade_stream_symbols == 20


def test_phase3_symbol_mapping_is_public_and_canonical():
    service = CollectorService(_settings(True))
    service.selected_symbols = ("BTCUSDT", "ETHUSDT")

    assert service.phase3_symbols_by_exchange() == {
        "bitget": ("BTCUSDT", "ETHUSDT"),
        "bybit": ("BTCUSDT", "ETHUSDT"),
        "hyperliquid": ("BTC", "ETH"),
    }


def test_phase3_hydration_routes_are_exchange_and_canonical_symbol_scoped():
    service = CollectorService(_settings(True))
    service.selected_symbols = ("BTCUSDT", "ETHUSDT")
    service.phase3_stage1_ab_symbols = ("SOLUSDT",)

    routes = set(service._phase3_hydration_routes())

    assert routes == {
        (exchange, f"{symbol}-USDT-PERP")
        for exchange in ("bitget", "bybit", "hyperliquid")
        for symbol in ("BTC", "ETH", "SOL")
    }


def test_phase3_candidates_include_stage1_ab_without_exceeding_cap():
    service = CollectorService(_settings(True))
    service.selected_symbols = tuple(f"U{index}USDT" for index in range(20))
    service.phase3_stage1_ab_symbols = ("ALPHAUSDT", "BETAUSDT")

    selected = service.phase3_symbols_by_exchange()

    assert len(selected["bybit"]) == 20
    assert selected["bybit"][:2] == ("ALPHAUSDT", "BETAUSDT")
    assert selected["hyperliquid"][:2] == ("ALPHA", "BETA")


def test_collector_owns_one_process_admission_controller_for_phase_runtimes(monkeypatch):
    monkeypatch.setenv("PHASE8_OPTIONS_ENABLED", "1")
    settings = Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
            "PHASE3_ENABLED": "1",
            "PHASE4_ENABLED": "1",
            "PHASE6_ENABLED": "1",
        }
    )

    first = CollectorService(settings)
    second = CollectorService(settings)

    assert first.admission.role is ProcessRole.COLLECTOR
    assert first.admission is first.phase7_runtime.admission
    assert first.admission is first.phase4_runtime.admission
    assert first.admission is first.phase6_runtime.admission
    assert first.phase8_runtime is not None
    assert first.admission is first.phase8_runtime.admission
    assert first.admission is not second.admission


def test_collector_observability_exposes_db_admission_counts_by_transaction_class():
    service = CollectorService(_settings(False))
    service.db_admission._pending[WorkClass.MEDIUM] = 2
    service.db_admission._active[WorkClass.HEAVY] = 1

    snapshot = service.observability_snapshot()

    assert snapshot.database.metric(DatabaseMetric.PENDING_ADMISSIONS).value == 2
    assert snapshot.database.metric(DatabaseMetric.ACTIVE_TRANSACTIONS).value == 1
    transaction_classes = {
        item.transaction_class: item for item in snapshot.database.transaction_classes
    }
    assert transaction_classes[TransactionClass.CANONICAL_BATCH].pending.value == 2
    assert transaction_classes[TransactionClass.LARGE_ATOMIC_SOURCE].active.value == 1
    assert transaction_classes[TransactionClass.LARGE_ATOMIC_SOURCE].hold_seconds.state is MeasurementState.NOT_EXPOSED


@pytest.mark.asyncio
async def test_phase3_processing_uses_collector_heavy_admission():
    service = CollectorService(_settings(True))
    active_during_processing = []

    class Phase3Runtime:
        async def process_all_pending_async(self, **kwargs):
            active_during_processing.append(
                service.admission.snapshot().active_by_class[WorkClass.HEAVY]
            )
            return ()

    service.phase3_runtime = Phase3Runtime()
    await service._persist_phase3_cycle()

    assert active_during_processing == [1]


@pytest.mark.asyncio
async def test_phase1_recovery_filter_uses_bounded_replayable_admission():
    service = CollectorService(_settings(False))
    requests = []

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            requests.append(request)
            yield None

    service.admission = AdmissionSpy()
    start = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
    candidate = RecoveryRange("BTCUSDT", "5m", start, start)
    candle = SimpleNamespace(
        is_closed=True,
        status=DataStatus.AVAILABLE,
        bar_open_timestamp=start,
    )

    recovered = await service._admit_recovery_page(
        candidate, cursor=start, page_end=start, candles=(candle,)
    )

    assert recovered == [candle]
    assert requests[0].work_class is WorkClass.HEAVY
    assert requests[0].cancellation_owner is ProcessRole.COLLECTOR
    assert requests[0].estimated_items == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instruments", "candles_by_symbol", "stream_id", "replay_class"),
    [
        ([], {}, "phase1.ticker_latest", ReplayClass.DERIVED_REPLACEABLE),
        ([object()], {}, "phase1.instruments", ReplayClass.RECOVERABLE_REPLAYABLE),
        ([], {"BTCUSDT": {"5m": [object()]}}, "phase1.closed_klines", ReplayClass.RECOVERABLE_REPLAYABLE),
    ],
)
async def test_phase1_market_batch_admission_uses_most_conservative_contained_stream(
    monkeypatch, instruments, candles_by_symbol, stream_id, replay_class
):
    service = CollectorService(_settings(False))
    requests = []
    persisted = []

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            requests.append(request)
            yield None

    async def persist(batch):
        persisted.append(batch)
        return True

    service.admission = AdmissionSpy()
    monkeypatch.setattr(service, "_persist_batch_admitted", persist)
    batch = SimpleNamespace(tickers=[object()], instruments=instruments, candles_by_symbol=candles_by_symbol)

    assert await service._persist_batch(batch) is True
    assert persisted == [batch]
    assert requests[0].stream_id == stream_id
    assert requests[0].replay_class is replay_class
    assert requests[0].work_class is WorkClass.MEDIUM


@pytest.mark.asyncio
async def test_phase1_batch_admission_deferral_never_calls_persistence(monkeypatch):
    service = CollectorService(_settings(False))
    persisted = []

    class FullAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            raise AdmissionDeferred(AdmissionReason.CAPACITY)
            yield None

    async def persist(batch):
        persisted.append(batch)
        return True

    service.admission = FullAdmission()
    monkeypatch.setattr(service, "_persist_batch_admitted", persist)

    result = await service._persist_batch(
        SimpleNamespace(tickers=[object()], candles_by_symbol={"BTCUSDT": {"5m": [object()]}})
    )

    assert result is False
    assert persisted == []
    diagnostics = service.diagnostics_snapshot()
    assert diagnostics["phase1_db_admission_deferred_count"] == 1
    assert diagnostics["phase1_db_admission_last_reason"] == "CAPACITY"
    assert diagnostics["phase1_db_admission_last_stream"] == "phase1.closed_klines"
