from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib
import json
from enum import StrEnum

import pytest


try:
    observability = importlib.import_module("quant_data_layer.observability")
    _observability_import_error = None
except ModuleNotFoundError as exc:
    observability = None
    _observability_import_error = exc


SAMPLED_AT = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)


def _api():
    assert observability is not None, (
        "Stage 1 observability contract is not implemented yet: "
        f"{_observability_import_error}"
    )
    return observability


def test_measured_zero_is_distinct_from_not_exposed():
    api = _api()

    measured_zero = api.Measurement.available(0)
    unknown = api.Measurement.not_exposed(api.MeasurementReason.NOT_INSTRUMENTED)

    assert measured_zero.state is api.MeasurementState.AVAILABLE
    assert measured_zero.value == 0
    assert unknown.state is api.MeasurementState.NOT_EXPOSED
    assert unknown.value is None
    assert unknown.reason is api.MeasurementReason.NOT_INSTRUMENTED


def test_source_contract_includes_required_fields_without_invented_zeroes():
    api = _api()
    snapshot = api.SourceSnapshot.not_exposed(
        source_id=api.SourceId.PHASE1_MARKET_DATA,
        phase=api.SourcePhase.PHASE1,
        configured=True,
        sampled_at_utc=SAMPLED_AT,
    )

    required = {
        "lifecycle_state", "data_status", "last_source_timestamp_utc",
        "last_fetched_at_utc", "last_processed_at_utc", "last_persisted_at_utc",
        "cursor_kind", "cursor_value", "cursor_updated_at_utc", "freshness_age_seconds",
        "queue_depth", "queue_capacity", "queue_bytes", "oldest_pending_age_seconds",
        "pending_work", "active_work", "backfill_depth", "retry_count",
        "consecutive_failures", "last_error_category", "last_success_at_utc",
        "admission_wait_seconds", "db_transaction_class", "reconnect_count",
        "gap_count", "drop_count",
    }
    assert required == {metric.value for metric in api.SourceMetric}
    for name in required:
        metric = snapshot.metric(api.SourceMetric(name))
        assert metric.state is api.MeasurementState.NOT_EXPOSED
        assert metric.value is None


def test_source_registry_rejects_high_cardinality_symbols():
    api = _api()

    with pytest.raises(ValueError):
        api.SourceId("BTCUSDT")


def test_runtime_snapshot_bounds_registered_source_count():
    api = _api()
    process = api.ProcessSnapshot.not_exposed(api.ProcessRole.COLLECTOR, sampled_at_utc=SAMPLED_AT)
    database = api.DatabaseSnapshot.not_exposed(sampled_at_utc=SAMPLED_AT)
    sources = tuple(
        api.SourceSnapshot.not_exposed(
            source_id=source_id,
            phase=api.SourcePhase.PHASE1,
            configured=True,
            sampled_at_utc=SAMPLED_AT,
        )
        for source_id in list(api.SourceId)[: api.MAX_SOURCE_SNAPSHOTS + 1]
    )

    with pytest.raises(ValueError, match="source"):
        api.RuntimeSnapshot(process=process, sources=sources, database=database)


def test_observability_contract_requires_utc_timestamps():
    api = _api()

    with pytest.raises(ValueError, match="UTC"):
        api.SourceSnapshot.not_exposed(
            source_id=api.SourceId.PHASE1_MARKET_DATA,
            phase=api.SourcePhase.PHASE1,
            configured=True,
            sampled_at_utc=datetime(2026, 9, 26, 7, 0),
        )


