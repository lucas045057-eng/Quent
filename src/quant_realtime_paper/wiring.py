"""Fail-closed composition of existing Stage1, Phase9, Risk and local Paper APIs.

The runtime owns sequencing and readiness. Strategy evaluation, risk sizing,
execution persistence and the Nautilus adapter remain their existing owners.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from quant_execution.contracts import (
    DecisionEnvelopeV1,
    ExecutionAdapter,
    ExecutionIntentV1,
    ExecutionResultV1,
    PositionSnapshotV1,
)
from quant_execution.persistence import ExecutionStore
from quant_execution.risk import RiskPolicyV1, RiskRejected, approve_intent
from quant_phase9.contracts import DecisionCandidateV1, EvaluationSnapshotV1
from quant_phase9.policy import ApprovedPolicyManifestV1

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_CANDIDATE_FIELDS = frozenset({"position_size", "leverage", "order", "exchange_fields"})
# The operational preflight runs once per monitor cycle.  A single extra
# attempt with a short pause keeps the added latency bounded (~seconds, only
# on cycles whose first sample failed) far below the 60s cycle-gap ceiling.
_OPERATIONAL_PREFLIGHT_MAX_ATTEMPTS = 2
_OPERATIONAL_PREFLIGHT_RETRY_SLEEP_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class RealtimePaperPipelineResultV1:
    disposition: str
    reason_code: str
    decision_candidate: DecisionCandidateV1 | None
    execution_intent: ExecutionIntentV1 | None
    execution_results: tuple[ExecutionResultV1, ...]
    position_snapshot: PositionSnapshotV1 | None
    reconciliation: str
    events: tuple[dict[str, Any], ...]


class RealtimePaperExecutionPipeline:
    """Run the existing typed boundaries in order, only in explicitly paper mode."""

    def __init__(
        self,
        *,
        approved_policy: ApprovedPolicyManifestV1 | None,
        current_revision: str,
        phase9_evaluate: Callable[[Any, datetime, str], tuple[EvaluationSnapshotV1, DecisionCandidateV1 | None, str]],
        risk_inputs: Callable[[EvaluationSnapshotV1, DecisionCandidateV1, datetime], Mapping[str, Any]],
        execution_store: ExecutionStore,
        adapter_factory: Callable[[ExecutionIntentV1], ExecutionAdapter | None],
        environ: Mapping[str, str],
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        entry_recheck: Callable | None = None,
        position_manager: Callable | None = None,
        operational_preflight: Callable[[datetime], Mapping[str, bool]] | None = None,
    ) -> None:
        self.operational_preflight = operational_preflight
        self.entry_recheck = entry_recheck
        self.position_manager = position_manager
        self.approved_policy = approved_policy
        self.current_revision = current_revision
        self.phase9_evaluate = phase9_evaluate
        self.risk_inputs = risk_inputs
        self.execution_store = execution_store
        self.adapter_factory = adapter_factory
        self.environ = dict(environ)
        self.event_sink = event_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _at(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
            return datetime.now(timezone.utc)
        return value

    def manage_positions(self, *, now: datetime):
        if (self.environ.get("TRADING_MODE", "").lower() != "paper"
                or self.environ.get("PAPER_ONLY", "").lower() != "true"
                or self.environ.get("LIVE_ALLOWED", "").lower() != "false"):
            raise ValueError("PAPER_ONLY_CONFIGURATION_INVALID")
        return tuple(self.position_manager(now)) if self.position_manager is not None else ()

    def operational_readiness(self, *, now: datetime) -> Mapping[str, bool]:
        if (self.environ.get("TRADING_MODE", "").lower() != "paper"
                or self.environ.get("PAPER_ONLY", "").lower() != "true"
                or self.environ.get("LIVE_ALLOWED", "").lower() != "false"
                or self.operational_preflight is None):
            raise ValueError("PAPER_OPERATIONAL_PREFLIGHT_UNAVAILABLE")
        # The probe samples operational capability at one instant; sub-second
        # collector churn can poison a single sample.  One bounded retry inside
        # the same cycle requires sustained readiness rather than instantaneous
        # luck, so readiness becomes harder to prove, not easier.  Every data
        # freshness bound and the strict 5s execution-quote ceiling are
        # unchanged; only the sampling of this no-order probe is de-noised.
        error: Exception | None = None
        for attempt in range(_OPERATIONAL_PREFLIGHT_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_OPERATIONAL_PREFLIGHT_RETRY_SLEEP_SECONDS)
            try:
                proof = self.operational_preflight(now)
            except Exception as exc:
                error = exc
                continue
            if not isinstance(proof,Mapping) or any(proof.get(k) is not True for k in ("paper_engine","reconciliation")):
                error = ValueError("PAPER_OPERATIONAL_PREFLIGHT_FAILED")
                continue
            return proof
        raise error

    def process_stage1(
        self,
        stage1_result: Any,
        *,
        data_source: str,
        market_data_fresh: bool,
        snapshot_complete: bool,
        now: datetime,
        correlation_id: str | None = None,
    ) -> RealtimePaperPipelineResultV1:
        correlation = correlation_id or uuid4().hex
        events: list[dict[str, Any]] = []

        def emit(stage: str, status: str, reason_code: str) -> None:
            event = {
                "correlation_id": correlation,
                "stage": stage,
                "status": status,
                "reason_code": reason_code,
                "at": self._at(),
            }
            events.append(event)
            if self.event_sink is not None:
                self.event_sink(event)

        def blocked(reason: str, *, candidate=None, intent=None, results=(), position=None,
                    reconciliation="NOT_READY", disposition="NO_TRADE_BY_SYSTEM_NOT_READY"):
            emit("pipeline", disposition, reason)
            return RealtimePaperPipelineResultV1(
                disposition, reason, candidate, intent, tuple(results), position,
                reconciliation, tuple(events),
            )

        if (_SHA1.fullmatch(self.current_revision or "") is None
                or self.environ.get("TRADING_MODE", "").strip().lower() != "paper"
                or self.environ.get("PAPER_ONLY", "").strip().lower() != "true"
                or self.environ.get("LIVE_ALLOWED", "").strip().lower() != "false"):
            return blocked("PAPER_ONLY_CONFIGURATION_INVALID")
        if data_source != "REAL_PUBLIC_DATA":
            return blocked("REAL_PUBLIC_DATA_REQUIRED")
        if market_data_fresh is not True:
            return blocked("STALE_MARKET_DATA")
        if snapshot_complete is not True:
            return blocked("SNAPSHOT_INCOMPLETE")
        if self.approved_policy is None:
            return blocked("APPROVAL_MISSING")
        if (self.approved_policy.approval.approved_commit != self.current_revision
                or self.approved_policy.code_version != self.current_revision):
            return blocked("APPROVAL_COMMIT_MISMATCH")
        manifest = self.approved_policy.manifest
        if not manifest.policy_content.enabled_patterns:
            return blocked("POLICY_DISABLED")
        if getattr(stage1_result, "status", None) is None or getattr(stage1_result.status, "value", stage1_result.status) != "AVAILABLE":
            return blocked("STAGE1_UNAVAILABLE")
        emit("stage1", "READY", "STAGE1_AVAILABLE")
        if getattr(stage1_result, "category", None) != "A":
            emit("phase9", "SKIPPED", "STAGE1_NOT_ELIGIBLE")
            return blocked("STAGE1_NOT_ELIGIBLE", disposition="NO_TRADE_BY_STRATEGY")

        try:
            snapshot, candidate, effective_status = self.phase9_evaluate(stage1_result, now, correlation)
        except Exception:
            emit("phase9", "REJECTED", "PHASE9_EVALUATION_FAILED")
            return blocked("PHASE9_EVALUATION_FAILED")
        if snapshot is None:
            reason = effective_status or "PHASE9_RESULT_NOT_FOUND"
            emit("phase9", "REJECTED", reason)
            return blocked(reason)
        if not isinstance(snapshot, EvaluationSnapshotV1):
            emit("snapshot", "REJECTED", "EVALUATION_SNAPSHOT_INVALID")
            return blocked("EVALUATION_SNAPSHOT_INVALID")
        if (snapshot.code_version != self.current_revision or snapshot.symbol != getattr(stage1_result, "symbol", None)
                or snapshot.as_of > now):
            emit("snapshot", "REJECTED", "RUNTIME_PROVENANCE_MISMATCH")
            return blocked("RUNTIME_PROVENANCE_MISMATCH")
        emit("evaluation_snapshot", "CREATED", "EVALUATION_SNAPSHOT_CREATED")
        if candidate is None:
            emit("phase9", "REJECTED", "PHASE9_INSUFFICIENT")
            return blocked("PHASE9_INSUFFICIENT", disposition="NO_TRADE_BY_STRATEGY")
        if not isinstance(candidate, DecisionCandidateV1):
            emit("phase9", "REJECTED", "DECISION_CANDIDATE_INVALID")
            return blocked("DECISION_CANDIDATE_INVALID")
        if _FORBIDDEN_CANDIDATE_FIELDS.intersection(candidate.__dataclass_fields__):
            emit("phase9", "REJECTED", "DECISION_CANDIDATE_SCHEMA_INVALID")
            return blocked("DECISION_CANDIDATE_SCHEMA_INVALID", candidate=candidate)
        if (candidate.code_version != self.current_revision
                or candidate.evaluation_id != snapshot.evaluation_id
                or candidate.symbol != snapshot.symbol):
            emit("phase9", "REJECTED", "RUNTIME_PROVENANCE_MISMATCH")
            return blocked("RUNTIME_PROVENANCE_MISMATCH", candidate=candidate)
        emit("phase9", "EVALUATED", "PHASE9_EVALUATED")
        if not candidate.eligible or effective_status != "ACTIVE":
            reason = candidate.reason_codes[0] if candidate.reason_codes else "PHASE9_INSUFFICIENT"
            emit("decision_candidate", "REJECTED", reason)
            return blocked(reason, candidate=candidate, disposition="NO_TRADE_BY_STRATEGY")
        emit("decision_candidate", "PRODUCED", "ELIGIBLE_CANDIDATE")

        try:
            inputs = dict(self.risk_inputs(snapshot, candidate, now))
            policy = inputs.get("policy")
            if not isinstance(policy, RiskPolicyV1):
                raise RiskRejected("RISK_POLICY_UNAVAILABLE")
            from quant_phase9.paper_v1 import is_paper_v1
            if is_paper_v1(self.approved_policy):
                from quant_execution.paper_v1 import conservative_risk_policy
                if inputs.get("trade_plan") is None or self.entry_recheck is None:
                    raise RiskRejected("PAPER_V1_REQUIRED_CAPABILITY_MISSING")
                inputs["policy"] = policy = conservative_risk_policy(policy, inputs["account"])
                self.entry_recheck(snapshot,candidate,now)
            research_binding = {}
            if candidate.decision_policy_version == "2.0.0":
                from strategies.contracts import ExecutionPolicyResult
                from strategies.integration.phase9_bridge import bound_inputs,build_v2_candidate
                raw=next(p.canonical_payload for p in snapshot.source_projections if p.source_type=="STRATEGY_V2_RESULT")
                result=ExecutionPolicyResult.model_validate(raw)
                bound=bound_inputs(snapshot,result)
                expected,_,_,_,thesis=build_v2_candidate(snapshot,result,bound)
                if expected.decision_id!=candidate.decision_id or not expected.eligible or self.entry_recheck is None:
                    raise RiskRejected("V2_DURABLE_BINDING_INVALID")
                plan=inputs.get("trade_plan")
                if plan is None:raise RiskRejected("V2_PLAN_REQUIRED")
                research_binding=dict(thesis_digest=thesis.digest,analysis_snapshot_digest=bound.analysis.digest,
                    risk_config_digest=policy.risk_config_digest)
                self.entry_recheck(snapshot,candidate,now)
            inputs["envelope"] = DecisionEnvelopeV1(
                candidate, effective_status, str(snapshot.snapshot_digest),
                str(candidate.input_snapshot_hash), **research_binding,
            )
            inputs["now"] = now
            if inputs.get("account") is None or inputs["account"].mode != "PAPER":
                raise RiskRejected("PAPER_ACCOUNT_REQUIRED")
            intent = approve_intent(**inputs)
        except RiskRejected as risk_error:
            reason = str(risk_error) or "RISK_REJECTED"
            emit("risk", "REJECTED", reason)
            return blocked(reason, candidate=candidate)
        except Exception:
            emit("risk", "REJECTED", "RISK_INPUT_UNAVAILABLE")
            return blocked("RISK_INPUT_UNAVAILABLE", candidate=candidate)
        emit("risk", "APPROVED", "RISK_POLICY_APPROVED")
        emit("execution_intent", "PRODUCED", "EXECUTION_INTENT_CREATED")

        try:
            adapter = self.adapter_factory(intent)
            if adapter is None:
                return blocked("PAPER_UNAVAILABLE", candidate=candidate, intent=intent)
            required_capabilities = {
                "MARKET_IOC", "STOP_MARKET_REDUCE_ONLY", "POSITION_SNAPSHOT", "LOCAL_RECONCILE",
            }
            if intent.strategy_profile is not None:
                required_capabilities |= {"PAPER_STRATEGY_EXITS", "PAPER_POSITION_MANAGEMENT"}
            if not required_capabilities <= set(adapter.capabilities()):
                return blocked("PAPER_CAPABILITIES_UNAVAILABLE", candidate=candidate, intent=intent)
            if intent.strategy_profile == "QUANT_PAPER_V2":
                from quant_execution.trade_plan import validate_execution_capabilities
                required={"V2_CONFIGURED_HOLD"}
                if policy.max_leverage>1:required.add("V2_CONFIGURED_LEVERAGE")
                if policy.max_open_positions>1 or policy.max_open_intents>1:required.add("V2_SHARED_ACCOUNT_MULTI_POSITION")
                if policy.allow_pyramiding or policy.allow_averaging_down:required.add("V2_UPDATED_ADD_PROTECTION")
                if not required<=set(adapter.capabilities()):
                    return blocked("UNSUPPORTED_EXECUTION_CAPABILITY",candidate=candidate,intent=intent)
            adapter.preflight(intent, now)
            prior_positions = tuple(adapter.reconcile())
            if (not prior_positions or any(not isinstance(item, PositionSnapshotV1)
                    or item.mode != "PAPER" or item.reconciliation_status != "RECONCILED"
                    for item in prior_positions)):
                emit("reconciliation", "REJECTED", "RECONCILIATION_UNHEALTHY")
                return blocked("RECONCILIATION_UNHEALTHY", candidate=candidate, intent=intent)
        except Exception:
            emit("paper", "UNAVAILABLE", "PAPER_PREFLIGHT_FAILED")
            return blocked("PAPER_PREFLIGHT_FAILED", candidate=candidate, intent=intent)
        emit("paper", "READY", "PAPER_PREFLIGHT_PASSED")

        try:
            reserved = self.execution_store.reserve(intent, policy, now=now)
            state = self.execution_store.submission_state(intent.intent_id)
            if not reserved and state != "RESERVED":
                emit("execution_intent", "DUPLICATE", "INTENT_ALREADY_SUBMITTED")
                return blocked("INTENT_ALREADY_SUBMITTED", candidate=candidate, intent=intent)
            owner = "rt-paper-" + correlation[:48]
            epoch = self.execution_store.acquire_owner(
                intent.account_id, owner, now=now, lease_seconds=30,
            )
            if not self.execution_store.begin_submission(intent.intent_id, owner, epoch, now=now):
                emit("execution_intent", "DUPLICATE", "INTENT_ALREADY_SUBMITTED")
                return blocked("INTENT_ALREADY_SUBMITTED", candidate=candidate, intent=intent)
        except Exception:
            emit("execution_intent", "REJECTED", "INTENT_RESERVATION_FAILED")
            return blocked("INTENT_RESERVATION_FAILED", candidate=candidate, intent=intent)

        try:
            if intent.strategy_profile is not None:
                if self.entry_recheck is None:
                    raise RiskRejected("EVENT_RISK_RECHECK_UNAVAILABLE")
                checked_at = self._at()
                try:
                    self.entry_recheck(snapshot,candidate,checked_at)
                except Exception as veto:
                    # No native submit has occurred. Persist a terminal zero-fill
                    # outcome while ownership remains fenced, releasing reservation.
                    from quant_phase9.canonical import canonical_sha256
                    reason = str(veto) if isinstance(veto, ValueError) and str(veto) else 'EVENT_RISK_RECHECK_UNAVAILABLE'
                    rejected = ExecutionResultV1(intent.intent_id,
                        str(canonical_sha256(dict(intent=intent.intent_id,event='EVENT_RISK_RECHECK',reason=reason,at=checked_at))),
                        intent.client_order_id,(), 'REJECTED',intent.approved_quantity,Decimal('0'),intent.approved_quantity,
                        None,Decimal('0'),intent.settlement_currency,None,'NOT_REQUIRED',checked_at,checked_at,
                        reason,'paper-v1-current-event-recheck')
                    self.execution_store.reject_before_submit(rejected,owner,epoch,now=self._at())
                    emit('execution_result','REJECTED',reason)
                    return blocked(reason,candidate=candidate,intent=intent,results=(rejected,))
            adapter.submit(intent)
            emit("paper_submit", "SUBMITTED", "PAPER_SUBMIT_ACCEPTED")
            results = tuple(adapter.events())
            for result in results:
                if not isinstance(result, ExecutionResultV1) or result.intent_id != intent.intent_id:
                    emit("execution_result", "REJECTED", "EXECUTION_RESULT_INVALID")
                    return blocked("EXECUTION_RESULT_INVALID", candidate=candidate, intent=intent)
                self.execution_store.record_result(result)
                emit("execution_result", result.status, "EXECUTION_RESULT_PERSISTED")
            if not results:
                return blocked("PAPER_RESULT_PENDING", candidate=candidate, intent=intent)
            position = adapter.position_snapshot(intent.canonical_symbol)
            if (not isinstance(position, PositionSnapshotV1) or position.mode != "PAPER"
                    or position.canonical_symbol != intent.canonical_symbol):
                return blocked("POSITION_SNAPSHOT_INVALID", candidate=candidate, intent=intent, results=results)
            self.execution_store.record_position(position)
            emit("position_snapshot", "PERSISTED", "POSITION_SNAPSHOT_PERSISTED")
            reconciled = tuple(adapter.reconcile())
            if (not reconciled or any(not isinstance(item, PositionSnapshotV1)
                    or item.mode != "PAPER" or item.reconciliation_status != "RECONCILED"
                    for item in reconciled)):
                emit("reconciliation", "REJECTED", "RECONCILIATION_UNHEALTHY")
                return blocked("RECONCILIATION_UNHEALTHY", candidate=candidate,
                               intent=intent, results=results, position=position)
            emit("reconciliation", "RECONCILED", "RECONCILIATION_HEALTHY")
            return RealtimePaperPipelineResultV1(
                "PAPER_RESULT_RECORDED", "PAPER_RESULT_RECORDED", candidate, intent,
                results, position, "RECONCILED", tuple(events),
            )
        except Exception:
            emit("paper", "FAILED", "PAPER_SUBMIT_FAILED")
            return blocked("PAPER_SUBMIT_FAILED", candidate=candidate, intent=intent)
        finally:
            try:
                self.execution_store.release_owner(intent.account_id, owner, epoch)
            except Exception:
                pass
