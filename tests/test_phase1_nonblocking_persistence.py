import asyncio
import threading
from types import SimpleNamespace

import pytest

import quant_phase1.entrypoints.collector as collector_module
from quant_phase1.entrypoints.collector import CollectorService
from quant_phase1.runtime import RuntimeHealthTracker


@pytest.mark.asyncio
async def test_phase1_postgres_batch_persistence_runs_off_event_loop(monkeypatch):
    service = object.__new__(CollectorService)
    service.settings = SimpleNamespace(postgres_dsn="unused")
    service.component = "quant-collector"
    service.health = RuntimeHealthTracker(service.component)
    service.gap_recovery = None
    service.phase4_runtime = None
    service._db_writer_active_transactions = 0

    started = threading.Event()
    release = threading.Event()
    worker_threads = []

    def blocked_transaction(_service, batch, health_snapshot, gap_recovery, recovery):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return True

    monkeypatch.setattr(
        collector_module.CollectorService,
        "_persist_batch_transaction",
        blocked_transaction,
        raising=False,
    )

    main_thread = threading.get_ident()
    task = asyncio.create_task(service._persist_batch_admitted(object()))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=1)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert worker_threads and worker_threads[0] != main_thread
        assert service._db_writer_active_transactions == 1
    finally:
        release.set()

    assert await asyncio.wait_for(task, timeout=1) is True
    assert service._db_writer_active_transactions == 0


