# Phase 6 Runtime Contract Implementation Delta

**Status:** implemented for this Phase 6 task; see `PHASE_6_RUNTIME_INTEGRATION_REPORT.md` for verification and runtime results.
**Target:** implement only the frozen Phase 6 AI Runtime Contract V1 and its collector/engine lifecycle.

## Required implementation changes

1. **Registry and PromptDefinition**
   - Add the finite task registry entry for phase6.news.event_classification.
   - Extend declarative prompt metadata to include purpose, input/output schema IDs, evidence policy, factual-source allowlist, forbidden behavior, review state, immutable content hash, and version metadata.
   - Register the reviewed V1 prompt as `APPROVED_FOR_IMPLEMENTATION` in code and activate only that exact definition at runtime; reject unknown IDs and schema-version combinations.

2. **Task-safe input and schemas**
   - Add the News-only projection from AISafeContext; recompute the canonical task context hash with ensure_ascii=false.
   - Enforce the controlled News taxonomy and plain-text gate; preserve unrecognized canonical source types as NOT_AVAILABLE and store only an AI candidate.
   - Add strict input and output JSON Schema validation, including byte bounds and null/evidence cross-field rules.
   - Keep source headline/summary, event identity, and Phase 5/Macro/Unlock fields out of system instructions and out of unrelated task input.

3. **Evidence model and validation**
   - Add the typed EvidenceReference builder and deterministic SHA-256 IDs/hashes for event identity, source reference, content, and exact field text.
   - Resolve every returned evidence ID against the submitted task catalog and reject the whole result on missing, foreign, duplicate, or invalid references.
   - Store hashes, paths, byte-span metadata, and UTC source/observed timestamps only; never persist excerpt text or raw source_ref in AI evidence JSON.

4. **Gateway/cache semantics**
   - Keep one selected provider/model per V1 execution; do not use fallback provider routing.
   - Add a task evidence-validation hook before cache admission and revalidate cache hits.
   - Admit a new result to cache only after the atomic persistence transaction commits.
   - Give cache-hit executions their own idempotent execution ID and usage row; do not copy original provider token/cost values.
   - Preserve distinct no-config, soft-budget, hard-budget, stale, queue, provider, schema/evidence, and persistence outcomes without raising into the main engine loop.

5. **Additive persistence migration and repository changes**
   - Add an additive corrective migration; do not edit Migration 011 or Migration 012.
   - Replace the legacy analysis cache unique key with unique request_hash identity.
   - Add analysis_id FK and unique execution_id to AI usage persistence, plus repository transaction/idempotency support.
   - Preflight duplicate hashes and ambiguous historical usage-to-analysis mappings; abort without deleting or silently selecting data.
   - Persist analysis/extraction/usage atomically. A task outcome with no configured provider has no fabricated provider or usage row and is exposed through the future runtime health/task status lifecycle.

6. **Fake Provider fixtures and tests**
   - Add FakeAIProvider test injection using the formal AIRequest/AIResponse interface only; no network access.
   - Cover valid classification, null/unavailable output, invalid schema, unknown/foreign/missing evidence, prompt injection (including closing delimiter), plain-text rejection, no provider configured, soft/hard budgets, retries, cache-hit accounting, and transaction rollback.
   - Prove the full test path through registry, Gateway, schema validation, evidence validation, persistence, and usage; direct repository tests remain unit tests only.

## Implementation boundary

The current task authorizes the collector/engine lifecycle, local source test
fixtures, health/shutdown wiring, and local Docker verification described by the
report. The production source registry remains empty because no live
News/Macro/Unlock source has passed the separately required source audit; the
runtime reports this as `NOT_AVAILABLE` rather than selecting an unapproved
endpoint. It still does not authorize live AI providers/credentials, Phase 7
runtime activation, Phase 8, ECS access, or trading.

## Migration-number reconciliation (2026-09-23)

The earlier draft's “Migration 012” label is superseded. Migration 012 is the
immutable Phase 7 on-chain/spot migration. The request-hash uniqueness and
AI-usage execution linkage are required database semantics, so implementation
uses additive Migration 013. Migrations 001–012 remain unchanged.
