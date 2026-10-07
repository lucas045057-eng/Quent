# Phase 6 AI Runtime Contract — Existing AI Contract Audit

Audit date: 2026-09-23
Project: /home/lucas045057/projects/quant
Branch / HEAD: phase7 / a46ee1ae8a3758a9432f63943e775f993635dc45
Audit mode: read-only; no code, configuration, database, container, or runtime changes.

## Conclusion

The repository has reusable provider-neutral AI primitives, an explicit event-to-safe-context builder, versioned prompt-envelope primitives, generic persistence methods, and isolated gateway tests. It does not have a formal registered Phase 6 AI task, a task-specific PromptDefinition, a strict per-task input/output contract, a typed EvidenceReference or runtime evidence validator, or a gateway-to-validation-to-persistence Fake Provider smoke path.

This is the concrete design gap behind PHASE6_RUNTIME_INTEGRATION_BLOCKER. Existing unit-level primitives are reusable, but they do not determine what a production Phase 6 worker is allowed to ask or persist.

## Existing surfaces and gaps

| Contract area | Existing implementation | Audit finding |
|---|---|---|
| Task / purpose registry | AIRequest.purpose is an unconstrained string. AIErrorCode and BudgetDecision are enums. | No AITaskId / task-purpose enum or allowlisted AI_TASK_REGISTRY; arbitrary purposes can be constructed. |
| AISafeContext | security.py defines immutable AISafeContext(task_id, allowed_fields, context_hash) and build_safe_context(). It selects normalized event fields, checks/redacts secret-shaped keys, rejects unsafe/signed URLs, and bounds serialized bytes. | Reusable safety boundary exists. The generic context includes News, Macro, Unlock, and optional Phase 5 fields; no task-specific projection prevents a News classifier from seeing unrelated fields. No typed evidence catalog is generated. |
| AI request / response | AIRequest, AIResponse, AIResult, AIUsage, and AIProvider protocol exist in ai.py. Request identity includes purpose, prompt ID/version, schema version, model-policy version, provider/model, and context hash. | Reusable provider-neutral transport contract exists. There is no task registry binding those values to an approved task. |
| Structured output | StrictSchema checks object shape at the top level, required/allowed keys, and simple enums. AIService.complete() invokes it before returning AVAILABLE. | No formal task schema; nested types, nullability, bounds, conditional evidence rules, and evidence-ID resolution are not enforced by StrictSchema. The design spec calls for strict JSON Schema/Pydantic-level validation. |
| Prompt definitions and versions | PromptDefinition, PromptEnvelope, and PromptRegistry exist. The definition contains prompt ID/version, schema version, model-policy version, and system instruction. phase6_prompt_versions stores those IDs, a prompt hash, status, and creation time. | PromptRegistry defaults to empty. No production PromptDefinition registration exists. Existing definitions omit purpose, task/input/output schema references, evidence policy, allowed factual sources, forbidden behavior, and review metadata. |
| Evidence | phase6_ai_extractions.evidence_refs is bounded JSONB. offline_eval.py counts expected event/evidence mismatches in local evaluation. | No canonical EvidenceReference type, evidence-catalog IDs in model input, per-field evidence completeness validator, or runtime rejection of unknown evidence references. The offline evaluator is not a production validator. |
| AI analyses | Migration 011 defines phase6_ai_analyses with event identity, request/context hashes, provider/model, purpose, prompt/schema/model-policy versions, status/error, bounded response_json, cache/retry/latency, and processing time. | Suitable base table for validated task results and provenance. It has no separate task_id; V1 must map the single task to a stable purpose. Provider/model are NOT NULL. |
| Extractions | Migration 011 defines phase6_ai_extractions, FK-linked to analysis, unique by (analysis_id, field_name), with value, bounded evidence JSON, nullable confidence, status, and time. | Suitable base table for an enrichment field. Evidence JSON is not type constrained. The canonical source event must not be overwritten by an AI extraction. |
| Usage | Migration 011 defines phase6_ai_usage with request/context hashes, provider/model, purpose, token/cost/latency, status, retry/cache, budget decision, error, and time. | It has no FK to phase6_ai_analyses and no unique request identity. Current repository writes usage separately and correlates it only by request/context hashes; transaction and restart-idempotency requirements are not enforced by this table. |
| Cache identity | AIRequest.request_hash includes purpose, prompt ID/version, schema version, model-policy version, provider/model, and context hash; in-memory cache keys use this hash. | The Migration 011 analysis unique index / ON CONFLICT key omits prompt_id and request_hash. It can collide for distinct prompts sharing the remaining indexed values. An additive persistence correction is needed before runtime use; do not rewrite Migration 011. |
| News input eligibility / sanitization | normalize_news() substitutes UNKNOWN only when event_type is absent; arbitrary non-empty source labels can pass through. Headline/summary are bounded but are not guaranteed plain text by the normalizer. source_ref is source-controlled text and is not guaranteed to be a non-URL identifier. | V1 must enforce the exact taxonomy, deterministic plain-text eligibility gate, and hash-only event/source references before any AI request. These guarantees do not currently exist. |
| Gateway cache and fallback | AIService checks cache before budget; on a miss it calls schema.validate() then immediately caches. It may use a configured fallback provider while retaining the original AIRequest.request_hash. Cache hits return the original usage object with cache_hit=True. | No task evidence validation occurs before cache write/hit. Fallback identity can disagree with actual provider/model, and cache-hit persistence could duplicate original tokens/cost. V1 therefore requires post-evidence-validation cache admission, revalidation on hit, no provider fallback, and execution-specific usage semantics. |
| Prompt delimiter safety | PromptRegistry.render() JSON-serializes context into a literal UNTRUSTED_DATA wrapper. | JSON serialization does not escape angle brackets. V1 requires separate system/user message content or a renderer-level escaped representation and a closing-delimiter injection fixture. |
| Budget semantics | BudgetDecision has ALLOW, SOFT_DEGRADE, HARD_REJECT. AIService maps every non-ALLOW result to the same NOT_AVAILABLE/BUDGET AIResult. | V1 must preserve the distinction. Since no deterministic News classifier is registered, soft degrade means skip AI and preserve source label/UNKNOWN; hard reject means refuse new provider work. |
| Fake Provider and tests | tests/test_phase6_ai.py has a local non-network FakeProvider returning AIResponse; tests exercise gateway bounds, schema check, retry/fallback, cache, and usage metadata. Persistence tests call repository methods directly. Prompt tests check the untrusted-data wrapper. | Useful unit fixtures exist, but no formal News fixture/registered contract, evidence validation, prompt-injection contract fixture, or end-to-end fake path through task registry → gateway → schema/evidence validation → transactional analysis/extraction/usage persistence. Direct repository tests do not prove that path. |
| Provider setup / failure | Gateway errors and bounded retry controls exist; current error enum has timeout, rate-limit, authentication, transport, provider rejection, invalid JSON, schema, budget, policy, and queue-full cases. | No typed task outcome/reason-code contract, including NOT_CONFIGURED. It must not be represented as a successful provider call or fabricated provider usage. |
| Runtime | PHASE_6_RUNTIME_INTEGRATION_AUDIT.md records an inline engine context hook only; no formal collector/engine-owned ingestion, persistence, AI worker, health, or shutdown lifecycle. Migration 011 tables exist, but the audit found zero runtime-produced rows. | This audit does not change that conclusion. It only freezes the contract a later integration must consume. |