def test_observability_values_reject_credentials_and_raw_payloads():
    api = _api()

    with pytest.raises(ValueError):
        api.Measurement.available("https://rpc.example/secret-token")
    with pytest.raises(ValueError):
        api.Measurement.available("test-secret-value")

    class UnsafeToken(StrEnum):
        VALUE = "test-secret-value"

    with pytest.raises(ValueError):
        api.Measurement.available(UnsafeToken.VALUE)
    assert api.Measurement.available("AVAILABLE").value == "AVAILABLE"
    with pytest.raises(TypeError):
        api.Measurement.available({"raw_payload": {"large": "body"}})

    source = api.SourceSnapshot.not_exposed(
        source_id=api.SourceId.PHASE1_MARKET_DATA,
        phase=api.SourcePhase.PHASE1,
        configured=True,
        sampled_at_utc=SAMPLED_AT,
    )
    process = api.ProcessSnapshot.not_exposed(api.ProcessRole.COLLECTOR, sampled_at_utc=SAMPLED_AT)
    database = api.DatabaseSnapshot.not_exposed(sampled_at_utc=SAMPLED_AT)
    encoded = json.dumps(api.RuntimeSnapshot(process=process, sources=(source,), database=database).to_dict())

    assert "raw_payload" not in encoded
    assert "authorization" not in encoded.lower()
    assert "secret-token" not in encoded


def test_process_snapshot_keeps_admission_unknown_until_instrumented():
    api = _api()

    uninstrumented = api.ProcessSnapshot.not_exposed(api.ProcessRole.COLLECTOR, sampled_at_utc=SAMPLED_AT)
    assert all(
        uninstrumented.metric(metric).state is api.MeasurementState.NOT_EXPOSED
        for metric in api.ProcessMetric
    )

    async def capture():
        return api.capture_process_snapshot(api.ProcessRole.COLLECTOR, sampled_at_utc=SAMPLED_AT)

    process = asyncio.run(capture())
    assert process.metric(api.ProcessMetric.ASYNC_TASK_COUNT).state is api.MeasurementState.AVAILABLE
    assert process.metric(api.ProcessMetric.ASYNC_TASK_COUNT).value >= 1
    for metric in (
        api.ProcessMetric.LIGHT_ACTIVE,
        api.ProcessMetric.LIGHT_PENDING,
        api.ProcessMetric.MEDIUM_ACTIVE,
        api.ProcessMetric.MEDIUM_PENDING,
        api.ProcessMetric.HEAVY_ACTIVE,
        api.ProcessMetric.HEAVY_PENDING,
    ):
        assert process.metric(metric).state is api.MeasurementState.NOT_EXPOSED
    assert process.metric(api.ProcessMetric.CGROUP_MEMORY_CURRENT_BYTES).state in {
        api.MeasurementState.AVAILABLE,
        api.MeasurementState.NOT_EXPOSED,
    }


def test_process_snapshot_exports_controller_active_pending_and_oldest_wait():
    from datetime import timedelta

    from quant_data_layer.admission import (
        AdmissionLimits,
        ReplayClass,
        WorkAdmissionController,
        WorkAdmissionRequest,
    )

    api = _api()
    role = api.ProcessRole.COLLECTOR
    limits = AdmissionLimits(
        max_active=1,
        max_active_items=2,
        max_active_bytes=128,
        max_pending_requests=2,
        max_pending_items=2,
        max_pending_bytes=128,
        max_active_by_class={work: 1 for work in api.WorkClass},
        weights={work: 1 for work in api.WorkClass},
    )
    controller = WorkAdmissionController(role=role, limits=limits)

    def request(identity):
        return WorkAdmissionRequest(
            request_id=identity,
            phase=api.SourcePhase.PHASE1,
            source_id=api.SourceId.PHASE1_MARKET_DATA,
            work_class=api.WorkClass.LIGHT,
            estimated_items=1,
            estimated_bytes=64,
            deadline_utc=datetime.now(timezone.utc) + timedelta(seconds=1),
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=role,
        )

    async def capture_while_queued():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def active():
            async with controller.admit(request("active")):
                entered.set()
                await release.wait()

        async def queued():
            async with controller.admit(request("queued")):
                pytest.fail("queued work must remain pending")

        active_task = asyncio.create_task(active())
        await entered.wait()
        queued_task = asyncio.create_task(queued())
        await asyncio.sleep(0.02)
        process = api.capture_process_snapshot(
            role, sampled_at_utc=SAMPLED_AT, admission_snapshot=controller.snapshot()
        )
        queued_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        release.set()
        await active_task
        return process

    process = asyncio.run(capture_while_queued())
    assert process.metric(api.ProcessMetric.LIGHT_ACTIVE).value == 1
    assert process.metric(api.ProcessMetric.LIGHT_PENDING).value == 1
    assert process.metric(api.ProcessMetric.LIGHT_OLDEST_WAIT_SECONDS).value >= 0.01
    assert process.metric(api.ProcessMetric.MEDIUM_ACTIVE).value == 0
    assert process.metric(api.ProcessMetric.HEAVY_PENDING).value == 0


