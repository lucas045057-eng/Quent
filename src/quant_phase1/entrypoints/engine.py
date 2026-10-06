"""Long-running paper-only Stage1 engine."""

from __future__ import annotations

import argparse
import ctypes
import asyncio
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import partial
from datetime import datetime
import logging
import os
from pathlib import Path
import signal
import time

from ..adapters.bitget_v3.rest import BitgetV3UtaRestClient
from ..config import Settings
from ..contracts import DataStatus
from ..db import assert_schema_ready
from ..logging import configure_logging
from ..pipeline import MarketDataCollector
from strategies.runtime import screen_batch, stage1_results, persist_screening, sampled_instant
from ..repositories import Phase1Repository
from ..runtime import PeriodicScheduler, RuntimeHealthTracker, await_or_stop
from ..service import (
    mark_degraded,
    mark_running,
    persist_health,
    persist_market_batch,
    persist_stage1,
    write_health_file,
)
from ..time import utc_now
from quant_data_layer.errors import normalize_error
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.observability import (
    DatabaseSnapshot,
    ProcessRole,
    RuntimeSnapshot,
    SourceId,
    SourcePhase,
    SourceSnapshot,
    WorkClass,
    capture_process_snapshot,
)
from quant_phase2.runtime import Phase2DerivativeRuntime
from quant_phase2.persistence import Phase2Repository
from quant_phase3.enrichment import enrich_stage1_flow
from quant_phase3.persistence import Phase3Repository
from quant_phase9.config import Phase9RuntimeConfig, load_phase9_runtime_config


LOGGER = logging.getLogger("quant_phase1")


def _load_allocator_trim():
    """Optional glibc free-page release; never change live object ownership."""
    try:
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = (ctypes.c_size_t,)
        trim.restype = ctypes.c_int
        return trim
    except (AttributeError, OSError):
        return None


_allocator_trim = _load_allocator_trim()


def _process_rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _release_unused_allocator_pages() -> None:
    """Return only already-free pages after the large cycle's frame is gone.

    Unsupported allocators or a zero return are normal. This is engineering
    resource management and does not change data, readiness or trade rules.
    """
    if _allocator_trim is None:
        return
    before = _process_rss_kib()
    started = time.monotonic()
    try:
        released = bool(_allocator_trim(0))
    except Exception:
        LOGGER.warning("stage1_allocator_release_unavailable")
        return
    LOGGER.info(
        "stage1_allocator_release released=%s rss_before_kib=%s rss_after_kib=%s elapsed_ms=%.3f",
        released, before, _process_rss_kib(), (time.monotonic() - started) * 1000,
    )


def _run_database_cycle_owned(*args, **kwargs):
    # Keep reclamation inside the original single worker and admission lease.
    # Inner frame termination releases the full-market batch before trimming.
    try:
        return run_database_cycle(*args, **kwargs)
    finally:
        _release_unused_allocator_pages()

def _phase2_next_delay_seconds(*, interval_seconds: float, elapsed_seconds: float) -> float:
    return max(0.0, float(interval_seconds) - max(0.0, float(elapsed_seconds)))


def build_engine_observability_snapshot(
    settings: Settings,
    *,
    sampled_at_utc: datetime | None = None,
    shutdown_requested: bool = False,
    admission_controller: WorkAdmissionController | None = None,
    phase9_config: "Phase9RuntimeConfig | None" = None,
) -> RuntimeSnapshot:
    """Build the engine's bounded, process-local observability view."""

    sampled_at = sampled_at_utc or utc_now()
    configured_sources = (
        (SourceId.ENGINE_STAGE1, SourcePhase.PHASE1, True),
        (SourceId.ENGINE_PHASE2, SourcePhase.PHASE2, settings.phase2_enabled),
        (SourceId.PHASE5_CONTEXT, SourcePhase.PHASE5, settings.phase5_enabled),
        (SourceId.PHASE6_EXTERNAL_CONTEXT, SourcePhase.PHASE6, settings.phase6_enabled),
        (SourceId.PHASE6_AI, SourcePhase.PHASE6, settings.phase6_enabled),
        (SourceId.PHASE7_ONCHAIN, SourcePhase.PHASE7, settings.phase7_enabled),
        (SourceId.PHASE7_SPOT, SourcePhase.PHASE7, settings.phase7_enabled),
        (SourceId.PHASE9_EVALUATIONS, SourcePhase.PHASE9, phase9_config.enabled if phase9_config else False),
    )
    sources = tuple(
        SourceSnapshot.not_exposed(
            source_id=source_id,
            phase=phase,
            configured=configured,
            sampled_at_utc=sampled_at,
        )
        for source_id, phase, configured in configured_sources
    )
    process = capture_process_snapshot(
        ProcessRole.ENGINE,
        sampled_at_utc=sampled_at,
        admission_snapshot=admission_controller.snapshot() if admission_controller else None,
        shutdown_requested=shutdown_requested,
    )
    database = DatabaseSnapshot.not_exposed(sampled_at_utc=sampled_at)
    return RuntimeSnapshot(process=process, sources=sources, database=database)


