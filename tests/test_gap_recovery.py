import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.config import Settings
from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.entrypoints.collector import CollectorService
from quant_phase1.freshness import INTERVAL_SECONDS, expected_latest_closed_open
from quant_phase1.runtime import BoundedEventBuffer, WebSocketCanonicalStore
from quant_phase1.gap_recovery import (
    GapRecoveryCoordinator,
    RecoveryRange,
    RecoveryScan,
)


NOW = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def test_bounded_event_buffer_depth_is_a_read_only_observation():
    buffer = BoundedEventBuffer(capacity=3)
    buffer.append("one")
    buffer.append("two")

    assert buffer.depth == 2
    assert buffer.capacity == 3
    assert buffer.depth == 2
    assert buffer.drain() == ["one", "two"]


def test_gap_recovery_diagnostics_report_queue_without_consuming_it():
    async def recover(_symbol, _interval):
        return ()

    coordinator = GapRecoveryCoordinator(
        recover,
        symbols_provider=lambda: (),
        on_candles=lambda _result: None,
        max_work_items=4,
    )
    coordinator._queue.put_nowait(("BTCUSDT", "5m"))
    coordinator._pending.add(("BTCUSDT", "5m"))

    first = coordinator.diagnostics_snapshot()
    second = coordinator.diagnostics_snapshot()

    assert first == second
    assert first["queue_depth"] == 1
    assert first["queue_capacity"] == 4
    assert first["pending_count"] == 1
    assert coordinator._queue.qsize() == 1


def test_overlapping_reconnect_batches_share_one_global_recovery_limit():
    async def run():
        symbols = tuple(f"S{index}USDT" for index in range(12))
        capacity = len(symbols) * 4
        release = asyncio.Event()
        active = 0
        max_active = 0
        calls = Counter()

        async def recover(symbol, interval):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls[(symbol, interval)] += 1
            try:
                await release.wait()
                return (symbol, interval)
            finally:
                active -= 1

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: symbols,
            on_candles=lambda _result: None,
            concurrency=3,
            max_work_items=capacity,
            retry_base_seconds=0.001,
            retry_max_seconds=0.01,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(_wait_for(lambda: coordinator.active_count == 3), timeout=1)

        for _ in range(3):
            coordinator.request()

        assert coordinator.active_count <= 3
        assert coordinator.pending_count + coordinator.inflight_count <= capacity
        assert coordinator.task_count == 4  # scheduler + exactly three fixed workers

        release.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=2)

        assert max_active == 3
        assert coordinator.completed_count == capacity + 3
        assert len(calls) == capacity
        assert max(calls.values()) == 2
        assert coordinator.pending_count == 0
        assert coordinator.inflight_count == 0
        await coordinator.stop()
        assert coordinator.task_count == 0

    asyncio.run(run())


def test_duplicate_inflight_gap_requests_coalesce_without_losing_latest_recovery():
    async def run():
        first_fetch_started = asyncio.Event()
        release_first_fetch = asyncio.Event()
        fetches = 0

        async def recover(_symbol, _interval):
            nonlocal fetches
            fetches += 1
            if fetches == 1:
                first_fetch_started.set()
                await release_first_fetch.wait()
            return (fetches,)

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: ("BTCUSDT",),
            on_candles=lambda _result: None,
            intervals=("5m",),
            concurrency=1,
            max_work_items=1,
            retry_base_seconds=0.001,
            retry_max_seconds=0.01,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(first_fetch_started.wait(), timeout=1)

        for _ in range(20):
            coordinator.request()

        assert coordinator.pending_count + coordinator.inflight_count == 1
        release_first_fetch.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

        assert fetches == 2
        assert coordinator.completed_count == 2
        assert coordinator.pending_count == coordinator.inflight_count == 0
        await coordinator.stop()

    asyncio.run(run())


