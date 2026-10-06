"""Translate verified V2 admission to the existing Phase9 contracts."""
import json
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5
from pydantic import model_validator
from strategies.contracts import Record, AnalysisSnapshot, StructuredTradeThesis, ExecutionPolicyResult
from strategies.market_view import MarketView
from strategies.execution.execution_policy import ExecutionPolicyV2, evaluate_execution
from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_phase9.contracts import *

class StrategyExecutionInputs(Record):
    analysis: AnalysisSnapshot
    theses: tuple[StructuredTradeThesis,...]
    view: MarketView
    policy: ExecutionPolicyV2

def verify_bound_inputs(inputs, result):
    expected=evaluate_execution(inputs.theses,inputs.view,policy=inputs.policy,now=result.evaluated_at)
    if expected.digest!=result.digest:
        raise ValueError("execution result is not the deterministic policy result")
    if inputs.analysis.digest not in {s.digest for s in inputs.view.analysis_snapshots}:
        raise ValueError("analysis snapshot is not bound to the current view")
    if result.snapshot_digest is not None and result.snapshot_digest!=inputs.analysis.digest:
        raise ValueError("execution result analysis binding mismatch")

class StrategyV2SourceReader:
    def __init__(self, *, analysis, theses, view, policy, result):
        self.inputs=StrategyExecutionInputs(analysis=analysis,theses=theses,view=view,policy=policy)
        self.result=result
        verify_bound_inputs(self.inputs,result)

    def read(self, conn, *, candidate, timeframe, as_of):
        if candidate.symbol!=self.inputs.analysis.symbol or candidate.stage1_policy_version!="QUANT_PAPER_V2":
            raise ValueError("V2 requires its own Stage A admission")
        if self.result.evaluated_at>as_of:raise ValueError("future execution result")
        rows=[]
        for kind,record in (("STRATEGY_V2_INPUTS",self.inputs),("STRATEGY_V2_RESULT",self.result)):
            payload=record.model_dump(mode="json");digest=canonical_sha256(payload)
            rows.append(SourceProjectionV1(projection_id=uuid5(NAMESPACE_URL,kind+str(digest)),
                evaluation_id=uuid5(NAMESPACE_URL,"unbound-v2"),source_phase=SourcePhaseV1.PHASE6,
                source_type=kind,source_ref=kind+":"+record.digest,symbol=candidate.symbol,market=candidate.market,
                event_time=self.result.evaluated_at,observed_at=self.result.evaluated_at,
                captured_at=self.result.evaluated_at,processed_at=self.result.evaluated_at,available_at=self.result.evaluated_at,
                availability_status=PolicyDataStatusV1.AVAILABLE,freshness_status=EvidenceFreshnessV1.FRESH,
                quality_status=EvidenceQualityV1.VALID,coverage_status=PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE,
                canonical_payload=payload,source_schema_version="STRATEGY_V2",projection_version="2.0.0",
                canonical_digest=digest))
        return tuple(rows)

def _plain_payload(payload):
    """Durable projections are deep-frozen mappings; re-validate a plain tree."""
    return json.loads(canonical_json(payload))


def bound_inputs(snapshot,result):
    if (snapshot.pattern_policy_version!="2.0.0" or snapshot.decision_policy_version!="2.0.0"
        or snapshot.candidate_event.stage1_policy_version!="QUANT_PAPER_V2"):
        raise ValueError("legacy profile cannot produce a V2 candidate")
    from quant_phase9.snapshot import _snapshot_content
    if canonical_sha256(_snapshot_content(snapshot))!=snapshot.snapshot_digest:
        raise ValueError("evaluation snapshot content mismatch")
    sources={p.source_type:p for p in snapshot.source_projections}
    if set(sources)!={"STRATEGY_V2_INPUTS","STRATEGY_V2_RESULT"}:
        raise ValueError("V2 requires exact durable input and result projections")
    inputs=StrategyExecutionInputs.model_validate(_plain_payload(sources["STRATEGY_V2_INPUTS"].canonical_payload))
    stored=ExecutionPolicyResult.model_validate(_plain_payload(sources["STRATEGY_V2_RESULT"].canonical_payload))
    if stored.digest!=result.digest:raise ValueError("snapshot execution result mismatch")
    verify_bound_inputs(inputs,result)
    if snapshot.symbol!=inputs.analysis.symbol:raise ValueError("foreign analysis symbol")
    return inputs

