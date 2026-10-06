from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import inspect

import pytest

import quant_data_layer.admission as admission
from quant_data_layer.admission import (
    AdmissionDeferred,
    AdmissionLimits,
    ReplayClass,
    WorkAdmissionController,
    WorkAdmissionRequest,
)
from quant_data_layer.observability import ProcessMetric, ProcessRole, SourceId, SourcePhase, WorkClass


def _limits(**overrides):
    values = {
        "max_active": 3,
        "max_active_items": 12,
        "max_active_bytes": 1200,
        "max_pending_requests": 8,
        "max_pending_items": 24,
        "max_pending_bytes": 2400,
        "max_active_by_class": {
            WorkClass.LIGHT: 1,
            WorkClass.MEDIUM: 1,
            WorkClass.HEAVY: 2,
        },
        "weights": {WorkClass.LIGHT: 4, WorkClass.MEDIUM: 2, WorkClass.HEAVY: 1},
        "aging_interval_seconds": 0.02,
    }
    values.update(overrides)
    return AdmissionLimits(**values)


def _request(work_class=WorkClass.LIGHT, *, role=ProcessRole.COLLECTOR, **overrides):
    values = {
        "request_id": f"request-{work_class.value.lower()}",
        "phase": SourcePhase.PHASE1,
        "source_id": SourceId.PHASE1_MARKET_DATA,
        "work_class": work_class,
        "estimated_items": 1,
        "estimated_bytes": 64,
        "deadline_utc": datetime.now(timezone.utc) + timedelta(seconds=2),
        "replay_class": ReplayClass.RECOVERABLE_REPLAYABLE,
        "cancellation_owner": role,
    }
    values.update(overrides)
    return WorkAdmissionRequest(**values)


@pytest.mark.asyncio
async def test_collector_and_engine_controllers_own_independent_permits():
    limits = _limits(max_active=1, max_active_by_class={
        WorkClass.LIGHT: 1,
        WorkClass.MEDIUM: 1,
        WorkClass.HEAVY: 1,
    })
    collector = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    engine = WorkAdmissionController(role=ProcessRole.ENGINE, limits=limits)

    async with collector.admit(_request(role=ProcessRole.COLLECTOR)):
        async with engine.admit(_request(role=ProcessRole.ENGINE)):
            assert collector.snapshot().active == 1
            assert engine.snapshot().active == 1


@pytest.mark.asyncio
async def test_requests_must_match_the_process_cancellation_owner():
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=_limits())

    with pytest.raises(AdmissionDeferred) as caught:
        async with controller.admit(_request(role=ProcessRole.ENGINE)):
            pytest.fail("wrong process owner must not receive a permit")
    assert caught.value.reason.value == "OWNER_MISMATCH"


@pytest.mark.asyncio
async def test_active_and_pending_items_and_bytes_stay_within_configured_bounds():
    limits = _limits(max_active_items=2, max_active_bytes=100, max_pending_items=2, max_pending_bytes=100)
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    too_large = _request(estimated_items=3, estimated_bytes=20)

    with pytest.raises(AdmissionDeferred):
        async with controller.admit(too_large):
            pytest.fail("oversized work must be deferred")

    async with controller.admit(_request(estimated_items=2, estimated_bytes=100)):
        snapshot = controller.snapshot()
        assert snapshot.active_items == 2
        assert snapshot.active_bytes == 100


@pytest.mark.asyncio
async def test_snapshot_exposes_oldest_wait_per_registered_class_without_request_identity():
    limits = _limits(
        max_active=1,
        max_active_by_class={WorkClass.LIGHT: 1, WorkClass.MEDIUM: 1, WorkClass.HEAVY: 1},
    )
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_permit():
        async with controller.admit(_request(WorkClass.MEDIUM, request_id="active")):
            entered.set()
            await release.wait()

    async def wait_for_permit():
        async with controller.admit(_request(WorkClass.LIGHT, request_id="private-request-id")):
            pytest.fail("the queued request must remain blocked")

    active_task = asyncio.create_task(hold_permit())
    await entered.wait()
    waiter_task = asyncio.create_task(wait_for_permit())
    await asyncio.sleep(0.03)

    snapshot = controller.snapshot()
    assert snapshot.oldest_pending_wait_seconds_by_class[WorkClass.LIGHT] >= 0.02
    assert snapshot.oldest_pending_wait_seconds_by_class[WorkClass.MEDIUM] == 0
    assert "private-request-id" not in repr(snapshot)
    assert "oldest_pending_wait_seconds_by_class" in snapshot.to_dict()

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    release.set()
    await active_task