def persistence_status(succeeded: bool) -> DataStatus:
    return DataStatus.AVAILABLE if succeeded else DataStatus.ERROR


def run_phase5_context_hook(
    settings: Settings,
    processed_at,
    *,
    connection=None,
    repository=None,
    runtime=None,
    batch=None,
    results=(),
) -> dict[str, int]:
    """Run the complete additive Phase 5 context lifecycle without changing Stage1."""
    if not settings.phase5_enabled:
        return {"loaded": 0, "persisted": 0, "errors": 0}
    from quant_phase5.runtime import Phase5Runtime, run_phase5_data_cycle
    from quant_phase1.contracts import DataStatus

    def write_phase5_health(snapshot) -> None:
        if repository is None:
            return
        status = {
            "AVAILABLE": DataStatus.AVAILABLE,
            # Phase 1's system_health enum has no PARTIAL value.  Preserve the
            # exact Phase 5 status in details and fail closed at the legacy
            # health boundary rather than claiming full availability.
            "PARTIAL": DataStatus.NOT_AVAILABLE,
            "STALE": DataStatus.STALE,
            "NOT_AVAILABLE": DataStatus.NOT_AVAILABLE,
            "ERROR": DataStatus.ERROR,
        }[snapshot.status.value]
        details = {**snapshot.details, "phase5_status": snapshot.status.value}

        def persist_health_rows(target_repository) -> None:
            target_repository.upsert_system_health("phase5-context", status, snapshot.checked_at, details)
            freshness = snapshot.details.get("freshness_evidence", {})
            if not isinstance(freshness, dict):
                return
            for source_key, evidence in sorted(freshness.items()):
                if str(source_key).startswith("timeframe:"):
                    continue
                if not isinstance(evidence, dict):
                    continue
                source_status = str(evidence.get("status", "NOT_AVAILABLE")).upper()
                row_status = {
                    "AVAILABLE": DataStatus.AVAILABLE,
                    "STALE": DataStatus.STALE,
                    "ERROR": DataStatus.ERROR,
                }.get(source_status, DataStatus.NOT_AVAILABLE)
                component = "phase5-freshness-" + "-".join(
                    part.lower() for part in str(source_key).split(":", 1)
                )
                target_repository.upsert_system_health(
                    component,
                    row_status,
                    snapshot.checked_at,
                    {
                        "phase5_status": source_status,
                        "data_quality": "PARTIAL" if source_status in {"STALE", "NOT_AVAILABLE"} else "AVAILABLE",
                        **evidence,
                    },
                )

        try:
            persist_health_rows(repository)
        except Exception:
            # A failed query can leave the original transaction unusable. Try
            # the health write on a fresh connection without changing data.
            try:
                import psycopg

                with psycopg.connect(settings.postgres_dsn) as health_connection:
                    persist_health_rows(Phase1Repository(health_connection))
            except Exception:
                LOGGER.warning("phase5_health_persist_unavailable")

    def write_phase5_recovery_event(snapshot, event) -> None:
        if repository is None:
            return
        payload = {"event": event.get("event", "recovered")}
        try:
            repository.insert_runtime_health_event(
                "phase5-context",
                "RUNNING",
                outage_started_at=event.get("outage_started_at"),
                recovered_at=snapshot.checked_at,
                reason=event.get("reason"),
                details=payload,
            )
        except Exception:
            try:
                import psycopg

                with psycopg.connect(settings.postgres_dsn) as health_connection:
                    Phase1Repository(health_connection).insert_runtime_health_event(
                        "phase5-context",
                        "RUNNING",
                        outage_started_at=event.get("outage_started_at"),
                        recovered_at=snapshot.checked_at,
                        reason=event.get("reason"),
                        details=payload,
                    )
            except Exception:
                LOGGER.warning("phase5_recovery_event_unavailable")

    runtime = runtime or Phase5Runtime(
        settings,
        health_writer=write_phase5_health,
        health_event_writer=write_phase5_recovery_event,
    )
    if runtime.health_writer is None:
        runtime.health_writer = write_phase5_health
    if runtime.health_event_writer is None:
        runtime.health_event_writer = write_phase5_recovery_event
    if batch is not None and connection is not None and repository is not None and repository.last_screening_run_id is not None:
        savepoint = "phase5_context_cycle"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            counts = run_phase5_data_cycle(
                batch,
                results,
                settings,
                connection=connection,
                screening_run_id=repository.last_screening_run_id,
                processed_at=processed_at,
            )
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            phase5_status = counts["phase5_status"]
            heartbeat = runtime.run_cycle(
                ({"key": f"phase5:{processed_at.isoformat()}", "status": phase5_status.value},),
                now=processed_at,
                context_status=phase5_status,
                health_details={
                    **counts.get("health_diagnostics", {}),
                    "freshness_evidence": counts.get("freshness_evidence", {}),
                },
            )
            return {
                "loaded": heartbeat.loaded,
                "persisted": sum(
                    value for key, value in counts.items()
                    if key not in {"universe_run_id", "phase5_status", "health_diagnostics", "freshness_evidence"}
                ),
                "errors": heartbeat.errors,
            }
        except Exception as exc:
            try:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                LOGGER.warning("phase5_savepoint_rollback_unavailable")
            failed = runtime.run_cycle(
                (),
                persist=lambda _rows: (_ for _ in ()).throw(exc),
                now=processed_at,
            )
            return {"loaded": 0, "persisted": 0, "errors": max(1, failed.errors)}
    result = runtime.run_from_connection(connection, now=processed_at) if connection is not None else runtime.run_cycle((), now=processed_at)
    return {"loaded": result.loaded, "persisted": result.persisted, "errors": result.errors}


