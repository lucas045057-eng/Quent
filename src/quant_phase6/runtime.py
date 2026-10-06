"""Lifecycle-owned Phase 6 source and AI workers.

Production starts with an empty source registry and no AI provider. Local source
and Fake Provider dependencies are supplied explicitly by tests only.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import threading
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID

from quant_phase1.config import Settings
from quant_phase1.contracts import DataStatus
from quant_phase1.db import assert_schema_ready
from quant_phase1.repositories import Phase1Repository
from quant_phase1.service import write_health_file
from quant_phase1.time import utc_now
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.observability import ProcessRole, SourceId, SourcePhase, WorkClass

from .ai import AIErrorCode, AIProvider, AIRequest, AIResult, AIService, BudgetDecision
from .contract_v1 import (
    NEWS_CLASSIFICATION_TASK,
    NewsClassificationOutputSchema,
    PreparedNewsClassification,
    TaskOutcome,
    TaskReason,
    TaskRegistry,
    build_news_classification_context,
    make_news_classification_request,
    pre_request_outcome,
    stable_execution_id,
    validate_news_classification_output,
)
from .contracts import EventStatus, MacroEvent, NewsEvent, UnlockEvent
from .ingestion import ExternalEventIngestor, SourceFetcher
from .normalization import FreshnessPolicy
from .persistence import Phase6Repository, cleanup_phase6
from .prompts import NEWS_CLASSIFICATION_PROMPTS
from .sources import SourceDefinition, SourceRegistry


LOGGER = logging.getLogger("quant_phase6")
_EVENT_KINDS = ("news", "macro", "unlock")
_RETRYABLE_AI_ERRORS = frozenset({
    AIErrorCode.TIMEOUT.value,
    AIErrorCode.RATE_LIMIT.value,
    AIErrorCode.TRANSPORT.value,
})


def _connect(settings: Settings):
    import psycopg

    return psycopg.connect(settings.postgres_dsn)


async def _owned_thread_call(function: Callable[..., Any], *args: Any) -> Any:
    """Keep ownership of blocking DB/source work through cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling asyncio.to_thread only cancels its waiter, not the worker
        # thread. Await the bounded operation before reporting lifecycle stop.
        try:
            await task
        except Exception:
            pass
        raise


@dataclass(frozen=True, slots=True)
class Phase6SourceBinding:
    kind: str
    definition: SourceDefinition
    fetcher: SourceFetcher

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError("Phase 6 source kind must be news, macro, or unlock")


