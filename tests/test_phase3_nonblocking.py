import asyncio
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

import quant_phase1.entrypoints.collector as collector_module
from quant_phase1.config import Settings
from quant_phase1.entrypoints.collector import CollectorService


def _service_with_one_window():
    service = CollectorService(Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
        "PHASE3_ENABLED": "1",
    }))
    window = SimpleNamespace(
        exchange="bitget",
        canonical_symbol="BTCUSDT",
        timeframe="1m",
        window_close=datetime.now(timezone.utc),
    )

    class Runtime:
        async def process_all_pending_async(self, **kwargs):
            return (window,)

    class Cvd:
        def ingest(self, window, *, processed_at):
            return ()

    service.phase3_runtime = Runtime()
    service.phase3_cvd = Cvd()
    return service


@pytest.mark.asyncio
async def test_phase3_rollup_calculation_does_not_block_event_loop(monkeypatch):
    service = _service_with_one_window()
    service.phase3_history[("bybit", "ETHUSDT")].append(SimpleNamespace(marker="unrelated"))
    started = threading.Event()
    release = threading.Event()
    worker_threads = []
    histories_seen = []

    def blocked_rollup(*args):
        histories_seen.append(args[1])
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return (), ()

    monkeypatch.setattr(collector_module, "_build_phase3_derived_outputs", blocked_rollup)
    monkeypatch.setattr(collector_module, "_persist_phase3_outputs", lambda *args: None)
    main_thread = threading.get_ident()
    task = asyncio.create_task(service._persist_phase3_cycle_admitted())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert worker_threads and worker_threads[0] != main_thread
        assert len(histories_seen[0]) == 1
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_phase3_database_persistence_does_not_block_event_loop(monkeypatch):
    service = _service_with_one_window()
    started = threading.Event()
    release = threading.Event()
    worker_threads = []

    def blocked_persistence(*args):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)

    monkeypatch.setattr(collector_module, "_build_phase3_derived_outputs", lambda *args: ((), ()))
    monkeypatch.setattr(collector_module, "_persist_phase3_outputs", blocked_persistence)
    main_thread = threading.get_ident()
    task = asyncio.create_task(service._persist_phase3_cycle_admitted())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert worker_threads and worker_threads[0] != main_thread
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=1)
