# Phase 6 AI Runtime Contract — Independent Design Review

Review date: 2026-09-23
Review mode: independent, read-only
Files reviewed: PHASE_6_AI_RUNTIME_CONTRACT_AUDIT.md and PHASE_6_AI_RUNTIME_CONTRACT.md
Conclusion: PASS

## Review scope

The reviewer checked the V1 system instruction and task boundary against the accepted Phase 6 design, the Phase 6 runtime audit, Migration 011, and the current AI Gateway, prompt, security, normalization, persistence, and test surfaces.

The independent reviewer confirmed that they read the complete V1 system instruction in §4. They found no trading decision, tool permission, secret access, or unsupported free-text output; the instruction forbids those behaviors and requires schema-only JSON.

| Check | Result |
|---|---|
| Finite task scope and Why AI | PASS — exactly one optional News event-type candidate task; entity/symbol/time/summary-generation, Macro, and Unlock AI tasks are excluded with deterministic reasons. |
| Input boundary | PASS — task-only derived AISafeContext, closed input schema, bounded plain-text fields, exact taxonomy eligibility, no raw URL/source reference or unrelated Phase 5/Macro/Unlock state. |
| Prompt and versioning | PASS — stable provider-neutral prompt ID, explicit prompt/schema versions, reviewed short instruction, version/hash/cache invalidation relation. |
| Structured output | PASS — one closed taxonomy/null schema, strict bounds and cross-field validation rules; existing top-level StrictSchema is explicitly insufficient. |
| Evidence and unsupported claims | PASS — server-generated evidence IDs, source/event/content/field hashes, field path and byte span, exact-context resolution; any unsupported/foreign reference rejects the whole result. |
| Numeric safety | PASS — no numeric output; Macro surprise and Unlock percentages remain deterministic. |
| Status and failure semantics | PASS — task-level pre-request outcome is separate from AIResult; NOT_CONFIGURED, stale, soft/hard budget, queue/provider/schema/evidence/persistence failures are distinguished and fail soft. |
| Cache and provider identity | PASS — provider fallback disabled for V1; cache admission follows evidence validation and persistence commit; cache hits are revalidated and do not duplicate provider token/cost usage. |
| Persistence and usage linkage | PASS — accepted output and usage are transactional; one semantic analysis identity and execution-specific usage identity are defined. Migration 011 gaps are explicit prerequisites for a new additive migration, not misrepresented as satisfied. |
| Fake Provider | PASS — test-only, non-network provider must pass the real registry → Gateway → schema/evidence → persistence/usage path. |
| Security and Phase boundaries | PASS — untrusted input, delimiter escape, no tools/secrets/external access, no trading semantics, and no Phase 7/8/runtime authorization. |

## Accepted limitations and implementation gates

- Evidence proves traceability to the supplied text, not the semantic correctness of an event classification. The only output is therefore persisted as ai_event_type_candidate and never as authoritative source data.
- The default Phase 6 source registry is empty; this design approves no live News source and asserts no model/data quality.
- A future runtime implementation must add the specified migration before enabling the task: request_hash-consistent analysis uniqueness, a usage → analysis FK, and unique execution IDs, with a fail-closed preflight for historical ambiguity.
- No code, provider, credentials, database migration, runtime lifecycle, or deployment is approved by this design review.

No blocking design issue remains. The PASS means the contract is sufficiently explicit for a separately authorized implementation; it is not Phase 6 runtime acceptance.