def run_phase6_context_hook(
    settings: Settings,
    processed_at,
    *,
    screening_run_id: int | None = None,
    results=(),
    refs_by_symbol=None,
    runtime=None,
) -> dict[str, int]:
    """Build Phase 6 context after Stage1/Phase5 without altering eligibility.

    Source and provider adapters are intentionally supplied by a later
    runtime integration.  With no configured inputs this produces bounded
    ``NOT_AVAILABLE`` context and keeps the existing paper-only engine alive.
    """
    if not settings.phase6_enabled:
        return {"contexts": 0, "available": 0, "errors": 0}
    if screening_run_id is None or screening_run_id <= 0:
        return {"contexts": 0, "available": 0, "errors": 0}
    from quant_phase6.contracts import EventStatus
    from quant_phase6.enrichment import Phase6ContextRuntime

    try:
        context_runtime = runtime or Phase6ContextRuntime()
        contexts = context_runtime.build(
            tuple(results),
            screening_run_id=screening_run_id,
            refs_by_symbol=refs_by_symbol or {},
            processed_at=processed_at,
        )
        return {
            "contexts": len(contexts),
            "available": sum(context.status is EventStatus.AVAILABLE for context in contexts),
            "errors": sum(context.status is EventStatus.ERROR for context in contexts),
        }
    except Exception as exc:
        # Phase 6 is a context layer.  A source/provider failure must not
        # demote or replace the already-computed Stage1 result.
        LOGGER.warning("phase6_context_unavailable error=%s", normalize_error(exc).safe_summary)
        return {"contexts": 0, "available": 0, "errors": 1}


def persist_phase3_enrichment(repository: Phase1Repository, run_id: int, results: list, processed_at) -> int:
    """Persist flow as context-only data; never alter the Phase 1 result."""
    phase3 = Phase3Repository(repository.connection)
    enrichments = []
    for result in results:
        symbol = str(result.symbol).upper()
        canonical = f"{symbol[:-4]}-USDT-PERP" if symbol.endswith("USDT") else None
        windows, cvd_points, cross_snapshot = phase3.load_latest_flow_context(canonical) if canonical else ([], [], None)
        enrichments.append(
            enrich_stage1_flow(
                result,
                canonical_symbol=canonical,
                flow_windows=windows,
                cvd_points=cvd_points,
                cross_exchange_snapshot=cross_snapshot,
                processed_at=processed_at,
            )
        )
    return phase3.insert_stage1_flow_enrichment(run_id, enrichments)