def test_database_snapshot_does_not_report_unknown_counters_as_zero():
    api = _api()
    database = api.DatabaseSnapshot.not_exposed(sampled_at_utc=SAMPLED_AT)

    assert all(
        database.metric(metric).state is api.MeasurementState.NOT_EXPOSED
        for metric in api.DatabaseMetric
    )
    assert database.metric(api.DatabaseMetric.CONNECTIONS).state is api.MeasurementState.NOT_EXPOSED
    assert database.metric(api.DatabaseMetric.CONNECTIONS).value is None
    assert database.metric(api.DatabaseMetric.WAL_BYTES).value is None
    assert {row.transaction_class for row in database.transaction_classes} == set(api.TransactionClass)
    assert all(
        measurement.state is api.MeasurementState.NOT_EXPOSED
        for row in database.transaction_classes
        for measurement in (row.pending, row.active, row.wait_seconds, row.hold_seconds, row.timeouts)
    )


def test_collector_observability_reads_queues_without_consuming_them():
    api = _api()
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.collector import CollectorService

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
        "PHASE3_ENABLED": "0",
        "PHASE4_ENABLED": "0",
        "PHASE6_ENABLED": "0",
    })
    service = CollectorService(settings)
    service.events.append({"channel": "test-counter-only"})

    snapshot = service.observability_snapshot(sampled_at_utc=SAMPLED_AT)
    market = next(source for source in snapshot.sources if source.source_id is api.SourceId.PHASE1_MARKET_DATA)

    assert market.metric(api.SourceMetric.QUEUE_DEPTH).value == 1
    assert market.metric(api.SourceMetric.QUEUE_BYTES).state is api.MeasurementState.NOT_EXPOSED
    assert service.events.depth == 1
    assert snapshot.process.role is api.ProcessRole.COLLECTOR
    assert all(source.sampled_at_utc == SAMPLED_AT for source in snapshot.sources)


def test_collector_observability_does_not_create_phase3_health_state():
    _api()
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.collector import CollectorService

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
        "PHASE3_ENABLED": "1",
        "PHASE4_ENABLED": "0",
        "PHASE6_ENABLED": "0",
    })
    service = CollectorService(settings)

    before = dict(service.phase3_runtime.health._states)
    snapshot = service.observability_snapshot(sampled_at_utc=SAMPLED_AT)
    after = dict(service.phase3_runtime.health._states)

    assert before == after == {}
    assert len(snapshot.sources) <= observability.MAX_SOURCE_SNAPSHOTS


def test_engine_observability_is_process_local_and_config_aware():
    api = _api()
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.engine import build_engine_observability_snapshot

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": "postgresql://quant:quant@localhost/quant",
        "PHASE2_ENABLED": "0",
        "PHASE5_ENABLED": "0",
        "PHASE6_ENABLED": "0",
        "PHASE7_ENABLED": "0",
    })

    snapshot = build_engine_observability_snapshot(settings, sampled_at_utc=SAMPLED_AT)

    assert snapshot.process.role is api.ProcessRole.ENGINE
    stage1 = next(source for source in snapshot.sources if source.source_id is api.SourceId.ENGINE_STAGE1)
    phase2 = next(source for source in snapshot.sources if source.source_id is api.SourceId.ENGINE_PHASE2)
    assert stage1.configured is True
    assert phase2.configured is False
    assert phase2.metric(api.SourceMetric.LIFECYCLE_STATE).value == "NOT_CONFIGURED"