@pytest.mark.asyncio
async def test_light_control_progresses_while_heavy_work_is_admitted():
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=_limits())
    heavy_started = [asyncio.Event(), asyncio.Event()]
    release_heavy = asyncio.Event()
    light_completed = asyncio.Event()

    async def heavy(index):
        async with controller.admit(_request(WorkClass.HEAVY, request_id=f"heavy-{index}")):
            heavy_started[index].set()
            await release_heavy.wait()

    async def light():
        async with controller.admit(_request(WorkClass.LIGHT, request_id="light-health")):
            light_completed.set()

    heavy_tasks = [asyncio.create_task(heavy(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in heavy_started))
    await asyncio.wait_for(light(), timeout=0.2)
    assert light_completed.is_set()
    release_heavy.set()
    await asyncio.gather(*heavy_tasks)


@pytest.mark.asyncio
async def test_aging_promotes_waiting_class_without_indefinite_starvation():
    limits = _limits(
        max_active=1,
        max_active_by_class={WorkClass.LIGHT: 1, WorkClass.MEDIUM: 1, WorkClass.HEAVY: 1},
        weights={WorkClass.LIGHT: 1, WorkClass.MEDIUM: 1, WorkClass.HEAVY: 8},
        aging_interval_seconds=0.01,
    )
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    entered = []
    first_release = asyncio.Event()
    light_entered = asyncio.Event()

    async def run(label, work_class, release):
        async with controller.admit(_request(work_class, request_id=label)):
            entered.append(label)
            if label == "aged-light":
                light_entered.set()
            await release.wait()

    active_release = asyncio.Event()
    active_task = asyncio.create_task(run("active", WorkClass.MEDIUM, active_release))
    await asyncio.sleep(0)
    light_task = asyncio.create_task(run("aged-light", WorkClass.LIGHT, first_release))
    await asyncio.sleep(0.12)
    heavy_task = asyncio.create_task(run("heavy-waiter", WorkClass.HEAVY, first_release))
    await asyncio.sleep(0.01)
    active_release.set()
    await asyncio.wait_for(light_entered.wait(), timeout=0.2)

    assert entered[0] == "active"
    assert entered[1] == "aged-light"
    first_release.set()
    await asyncio.gather(active_task, heavy_task, light_task)


@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_and_active_permit_is_released():
    limits = _limits(max_active=1, max_active_by_class={
        WorkClass.LIGHT: 1,
        WorkClass.MEDIUM: 1,
        WorkClass.HEAVY: 1,
    })
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    active_entered = asyncio.Event()
    release_active = asyncio.Event()

    async def active():
        async with controller.admit(_request()):
            active_entered.set()
            await release_active.wait()

    async def waiting():
        async with controller.admit(_request(request_id="waiting")):
            pytest.fail("cancelled waiter must never run")

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    waiter_task = asyncio.create_task(waiting())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    assert controller.snapshot().pending == 0

    active_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active_task
    assert controller.snapshot().active == 0
    release_active.set()


@pytest.mark.asyncio
async def test_deadline_expires_as_a_deferred_outcome_and_does_not_hold_a_permit():
    limits = _limits(max_active=1, max_active_by_class={
        WorkClass.LIGHT: 1,
        WorkClass.MEDIUM: 1,
        WorkClass.HEAVY: 1,
    })
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def active():
        async with controller.admit(_request()):
            entered.set()
            await release.wait()

    task = asyncio.create_task(active())
    await entered.wait()
    expired = _request(request_id="expired", deadline_utc=datetime.now(timezone.utc) + timedelta(milliseconds=20))
    with pytest.raises(AdmissionDeferred) as caught:
        async with controller.admit(expired):
            pytest.fail("expired request must not execute")
    assert caught.value.reason.value == "DEADLINE"
    assert controller.snapshot().pending == 0
    release.set()
    await task


@pytest.mark.asyncio
async def test_failed_attempt_releases_permit_before_retry_backoff_sleep():
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=_limits())

    with pytest.raises(RuntimeError):
        async with controller.admit(_request()):
            raise RuntimeError("temporary source failure")
    assert controller.snapshot().active == 0
    await asyncio.sleep(0)
    assert controller.snapshot().active == 0

    async with controller.admit(_request(request_id="retry")):
        assert controller.snapshot().active == 1