class Phase6CollectorRuntime:
    """Collector-owned source ingestion, canonical persistence, and health loops."""

    def __init__(
        self,
        settings: Settings,
        *,
        bindings: Iterable[Phase6SourceBinding] = (),
        connection_factory: Callable[[], Any] | None = None,
        interval_seconds: float | None = None,
        now: Callable[[], datetime] = utc_now,
        admission: WorkAdmissionController | None = None,
    ) -> None:
        self.settings = settings
        self.admission = admission or WorkAdmissionController(role=ProcessRole.COLLECTOR)
        if self.admission.role is not ProcessRole.COLLECTOR:
            raise ValueError("Phase 6 collector runtime requires Collector-owned admission")
        self.bindings = tuple(bindings)
        self.connection_factory = connection_factory or (lambda: _connect(settings))
        self.interval_seconds = interval_seconds or settings.phase6_ingestion_interval_seconds
        self.now = now
        self.registry = SourceRegistry([binding.definition for binding in self.bindings])
        self._ingestors = {
            (binding.kind, binding.definition.source_id): ExternalEventIngestor(
                self.registry,
                binding.fetcher,
                freshness=FreshnessPolicy(
                    news_max_age=timedelta(hours=settings.phase6_news_freshness_hours),
                    macro_max_age=timedelta(days=settings.phase6_macro_freshness_days),
                    unlock_max_age=timedelta(days=settings.phase6_unlock_freshness_days),
                ),
                max_events=100,
            )
            for binding in self.bindings
        }
        self._last_cleanup: datetime | None = None
        self._retention_lock = threading.Lock()
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self.connection_factory() as connection:
                assert_schema_ready(connection, required_version="011_phase6_external_context.sql")
            self._initialized = True

    def _cycle(self, kind: str) -> dict[str, Any]:
        self._ensure_initialized()
        checked_at = self.now().astimezone(timezone.utc)
        selected = [binding for binding in self.bindings if binding.kind == kind]
        results = []
        with self.connection_factory() as connection:
            phase6 = Phase6Repository(connection)
            phase1 = Phase1Repository(connection)
            for binding in selected:
                ingestor = self._ingestors[(kind, binding.definition.source_id)]
                result = ingestor.ingest(
                    kind,
                    binding.definition.source_id,
                    observed_at=checked_at,
                    fetched_at=checked_at,
                    processed_at=checked_at,
                )
                results.append(result)
                phase6.upsert_source_registry(
                    (binding.definition,),
                    status=result.status.value,
                    processed_at=checked_at,
                )
            persisted = 0
            for result in results:
                if kind == "news":
                    persisted += phase6.upsert_news(event for event in result.events if isinstance(event, NewsEvent))
                elif kind == "macro":
                    persisted += phase6.upsert_macro(event for event in result.events if isinstance(event, MacroEvent))
                else:
                    persisted += phase6.upsert_unlock(event for event in result.events if isinstance(event, UnlockEvent))

            with self._retention_lock:
                if self._last_cleanup is None or checked_at - self._last_cleanup >= timedelta(hours=24):
                    cleanup_phase6(phase6, self.settings, now=checked_at)
                    self._last_cleanup = checked_at

            event_statuses = [result.status for result in results]
            phase6_status = _combine_event_statuses(event_statuses) if selected else EventStatus.NOT_AVAILABLE
            health_status = (
                DataStatus.ERROR if phase6_status is EventStatus.ERROR
                else DataStatus.AVAILABLE if persisted or phase6_status is EventStatus.AVAILABLE
                else DataStatus.NOT_AVAILABLE
            )
            details = {
                "lifecycle": "ACTIVE",
                "phase6_status": phase6_status.value,
                "reason_code": "SOURCE_NOT_CONFIGURED" if not selected else None,
                "configured": bool(selected),
                "source_count": len(selected),
                "persisted_events": persisted,
                "rejected_events": sum(result.rejected_count for result in results),
            }
            phase1.upsert_system_health(f"phase6-{kind}-ingestion", health_status, checked_at, details)
            phase1.upsert_system_health(
                "phase6-collector",
                health_status,
                checked_at,
                {"lifecycle": "ACTIVE", "kind": kind, **details},
            )
            phase1.upsert_system_health(
                "phase6-runtime",
                health_status,
                checked_at,
                {"lifecycle": "ACTIVE", "owner": "collector", "kind": kind, **details},
            )
            phase1.upsert_system_health(
                "phase6-persistence",
                DataStatus.AVAILABLE,
                checked_at,
                {"lifecycle": "ACTIVE", "owner": "collector", "kind": kind},
            )
        return {"kind": kind, "status": phase6_status.value, "persisted": persisted}

    async def run_cycle_once(self, kind: str | None = None) -> tuple[dict[str, Any], ...]:
        await _owned_thread_call(self._ensure_initialized)
        kinds = (kind,) if kind else _EVENT_KINDS
        if any(value not in _EVENT_KINDS for value in kinds):
            raise ValueError("unsupported Phase 6 event kind")
        results = []
        for selected_kind in kinds:
            request = make_work_request(
                phase=SourcePhase.PHASE6,
                source_id=SourceId.PHASE6_EXTERNAL_CONTEXT,
                work_class=WorkClass.MEDIUM,
                estimated_items=100,
                estimated_bytes=2 * 1024 * 1024,
                replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
                cancellation_owner=ProcessRole.COLLECTOR,
                stream_id=f"phase6.{selected_kind}_feed",
            )
            async with self.admission.admit(request):
                results.append(await _owned_thread_call(self._cycle, selected_kind))
        return tuple(results)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.phase6_enabled:
            return
        try:
            write_health_file("phase6-collector", "RUNNING")
        except Exception:
            LOGGER.warning("phase6_collector_health_file_unavailable")

        configured_kinds = {binding.kind for binding in self.bindings}
        async def loop(kind: str) -> None:
            while not stop_event.is_set():
                try:
                    await self.run_cycle_once(kind)
                except asyncio.CancelledError:
                    raise
                except AdmissionDeferred as exc:
                    LOGGER.info(
                        "phase6_ingestion_cycle_deferred kind=%s reason=%s",
                        kind,
                        exc.reason.value,
                    )
                except Exception as exc:
                    sqlstate = getattr(exc, "sqlstate", None)
                    if not isinstance(sqlstate, str) or not re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                        sqlstate = "UNAVAILABLE"
                    diagnostic = getattr(exc, "diag", None)
                    column = getattr(diagnostic, "column_name", None)
                    table = getattr(diagnostic, "table_name", None)
                    if sqlstate == "42703" and (not isinstance(column, str) or not column):
                        match = re.search(
                            r'column\s+"?([A-Za-z_][A-Za-z0-9_.]*)"?', str(exc)
                        )
                        if match is not None:
                            column = match.group(1)
                    column = column if isinstance(column, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", column) else "UNAVAILABLE"
                    table = table if isinstance(table, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table) else "UNAVAILABLE"
                    LOGGER.warning(
                        "phase6_ingestion_cycle_failed kind=%s failure_type=%s sqlstate=%s table=%s column=%s",
                        kind, type(exc).__name__, sqlstate, table, column,
                    )
                    await _owned_thread_call(self._write_cycle_failure_health, kind)
                if kind not in configured_kinds:
                    break
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    continue

        tasks: list[asyncio.Task[None]] = []
        try:
            for kind in _EVENT_KINDS:
                tasks.append(asyncio.create_task(loop(kind), name=f"phase6-{kind}-ingestion"))
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._write_stopped_health()
            write_health_file("phase6-collector", "STOPPED")

    def _write_stopped_health(self) -> None:
        checked_at = self.now().astimezone(timezone.utc)
        try:
            with self.connection_factory() as connection:
                phase1 = Phase1Repository(connection)
                for kind in _EVENT_KINDS:
                    phase1.upsert_system_health(
                        f"phase6-{kind}-ingestion", DataStatus.NOT_AVAILABLE, checked_at,
                        {"lifecycle": "STOPPED"},
                    )
                phase1.upsert_system_health(
                    "phase6-collector", DataStatus.NOT_AVAILABLE, checked_at,
                    {"lifecycle": "STOPPED"},
                )
                phase1.upsert_system_health(
                    "phase6-runtime", DataStatus.NOT_AVAILABLE, checked_at,
                    {"lifecycle": "STOPPED", "owner": "collector"},
                )
                phase1.upsert_system_health(
                    "phase6-persistence", DataStatus.NOT_AVAILABLE, checked_at,
                    {"lifecycle": "STOPPED", "owner": "collector"},
                )
        except Exception:
            LOGGER.warning("phase6_collector_shutdown_health_unavailable")

    def _write_cycle_failure_health(self, kind: str) -> None:
        checked_at = self.now().astimezone(timezone.utc)
        details = {
            "lifecycle": "ACTIVE",
            "phase6_status": EventStatus.ERROR.value,
            "reason_code": "ISOLATED_FAILURE",
            "kind": kind,
        }
        try:
            with self.connection_factory() as connection:
                repository = Phase1Repository(connection)
                repository.upsert_system_health(
                    f"phase6-{kind}-ingestion", DataStatus.ERROR, checked_at, details
                )
                repository.upsert_system_health(
                    "phase6-collector", DataStatus.ERROR, checked_at, details
                )
                repository.upsert_system_health(
                    "phase6-runtime", DataStatus.ERROR, checked_at,
                    {"owner": "collector", **details},
                )
                repository.upsert_system_health(
                    "phase6-persistence", DataStatus.ERROR, checked_at,
                    {"owner": "collector", **details},
                )
        except Exception:
            LOGGER.warning("phase6_ingestion_failure_health_unavailable kind=%s", kind)


@dataclass(frozen=True, slots=True)
class _NewsWorkItem:
    prepared: PreparedNewsClassification
    request: AIRequest
    execution_id: UUID
    attempt_number: int
    budget_decision: str


class Phase6EngineRuntime:
    """Engine-owned bounded AI queue/worker and task-health lifecycle."""

    def __init__(
        self,
        settings: Settings,
        *,
        providers: Mapping[str, AIProvider] | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        candidate_source_ids: Iterable[str] | None = None,
        connection_factory: Callable[[], Any] | None = None,
        poll_interval_seconds: float = 30.0,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self.providers = dict(providers or {})
        self.provider_name = provider_name if provider_name is not None else settings.phase6_ai_primary_provider
        self.model = model or ""
        self.candidate_source_ids = (
            tuple(candidate_source_ids) if candidate_source_ids is not None else None
        )
        if self.candidate_source_ids is not None and (
            not self.candidate_source_ids
            or any(not source.strip() for source in self.candidate_source_ids)
        ):
            raise ValueError("candidate_source_ids must be non-empty when explicitly provided")
        if self.providers and (not provider_name or not self.model):
            raise ValueError("provider and model require explicit test/provider injection")
        self.connection_factory = connection_factory or (lambda: _connect(settings))
        self.poll_interval_seconds = poll_interval_seconds
        self.now = now
        self.gateway = AIService.from_settings(self.providers, settings, now=now)
        self.registry = TaskRegistry.v1()
        self.queue: asyncio.Queue[_NewsWorkItem] = asyncio.Queue(maxsize=settings.phase6_ai_queue_capacity)
        self.last_outcomes: deque[TaskOutcome] = deque(maxlen=100)
        self._reason_counts: Counter[str] = Counter()
        self._outcome_lock = threading.Lock()
        self._scheduled_execution_ids: set[UUID] = set()
        self._candidate_offset = 0
        self._persistence_status = DataStatus.NOT_AVAILABLE
        self._budget_status = DataStatus.AVAILABLE
        self._initialized = False
        self._active = False
        self._tasks: list[asyncio.Task[None]] = []

    def _initialize(self) -> None:
        checked_at = self.now().astimezone(timezone.utc)
        with self.connection_factory() as connection:
            assert_schema_ready(connection, required_version="013_phase6_ai_contract_runtime.sql")
            repository = Phase6Repository(connection)
            definition = NEWS_CLASSIFICATION_PROMPTS.require(
                NEWS_CLASSIFICATION_TASK.prompt_id,
                NEWS_CLASSIFICATION_TASK.prompt_version,
                NEWS_CLASSIFICATION_TASK.contract_schema_version,
            )
            repository.insert_prompt_version(
                definition,
                prompt_hash=definition.definition_hash,
                created_at=checked_at,
                status="ACTIVE",
            )
        self._initialized = True
        self._persistence_status = DataStatus.AVAILABLE

    def _load_candidates(self) -> tuple[NewsEvent, ...]:
        with self.connection_factory() as connection:
            return Phase6Repository(connection).load_news_classification_candidates(
                limit=100, offset=self._candidate_offset,
                source_ids=self.candidate_source_ids,
            )

    def _execution_state(self, request_hash: str):
        with self.connection_factory() as connection:
            return Phase6Repository(connection).latest_ai_execution_state(request_hash)

    async def run_cycle_once(self) -> dict[str, int]:
        if not self._initialized:
            await _owned_thread_call(self._initialize)
        events = await _owned_thread_call(self._load_candidates)
        self._candidate_offset += len(events)
        if len(events) < 100:
            # The scan reached its end; start another bounded pass so
            # retryable outcomes can be reconsidered after their cooldown.
            self._candidate_offset = 0
        counts: Counter[str] = Counter()
        for event in events:
            prepared_or_outcome = build_news_classification_context(
                event,
                now=self.now().astimezone(timezone.utc),
                max_age=timedelta(hours=self.settings.phase6_news_freshness_hours),
            )
            if isinstance(prepared_or_outcome, TaskOutcome):
                self._record_outcome(prepared_or_outcome)
                counts[prepared_or_outcome.reason_code.value if prepared_or_outcome.reason_code else "NO_REASON"] += 1
                continue
            prepared = prepared_or_outcome
            if not self.provider_name or not self.model or self.provider_name not in self.providers:
                outcome = pre_request_outcome(prepared, TaskReason.NOT_CONFIGURED)
                self._record_outcome(outcome)
                counts[TaskReason.NOT_CONFIGURED.value] += 1
                continue

            request = make_news_classification_request(
                prepared,
                provider=self.provider_name,
                model=self.model,
                timeout_seconds=self.settings.phase6_ai_timeout_seconds,
                max_output_bytes=self.settings.phase6_max_ai_output_bytes,
            )
            previous = await _owned_thread_call(self._execution_state, request.request_hash)
            attempt_number = 1
            if previous is not None:
                previous_status, previous_error, last_attempt_at, attempt_count = previous
                if (
                    previous_status not in {EventStatus.ERROR.value, EventStatus.NOT_AVAILABLE.value}
                    or previous_error not in _RETRYABLE_AI_ERRORS
                ):
                    counts["IDEMPOTENT_REPLAY_SKIPPED"] += 1
                    continue
                if last_attempt_at is not None:
                    retry_after = last_attempt_at + timedelta(
                        seconds=self.settings.phase6_ai_retry_interval_seconds
                    )
                    if self.now().astimezone(timezone.utc) < retry_after:
                        counts["RETRY_BACKOFF"] += 1
                        continue
                # Older analyses can predate durable usage linkage. Their
                # existence is still a previous attempt when count(*) is 0.
                attempt_number = max(2, attempt_count + 1)
            execution_id = stable_execution_id(
                prepared.event_id_hash, request.request_hash, attempt_number
            )
            if execution_id in self._scheduled_execution_ids:
                counts["ALREADY_SCHEDULED"] += 1
                continue

            decision = self.gateway.budget.decision(request.estimated_cost, self.now().astimezone(timezone.utc))
            cached = self.gateway.cache.get(request.request_hash, self.now().astimezone(timezone.utc))
            if cached is None and decision is not BudgetDecision.ALLOW:
                self._budget_status = DataStatus.NOT_AVAILABLE
                reason = (
                    TaskReason.SOFT_BUDGET_DEGRADED
                    if decision is BudgetDecision.SOFT_DEGRADE
                    else TaskReason.HARD_BUDGET_STOP
                )
                result = AIResult(
                    EventStatus.NOT_AVAILABLE, None, request.provider, request.model,
                    None, request.request_hash, AIErrorCode.BUDGET,
                )
                await _owned_thread_call(
                    self._persist_result, prepared, request, execution_id, result, None,
                    decision.value, attempt_number,
                )
                outcome = TaskOutcome(
                    task_id=prepared.context.task_id,
                    event_id_hash=prepared.event_id_hash,
                    input_context_hash=prepared.context.context_hash,
                    status=EventStatus.NOT_AVAILABLE,
                    reason_code=reason,
                    request_hash=request.request_hash,
                    execution_id=execution_id,
                    provider=request.provider,
                    model=request.model,
                )
                self._record_outcome(outcome)
                counts[reason.value] += 1
                continue
            self._budget_status = DataStatus.AVAILABLE

            item = _NewsWorkItem(prepared, request, execution_id, attempt_number, decision.value)
            self._scheduled_execution_ids.add(execution_id)
            try:
                self.queue.put_nowait(item)
                counts["QUEUED"] += 1
            except asyncio.QueueFull:
                self._scheduled_execution_ids.discard(execution_id)
                failure = AIResult(
                    EventStatus.ERROR, None, request.provider, request.model, None,
                    request.request_hash, AIErrorCode.QUEUE_FULL,
                )
                await _owned_thread_call(
                    self._persist_result, prepared, request, execution_id, failure,
                    None, "ALLOW", attempt_number,
                )
                outcome = TaskOutcome(
                    prepared.context.task_id, prepared.event_id_hash, prepared.context.context_hash,
                    EventStatus.ERROR, TaskReason.QUEUE_FULL, request_hash=request.request_hash,
                    execution_id=execution_id, provider=request.provider, model=request.model,
                )
                self._record_outcome(outcome)
                counts[TaskReason.QUEUE_FULL.value] += 1

        await self._write_health()
        return dict(counts)

    def _persist_result(
        self,
        prepared: PreparedNewsClassification,
        request: AIRequest,
        execution_id: UUID,
        result: AIResult,
        validated,
        budget_decision: str,
        attempt_number: int = 1,
    ) -> int:
        task_status = result.status
        if result.status is EventStatus.AVAILABLE and validated is not None and validated.event_type is None:
            task_status = EventStatus.NOT_AVAILABLE
        try:
            with self.connection_factory() as connection:
                analysis_id = Phase6Repository(connection).persist_news_classification_execution(
                    request,
                    result,
                    event_id_hash=prepared.event_id_hash,
                    validated=validated,
                    execution_id=execution_id,
                    task_status=task_status,
                    recorded_at=self.now().astimezone(timezone.utc),
                    budget_decision="CACHE_HIT" if result.cache_hit else budget_decision,
                    retry_attempt=attempt_number > 1,
                )
            self._persistence_status = DataStatus.AVAILABLE
            return analysis_id
        except Exception:
            self._persistence_status = DataStatus.ERROR
            raise

    def _execute_work_item(self, item: _NewsWorkItem) -> TaskOutcome:
        validated: list[Any] = []

        def validate_evidence(output: Mapping[str, Any]) -> None:
            validated.append(validate_news_classification_output(output, item.prepared))

        def persist(result: AIResult) -> None:
            resolved = validated[-1] if result.status is EventStatus.AVAILABLE and validated else None
            self._persist_result(
                item.prepared, item.request, item.execution_id, result, resolved,
                item.budget_decision, item.attempt_number,
            )

        result = self.gateway.complete(
            item.request,
            NewsClassificationOutputSchema(),
            fallback_provider=None,
            evidence_validator=validate_evidence,
            persist_result=persist,
        )
        resolved = validated[-1] if result.status is EventStatus.AVAILABLE and validated else None
        reason = _reason_from_result(result)
        if result.status is EventStatus.AVAILABLE and resolved is not None and resolved.event_type is None:
            status = EventStatus.NOT_AVAILABLE
            reason = TaskReason.NO_CLASSIFICATION
        elif result.status is EventStatus.AVAILABLE and resolved is not None:
            status = EventStatus.AVAILABLE
            reason = None
        else:
            status = result.status
        outcome = TaskOutcome(
            task_id=item.prepared.context.task_id,
            event_id_hash=item.prepared.event_id_hash,
            input_context_hash=item.prepared.context.context_hash,
            status=status,
            reason_code=reason,
            request_hash=item.request.request_hash,
            execution_id=item.execution_id,
            candidate=resolved.event_type if resolved else None,
            evidence_refs=resolved.evidence_refs if resolved else (),
            provider=result.provider,
            model=result.model,
            usage=result.usage,
        )
        self._record_outcome(outcome)
        return outcome

    async def _worker_loop(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await _owned_thread_call(self._execute_work_item, item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log exception text: provider and data exceptions are
                # untrusted and may carry sensitive request metadata.
                LOGGER.warning("phase6_ai_worker_item_failed reason=ISOLATED_FAILURE")
                with self._outcome_lock:
                    self._reason_counts[TaskReason.PERSISTENCE_ERROR.value] += 1
            finally:
                self._scheduled_execution_ids.discard(item.execution_id)
                self.queue.task_done()
                await self._write_health()

    async def _poll_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_cycle_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("phase6_ai_poll_failed reason=ISOLATED_FAILURE")
                await self._write_health(force_error=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _health_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._write_health()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                continue

    async def _write_health(self, *, force_error: bool = False) -> None:
        configured = bool(self.provider_name and self.model and self.provider_name in self.providers)
        worker_status = DataStatus.ERROR if force_error else (
            DataStatus.AVAILABLE if configured else DataStatus.NOT_AVAILABLE
        )
        runtime_status = DataStatus.ERROR if force_error else (
            DataStatus.AVAILABLE if self._active else DataStatus.NOT_AVAILABLE
        )
        reason = "WORKER_CYCLE_ERROR" if force_error else (
            None if configured else TaskReason.NOT_CONFIGURED.value
        )
        with self._outcome_lock:
            processed_reason_counts = dict(self._reason_counts)
        details = {
            "lifecycle": "ACTIVE" if self._active else "STARTING",
            "provider_status": "NOT_CONFIGURED" if not configured else "CONFIGURED",
            "reason_code": reason,
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "processed_reason_counts": processed_reason_counts,
        }
        try:
            checked_at = self.now().astimezone(timezone.utc)
            with self.connection_factory() as connection:
                repository = Phase1Repository(connection)
                repository.upsert_system_health(
                    "phase6-ai-worker", worker_status, checked_at, details
                )
                repository.upsert_system_health(
                    "phase6-ai-runtime", runtime_status, checked_at,
                    {"lifecycle": "ACTIVE" if self._active else "STARTING", "owner": "engine", "degraded": not configured, "reason_code": reason},
                )
                repository.upsert_system_health(
                    "phase6-ai-provider",
                    DataStatus.AVAILABLE if configured else DataStatus.NOT_AVAILABLE,
                    checked_at,
                    {"status": "CONFIGURED" if configured else "NOT_CONFIGURED"},
                )
                queue_status = DataStatus.ERROR if force_error else DataStatus.AVAILABLE
                repository.upsert_system_health(
                    "phase6-ai-queue", queue_status, checked_at,
                    {"lifecycle": "ACTIVE" if self._active else "STARTING", "depth": self.queue.qsize(), "capacity": self.queue.maxsize},
                )
                repository.upsert_system_health(
                    "phase6-ai-budget", self._budget_status, checked_at,
                    {"last_decision_status": self._budget_status.value},
                )
                repository.upsert_system_health(
                    "phase6-persistence", self._persistence_status, checked_at,
                    {"lifecycle": "ACTIVE" if self._active else "STARTING", "owner": "engine"},
                )
            write_health_file("phase6-ai-worker", worker_status.value, reason=reason)
        except Exception:
            LOGGER.warning("phase6_ai_health_write_unavailable")

    def _write_stopped_health(self) -> None:
        try:
            checked_at = self.now().astimezone(timezone.utc)
            with self.connection_factory() as connection:
                repository = Phase1Repository(connection)
                stopped_details = {"lifecycle": "STOPPED", "queue_depth": self.queue.qsize()}
                for component in (
                    "phase6-ai-worker", "phase6-ai-queue", "phase6-ai-provider",
                    "phase6-ai-budget", "phase6-persistence", "phase6-ai-runtime",
                ):
                    repository.upsert_system_health(
                        component, DataStatus.NOT_AVAILABLE, checked_at, stopped_details,
                    )
            write_health_file("phase6-ai-worker", "STOPPED")
        except Exception:
            LOGGER.warning("phase6_ai_shutdown_health_unavailable")

    def _record_outcome(self, outcome: TaskOutcome) -> None:
        with self._outcome_lock:
            self.last_outcomes.append(outcome)
            if outcome.reason_code is not None:
                self._reason_counts[outcome.reason_code.value] += 1

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.phase6_enabled:
            return
        self._active = True
        try:
            write_health_file("phase6-ai-worker", "RUNNING")
        except Exception:
            LOGGER.warning("phase6_engine_health_file_unavailable")
        workers: list[asyncio.Task[None]] = []
        poller: asyncio.Task[None] | None = None
        health: asyncio.Task[None] | None = None
        try:
            try:
                await _owned_thread_call(self._initialize)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("phase6_engine_startup_degraded reason=ISOLATED_FAILURE")
                await self._write_health(force_error=True)
            for index in range(self.settings.phase6_ai_concurrency):
                workers.append(asyncio.create_task(
                    self._worker_loop(), name=f"phase6-ai-worker-{index + 1}"
                ))
            health = asyncio.create_task(self._health_loop(stop_event), name="phase6-ai-health")
            poller = asyncio.create_task(self._poll_loop(stop_event), name="phase6-ai-poller")
            self._tasks = [*workers, poller, health]
            await stop_event.wait()
        finally:
            owned = [task for task in (poller, health) if task is not None]
            for task in owned:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            try:
                await asyncio.wait_for(self.queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._active = False
            await _owned_thread_call(self._write_stopped_health)
            self._tasks.clear()


def _combine_event_statuses(statuses: Iterable[EventStatus]) -> EventStatus:
    values = tuple(statuses)
    if not values:
        return EventStatus.NOT_AVAILABLE
    if all(value is EventStatus.ERROR for value in values):
        return EventStatus.ERROR
    if all(value is EventStatus.STALE for value in values):
        return EventStatus.STALE
    if all(value is EventStatus.NOT_AVAILABLE for value in values):
        return EventStatus.NOT_AVAILABLE
    if any(value in {EventStatus.ERROR, EventStatus.PARTIAL} for value in values):
        return EventStatus.PARTIAL
    return EventStatus.AVAILABLE


def _reason_from_result(result: AIResult) -> TaskReason | None:
    if result.error_code is None:
        return None
    return {
        AIErrorCode.TIMEOUT: TaskReason.TIMEOUT,
        AIErrorCode.RATE_LIMIT: TaskReason.RATE_LIMIT,
        AIErrorCode.AUTHENTICATION: TaskReason.AUTHENTICATION,
        AIErrorCode.TRANSPORT: TaskReason.TRANSPORT_EXHAUSTED,
        AIErrorCode.PROVIDER_REJECTED: TaskReason.PROVIDER_REJECTED,
        AIErrorCode.INVALID_JSON: TaskReason.INVALID_JSON,
        AIErrorCode.SCHEMA_ERROR: TaskReason.SCHEMA_ERROR,
        AIErrorCode.BUDGET: TaskReason.HARD_BUDGET_STOP,
        AIErrorCode.POLICY_BLOCK: TaskReason.POLICY_BLOCK,
        AIErrorCode.QUEUE_FULL: TaskReason.QUEUE_FULL,
        AIErrorCode.EVIDENCE_ERROR: TaskReason.EVIDENCE_ERROR,
        AIErrorCode.PERSISTENCE_ERROR: TaskReason.PERSISTENCE_ERROR,
    }[result.error_code]