def persist_phase4_enrichment(repository: Phase1Repository, run_id: int, results: list, processed_at) -> int:
    """Persist Phase 4 as context only; the Phase 1 result is never replaced."""
    from quant_phase4.contracts import BasisType, LongShortMetricType
    from quant_phase4.cross_exchange import build_phase4_context
    from quant_phase4.enrichment import enrich_stage1_phase4
    from quant_phase4.persistence import Phase4Repository

    phase4 = Phase4Repository(repository.connection)
    enrichments = []
    for result in results:
        symbol = str(result.symbol).upper()
        canonical = f"{symbol[:-4]}-USDT-PERP" if symbol.endswith("USDT") else None
        if canonical is None:
            continue
        liquidations, long_short, basis = phase4.load_latest_observations(
            canonical,
            long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
            long_short_period="5m",
            basis_type=BasisType.MARK_INDEX,
        )
        context = build_phase4_context(liquidations, long_short, basis, processed_at)
        enrichments.append(enrich_stage1_phase4(result, context, processed_at))
    if enrichments:
        phase4.insert_cross_exchange([enrichment.phase4_context for enrichment in enrichments])
    return phase4.insert_stage1_enrichment(run_id, enrichments)


def run_phase7_context_hook(settings: Settings, connection, screening_run_id, results, processed_at) -> dict[str, int]:
    """Persist Phase 7 context additively; isolate it from the Stage1 transaction."""
    if not settings.phase7_enabled or screening_run_id is None:
        return {"persisted": 0, "errors": 0}
    savepoint = "phase7_context_cycle"
    try:
        from quant_phase7.runtime import persist_stage1_phase7_context

        connection.execute(f"SAVEPOINT {savepoint}")
        persisted = persist_stage1_phase7_context(
            connection,
            screening_run_id=screening_run_id,
            candidates=results,
            processed_at=processed_at,
        )
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {"persisted": persisted, "errors": 0}
    except Exception as exc:  # noqa: BLE001 - Phase 7 cannot change Stage1 eligibility
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            LOGGER.warning("phase7_context_savepoint_rollback_unavailable")
        LOGGER.warning("phase7_context_enrichment_unavailable exception=%s", type(exc).__name__)
        return {"persisted": 0, "errors": 1}


async def run_once(
    settings: Settings | None = None, *, phase5_runtime=None,
    stage1_candidate_ttl_seconds: int | None = None,
) -> dict[str, int]:
    """Run one REST-backed cycle; retained for contract tests and --once."""
    settings = settings or Settings.from_env()
    tracker = RuntimeHealthTracker("quant-engine")
    write_health_file("quant-engine", "RUNNING")
    async with BitgetV3UtaRestClient(settings) as client:
        batch = await MarketDataCollector(
            client, max_symbols=settings.universe_limit, kline_limit=settings.kline_fetch_limit
        ).collect_once()
    screening = screen_batch(batch)
    results = stage1_results(screening)
    persisted = 0
    persistence_ok = False
    try:
        import psycopg

        with psycopg.connect(settings.postgres_dsn) as connection:
            assert_schema_ready(connection)
            repository = Phase1Repository(connection)
            persist_market_batch(
                repository,
                batch,
                retention_days=settings.kline_retention_days,
                market_snapshot_retention_days=settings.market_snapshot_retention_days,
            )
            persist_screening(connection, screening)
            persisted = persist_stage1(
                repository, batch, results,
                stage1_candidate_ttl_seconds=stage1_candidate_ttl_seconds,
            )
            if repository.last_screening_run_id is not None:
                Phase2Repository(connection).insert_stage1_enrichments(
                    repository.last_screening_run_id, results, utc_now()
                )
                if settings.phase3_enabled:
                    persist_phase3_enrichment(
                        repository, repository.last_screening_run_id, results, utc_now()
                    )
                if settings.phase4_enabled:
                    try:
                        persist_phase4_enrichment(
                            repository, repository.last_screening_run_id, results, utc_now()
                        )
                    except Exception as exc:
                        LOGGER.warning("phase4_enrichment_unavailable error=%s", normalize_error(exc).safe_summary)
                if settings.phase5_enabled:
                    run_phase5_context_hook(
                        settings, utc_now(), connection=connection, repository=repository, runtime=phase5_runtime,
                        batch=batch, results=results,
                    )
                if settings.phase6_enabled:
                    run_phase6_context_hook(
                        settings,
                        utc_now(),
                        screening_run_id=repository.last_screening_run_id,
                        results=results,
                    )
                run_phase7_context_hook(
                    settings,
                    connection,
                    repository.last_screening_run_id,
                    results,
                    utc_now(),
                )
            recovery = mark_running(tracker, "quant-engine")
            persist_health(
                repository,
                "quant-engine",
                tracker,
                details={"heartbeat": True, "symbols": len(results), "persisted": persisted},
                recovery=recovery,
            )
            persistence_ok = True
    except Exception as exc:
        mark_degraded(tracker, "quant-engine", exc)
        LOGGER.warning("persistence_unavailable error=%s", normalize_error(exc).safe_summary)
    available = sum(result.status.value == "AVAILABLE" for result in results)
    LOGGER.info(
        "stage1_cycle_complete",
        extra={
            "event_id": "stage1_cycle",
            "status": persistence_status(persistence_ok).value,
            "symbol": f"{available}_available",
            "classification": {key: sum(result.category == key for result in results) for key in "ABCD"},
        },
    )
    return {"symbols": len(results), "available": available, "persisted": persisted}