@pytest.mark.asyncio
async def test_shutdown_cancels_queued_work_and_drains_owned_active_work():
    limits = _limits(max_active=1, max_active_by_class={
        WorkClass.LIGHT: 1,
        WorkClass.MEDIUM: 1,
        WorkClass.HEAVY: 1,
    })
    controller = WorkAdmissionController(role=ProcessRole.COLLECTOR, limits=limits)
    active_entered = asyncio.Event()
    release_active = asyncio.Event()

    async def active():
        async with controller.admit(_request()):
            active_entered.set()
            await release_active.wait()

    async def queued():
        async with controller.admit(_request(request_id="queued")):
            pytest.fail("shutdown must cancel queued work")

    active_task = asyncio.create_task(active())
    await active_entered.wait()
    queued_task = asyncio.create_task(queued())
    await asyncio.sleep(0)
    shutdown_task = asyncio.create_task(controller.shutdown(timeout_seconds=0.5))
    with pytest.raises(AdmissionDeferred):
        await queued_task
    assert not active_task.cancelled()
    release_active.set()
    await active_task
    assert await shutdown_task is True
    assert controller.snapshot().active == 0
    with pytest.raises(AdmissionDeferred):
        async with controller.admit(_request(request_id="after-close")):
            pytest.fail("closed controller must not admit work")


def test_request_requires_utc_deadline_and_bounded_safe_identity():
    with pytest.raises(ValueError, match="UTC"):
        _request(deadline_utc=datetime(2026, 9, 26, 7, 0))
    with pytest.raises(ValueError, match="request_id"):
        _request(request_id="https://provider.example?token=hidden")


def test_work_request_factory_creates_safe_identity_and_bounded_utc_deadline():
    assert hasattr(admission, "make_work_request")
    before = datetime.now(timezone.utc)
    request = admission.make_work_request(
        phase=SourcePhase.PHASE3,
        source_id=SourceId.PHASE3_BITGET_TRADES,
        work_class=WorkClass.HEAVY,
        estimated_items=1,
        estimated_bytes=1024,
        replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
        cancellation_owner=ProcessRole.COLLECTOR,
        timeout_seconds=3,
    )

    assert request.phase is SourcePhase.PHASE3
    assert request.cancellation_owner is ProcessRole.COLLECTOR
    assert request.deadline_utc.tzinfo is timezone.utc
    assert before < request.deadline_utc <= before + timedelta(seconds=4)
    assert "request_id" not in repr(request)
    with pytest.raises(ValueError, match="timeout_seconds"):
        admission.make_work_request(
            phase=SourcePhase.PHASE3,
            source_id=SourceId.PHASE3_BITGET_TRADES,
            work_class=WorkClass.HEAVY,
            estimated_items=1,
            estimated_bytes=1024,
            replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            timeout_seconds=float("inf"),
        )


def test_engine_stage1_cycle_uses_independent_replaceable_admission(monkeypatch):
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints import engine as engine_entrypoint

    assert hasattr(engine_entrypoint, "_run_admitted_database_cycle")
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": "postgresql://unused/quant",
        "UNIVERSE_LIMIT": "24",
    })
    state = {"active": False, "observed": [], "requests": []}

    class AdmissionSpy:
        role = ProcessRole.ENGINE

        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    def database_cycle(*_args, stage1_candidate_ttl_seconds=None):
        state["observed"].append(state["active"])
        return {"symbols": 24}

    monkeypatch.setattr(engine_entrypoint, "run_database_cycle", database_cycle)
    result = asyncio.run(engine_entrypoint._run_admitted_database_cycle(
        settings, object(), None, AdmissionSpy()
    ))

    assert result == {"symbols": 24}
    assert state["observed"] == [True]
    assert state["requests"][0].phase is SourcePhase.PHASE1
    assert state["requests"][0].work_class is WorkClass.MEDIUM
    assert state["requests"][0].replay_class is ReplayClass.DERIVED_REPLACEABLE
    assert state["requests"][0].cancellation_owner is ProcessRole.ENGINE


def test_engine_health_snapshot_exports_its_local_admission_metrics():
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints import engine as engine_entrypoint

    assert "admission_controller" in inspect.signature(
        engine_entrypoint.build_engine_observability_snapshot
    ).parameters
    settings = Settings.from_env({"TRADING_MODE": "paper", "POSTGRES_DSN": "postgresql://unused/quant"})
    controller = WorkAdmissionController(role=ProcessRole.ENGINE)
    snapshot = engine_entrypoint.build_engine_observability_snapshot(
        settings, admission_controller=controller
    )

    assert snapshot.process.metric(ProcessMetric.LIGHT_ACTIVE).value == 0
    assert snapshot.process.metric(ProcessMetric.LIGHT_ACTIVE).state.value == "AVAILABLE"
