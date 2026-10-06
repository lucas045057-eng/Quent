# Phase 6 Context and AI — Implementation Plan Preparation

**State:** implementation executed on `phase6`; Tasks 1-9 completed under this plan.

**Base:** `phase6` from accepted Phase 5 HEAD `610c8afa389581f49b33cfd88cde6d6865ed59ab`

**Execution rule:** future work follows `TEST -> FAIL -> IMPLEMENT -> PASS ->
REVIEW -> FIX -> RETEST -> COMMIT`. The plan is not permission to start
implementation in the design gate.

## Guardrails

- WSL2 local repository only: `/home/lucas045057/projects/quant`.
- Keep `TRADING_MODE=paper`.
- No remote ECS, exchange private API, provider key, real AI request, order,
  position, executor, Phase 7+, or Migration 011 SQL during design.
- Do not install an AI SDK before the provider-neutral Gateway and security
  tests are reviewed.
- Do not add a daemon/container without an explicit architecture change.

## Planned task sequence

### Task 1 — Source audit and registry contract

Write failing tests for source type, exact host/path allowlist, HTTPS-only
fetches, redirect rejection, byte/timeout/rate bounds, and provenance. Review
official/RSS/public/licensed/exchange/project/third-party candidates, then
implement only the approved registry and bounded fetch interface.

### Task 2 — Canonical event contracts

Write failing contract tests for News, Macro, and Unlock fields, UTC time
separation, status precedence, compatible units, no fake unlock percentage,
and bounded raw references. Implement versioned normalizers and deterministic
reason/status mapping.

### Task 3 — Fingerprint, deduplication, and freshness

Write replay/duplicate/revision tests and type-specific freshness tests.
Implement deterministic fingerprints, source revision handling, and
config-driven retention/freshness policy without treating a missing value as
zero.

### Task 4 — Migration 011 and persistence

Write empty-database, repeat, invalid-object, rollback, uniqueness, UTC, and
retention tests. Only after those fail as expected should additive SQL be
implemented. Verify migrations 001-010 are unchanged and no role/schema
outside Quant is touched.

### Task 5 — `AISafeContext` and redaction

Write adversarial fixtures containing keys, headers, cookies, JWTs, passwords,
DSNs, SSH material, signed URLs, raw objects, and prompt-injection text.
Implement explicit field-by-field context construction, recursive redaction,
size caps, and safe telemetry. Prove no forbidden field reaches a request.

### Task 6 — Provider-neutral AI Gateway

Write contract tests against a fake in-process provider only: request shape,
structured output validation, provider errors, timeouts, rate limits, budget
decisions, bounded retry, fallback, and cache keys. Implement interfaces and a
test provider first; add a real provider only after a separate approval.

### Task 7 — Prompt/schema versioning and AI persistence

Write cache invalidation, prompt/schema compatibility, invalid JSON, unknown
field, unsupported unit, and evidence-reference tests. Implement versioned
metadata and bounded `ai_analyses`/`ai_extractions`/`ai_usage` persistence.

### Task 8 — Engine context-only integration

Write tests with Phase 6 disabled, all inputs missing, provider failure, and
deliberately conflicting context. Implement an additive engine hook after
Stage1/Phase5 that cannot enter the Stage1 eligibility call graph.

### Task 9 — Resource, retention, restart, and security acceptance

Run bounded cleanup, database growth sample, restart/idempotency, log-redaction,
container memory, and full regression tests. Report measured samples separately
from projections. Do not start a live provider or remote runtime as part of
local acceptance.

## Future implementation exit gate

The future implementation may be called complete only after all task reviews,
security tests, persistence/replay tests, Stage1 invariance tests, full local
regression, resource validation, and a completion report pass with a clean
worktree. Phase 7 remains blocked until separately authorized.