def _requested_screening_symbols() -> tuple[str, ...] | None:
    raw_symbols = os.environ.get("QUANT_REALTIME_PAPER_SYMBOLS")
    if raw_symbols is None:
        return None
    symbols = tuple(symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip())
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("QUANT_REALTIME_PAPER_SYMBOLS must be a unique non-empty list")
    return symbols


def run_database_cycle(
    settings: Settings, tracker: RuntimeHealthTracker, phase5_runtime=None, *,
    stage1_candidate_ttl_seconds: int | None = None,
) -> dict[str, int] | None:
    """Evaluate only collector-persisted canonical data, never private/live data."""
    try:
        import psycopg

        read_connection = psycopg.connect(settings.postgres_dsn, autocommit=True)
        try:
            assert_schema_ready(read_connection)
            # The screening assertion instant is when the inputs are sampled,
            # not when the (multi-second, full-universe) read finishes. Charging
            # the read duration to every source observation pushed the newest
            # ticker past the 5-second PRICE budget on every cycle, which made
            # every symbol fail Stage A for a data-plumbing reason rather than a
            # market one. Raise the instant only for a ticker that landed while
            # the read was in flight.
            as_of = utc_now()
            requested_symbols = _requested_screening_symbols()
            batch = Phase1Repository(read_connection).load_latest_market_batch(
                limit=max(settings.universe_limit, len(requested_symbols or ())),
                candle_limit=settings.kline_fetch_limit,
                requested_symbols=requested_symbols,
            )
            if batch is not None:
                as_of = sampled_instant(as_of, batch)
            screening = screen_batch(batch, now=as_of, connection=read_connection) if batch else None
        finally:
            read_connection.close()
        if batch is None:
            return None

        # Do not hold a database connection open while evaluating the configured symbol set.
        # The Candidate DB path is an SSH tunnel with an idle timeout; keeping
        # the read transaction open across local CPU work caused the next
        # INSERT to receive a server-closed-connection error.
        results = stage1_results(screening)
        with psycopg.connect(settings.postgres_dsn) as connection:
            assert_schema_ready(connection)
            repository = Phase1Repository(connection)
            persist_screening(connection, screening)
            persisted = persist_stage1(
                repository, batch, results,
                stage1_candidate_ttl_seconds=stage1_candidate_ttl_seconds,
            )
            if repository.last_screening_run_id is not None:
                Phase2Repository(connection).insert_stage1_enrichments(
                    repository.last_screening_run_id, results, utc_now()
                )
                if settings.phase3_enabled:
                    persist_phase3_enrichment(
                        repository, repository.last_screening_run_id, results, utc_now()
                    )
                if settings.phase4_enabled:
                    try:
                        persist_phase4_enrichment(
                            repository, repository.last_screening_run_id, results, utc_now()
                        )
                    except Exception as exc:
                        LOGGER.warning("phase4_enrichment_unavailable error=%s", normalize_error(exc).safe_summary)
                if settings.phase5_enabled:
                    run_phase5_context_hook(
                        settings, utc_now(), connection=connection, repository=repository, runtime=phase5_runtime,
                        batch=batch, results=results,
                    )
                if settings.phase6_enabled:
                    run_phase6_context_hook(
                        settings,
                        utc_now(),
                        screening_run_id=repository.last_screening_run_id,
                        results=results,
                    )
                run_phase7_context_hook(
                    settings,
                    connection,
                    repository.last_screening_run_id,
                    results,
                    utc_now(),
                )
            recovery = mark_running(tracker, "quant-engine")
            persist_health(
                repository,
                "quant-engine",
                tracker,
                details={"heartbeat": True, "symbols": len(results), "persisted": persisted, "source": "canonical_db"},
                recovery=recovery,
            )
        available = sum(result.status is DataStatus.AVAILABLE for result in results)
        LOGGER.info(
            "stage1_cycle_complete",
            extra={
                "event_id": "stage1_cycle",
                "status": "AVAILABLE",
                "symbol": f"{available}_available",
                "classification": {key: sum(result.category == key for result in results) for key in "ABCD"},
            },
        )
        return {"symbols": len(results), "available": available, "persisted": persisted}
    except Exception as exc:
        mark_degraded(tracker, "quant-engine", exc)
        LOGGER.warning("canonical_input_unavailable error=%s", normalize_error(exc).safe_summary)
        return {"symbols": 0, "available": 0, "persisted": 0}