def _type(kind):
    if "OI" in kind:return EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,SourcePhaseV1.PHASE2
    if "FUNDING" in kind:return EvidenceTypeV1.FUNDING_BASIS_POSITIONING,SourcePhaseV1.PHASE4
    if "FLOW" in kind:return EvidenceTypeV1.TRADE_FLOW,SourcePhaseV1.PHASE7 if "SPOT" in kind else SourcePhaseV1.PHASE3
    if "COVERAGE" in kind:return EvidenceTypeV1.ONCHAIN_SPOT_MACRO,SourcePhaseV1.PHASE6
    if "BENCHMARK" in kind:return EvidenceTypeV1.MARKET_REGIME,SourcePhaseV1.PHASE5
    return EvidenceTypeV1.PRICE_STRUCTURE,SourcePhaseV1.PHASE1

def build_v2_candidate(snapshot,result,inputs):
    thesis=next(t for t in inputs.theses if t.timeframe==snapshot.timeframe)
    eligible=result.disposition=="PASS" and result.thesis is not None and result.thesis.digest==thesis.digest
    input_hash=canonical_sha256({"schema":"V2_DECISION_BINDING","evaluation_snapshot":str(snapshot.snapshot_digest),
        "result":result.digest,"thesis":thesis.digest,"analysis":inputs.analysis.digest,
        "policy":inputs.policy.digest,"recheck":inputs.view.digest})
    decision_id=uuid5(NAMESPACE_URL,"v2-decision:"+str(snapshot.evaluation_id)+":"+str(input_hash))
    items=[];supporting=[];conflicting=[];degraded=[];missing=[]
    if thesis.evidence_chain is not None:
        for node in thesis.evidence_chain.nodes:
            o=node.observation;kind,phase=_type(o.kind)
            eid=uuid5(NAMESPACE_URL,str(snapshot.evaluation_id)+":"+node.digest)
            status=PolicyDataStatusV1("NOT_AVAILABLE" if o.availability=="UNAVAILABLE" else o.availability)
            freshness=EvidenceFreshnessV1(o.freshness if o.freshness in {"FRESH","STALE","UNKNOWN"} else "STALE")
            direction=EvidenceDirectionV1.BULLISH if thesis.bias=="LONG" else EvidenceDirectionV1.BEARISH if thesis.bias=="SHORT" else EvidenceDirectionV1.UNKNOWN
            if node.role in {"MISSING","STALE"}:direction=EvidenceDirectionV1.UNKNOWN
            item=EvidenceItemV1(evidence_id=eid,evaluation_id=snapshot.evaluation_id,symbol=snapshot.symbol,
                evidence_type=kind,semantic_code="V2:"+o.kind,direction=direction,strength=EvidenceStrengthV1.STRONG if o.usable else EvidenceStrengthV1.UNKNOWN,
                observed_at=o.observed_at,availability_status=status,freshness_status=freshness,
                quality_status=EvidenceQualityV1(o.quality),coverage_status=PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE if o.coverage=="COMPLETE" else PolicyCoverageStatusV1(o.coverage),
                source_phase=phase,source_ref=o.source_ref,provider=o.provider,provenance=o.digest,
                raw_refs=(o.source_ref,),interpretation=node.interpretation,schema_version=snapshot.evidence_schema_version,
                created_at=result.evaluated_at,numeric_value=o.value,numeric_unit=o.unit)
            items.append(item)
            if node.role=="SUPPORTING":supporting.append(eid)
            elif node.role=="CONTRADICTING":conflicting.append(eid)
            elif node.role=="STALE":degraded.append(eid)
            else:missing.append(MissingEvidenceV1(evidence_type=o.kind,source_status=status,
                reason=o.reason or "MISSING_EVIDENCE",source_timestamp=o.observed_at,captured_at=o.fetched_at))
    reason=result.reason_codes if result.disposition!="PASS" or eligible else ("HORIZON_NOT_SELECTED",)
    match_id=uuid5(NAMESPACE_URL,"v2-match:"+str(snapshot.evaluation_id)+":"+str(input_hash))
    matches=()
    if thesis.bias in {"LONG","SHORT"}:
        matches=(PatternMatchV1(pattern_match_id=match_id,evaluation_id=snapshot.evaluation_id,
            pattern_type="MARKET_EVIDENCE_CHAIN",direction=PolicyDirectionV1(thesis.bias),
            status=PatternMatchStatusV1.MATCHED if eligible else PatternMatchStatusV1.NOT_MATCHED,
            required_evidence_ids=tuple(supporting),supporting_evidence_ids=(),conflicting_evidence_ids=tuple(conflicting),
            missing=tuple(missing),vetoes=() if eligible else reason,pattern_policy_version="2.0.0"),)
    chain=EvidenceChainV1(stage1_candidate_id=snapshot.stage1_candidate_id,symbol=snapshot.symbol,
        evaluation_id=snapshot.evaluation_id,evaluation_time=result.evaluated_at,timeframe=snapshot.timeframe,
        supporting=tuple(supporting),conflicting=tuple(conflicting),neutral=(),missing=tuple(missing),
        degraded=tuple(degraded),hard_vetoes=() if eligible else reason,
        matched_patterns=(match_id,) if eligible else (),jev_review_required=False,
        evidence_schema_version=snapshot.evidence_schema_version,evaluation_snapshot_hash=snapshot.snapshot_digest,
        input_snapshot_hash=input_hash)
    valid_until=min(thesis.valid_until,result.evaluated_at+timedelta(seconds=inputs.policy.candidate_ttl_seconds))
    decision=DecisionCandidateV1(decision_id=decision_id,evaluation_id=snapshot.evaluation_id,
        stage1_candidate_id=snapshot.stage1_candidate_id,symbol=snapshot.symbol,market=snapshot.market,
        timeframe=snapshot.timeframe,created_at=result.evaluated_at,valid_until=valid_until,eligible=eligible,
        direction_bias=DecisionDirectionBiasV1.BULLISH if eligible and thesis.bias=="LONG" else DecisionDirectionBiasV1.BEARISH if eligible else DecisionDirectionBiasV1.NEUTRAL,
        confidence_band=ConfidenceBandV1(thesis.confidence),matched_pattern="MARKET_EVIDENCE_CHAIN" if eligible else None,
        pattern_status=PatternMatchStatusV1.MATCHED if eligible else PatternMatchStatusV1.NOT_MATCHED,
        supporting_evidence_ids=tuple(supporting),conflicting_evidence_ids=tuple(conflicting),
        degraded_evidence_ids=tuple(degraded),missing_evidence=tuple(missing),veto_reasons=() if eligible else reason,
        jev_review_id=None,reason_codes=reason,short_summary=thesis.summary or "V2 deterministic policy outcome",
        input_snapshot_hash=input_hash,evidence_schema_version=snapshot.evidence_schema_version,
        pattern_policy_version="2.0.0",freshness_policy_version=snapshot.freshness_policy_version,
        decision_policy_version="2.0.0",ttl_policy_version=snapshot.ttl_policy_version,prompt_version="2.0.0",
        code_version=snapshot.code_version,supersedes_decision_id=None)
    return decision,tuple(items),chain,matches,thesis
