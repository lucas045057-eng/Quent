"""Runtime conflict review through the existing Phase6 gateway only."""
from threading import Lock
from uuid import NAMESPACE_URL, uuid5
import json

from quant_phase6.ai import AIService, AIResponse, AIUsage, FakeAIProvider, BudgetConfig, BudgetLedger
from quant_phase6.prompts import PromptDefinition, PromptRegistry
from .jev import build_jev_review_request, review_with_jev, JEV_REVIEW_OUTPUT_SCHEMA


class _RecordedFixtureProvider:
    def __init__(self, records):
        self.records = records
    def complete(self, request):
        return AIResponse('fixture-recorded','fixture-model-v1',self.records[request.context_hash],AIUsage())


class JevConflictReviewer:
    def __init__(self, *, mode='unconfigured'):
        if mode not in {'unconfigured','fake','recorded'}:
            raise ValueError('unsupported local Jev mode')
        self.mode = mode
        self.calls = 0
        self.external_calls = 0
        self._lock = Lock()

    @classmethod
    def for_fixture(cls, mode):
        if mode not in {'fake','recorded'}:
            raise ValueError('explicit fixture Jev mode required')
        return cls(mode=mode)

    def __call__(self, context):
        with self._lock:
            self.calls += 1
            review_id = uuid5(NAMESPACE_URL,f'phase9-runtime-jev:{context.context_hash}:{self.mode}')
            request = build_jev_review_request(context=context,review_id=review_id,
                prompt_id='phase9.jev.review',prompt_version=context.policy_versions.prompt_version,
                schema_version='phase9.jev-review.output.v1',provider='fixture' if self.mode!='unconfigured' else 'jev',
                model='fixture-model-v1' if self.mode!='unconfigured' else 'NOT_CONFIGURED')
            # Fixture review preserves conflict and never supplies trading approval.
            output = dict(review_id=str(review_id),relation='INDETERMINATE',conflict_severity='HIGH',
                dominant_context='UNKNOWN',supporting_assessments=[],conflicting_assessments=[],
                unresolved_conflicts=[],degradation_notes=[],reasoning_summary='FIXTURE_DRIVEN_ACCEPTANCE: unresolved supplied conflict.')
            records = {}
            if self.mode=='fake':
                providers = {'fixture':FakeAIProvider(lambda _:AIResponse('fixture','fixture-model-v1',output,AIUsage()))}
            elif self.mode=='recorded':
                providers = {'fixture':_RecordedFixtureProvider(records)}
            else:
                providers = {}
            service = AIService(providers,budget=BudgetLedger(BudgetConfig()),max_retries=0,
                now=lambda:context.requested_at)
            if self.mode=='recorded':
                # The fixture record is bound to the rendered public context.
                from .jev import build_phase6_safe_context_for_phase9
                import hashlib
                data = build_phase6_safe_context_for_phase9(context=context).to_dict()
                data['review_id']=str(review_id)
                data['request_digest']=str(request.request_digest)
                digest = hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                records[digest] = json.loads(json.dumps(output))
            prompt = PromptDefinition(prompt_id='phase9.jev.review',prompt_version=request.prompt_version,
                schema_version=request.schema_version,model_policy_version='jev-model-policy-v1',
                system_instructions='Describe only conflicts among supplied evidence. Never issue trading instructions.',
                purpose='evidence_conflict_review',forbidden_behavior=('BUY','SELL','ORDER'))
            ids = frozenset(str(i.evidence_id) for i in (*context.supporting_evidence,
                *context.conflicting_evidence,*context.degraded_evidence))
            return review_with_jev(request=request,prompt_registry=PromptRegistry((prompt,)),
                ai_service=service,output_schema=JEV_REVIEW_OUTPUT_SCHEMA,allowed_evidence_ids=ids)