## Source-level evidence

- src/quant_phase6/ai.py: generic request/result/provider contracts; top-level StrictSchema; synchronous gateway and bounded in-memory queue/cache.
- src/quant_phase6/prompts.py: generic PromptDefinition and empty-by-default registry.
- src/quant_phase6/security.py: general AISafeContext builder and redaction; no task evidence catalog.
- src/quant_phase6/persistence.py: independent analysis, extraction, usage, and prompt-version repository methods.
- migrations/011_phase6_external_context.sql: eight additive Phase 6 tables; phase6_ai_analyses, phase6_ai_extractions, phase6_ai_usage, and phase6_prompt_versions have the limitations listed above.
- tests/test_phase6_ai.py, tests/test_phase6_ai_persistence.py, tests/test_phase6_prompts.py, and tests/test_phase6_offline_eval.py: component-level tests; no production task registration or evidence gate.
- PHASE_6_DESIGN_SPEC.md §§6–16: News taxonomy, deterministic Macro/Unlock rules, status semantics, prompt-injection boundary, evidence requirement, provider neutrality, cache/version policy, and AI fail-soft boundary.
- PHASE_6_RUNTIME_INTEGRATION_AUDIT.md: existing formal runtime-call-graph and database evidence; this contract audit does not reinterpret it.

## Reconciliation with accepted Phase 6 design

The accepted design says AI can assist with event/entity classification, extraction, and bounded summaries, but it does not require all three for every event class. It also says unknown News event types remain unavailable/reviewable, AI cannot promote a source row from NOT_AVAILABLE, and Macro/Unlock numbers remain deterministic. Accordingly, the V1 contract proposes one optional News event-type classifier only. Entity, symbol, event-time, summary-generation, Macro, and Unlock tasks are explicitly evaluated and excluded below.

No code or runtime task registry is added by this audit.
