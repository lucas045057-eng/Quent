"""Bounded Engine-owned Phase 9 supervision over the durable outbox."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any, Protocol
from uuid import uuid4
from dataclasses import replace
from contextlib import contextmanager
from threading import Timer

from quant_data_layer.admission import (
    AdmissionDeferred, ReplayClass, WorkAdmissionController, make_work_request,
)
from quant_data_layer.db_admission import DbWorkClass, PostgresWriteAdmission
from quant_data_layer.observability import ProcessRole, SourceId, SourcePhase, WorkClass
from quant_phase1.db import assert_schema_ready

from .canonical import canonical_bytes, evaluation_id_for
from .config import Phase9RuntimeConfig
from .contracts import EvaluationIdentityV1, Stage1CandidateEventV1
from .intake import AdmissionDispositionV1, ack, admit_event, claim_pending
from .policy import ApprovedPolicyManifestV1


LOGGER = logging.getLogger("quant_phase9.runtime")
_TIMEFRAMES = {"15m": 15, "1H": 60, "4H": 240}


@contextmanager
def _database_deadline(conn, deadline):
    if deadline is None:
        yield conn
        return
    remaining = deadline-time.monotonic()
    if remaining<=0:
        raise TimeoutError('Phase 9 database deadline reached')
    def cancel():
        try:
            conn.cancel_safe(timeout=2)
        except Exception:
            pass  # Deadline checks still prohibit any final write.
    timer = Timer(remaining,cancel)
    timer.daemon = True
    timer.start()
    try:
        yield conn
    except Exception as error:
        if time.monotonic()>=deadline:
            raise TimeoutError('Phase 9 database deadline reached') from error
        raise
    finally:
        timer.cancel()
        timer.join(timeout=2.1)


class Phase9EventEvaluator(Protocol):
    """Completes all admitted timeframes durably before acknowledging the event."""

    def __call__(
        self, event: Stage1CandidateEventV1,
        identities: tuple[EvaluationIdentityV1, ...], consumer_name: str,
    ) -> None: ...


def _identity(event: Stage1CandidateEventV1, timeframe: str,
              manifest: ApprovedPolicyManifestV1) -> EvaluationIdentityV1:
    end = event.source_as_of
    return EvaluationIdentityV1(
        stage1_candidate_id=event.stage1_candidate_id, market=event.market,
        symbol=event.symbol, timeframe=timeframe,
        evaluation_window_start=end - timedelta(minutes=_TIMEFRAMES[timeframe]),
        evaluation_window_end=end,
        policy_generation=f"{manifest.manifest_version}:{manifest.manifest_digest}",
        material_change_generation="0",
    )


class Phase9EngineRuntime:
    """One supervisor, no process queue; PostgreSQL owns restart recovery."""

    def __init__(
        self, *, config: Phase9RuntimeConfig, dsn: str,
        policy_manifest: ApprovedPolicyManifestV1 | None = None,
        evaluator: Phase9EventEvaluator | None = None,
        connection_factory: Callable[..., Any] | None = None,
        admission_controller: WorkAdmissionController | None = None,
        db_admission: PostgresWriteAdmission | None = None,
        consumer_name: str | None = None, poll_seconds: float = 1.0,
    ) -> None:
        if not isinstance(config, Phase9RuntimeConfig):
            raise TypeError("config must be Phase9RuntimeConfig")
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("dsn must be configured")
        if config.enabled and not isinstance(policy_manifest, ApprovedPolicyManifestV1):
            raise ValueError("enabled Phase 9 requires an approved policy manifest")
        if config.enabled and evaluator is None:
            raise ValueError("enabled Phase 9 requires a durable evaluation processor")
        if consumer_name is None:
            consumer_name = f"phase9-engine-{uuid4().hex}"
        if not isinstance(consumer_name, str) or not consumer_name or len(consumer_name) > 64:
            raise ValueError("consumer_name must be a bounded identifier")
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0 < poll_seconds <= 30:
            raise ValueError("poll_seconds must be in (0, 30]")
        self.config = config
        self.dsn = dsn
        self.policy_manifest = policy_manifest
        self.evaluator = evaluator
        if isinstance(evaluator, (Phase9DeterministicEvaluator, Phase9StrategyV2Evaluator)):
            evaluator.evaluation_timeout_seconds = min(evaluator.evaluation_timeout_seconds,
                                                       config.evaluation_timeout_seconds)
        self._owns_admission = admission_controller is None
        self.admission = admission_controller or WorkAdmissionController(role=ProcessRole.ENGINE)
        self.db_admission = db_admission or PostgresWriteAdmission(connection_factory=connection_factory)
        self.consumer_name = consumer_name
        self.poll_seconds = float(poll_seconds)
        self._state = "NOT_CONFIGURED" if not config.enabled else "INITIALIZING"
        self._queue_depth = 0
        self._queue_bytes = 0
        self._completed = 0
        self._deferred = 0
        self._failures = 0
        self._observations_skipped = 0
        self._stopping = False

    def health(self) -> dict[str, object]:
        """Bounded aggregate; never exposes candidate IDs, symbols, or secrets."""
        return {
            "state": self._state, "queue_depth": self._queue_depth,
            "queue_bytes": self._queue_bytes, "completed": self._completed,
            "deferred": self._deferred, "failures": self._failures,
            "observations_skipped": self._observations_skipped,
            "real_jev_status": "NOT_CONFIGURED",
        }

    def _claim_and_admit(self) -> tuple[tuple[Stage1CandidateEventV1, tuple[EvaluationIdentityV1, ...]], ...]:
        assert self.policy_manifest is not None
        now = datetime.now(timezone.utc)
        page_limit = min(self.config.max_inflight_evaluations, self.config.max_queued_ids)
        observation_count = 0
        with self.db_admission.transaction(
            self.dsn, work_class=DbWorkClass.LIGHT,
            timeout_seconds=2.0, identity="phase9-intake",
        ) as conn:
            assert_schema_ready(conn, required_version="016_phase9_evidence_chain.sql")
            events = claim_pending(
                conn, consumer_name=self.consumer_name, limit=page_limit,
                now=now, lease_until=now + timedelta(seconds=120),
            )
            pending = []
            for event in events:
                if (isinstance(self.evaluator, Phase9StrategyV2Evaluator)
                        and event.stage1_policy_version == "QUANT_PAPER_V2"
                        and event.canonical_payload.get("stage1_projection", {}).get("category") == "B"):
                    # Recover valid B events emitted before the producer fix.
                    # ACK only drains intake: no evaluation, research or decision.
                    ack(conn, event_id=event.event_id, consumer_name=self.consumer_name, now=now)
                    observation_count += 1
                    continue
                identities = tuple(
                    _identity(event, timeframe, self.policy_manifest)
                    for timeframe in _TIMEFRAMES
                )
                states = []
                for identity in identities:
                    disposition = admit_event(conn, event=event, identity=identity, now=now)
                    if disposition is AdmissionDispositionV1.INVALID_EVENT:
                        raise ValueError("Phase 9 intake rejected event")
                    evaluation_id = evaluation_id_for(identity)
                    if event.candidate_valid_until is not None and event.candidate_valid_until <= now:
                        conn.execute(
                            """UPDATE phase9_evaluations
                               SET evaluation_state='CANCELLED', intake_disposition='EXPIRED',
                                   reason_code='STAGE1_EXPIRED', lease_owner=NULL,
                                   lease_expires_at=NULL, updated_at=%s
                               WHERE evaluation_id=%s AND evaluation_state IN
                                   ('QUEUED', 'RUNNING', 'WAITING_JEV')""",
                            (now, evaluation_id),
                        )
                    state = conn.execute(
                        "SELECT evaluation_state FROM phase9_evaluations WHERE evaluation_id=%s",
                        (evaluation_id,),
                    ).fetchone()
                    if state is None:
                        raise ValueError("Phase 9 intake ledger row is missing")
                    states.append(state[0])
                if all(state in {"CANCELLED", "COMPLETED"} for state in states):
                    ack(conn, event_id=event.event_id, consumer_name=self.consumer_name, now=now)
                    continue
                if any(state != "QUEUED" and state != "RUNNING" for state in states):
                    raise ValueError("Phase 9 intake ledger states are inconsistent")
                for identity in identities:
                    conn.execute(
                        """UPDATE phase9_evaluations
                           SET evaluation_state='RUNNING', lease_owner=%s,
                               lease_expires_at=%s, attempt_count=attempt_count+1,
                               updated_at=%s
                           WHERE evaluation_id=%s""",
                        (self.consumer_name, now + timedelta(seconds=120), now,
                         evaluation_id_for(identity)),
                    )
                pending.append((event, identities))
            admitted = tuple(pending)
        # Count committed observation drains, never failed/rolled-back writes.
        self._observations_skipped += observation_count
        return admitted

    async def poll_once(self) -> int:
        if not self.config.enabled or self._stopping:
            return 0
        assert self.evaluator is not None
        await asyncio.to_thread(self.maintain_lifecycle)
        batch = await asyncio.to_thread(self._claim_and_admit)
        bytes_used = sum(len(canonical_bytes(event.canonical_payload)) for event, _ in batch)
        if len(batch) > self.config.max_queued_ids or bytes_used > self.config.max_queued_bytes:
            raise RuntimeError("Phase 9 claimed page exceeds bounded queue")
        self._queue_depth = len(batch)
        self._queue_bytes = bytes_used
        processed = 0
        try:
            for event, identities in batch:
                request = make_work_request(
                    phase=SourcePhase.PHASE9, source_id=SourceId.PHASE9_EVALUATIONS,
                    work_class=WorkClass.MEDIUM, estimated_items=1,
                    estimated_bytes=min(64 * 1024, self.config.max_queued_bytes),
                    replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
                    cancellation_owner=ProcessRole.ENGINE,
                    stream_id="phase9.evaluations",
                    timeout_seconds=self.config.evaluation_timeout_seconds,
                )
                try:
                    async with self.admission.admit(request):
                        await asyncio.to_thread(self.evaluator, event, identities, self.consumer_name)
                    self._completed += 1
                    processed += 1
                except AdmissionDeferred:
                    self._deferred += 1
                    break
                except Exception as exc:
                    await asyncio.to_thread(self._record_failure,event,identities,exc)
                    raise
                finally:
                    self._queue_depth -= 1
                    self._queue_bytes -= len(canonical_bytes(event.canonical_payload))
        finally:
            self._queue_depth = 0
            self._queue_bytes = 0
        return processed

    def request_revalidation(self, *, stage1_candidate_id, timeframe):
        from .revalidation import request
        assert self.policy_manifest is not None
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                timeout_seconds=2.0,identity='phase9-material-notification') as conn:
            return request(conn,stage1_candidate_id=stage1_candidate_id,timeframe=timeframe,
                policy_generation=f'{self.policy_manifest.manifest_version}:{self.policy_manifest.manifest_digest}',
                now=datetime.now(timezone.utc))

    async def poll_revalidations(self):
        from .revalidation import claim
        if not isinstance(self.evaluator,(Phase9DeterministicEvaluator,Phase9StrategyV2Evaluator)):
            return 0
        def claimed():
            with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                    timeout_seconds=2.0,identity='phase9-revalidation-claim') as conn:
                assert_schema_ready(conn,required_version='018_phase9_revalidation.sql')
                return claim(conn,policy=self.policy_manifest,consumer=self.consumer_name,
                    now=datetime.now(timezone.utc),limit=self.config.max_inflight_evaluations,
                    max_per_minute=self.config.max_revalidations_per_minute)
        jobs = await asyncio.to_thread(claimed)
        for event,identities in jobs:
            request = make_work_request(phase=SourcePhase.PHASE9,source_id=SourceId.PHASE9_EVALUATIONS,
                work_class=WorkClass.MEDIUM,estimated_items=1,estimated_bytes=64*1024,
                replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,cancellation_owner=ProcessRole.ENGINE,
                stream_id='phase9.evaluations',timeout_seconds=self.config.evaluation_timeout_seconds)
            try:
                async with self.admission.admit(request):
                    await asyncio.to_thread(self.evaluator,event,identities,self.consumer_name)
                self._completed += 1
            except AdmissionDeferred:
                self._deferred += 1
                break
            except Exception as error:
                await asyncio.to_thread(self._record_revalidation_failure,event,identities,error)
                raise
        return len(jobs)

    def _record_revalidation_failure(self,event,identities,error):
        from .lifecycle import load_decision,append_status
        from .decision import build_status_event
        from .revalidation import rule_for
        now = datetime.now(timezone.utc)
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                timeout_seconds=2.0,identity='phase9-revalidation-failure') as conn:
            for identity in identities:
                eid = evaluation_id_for(identity)
                key = (identity.stage1_candidate_id,identity.timeframe,identity.policy_generation)
                old = conn.execute('SELECT d.payload FROM phase9_revalidation_cursors c JOIN phase9_decision_candidates d ON d.decision_id=c.decision_id WHERE c.stage1_candidate_id=%s AND c.timeframe=%s AND c.policy_generation=%s FOR UPDATE OF c',key).fetchone()
                row = conn.execute('SELECT lease_owner,lease_expires_at FROM phase9_evaluations WHERE evaluation_id=%s FOR UPDATE',(eid,)).fetchone()
                if row is None or row[0]!=self.consumer_name or row[1] is None or row[1]<=now:
                    raise RuntimeError('revalidation failure writer was fenced')
                conn.execute('''UPDATE phase9_evaluations SET evaluation_state='FAILED',reason_code=%s,
                    lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE evaluation_id=%s''',
                    (self._timeout_reason(error) if isinstance(error,TimeoutError) else 'CONTRACT_INVALID',now,eid))
                if old is not None:
                    append_status(conn,build_status_event(decision=load_decision(old[0]),status='INVALIDATED',
                        event_time=now,reason_code='REVALIDATION_FAILED'))
                rule = rule_for(self.policy_manifest,identity.timeframe)
                conn.execute('UPDATE phase9_revalidation_cursors SET current_evaluation_id=NULL,current_identity=NULL,next_due=%s WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s',
                    (now+timedelta(seconds=rule.cadence_seconds),*key))

    def maintain_lifecycle(self, *, now=None):
        from .lifecycle import expire_decisions
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                timeout_seconds=2.0,identity='phase9-lifecycle') as conn:
            return expire_decisions(conn,now=now or datetime.now(timezone.utc),
                                    limit=self.config.max_queued_ids)

    @staticmethod
    def _timeout_reason(error):
        from strategies.runtime import ResearchDeadlineExceeded
        if isinstance(error,ResearchDeadlineExceeded) and error.stage in {'SOURCE_REFRESH','AI_ANALYSIS','FINAL_CANONICAL_READ'}:
            return 'EVALUATION_TIMEOUT_'+error.stage
        return 'EVALUATION_TIMEOUT'

    def _record_failure(self,event,identities,error):
        from .intake import release_for_retry, RetryableErrorCodeV1, LeaseOwnershipError
        now = datetime.now(timezone.utc)
        retryable = getattr(error,'sqlstate',None) in {'40001','40P01'}
        state = 'QUEUED' if retryable else 'DEGRADED' if isinstance(error,TimeoutError) else 'FAILED'
        reason = 'DB_SERIALIZATION_CONFLICT' if retryable else self._timeout_reason(error) if isinstance(error,TimeoutError) else 'CONTRACT_INVALID'
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                timeout_seconds=2.0,identity='phase9-failure') as conn:
            owner = conn.execute('SELECT phase9_state,lease_owner,lease_expires_at FROM outbox_events WHERE event_id=%s FOR UPDATE',
                                 (event.event_id,)).fetchone()
            if owner is None or owner[0]!='LEASED' or owner[1]!=self.consumer_name or owner[2]<=now:
                raise LeaseOwnershipError('failure writer no longer owns the event')
            for identity in identities:
                conn.execute('''UPDATE phase9_evaluations SET evaluation_state=%s,reason_code=%s,
                    lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE evaluation_id=%s
                    AND evaluation_state IN ('RUNNING','WAITING_JEV','QUEUED')''',
                    (state,reason,now,evaluation_id_for(identity)))
            if retryable:
                release_for_retry(conn,event_id=event.event_id,consumer_name=self.consumer_name,
                    retry_at=now+timedelta(seconds=1),error_code=RetryableErrorCodeV1.DB_SERIALIZATION_CONFLICT,now=now)
            else:
                # ACK means the failed intake outcome is durable, not an eligible decision.
                ack(conn,event_id=event.event_id,consumer_name=self.consumer_name,now=now)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.config.enabled:
            return
        self._state = "AVAILABLE"
        try:
            while not stop_event.is_set():
                try:
                    await self.poll_once()
                    await self.poll_revalidations()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._failures += 1
                    self._state = "DEGRADED"
                    LOGGER.warning("phase9_cycle_failed category=%s", type(exc).__name__)
                else:
                    self._state = "AVAILABLE"
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._stopping = True
            self._state = "SHUTTING_DOWN"
            if self._owns_admission:
                await self.admission.shutdown(timeout_seconds=10.0)
            self.db_admission.close(timeout_seconds=10.0)
            self._state = "STOPPED"


class Phase9DeterministicEvaluator:
    """Snapshot, validate, decide, and persist using an approved policy."""

    def __init__(
        self, *, dsn: str, policy_manifest: ApprovedPolicyManifestV1,
        connection_factory: Callable[..., Any] | None = None,
        db_admission: PostgresWriteAdmission | None = None,
        source_reader: Any | None = None,
        evaluation_timeout_seconds: int = 30,
        conflict_reviewer=None,
    ) -> None:
        if isinstance(evaluation_timeout_seconds, bool) or not isinstance(evaluation_timeout_seconds, int) or not 1 <= evaluation_timeout_seconds <= 30:
            raise ValueError("evaluation timeout exceeds Phase 9 V1 ceiling")
        self.evaluation_timeout_seconds = evaluation_timeout_seconds
        if not isinstance(policy_manifest, ApprovedPolicyManifestV1):
            raise TypeError("evaluator requires an approved policy manifest")
        self.dsn = dsn
        self.policy_manifest = policy_manifest
        self.connection_factory = connection_factory
        self.db_admission = db_admission or PostgresWriteAdmission(connection_factory=connection_factory)
        if source_reader is None:
            from .sources import CorePhase9SourceReader
            from .paper_v1 import is_paper_v1
            if is_paper_v1(policy_manifest):
                from .sources.paper_v1 import PaperV1SourceReader
                source_reader = PaperV1SourceReader()
            else:
                source_reader = CorePhase9SourceReader()
        self.source_reader = source_reader
        if conflict_reviewer is None:
            from .runtime_jev import JevConflictReviewer
            conflict_reviewer = JevConflictReviewer()
        self.conflict_reviewer = conflict_reviewer

    def _connect(self):
        if self.connection_factory is not None:
            return self.connection_factory(self.dsn,connect_timeout=2)
        import psycopg
        return psycopg.connect(self.dsn,connect_timeout=2)

    def _snapshot(self, event: Stage1CandidateEventV1, identity: EvaluationIdentityV1, *, deadline=None):
        from .contracts import EvaluationSnapshotV1
        from .snapshot import build_snapshot, persist_evaluation_snapshot

        with self._connect() as conn, _database_deadline(conn,deadline):
            conn.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ"
            )
            conn.commit()
            with self.db_admission.transaction(
                self.dsn, work_class=DbWorkClass.MEDIUM,
                timeout_seconds=2.0, identity="phase9-snapshot",
                business_connection=conn,
            ) as admitted:
                assert_schema_ready(admitted, required_version="016_phase9_evidence_chain.sql")
                row = admitted.execute(
                    "SELECT payload FROM phase9_evaluation_snapshots WHERE evaluation_id = %s",
                    (evaluation_id_for(identity),),
                ).fetchone()
                if row is not None:
                    snapshot = _load_snapshot(row[0])
                    if (
                        snapshot.identity != identity
                        or snapshot.candidate_event.event_id != event.event_id
                        or snapshot.candidate_event.canonical_payload_digest != event.canonical_payload_digest
                        or snapshot.code_version != self.policy_manifest.code_version
                    ):
                        raise ValueError("durable snapshot does not match claimed event or policy")
                    return snapshot
                snapshot = build_snapshot(
                    admitted, identity=identity, stage1_candidate=event,
                    as_of=datetime.now(timezone.utc),
                    source_reader=self.source_reader,
                    policy_manifest=self.policy_manifest,
                    code_version=self.policy_manifest.code_version,
                )
                persist_evaluation_snapshot(admitted, snapshot=snapshot)
                return snapshot

    def __call__(
        self, event: Stage1CandidateEventV1,
        identities: tuple[EvaluationIdentityV1, ...],
        consumer_name: str,
    ) -> None:
        from .contracts import EvidenceChainV1, PatternMatchStatusV1
        from .decision import build_decision_candidate, input_snapshot_hash_for
        from .evidence import build_chain_draft, build_evidence
        from .patterns import match_patterns
        from .persistence import persist_final, _persist_final_records, persist_jev_review
        from .validator import validate_evidence

        deadline = time.monotonic() + self.evaluation_timeout_seconds
        outcomes = []
        for identity in identities:
            if time.monotonic() >= deadline:
                raise TimeoutError("Phase 9 evaluation deadline reached")
            snapshot = self._snapshot(event, identity,deadline=deadline)
            items = build_evidence(snapshot=snapshot, policy_manifest=self.policy_manifest)
            validation = validate_evidence(
                snapshot=snapshot, evidence_items=items,
                policy_manifest=self.policy_manifest,
            )
            draft = build_chain_draft(
                snapshot=snapshot, evidence_items=items, validation=validation,
            )
            matches = match_patterns(
                evidence_items=items, validation=validation,
                policy_manifest=self.policy_manifest, timeframe=identity.timeframe,
            )
            from .paper_v1 import is_paper_v1
            review_required = not is_paper_v1(self.policy_manifest) and bool(
                draft.conflicting
                or any(match.status is PatternMatchStatusV1.CONFLICTED for match in matches)
            )
            chain = EvidenceChainV1(
                stage1_candidate_id=snapshot.stage1_candidate_id,
                symbol=snapshot.symbol, evaluation_id=snapshot.evaluation_id,
                evaluation_time=snapshot.evaluation_time,
                timeframe=snapshot.timeframe,
                supporting=draft.supporting, conflicting=draft.conflicting,
                neutral=draft.neutral, missing=draft.missing,
                degraded=draft.degraded, hard_vetoes=draft.hard_vetoes,
                matched_patterns=tuple(
                    match.pattern_match_id for match in matches
                    if match.status is PatternMatchStatusV1.MATCHED
                ),
                jev_review_required=review_required,
                evidence_schema_version=snapshot.evidence_schema_version,
                evaluation_snapshot_hash=snapshot.snapshot_digest,
                input_snapshot_hash=input_snapshot_hash_for(
                    snapshot_hash=snapshot.snapshot_digest, jev_review=None,
                ),
            )
            review = None
            if review_required:
                from .jev import build_phase9_jev_safe_context
                context = build_phase9_jev_safe_context(snapshot=snapshot,evidence_items=items,
                    evidence_chain=chain,pattern_matches=matches,requested_at=snapshot.as_of)
                with self._connect() as conn:
                    row = conn.execute('SELECT payload FROM phase9_jev_reviews WHERE evaluation_id=%s',(snapshot.evaluation_id,)).fetchone()
                if row is not None:
                    from .replay import _parse_jev
                    review = _parse_jev(row[0]['value'])
                else:
                    review = self.conflict_reviewer(context)
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Phase 9 Jev deadline reached')
                    with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.MEDIUM,
                            timeout_seconds=2.0,identity='phase9-jev-review') as conn:
                        persist_jev_review(conn,review=review)
                chain = replace(chain,input_snapshot_hash=input_snapshot_hash_for(
                    snapshot_hash=snapshot.snapshot_digest,jev_review=review))
            decision, lifecycle_event = build_decision_candidate(
                snapshot=snapshot, evidence_chain=chain,
                pattern_matches=matches, jev_review=review,
                policy_manifest=self.policy_manifest,
                now=datetime.now(timezone.utc),
            )
            outcomes.append((snapshot, items, chain, matches, decision, lifecycle_event,review))

        if time.monotonic() >= deadline:
            raise TimeoutError("Phase 9 evaluation deadline reached")
        with self.db_admission.transaction(
            self.dsn, work_class=DbWorkClass.MEDIUM,
            timeout_seconds=2.0, identity="phase9-final",
        ) as conn, _database_deadline(conn,deadline):
            assert_schema_ready(conn, required_version="016_phase9_evidence_chain.sql")
            now = datetime.now(timezone.utc)
            for snapshot, items, chain, matches, decision, lifecycle_event,review in outcomes:
                if time.monotonic()>=deadline:
                    raise TimeoutError('Phase 9 final persistence deadline reached')
                from .revalidation import semantic_digest,register,finish
                cursor = None
                unchanged = False
                if snapshot.identity.material_change_generation.startswith('revalidate:'):
                    cursor = conn.execute('''SELECT decision_id,semantic_digest FROM phase9_revalidation_cursors
                        WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s FOR UPDATE''',
                        (snapshot.stage1_candidate_id,snapshot.timeframe,snapshot.identity.policy_generation)).fetchone()
                lease = conn.execute('SELECT evaluation_state,lease_owner,lease_expires_at FROM phase9_evaluations WHERE evaluation_id=%s FOR UPDATE',
                    (snapshot.evaluation_id,)).fetchone()
                now = datetime.now(timezone.utc)
                if lease is None or lease[0]!='RUNNING' or lease[1]!=consumer_name or lease[2] is None or lease[2]<=now:
                    raise RuntimeError('evaluation final writer was fenced')
                decision,lifecycle_event = build_decision_candidate(snapshot=snapshot,evidence_chain=chain,
                    pattern_matches=matches,jev_review=review,policy_manifest=self.policy_manifest,now=now)
                digest = semantic_digest(decision,items,review)
                unchanged = cursor is not None and cursor[1]==digest
                if cursor is not None and not unchanged:
                    decision = replace(decision,supersedes_decision_id=cursor[0])
                _persist_final_records(
                    conn, evaluation_id=snapshot.evaluation_id,
                    snapshot_digest=str(snapshot.snapshot_digest),
                    evidence_items=items, evidence_chain=chain,
                    pattern_matches=matches, jev_review=review,
                    decision=decision, lifecycle_event=lifecycle_event,
                    event_id=event.event_id, consumer_name=consumer_name, now=now,
                    emit_decision=not unchanged,
                )
                conn.execute(
                    """UPDATE phase9_evaluations
                       SET evaluation_state='COMPLETED', lease_owner=NULL,
                           lease_expires_at=NULL, updated_at=%s, reason_code=%s
                       WHERE evaluation_id=%s""",
                    (now,'SEMANTIC_UNCHANGED' if unchanged else None,snapshot.evaluation_id),
                )
                if cursor is None:
                    register(conn,event=event,identity=snapshot.identity,decision=decision,items=items,
                        review=review,policy=self.policy_manifest,now=now)
                else:
                    if unchanged:
                        from .lifecycle import load_decision
                        decision = load_decision(conn.execute('SELECT payload FROM phase9_decision_candidates WHERE decision_id=%s',(cursor[0],)).fetchone()[0])
                    finish(conn,identity=snapshot.identity,decision=decision,digest=digest,
                        policy=self.policy_manifest,now=now)


def _load_snapshot(payload: object):
    """Restore the exact previously committed snapshot; reject any altered field."""
    from dataclasses import fields
    from collections.abc import Mapping
    from uuid import UUID

    from .canonical import canonical_sha256
    from .contracts import (
        EvaluationIdentityV1, EvaluationSnapshotV1,
        EvidenceFreshnessV1, EvidenceQualityV1, PolicyCoverageStatusV1,
        PolicyDataStatusV1, SourcePhaseV1, SourceProjectionV1,
        Stage1CandidateEventV1,
    )

    def exact(value: object, cls: type, *, extra: tuple[str, ...] = ()) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("durable snapshot object is invalid")
        expected = {field.name for field in fields(cls)} | set(extra)
        if set(value) != expected:
            raise ValueError("durable snapshot fields are invalid")
        return dict(value)

    def timestamp(value: object, *, optional: bool = False):
        if value is None and optional:
            return None
        if not isinstance(value, str):
            raise ValueError("durable snapshot timestamp is invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("durable snapshot timestamp is not UTC")
        return parsed.astimezone(timezone.utc)

    raw = exact(payload, EvaluationSnapshotV1, extra=("schema",))
    if raw.pop("schema") != "PHASE9_EVALUATION_SNAPSHOT_V1":
        raise ValueError("durable snapshot schema is invalid")
    stored_digest = raw.pop("snapshot_digest")
    if canonical_sha256({"schema": "PHASE9_EVALUATION_SNAPSHOT_V1", **raw}) != stored_digest:
        raise ValueError("durable snapshot digest mismatch")
    identity_raw = exact(raw["identity"], EvaluationIdentityV1)
    identity = EvaluationIdentityV1(
        **{**identity_raw,
           "evaluation_window_start": timestamp(identity_raw["evaluation_window_start"]),
           "evaluation_window_end": timestamp(identity_raw["evaluation_window_end"])}
    )
    event_raw = exact(raw["candidate_event"], Stage1CandidateEventV1)
    event = Stage1CandidateEventV1(
        **{**event_raw,
           "candidate_created_at": timestamp(event_raw["candidate_created_at"]),
           "candidate_valid_until": timestamp(event_raw["candidate_valid_until"], optional=True),
           "source_as_of": timestamp(event_raw["source_as_of"]),
           "created_at": timestamp(event_raw["created_at"]),
           "source_refs": tuple(event_raw["source_refs"])}
    )
    projections = []
    if not isinstance(raw["source_projections"], list):
        raise ValueError("durable source projections are invalid")
    for item in raw["source_projections"]:
        source = exact(item, SourceProjectionV1)
        projections.append(SourceProjectionV1(
            **{**source,
               "projection_id": UUID(source["projection_id"]),
               "evaluation_id": UUID(source["evaluation_id"]),
               "source_phase": SourcePhaseV1(source["source_phase"]),
               "availability_status": PolicyDataStatusV1(source["availability_status"]),
               "freshness_status": EvidenceFreshnessV1(source["freshness_status"]),
               "quality_status": EvidenceQualityV1(source["quality_status"]),
               "coverage_status": (
                   PolicyCoverageStatusV1(source["coverage_status"])
                   if source["coverage_status"] is not None else None
               ),
               **{name: timestamp(source[name], optional=True) for name in
                  ("event_time", "observed_at", "captured_at", "processed_at", "available_at")}}
        ))
    snapshot = EvaluationSnapshotV1(
        **{**raw,
           "identity": identity, "evaluation_id": UUID(raw["evaluation_id"]),
           "candidate_event": event, "source_projections": tuple(projections),
           "evaluation_time": timestamp(raw["evaluation_time"]),
           "as_of": timestamp(raw["as_of"]),
           "created_at": timestamp(raw["created_at"]),
           "snapshot_digest": stored_digest}
    )
    if snapshot.evaluation_id != evaluation_id_for(snapshot.identity):
        raise ValueError("durable snapshot identity mismatch")
    return snapshot


class Phase9StrategyV2Evaluator:
    """V2 producer reuses Phase9 snapshot, leases, admission and lifecycle."""
    def __init__(self, *, dsn, policy_manifest, input_factory, connection_factory=None,
                 db_admission=None, evaluation_timeout_seconds=30):
        from strategies.integration.policy_manifest import is_strategy_v2
        if not isinstance(policy_manifest,ApprovedPolicyManifestV1) or not is_strategy_v2(policy_manifest):
            raise ValueError("V2 evaluator requires the existing approved V2 manifest")
        if not callable(input_factory):raise TypeError("V2 bounded research factory required")
        if isinstance(evaluation_timeout_seconds,bool) or not 1<=evaluation_timeout_seconds<=30:
            raise ValueError("evaluation timeout exceeds the supervisor limit")
        self.dsn=dsn;self.policy_manifest=policy_manifest;self.input_factory=input_factory
        self.connection_factory=connection_factory
        self.db_admission=db_admission or PostgresWriteAdmission(connection_factory=connection_factory)
        self.evaluation_timeout_seconds=evaluation_timeout_seconds

    def __call__(self,event,identities,consumer_name):
        from strategies.integration.phase9_bridge import StrategyExecutionInputs, StrategyV2SourceReader, bound_inputs
        from strategies.execution.execution_policy import evaluate_execution
        from strategies.contracts import ExecutionPolicyResult
        from strategies.persistence import assert_event_lease_held, persist_strategy_decision
        from .revalidation import register,finish,semantic_digest
        from strategies.integration.phase9_bridge import build_v2_candidate
        deadline=time.monotonic()+self.evaluation_timeout_seconds
        inputs=None;result=None
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.LIGHT,
                timeout_seconds=2.0,identity="v2-retry-snapshot") as conn:
            assert_schema_ready(conn,required_version="019_strategy_v2.sql")
            for identity in identities:
                row=conn.execute("SELECT payload FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
                    (evaluation_id_for(identity),)).fetchone()
                if row is not None:
                    stored=_load_snapshot(row[0])
                    result_projection=next(
                        (p for p in stored.source_projections if p.source_type=="STRATEGY_V2_RESULT"),
                        None,
                    )
                    if result_projection is None:
                        continue
                    result=ExecutionPolicyResult.model_validate(result_projection.canonical_payload)
                    inputs=bound_inputs(stored,result)
                    break
        if inputs is None:
            inputs=self.input_factory(event,deadline=deadline)
            if not isinstance(inputs,StrategyExecutionInputs):
                raise TypeError("V2 factory must return a complete typed research binding")
            result=evaluate_execution(inputs.theses,inputs.view,policy=inputs.policy,now=datetime.now(timezone.utc))
        approved_digests={p.strategy_policy_digest for p in self.policy_manifest.manifest.policy_content.enabled_patterns}
        if approved_digests!={inputs.policy.digest}:
            raise ValueError("research execution policy differs from the approved manifest")
        reader=StrategyV2SourceReader(analysis=inputs.analysis,theses=inputs.theses,view=inputs.view,policy=inputs.policy,result=result)
        base=Phase9DeterministicEvaluator(dsn=self.dsn,policy_manifest=self.policy_manifest,
            connection_factory=self.connection_factory,db_admission=self.db_admission,source_reader=reader,
            evaluation_timeout_seconds=self.evaluation_timeout_seconds)
        snapshots=[]
        for identity in identities:
            if time.monotonic()>=deadline:raise TimeoutError("V2 research deadline reached")
            snapshots.append(base._snapshot(event,identity,deadline=deadline))
        with self.db_admission.transaction(self.dsn,work_class=DbWorkClass.MEDIUM,
                timeout_seconds=2.0,identity="v2-final") as conn, _database_deadline(conn,deadline):
            lease_owner=assert_event_lease_held(
                conn,event_id=event.event_id,
                evaluation_ids=[snapshot.evaluation_id for snapshot in snapshots],
                consumer_name=consumer_name,now=datetime.now(timezone.utc))
            for snapshot in snapshots:
                if time.monotonic()>=deadline:raise TimeoutError("V2 final deadline reached")
                now=datetime.now(timezone.utc)
                decision=persist_strategy_decision(conn,result=result,snapshot=snapshot,now=now,
                    validated_event_lease_owner=lease_owner)
                _,items,_,_,_=build_v2_candidate(snapshot,result,inputs)
                if snapshot.identity.material_change_generation.startswith("revalidate:"):
                    finish(conn,identity=snapshot.identity,decision=decision,
                        digest=semantic_digest(decision,items,None),policy=self.policy_manifest,now=now)
                else:
                    register(conn,event=event,identity=snapshot.identity,decision=decision,items=items,
                        review=None,policy=self.policy_manifest,now=now)