async def _run_admitted_database_cycle(
    settings: Settings,
    tracker: RuntimeHealthTracker,
    phase5_runtime,
    admission: WorkAdmissionController,
    stage1_candidate_ttl_seconds: int | None = None,
    *, executor: Executor | None = None,
) -> dict[str, int] | None:
    request = make_work_request(
        phase=SourcePhase.PHASE1,
        source_id=SourceId.ENGINE_STAGE1,
        work_class=WorkClass.MEDIUM,
        estimated_items=max(1, settings.universe_limit),
        estimated_bytes=max(65_536, settings.universe_limit * 32_768),
        replay_class=ReplayClass.DERIVED_REPLACEABLE,
        cancellation_owner=ProcessRole.ENGINE,
        stream_id="phase1.stage1_results",
    )
    try:
        async with admission.admit(request):
            # Keep the event loop available for source clocks, leases and AI
            # deadlines. One cycle is awaited inside its original admission;
            # the repository opens/uses/closes its own connection in the worker.
            args = (settings, tracker) if phase5_runtime is None else (settings, tracker, phase5_runtime)
            # Reuse one owned worker for the large canonical batch. Sharing
            # asyncio's default pool spreads high-water malloc arenas across
            # unrelated source/research workers even after batch objects die.
            task = asyncio.get_running_loop().run_in_executor(
                executor, partial(_run_database_cycle_owned, *args,
                    stage1_candidate_ttl_seconds=stage1_candidate_ttl_seconds))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A canceled await cannot kill a Python thread. Retain admission
                # and join the existing work before releasing its ownership.
                # Further cancellation requests must not cancel the executor
                # Future or release admission while its thread still runs.
                while True:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        if task.done():
                            break
                    except Exception:
                        # Observe a worker exception, preserving caller cancel.
                        break
                    else:
                        break
                raise
    except AdmissionDeferred as exc:
        LOGGER.info("stage1_cycle_deferred reason=%s", exc.reason.value)
        return None


async def run_service(settings: Settings | None = None, *, stop_event: asyncio.Event | None = None) -> None:
    # The executor belongs to this original engine lifecycle, not a second
    # service. Its single worker also preserves Stage1 admission serialization.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="quant-stage1-db") as stage1_executor:
        await _run_service_owned(settings, stop_event=stop_event, stage1_executor=stage1_executor)