def test_reconnect_storm_has_bounded_work_and_does_not_spawn_per_signal_tasks():
    async def run():
        symbols = tuple(f"S{index}USDT" for index in range(25))
        capacity = len(symbols) * 4
        release = asyncio.Event()
        coordinator = GapRecoveryCoordinator(
            lambda _symbol, _interval: _blocked_result(release),
            symbols_provider=lambda: symbols,
            on_candles=lambda _result: None,
            concurrency=4,
            max_work_items=capacity,
            retry_base_seconds=0.001,
            retry_max_seconds=0.01,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(_wait_for(lambda: coordinator.active_count == 4), timeout=1)
        baseline_tasks = len(asyncio.all_tasks())

        for _ in range(500):
            coordinator.request()
            assert coordinator.pending_count + coordinator.inflight_count <= capacity

        assert coordinator.task_count == 5
        assert len(asyncio.all_tasks()) == baseline_tasks

        release.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
        assert coordinator.pending_count + coordinator.inflight_count == 0
        assert coordinator.active_count <= 4
        await coordinator.stop()

    asyncio.run(run())


def test_capacity_backpressure_preserves_a_new_universe_recovery_request():
    async def run():
        selected = ["OLD0USDT", "OLD1USDT"]
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []
        max_outstanding = 0

        async def recover(symbol, interval):
            calls.append((symbol, interval))
            started.set()
            if symbol.startswith("OLD"):
                await release.wait()
            return (symbol, interval)

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: tuple(selected),
            on_candles=lambda _result: None,
            intervals=("5m", "15m"),
            concurrency=1,
            max_work_items=4,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(started.wait(), timeout=1)
        selected[:] = ["NEW0USDT", "NEW1USDT"]
        coordinator.request()
        await _wait_for(lambda: coordinator._scheduling)
        for _ in range(50):
            max_outstanding = max(max_outstanding, coordinator.outstanding_count)
            assert coordinator.outstanding_count <= 4
            await asyncio.sleep(0.001)

        release.set()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
        assert set(calls) == {
            (symbol, interval)
            for symbol in ("OLD0USDT", "OLD1USDT", "NEW0USDT", "NEW1USDT")
            for interval in ("5m", "15m")
        }
        assert max_outstanding <= 4
        await coordinator.stop()

    asyncio.run(run())


def test_retry_backoff_releases_worker_for_other_pending_recovery():
    async def run():
        calls = []

        async def recover(symbol, _interval):
            calls.append(symbol)
            if symbol == "FIRSTUSDT" and calls.count(symbol) == 1:
                raise RuntimeError("temporary failure")
            return (symbol,)

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: ("FIRSTUSDT", "SECONDUSDT"),
            on_candles=lambda _result: None,
            intervals=("1H",),
            concurrency=1,
            max_work_items=2,
            retry_base_seconds=0.05,
            retry_max_seconds=0.05,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
        assert calls == ["FIRSTUSDT", "SECONDUSDT", "FIRSTUSDT"]
        assert coordinator.completed_count == 2
        await coordinator.stop()

    asyncio.run(run())


def test_recovery_failure_retries_inside_the_same_bounded_worker_and_cleans_state():
    async def run():
        attempts = 0
        failures = []

        async def recover(_symbol, _interval):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("temporary source failure")
            return ("closed-bars",)

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: ("ETHUSDT",),
            on_candles=lambda _result: None,
            on_failure=lambda symbol, interval, exc: failures.append((symbol, interval, str(exc))),
            intervals=("15m",),
            concurrency=1,
            max_work_items=1,
            retry_base_seconds=0.001,
            retry_max_seconds=0.002,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

        assert attempts == 3
        assert len(failures) == 2
        assert coordinator.completed_count == 1
        assert coordinator.failed_count == 0
        assert coordinator.pending_count == coordinator.inflight_count == 0
        assert coordinator.task_count == 2
        await coordinator.stop()

    asyncio.run(run())


def test_shutdown_cancels_workers_and_pending_work_is_rediscoverable_after_restart():
    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def recover(_symbol, _interval):
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        coordinator = GapRecoveryCoordinator(
            recover,
            symbols_provider=lambda: ("SOLUSDT",),
            on_candles=lambda _result: None,
            intervals=("1H",),
            concurrency=1,
            max_work_items=1,
        )
        await coordinator.start()
        coordinator.request()
        await asyncio.wait_for(started.wait(), timeout=1)
        await coordinator.stop()

        assert cancelled.is_set()
        assert coordinator.task_count == 0
        assert coordinator.pending_count == coordinator.inflight_count == 0
        assert coordinator.request() is False

    asyncio.run(run())


def test_collector_reconnect_signals_share_workers_and_merge_closed_bars():
    async def run():
        symbols = tuple(f"S{index}USDT" for index in range(3))
        release = asyncio.Event()
        started = asyncio.Event()
        active = 0
        max_active = 0
        calls = Counter()

        class FakeRest:
            async def get_candles(self, *, symbol, interval, limit, start_time=None, end_time=None):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                calls[(symbol, interval)] += 1
                started.set()
                try:
                    await release.wait()
                    return (_closed_candle_at(symbol, interval, end_time),)
                finally:
                    active -= 1

        service = CollectorService(Settings.from_env({"TRADING_MODE": "paper"}))
        service.selected_symbols = symbols
        client = FakeRest()
        await asyncio.gather(*(service._recover_gaps(client) for _ in range(3)))
        coordinator = service.gap_recovery
        assert coordinator is not None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(_wait_for(lambda: coordinator.active_count >= 8), timeout=1)
            assert coordinator.active_count <= 8

            for _ in range(20):
                await service._recover_gaps(client)
            assert coordinator.pending_count + coordinator.inflight_count <= coordinator.max_work_items
            assert coordinator.task_count == 9  # one scheduler and eight shared recovery workers

            release.set()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
            expected_keys = {
                (symbol, interval)
                for symbol in symbols
                for interval in ("5m", "15m", "1H", "4H")
            }
            assert set(calls) == expected_keys
            assert max(calls.values()) <= 2
            assert max_active <= 8
            assert coordinator.completed_count == sum(calls.values())
            assert all(
                candle.is_closed and candle.status is DataStatus.AVAILABLE
                for symbol_bars in service.store.closed_klines.values()
                for history in symbol_bars.values()
                for candle in history
            )
        finally:
            release.set()
            await coordinator.stop()

    asyncio.run(run())


def test_realtime_receive_loop_continues_while_recovery_is_blocked():
    async def run():
        recovery_started = asyncio.Event()
        second_receive = asyncio.Event()

        class FakeRest:
            async def get_candles(self, *, symbol, interval, limit, start_time=None, end_time=None):
                recovery_started.set()
                await asyncio.Future()

        class FlakySocket:
            receives = 0

            async def receive(self):
                self.receives += 1
                if self.receives == 1:
                    raise ConnectionResetError("deterministic test reset")
                second_receive.set()
                await service.stop_event.wait()
                return {"event": "pong"}

            async def reconnect(self):
                return None

        service = CollectorService(
            Settings.from_env({"TRADING_MODE": "paper", "WS_RECONNECT_SECONDS": "0.001"})
        )
        service.selected_symbols = ("BTCUSDT", "ETHUSDT")
        socket = FlakySocket()
        receive_task = asyncio.create_task(service._receive_loop(socket, FakeRest()))
        try:
            await asyncio.wait_for(recovery_started.wait(), timeout=1)
            await asyncio.wait_for(second_receive.wait(), timeout=1)
            assert service.gap_recovery is not None
            assert service.gap_recovery.active_count > 0
            assert service.ws_reconnect_count == 1
        finally:
            service.stop_event.set()
            await asyncio.gather(receive_task, return_exceptions=True)
            if service.gap_recovery is not None:
                await service.gap_recovery.stop()

    asyncio.run(run())


def test_empty_recovery_response_is_degraded_and_retried_not_available(monkeypatch):
    async def run():
        calls = Counter()

        class FakeRest:
            async def get_candles(self, *, symbol, interval, limit, start_time=None, end_time=None):
                key = (symbol, interval)
                calls[key] += 1
                if calls[key] == 1:
                    return ()
                return (_closed_candle_at(symbol, interval, end_time),)

        service = CollectorService(Settings.from_env({"TRADING_MODE": "paper"}))
        service.selected_symbols = ("BTCUSDT",)
        client = FakeRest()
        await service._recover_gaps(client)
        coordinator = service.gap_recovery
        assert coordinator is not None

        await asyncio.wait_for(_wait_for(lambda: service.health.state == "DEGRADED"), timeout=1)
        assert coordinator.failed_count > 0
        await asyncio.wait_for(coordinator.wait_idle(), timeout=3)

        assert coordinator.failed_count == 0
        assert coordinator.is_healthy
        assert len(calls) == 4
        assert all(count == 2 for count in calls.values())
        assert service.health.state == "DEGRADED"  # recovery becomes AVAILABLE only after persisted health
        assert all(
            candle.status is DataStatus.AVAILABLE and candle.is_closed
            for symbol_bars in service.store.closed_klines.values()
            for history in symbol_bars.values()
            for candle in history
        )
        await coordinator.stop()

    monkeypatch.setattr("quant_phase1.service.write_health_file", lambda *_args, **_kwargs: None)
    asyncio.run(run())


def test_actual_gap_first_admits_only_the_twenty_missing_keys():
    async def run():
        missing = {(f"S{index}USDT", "5m") for index in range(20)}
        fetches = []

        def scan():
            candidates = tuple(
                _recovery_range(symbol, interval)
                for symbol, interval in sorted(missing)
            )
            return RecoveryScan(scanned_count=800, candidates=candidates)

        async def recover(candidate):
            fetches.append(candidate)
            return ((candidate.symbol, candidate.interval),)

        def persist(rows):
            missing.difference_update(rows)

        coordinator = GapRecoveryCoordinator(
            recover,
            candidate_provider=scan,
            still_needed=lambda candidate: (candidate.symbol, candidate.interval) in missing,
            on_candles=persist,
            concurrency=4,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
            assert len(fetches) == 20
            assert coordinator.candidate_scanned_total == 800
            assert coordinator.actual_gap_total == 20
            assert coordinator.admitted_count == 20
            assert missing == set()
        finally:
            await coordinator.stop()

    asyncio.run(run())


def test_eight_reconciliation_requests_coalesce_while_first_scan_is_running():
    async def run():
        missing = {(f"S{index}USDT", "5m") for index in range(20)}
        scan_entered = asyncio.Event()
        release_scan = asyncio.Event()
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        scan_active = 0
        scan_active_max = 0
        fetches = []

        async def scan():
            nonlocal scan_active, scan_active_max
            scan_active += 1
            scan_active_max = max(scan_active_max, scan_active)
            scan_entered.set()
            try:
                if coordinator.reconciliation_runs == 1:
                    await release_scan.wait()
                return RecoveryScan(
                    scanned_count=800,
                    candidates=tuple(
                        _recovery_range(symbol, interval)
                        for symbol, interval in sorted(missing)
                    ),
                )
            finally:
                scan_active -= 1

        async def recover(candidate):
            fetches.append(candidate)
            fetch_started.set()
            await release_fetch.wait()
            return ((candidate.symbol, candidate.interval),)

        coordinator = GapRecoveryCoordinator(
            recover,
            candidate_provider=scan,
            still_needed=lambda candidate: (candidate.symbol, candidate.interval) in missing,
            on_candles=lambda rows: missing.difference_update(rows),
            concurrency=1,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(scan_entered.wait(), timeout=1)
            for _ in range(8):
                assert coordinator.request()
            release_scan.set()
            await asyncio.wait_for(fetch_started.wait(), timeout=1)
            await asyncio.wait_for(
                _wait_for(lambda: coordinator.reconciliation_runs == 2), timeout=1
            )
            assert scan_active_max == 1
            assert coordinator.candidate_scanned_total == 1600
            assert coordinator.actual_gap_total == 40
            assert coordinator.admitted_count == 20
            assert coordinator.coalesced_count == 20
            assert coordinator.reconciliation_runs == 2
            assert coordinator.outstanding_count <= 20
            release_fetch.set()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
            assert len(fetches) == 20
            assert missing == set()
        finally:
            release_scan.set()
            release_fetch.set()
            await coordinator.stop()

    asyncio.run(run())


def test_dirty_generation_runs_one_followup_check_for_three_reconnects():
    async def run():
        scan_entered = asyncio.Event()
        release_scan = asyncio.Event()
        active = 0
        active_max = 0

        async def scan():
            nonlocal active, active_max
            active += 1
            active_max = max(active_max, active)
            scan_entered.set()
            try:
                if coordinator.reconciliation_runs == 1:
                    await release_scan.wait()
                return RecoveryScan(scanned_count=800, candidates=())
            finally:
                active -= 1

        coordinator = GapRecoveryCoordinator(
            lambda _candidate: _empty_recovery(),
            candidate_provider=scan,
            on_candles=lambda _rows: None,
            concurrency=1,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(scan_entered.wait(), timeout=1)
            for _ in range(3):
                assert coordinator.request()
            release_scan.set()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
            assert coordinator.reconciliation_runs == 2
            assert active_max == 1
            assert coordinator.request_generation == 4
        finally:
            release_scan.set()
            await coordinator.stop()

    asyncio.run(run())


def test_gap_that_disappears_while_pending_does_not_call_expensive_recovery():
    async def run():
        present = {("BUSYUSDT", "5m"), ("RECOVEREDUSDT", "5m")}
        busy_started = asyncio.Event()
        release_busy = asyncio.Event()
        fetched = []

        def scan():
            return RecoveryScan(
                scanned_count=800,
                candidates=tuple(_recovery_range(*key) for key in sorted(present)),
            )

        async def recover(candidate):
            fetched.append((candidate.symbol, candidate.interval))
            if candidate.symbol == "BUSYUSDT":
                busy_started.set()
                await release_busy.wait()
            return ((candidate.symbol, candidate.interval),)

        coordinator = GapRecoveryCoordinator(
            recover,
            candidate_provider=scan,
            still_needed=lambda candidate: (candidate.symbol, candidate.interval) in present,
            on_candles=lambda rows: present.difference_update(rows),
            concurrency=1,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(busy_started.wait(), timeout=1)
            present.discard(("RECOVEREDUSDT", "5m"))
            release_busy.set()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
            assert fetched == [("BUSYUSDT", "5m")]
            assert coordinator.skipped_count == 1
        finally:
            release_busy.set()
            await coordinator.stop()

    asyncio.run(run())


def test_expanding_same_key_gap_ranges_merge_into_one_followup_fetch():
    async def run():
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        requested = [_recovery_range("BTCUSDT", "5m", 0, 2)]
        fetch_ranges = []
        covered_until = NOW

        def scan():
            return RecoveryScan(scanned_count=800, candidates=tuple(requested))

        async def recover(candidate):
            nonlocal covered_until
            fetch_ranges.append((candidate.start_open, candidate.end_open))
            if len(fetch_ranges) == 1:
                first_started.set()
                await release_first.wait()
            return ((candidate.start_open, candidate.end_open),)

        def persist(rows):
            nonlocal covered_until
            covered_until = max(covered_until, *(end for _start, end in rows))

        coordinator = GapRecoveryCoordinator(
            recover,
            candidate_provider=scan,
            still_needed=lambda candidate: covered_until < candidate.end_open,
            on_candles=persist,
            concurrency=1,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(first_started.wait(), timeout=1)
            requested[:] = [_recovery_range("BTCUSDT", "5m", 0, 4)]
            assert coordinator.request()
            await asyncio.wait_for(
                _wait_for(lambda: coordinator.reconciliation_runs == 2), timeout=1
            )
            release_first.set()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
            assert fetch_ranges == [
                (requested[0].start_open, _recovery_range("BTCUSDT", "5m", 0, 2).end_open),
                (requested[0].start_open, requested[0].end_open),
            ]
            assert coordinator.admitted_count == 1
            assert coordinator.coalesced_count == 1
        finally:
            release_first.set()
            await coordinator.stop()

    asyncio.run(run())


def test_collector_scans_800_keys_but_admits_only_real_expected_bar_gaps():
    from quant_phase1.entrypoints import collector as collector_module

    service = CollectorService(Settings.from_env({"TRADING_MODE": "paper"}))
    service.selected_symbols = tuple(f"S{index}USDT" for index in range(200))
    now = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
    old_now = collector_module.utc_now
    collector_module.utc_now = lambda: now
    try:
        missing = {(f"S{index}USDT", interval) for index in range(5) for interval in ("5m", "15m", "1H", "4H")}
        for symbol in service.selected_symbols:
            for interval in ("5m", "15m", "1H", "4H"):
                expected = expected_latest_closed_open(now, interval)
                bar_open = expected - timedelta(seconds=INTERVAL_SECONDS[interval]) if (symbol, interval) in missing else expected
                service.store.add_closed_candles([_closed_candle_at(symbol, interval, bar_open)])
        scan = service._scan_recovery_candidates()
    finally:
        collector_module.utc_now = old_now

    assert scan.scanned_count == 800
    assert len(scan.candidates) == 20
    assert {(candidate.symbol, candidate.interval) for candidate in scan.candidates} == missing
    assert all(candidate.start_open == candidate.end_open for candidate in scan.candidates)


def test_collector_recovery_streams_ranges_in_configured_bounded_pages(monkeypatch):
    from quant_phase1.entrypoints import collector as collector_module

    now = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(collector_module, "utc_now", lambda: now)
    service = CollectorService(
        Settings.from_env({"TRADING_MODE": "paper", "UNIVERSE_LIMIT": "1", "KLINE_FETCH_LIMIT": "2"})
    )
    service.selected_symbols = ("BTCUSDT",)
    for interval in ("5m", "15m", "1H", "4H"):
        expected = expected_latest_closed_open(now, interval)
        old = expected - timedelta(seconds=INTERVAL_SECONDS[interval] * (3 if interval == "5m" else 0))
        service.store.add_closed_candles([_closed_candle_at("BTCUSDT", interval, old)])
    calls = []

    class FakeRest:
        async def get_candles(self, *, symbol, interval, limit, start_time, end_time):
            calls.append((interval, start_time, end_time, limit))
            rows = []
            cursor = start_time
            while cursor <= end_time:
                rows.append(_closed_candle_at(symbol, interval, cursor))
                cursor += timedelta(seconds=INTERVAL_SECONDS[interval])
            return rows

    async def run():
        await service._start_gap_recovery(FakeRest())
        coordinator = service.gap_recovery
        assert coordinator is not None
        try:
            assert coordinator.request()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=2)
        finally:
            await coordinator.stop()

    asyncio.run(run())
    five_minute_calls = [call for call in calls if call[0] == "5m"]
    assert len(five_minute_calls) == 2
    assert all(call[3] == 2 for call in five_minute_calls)
    assert five_minute_calls[0][2] + timedelta(seconds=INTERVAL_SECONDS["5m"]) == five_minute_calls[1][1]


def test_recovery_store_deduplicates_overlapping_closed_bar_ranges():
    store = WebSocketCanonicalStore(capacity=4)
    bars = [_closed_candle_at("BTCUSDT", "5m", NOW + timedelta(minutes=index)) for index in range(5)]
    store.add_closed_candles(bars)
    store.add_closed_candles([bars[3], _closed_candle_at("BTCUSDT", "5m", NOW + timedelta(minutes=5))])
    history = store.closed_klines["BTCUSDT"]["5m"]
    assert len(history) == 4
    assert [bar.bar_open_timestamp for bar in history] == [
        NOW + timedelta(minutes=index) for index in range(2, 6)
    ]


def test_completed_recovery_releases_range_metadata_before_a_later_gap():
    async def run():
        first_candidate = _recovery_range("BTCUSDT", "5m", 0, 0)
        current = [first_candidate]
        needed = True
        fetched = []

        def scan():
            return RecoveryScan(scanned_count=800, candidates=tuple(current))

        async def recover(candidate):
            fetched.append(candidate)
            return (candidate,)

        def persist(_rows):
            nonlocal needed
            needed = False

        coordinator = GapRecoveryCoordinator(
            recover,
            candidate_provider=scan,
            still_needed=lambda _candidate: needed,
            on_candles=persist,
            concurrency=1,
            max_work_items=800,
        )
        await coordinator.start()
        try:
            assert coordinator.request()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
            assert coordinator._ranges == {}
            current[:] = [_recovery_range("BTCUSDT", "5m", 10, 10)]
            needed = True
            assert coordinator.request()
            await asyncio.wait_for(coordinator.wait_idle(), timeout=1)
            assert fetched == [first_candidate, current[0]]
            assert coordinator._ranges == {}
        finally:
            await coordinator.stop()

    asyncio.run(run())


def _recovery_range(symbol: str, interval: str = "5m", start_minute: int = 0, end_minute: int = 0):
    from datetime import timedelta

    start = NOW + timedelta(minutes=start_minute)
    end = NOW + timedelta(minutes=end_minute)
    return RecoveryRange(symbol, interval, start, end)


async def _empty_recovery():
    return ()


async def _blocked_result(release: asyncio.Event):
    await release.wait()
    return ()


async def _wait_for(predicate):
    while not predicate():
        await asyncio.sleep(0.001)


def _closed_candle(symbol: str, interval: str) -> Candle:
    return _closed_candle_at(symbol, interval, NOW)


def _closed_candle_at(symbol: str, interval: str, bar_open: datetime) -> Candle:
    return Candle(
        symbol,
        interval,
        bar_open,
        Decimal("1"),
        Decimal("2"),
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
        Decimal("6"),
        bar_open,
        bar_open,
        bar_open,
        DataStatus.AVAILABLE,
        True,
        [],
    )