async def _run_service_owned(
    settings: Settings | None = None, *, stop_event: asyncio.Event | None = None,
    stage1_executor: Executor,
) -> None:
    settings = settings or Settings.from_env()
    stop_event = stop_event or asyncio.Event()
    tracker = RuntimeHealthTracker("quant-engine")
    admission = WorkAdmissionController(role=ProcessRole.ENGINE)
    phase9_config = load_phase9_runtime_config()
    phase9_runtime = None
    if phase9_config.enabled:
        from quant_data_layer.db_admission import PostgresWriteAdmission
        from quant_phase9.policy import load_approved_policy_manifest
        from quant_phase9.runtime import Phase9StrategyV2Evaluator, Phase9EngineRuntime
        from strategies.runtime import CanonicalResearchFactory, execution_policy_from_env

        policy_manifest = load_approved_policy_manifest(
            Path(os.environ.get("PHASE9_POLICY_MANIFEST_PATH", "policies/phase9_policy_v1.json")),
            Path(os.environ.get("PHASE9_POLICY_APPROVAL_PATH", "policies/phase9_policy_v1.approval.json")),
            expected_commit=os.environ["PHASE9_CODE_VERSION"],
        )
        phase9_db_admission = PostgresWriteAdmission()
        phase9_evaluator = Phase9StrategyV2Evaluator(
            input_factory=CanonicalResearchFactory(settings.postgres_dsn, policy=execution_policy_from_env()),
            dsn=settings.postgres_dsn,
            policy_manifest=policy_manifest,
            db_admission=phase9_db_admission,
            evaluation_timeout_seconds=phase9_config.evaluation_timeout_seconds,
        )
        phase9_runtime = Phase9EngineRuntime(
            config=phase9_config, dsn=settings.postgres_dsn,
            policy_manifest=policy_manifest, evaluator=phase9_evaluator,
            admission_controller=admission, db_admission=phase9_db_admission,
        )
    write_health_file("quant-engine", "RUNNING")
    phase5_runtime = None
    if settings.phase5_enabled:
        from quant_phase5.runtime import Phase5Runtime

        phase5_runtime = Phase5Runtime(settings)

    # Bootstrap once from public REST. Subsequent cycles consume canonical
    # collector data so WS -> canonical -> Stage1 is the normal path.
    if phase5_runtime is None:
        bootstrap = await await_or_stop(
            run_once(settings, stage1_candidate_ttl_seconds=phase9_config.stage1_candidate_ttl_seconds),
            stop_event,
        )
    else:
        bootstrap = await await_or_stop(
            run_once(
                settings, phase5_runtime=phase5_runtime,
                stage1_candidate_ttl_seconds=phase9_config.stage1_candidate_ttl_seconds,
            ),
            stop_event,
        )
    if bootstrap is None:
        await admission.shutdown(timeout_seconds=10.0)
        write_health_file("quant-engine", "STOPPED")
        return

    async def health_probe() -> None:
        while not stop_event.is_set():
            try:
                import psycopg

                with psycopg.connect(settings.postgres_dsn) as connection:
                    assert_schema_ready(connection)
                    repository = Phase1Repository(connection)
                    recovery = mark_running(tracker, "quant-engine")
                    persist_health(
                        repository,
                        "quant-engine",
                        tracker,
                        details={
                            "heartbeat": True,
                            "source": "health_probe",
                            "data_layer_observability": build_engine_observability_snapshot(
                                settings,
                                shutdown_requested=stop_event.is_set(),
                                admission_controller=admission,
                                phase9_config=phase9_config,
                            ).to_dict(),
                            "phase9_runtime": phase9_runtime.health() if phase9_runtime else {"state": "NOT_CONFIGURED"},
                        },
                        recovery=recovery,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                mark_degraded(tracker, "quant-engine", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                continue

    async def cycle() -> None:
        result = await _run_admitted_database_cycle(
            settings, tracker, phase5_runtime, admission,
            stage1_candidate_ttl_seconds=phase9_config.stage1_candidate_ttl_seconds,
            executor=stage1_executor,
        )
        if result and result.get("available", 0) == 0:
            LOGGER.info("no_high_confidence_candidate", extra={"event_id": "stage1_cycle", "status": "AVAILABLE"})

    async def phase2_cycle() -> None:
        runtime = Phase2DerivativeRuntime(settings)
        loop = asyncio.get_running_loop()
        while not stop_event.is_set():
            cycle_started = loop.time()
            try:
                result = await runtime.run_cycle()
                if result.errors:
                    # Console format omits classification extras. Keep bounded,
                    # token-free source codes visible for actual failures.
                    from collections import Counter
                    codes=Counter(error.rsplit(':',1)[-1] for error in result.errors)
                    LOGGER.warning('phase2_source_failures counts=%s',dict(codes.most_common(12)))
                LOGGER.info(
                    "phase2_derivatives_cycle_complete",
                    extra={
                        "event_id": "phase2_derivatives_cycle",
                        "status": "AVAILABLE" if not result.errors else "DEGRADED",
                        "symbol": result.symbols,
                        "classification": {
                            "oi_observations": result.oi_observations,
                            "funding_observations": result.funding_observations,
                            "snapshots": result.snapshots,
                            "errors": result.errors,
                        },
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Derivatives enrichment is failure-isolated from the Phase 1
                # Stage1 loop. A public exchange outage must not stop health
                # heartbeats or the existing paper-only engine.
                LOGGER.warning("phase2_derivatives_cycle_failed error=%s", normalize_error(exc).safe_summary)
            delay = _phase2_next_delay_seconds(
                interval_seconds=settings.phase2_interval_seconds,
                elapsed_seconds=loop.time() - cycle_started,
            )
            if delay <= 0:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    probe_task = asyncio.create_task(health_probe())
    phase2_task = asyncio.create_task(phase2_cycle()) if settings.phase2_enabled else None
    phase6_runtime = None
    phase6_task = None
    if settings.phase6_enabled:
        from quant_phase6.runtime import Phase6EngineRuntime

        phase6_runtime = Phase6EngineRuntime(settings)
        phase6_task = asyncio.create_task(
            phase6_runtime.run(stop_event),
            name="phase6-engine-supervisor",
        )
    phase7_runtime = None
    phase7_task = None
    if settings.phase7_enabled:
        from quant_phase7.runtime import Phase7EngineRuntime

        phase7_runtime = Phase7EngineRuntime(settings)
        phase7_task = asyncio.create_task(
            phase7_runtime.run(stop_event),
            name="phase7-engine-supervisor",
        )
    phase9_task = (
        asyncio.create_task(phase9_runtime.run(stop_event), name="phase9-engine-supervisor")
        if phase9_runtime is not None else None
    )
    try:
        await PeriodicScheduler(settings.stage1_interval_seconds, cycle).run(stop_event)
    finally:
        probe_task.cancel()
        tasks = [probe_task]
        if phase2_task is not None:
            phase2_task.cancel()
            tasks.append(phase2_task)
        if phase6_task is not None:
            phase6_task.cancel()
            tasks.append(phase6_task)
        if phase7_task is not None:
            phase7_task.cancel()
            tasks.append(phase7_task)
        if phase9_task is not None:
            stop_event.set()
            try:
                await asyncio.wait_for(phase9_task, timeout=35.0)
            except asyncio.TimeoutError:
                phase9_task.cancel()
                tasks.append(phase9_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        drained = await admission.shutdown(timeout_seconds=10.0)
        if not drained:
            LOGGER.error("engine_admission_shutdown_drain_timeout")
        write_health_file("quant-engine", "STOPPED")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one REST-backed Stage1 cycle and exit")
    args = parser.parse_args(argv)
    configure_logging()
    settings = Settings.from_env()
    if args.once:
        phase9_config = load_phase9_runtime_config()
        asyncio.run(run_once(
            settings, stage1_candidate_ttl_seconds=phase9_config.stage1_candidate_ttl_seconds,
        ))
        return

    async def run_until_signal() -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        previous_handlers: dict[signal.Signals, object] = {}

        def stop(*_: object) -> None:
            # A Python signal handler can run while the event loop is blocked
            # in selector.select().  Event.set() queues waiter callbacks but
            # does not wake that selector; call_soon_threadsafe writes to the
            # loop's self-pipe so shutdown is prompt even with long timers.
            loop.call_soon_threadsafe(stop_event.set)

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, stop)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
        try:
            await run_service(settings, stop_event=stop_event)
        finally:
            for signum, previous in previous_handlers.items():
                try:
                    signal.signal(signum, previous)
                except (NotImplementedError, RuntimeError, ValueError):
                    continue

    asyncio.run(run_until_signal())


if __name__ == "__main__":
    main()
