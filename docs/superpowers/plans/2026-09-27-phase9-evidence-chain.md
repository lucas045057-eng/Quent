# Phase 9 Evidence Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Native execution is selected for this plan; do not dispatch dependent implementation tasks in parallel.

**Goal:** Convert persisted Stage 1 A/B candidate events and immutable Phase 1–8 context into deterministic, auditable, paper-only DecisionCandidateV1 records.

**Architecture:** The existing Engine owns a bounded Phase 9 supervisor and consumes a same-transaction Stage 1 outbox. PostgreSQL stores immutable as-of projections and an operational admission ledger. Python performs evidence validation, approved-policy pattern matching and final decisions; optional conflict review reuses the Phase 6 AI Gateway. Phase 9 adds no provider, broker, container, execution, or Phase 10 behavior.

**Tech Stack:** Python >=3.12; PostgreSQL 16 (local Compose uses postgres:16-alpine; acceptance Compose pins postgres:16.15-bookworm); pytest >=8 and pytest-asyncio >=0.23; aiohttp >=3.9,<4; websockets >=15,<16; Pydantic >=2.7,<3; psycopg[binary] >=3.2,<4; existing Docker Compose deployment.

**Spec:** `docs/superpowers/specs/2026-09-27-phase9-evidence-chain-design.md`

## Global Constraints

- Consume only persisted Phase 1–8 canonical rows and contexts. Do not call public exchange, chain RPC, news, Deribit, or other provider endpoints.
- Preserve TRADING_MODE=paper. Phase 9 has no order, position, account, balance, sizing, execution, private API, or Phase 10 interface.
- Do not alter Stage 1 eligibility. Intake accepts only Stage 1 A/B event outcomes; B gains no automatic eligibility.
- Preserve migrations 001–015 and Phase 1–8 semantics. Add only additive migration 016_phase9_evidence_chain.sql.
- Missing and UNKNOWN are not NEUTRAL or zero. PARTIAL is never COMPLETE. Unknown units, timestamps, provenance, or policy versions fail closed.
- Do not use bullish/bearish counts, indicator voting, weighted sums, hidden scores, guessed thresholds, or runtime policy tuning.
- Jev only reviews bounded evidence conflict; Python policy retains final authority. Real Jev may remain NOT_CONFIGURED. Deterministic replay never calls real AI.
- Every MATCHED pattern requires a reviewed, approved policy manifest for that exact pattern/timeframe/direction. Unconfigured patterns remain NOT_CONFIGURED or NOT_MATCHED.
- Task 0 compatibility closure is mandatory before Tasks 1–9. Formal acceptance rejects every required test skip.
- Preserve canonical JSON, UTC, immutable as-of snapshots, transaction ownership, durable retry and idempotency contracts in the Spec.
- Preserve exact ceilings: event 64 KiB; pending outbox 1,024 rows / 64 MiB; snapshot 256 KiB; chain 64 KiB; Jev request 64 KiB / response 32 KiB; 64 EvidenceItems; 16 rows/category.
- Runtime ceilings are 2 in-flight evaluations, 32 queued IDs, 8 MiB queued bytes, 30 seconds/evaluation, 1 concurrent Jev call, 16 revalidations/minute, and the fixed `PHASE9_OUTBOX_MAX_ATTEMPTS_V1 = 3` total claims (including the initial claim). The outbox retry ceiling is a V1 contract constant, not caller- or runtime-configurable.
- Preserve PostgreSQL <=768 MiB, Collector <=256 MiB, and Engine <=384 MiB.
- Add one Phase 9 aggregate source to the existing Data Layer registry; preserve MAX_SOURCE_SNAPSHOTS=12 and existing durable backpressure semantics.
- PHASE9_REPLAY_V1 layers on DATA_LAYER_REPLAY_V1. Do not change Data Layer replay manifest, loader, fixture, or acceptance semantics.
- Formal local runtime acceptance is at least 900 seconds using PostgreSQL, a Stage 1 fixture stream, and Fake/Recorded Jev. Do not add a real-provider Phase 9 probe or a 60-minute public-source soak.

## Review Focus

- Phase 4 PARTIAL_AGGREGATED or a known liquidation gap must never become complete: Task 0.5, test_liquidation_gate.py.
- Upserted/deleted source rows must not change a stored as-of replay: Task 0.6, test_snapshot_replay_gate.py.
- Real Jev unavailable with required HIGH conflict must fail closed: Task 4, test_decision.py.
- Duplicate Stage 1 event/timeframe must not create a second evaluation or Decision: Tasks 0.3–0.4, test_stage1_outbox_gate.py and test_outbox_recovery.py.
- Missing/UNKNOWN values must not become NEUTRAL or zero: Tasks 2–3, test_evidence.py and test_patterns.py.

---

## Canonical Scalar Types

`GitSha` is a validated `NewType`/equivalent containing exactly 40 lowercase hexadecimal characters for a full Git SHA-1. `Sha256Hex` is a validated `NewType`/equivalent containing exactly 64 characters matching `[0-9a-f]`, representing a lowercase SHA-256 hexadecimal digest. Parsers reject wrong length, uppercase, or any non-hex character.

## Compatibility Closure Contract

Task 0.9 creates the tracked marker `config/phase9/compatibility-closure-v1.json` with exactly `{"schema":"PHASE9_COMPATIBILITY_CLOSURE_MARKER_V1","version":"1"}` and the four isolated suite commands. The marker contains no commit value. Its unique first-add commit is the sole machine source for the compatibility closure anchor. After the Task 0.9 implementation commit, run the four commands once to establish closure evidence. Each command writes exactly one CompatibilitySuiteEvidenceV1 JSON file. The evidence directory is `artifacts/phase9/compatibility/`; it is a new project convention because the repository has no existing Phase 9 acceptance artifact directory. Add `/artifacts/phase9/` to `.gitignore` so generated evidence cannot dirty the source worktree.

`resolve_phase9_compatibility_closure_commit(repo_root: Path) -> GitSha` runs the exact lookup `git -C <repo_root> log --follow --diff-filter=A --format=%H -- config/phase9/compatibility-closure-v1.json`, parses full lowercase 40-character commit SHAs, and requires exactly one result. It verifies that the resolved object exists as a commit with `git cat-file -e <resolved_sha>^{commit}`; missing, malformed, or multiple results fail closed. It does not accept a caller SHA, fall back to HEAD, or use ancestry/merge-base as a substitute for the closure commit.

`COMPATIBILITY_PROTECTED_PATHS` is a frozen exact sorted tuple of: `.gitignore`; `config/phase9/compatibility-closure-v1.json`; `migrations/016_phase9_evidence_chain.sql`; `scripts/run_phase9_compatibility_suite.py`; `src/quant_phase1/repositories.py`; `src/quant_phase1/service.py`; `src/quant_phase6/contract_v1.py`; `src/quant_phase9/__init__.py`; `src/quant_phase9/canonical.py`; `src/quant_phase9/compatibility_gate.py`; `src/quant_phase9/contracts.py`; `src/quant_phase9/intake.py`; `src/quant_phase9/jev.py`; `src/quant_phase9/jev_persistence.py`; `src/quant_phase9/liquidation.py`; `src/quant_phase9/snapshot.py`; `src/quant_phase9/sources/__init__.py`; `src/quant_phase9/sources/phase1.py`; `src/quant_phase9/sources/phase2.py`; `src/quant_phase9/sources/phase3.py`; `src/quant_phase9/sources/phase4.py`; `tests/quant_phase9/test_canonical.py`; `tests/quant_phase9/test_compatibility_gate.py`; `tests/quant_phase9/test_contracts.py`; `tests/quant_phase9/test_intake.py`; `tests/quant_phase9/test_jev_gate.py`; `tests/quant_phase9/test_liquidation_gate.py`; `tests/quant_phase9/test_snapshot_replay_gate.py`; `tests/quant_phase9/test_stage1_outbox_gate.py`; `tests/test_phase6_contract_v1.py`; `tests/test_repository_integration.py`. This list is the exact Task 0.1–0.9 compatibility-critical ownership set; it is not a repository-wide tree hash.

`protected_tree_digest` is SHA-256 over canonical JSON for the sorted `git ls-tree -r -z HEAD -- <COMPATIBILITY_PROTECTED_PATHS>` entries, each decoded as `{path, mode, object_type, object_sha}`. All listed paths must be tracked; missing, duplicate, or malformed entries fail closed. Before running suite tests and every time the aggregate gate runs, require `git diff --quiet HEAD -- <COMPATIBILITY_PROTECTED_PATHS>` to succeed so neither staged nor unstaged changes can make tested worktree bytes differ from the anchored HEAD. Each suite artifact records this digest. The aggregate gate recomputes it from current HEAD and requires equality with all four suite artifacts. Any compatibility-protected path change invalidates closure evidence and requires regenerating all four suite artifacts; ordinary changes outside the frozen list do not.

Exact commands:

    python scripts/run_phase9_compatibility_suite.py --suite STAGE1_OUTBOX --output artifacts/phase9/compatibility/stage1-outbox.json
    python scripts/run_phase9_compatibility_suite.py --suite LIQUIDATION_PARTIAL --output artifacts/phase9/compatibility/liquidation-partial.json
    python scripts/run_phase9_compatibility_suite.py --suite IMMUTABLE_SNAPSHOT --output artifacts/phase9/compatibility/immutable-snapshot.json
    python scripts/run_phase9_compatibility_suite.py --suite DETERMINISTIC_JEV --output artifacts/phase9/compatibility/deterministic-jev.json

CompatibilitySuiteEvidenceV1 has exactly these fields:
- schema: Literal["PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1"]
- suite_id: Literal["STAGE1_OUTBOX", "LIQUIDATION_PARTIAL", "IMMUTABLE_SNAPSHOT", "DETERMINISTIC_JEV"]
- source_commit: 40-character lowercase Git SHA-1
- protected_tree_digest: 64-character lowercase SHA-256 of the frozen compatibility-protected path tree
- command_id: exact suite_id value
- tests_passed, tests_skipped, tests_failed, tests_errors: non-negative integers
- artifact_digest: 64-character lowercase SHA-256
- passed: bool
- generated_at: timezone-aware UTC datetime

artifact_digest is SHA-256 of canonical JSON for every evidence field except artifact_digest. `source_commit` is exactly `resolve_phase9_compatibility_closure_commit(repo_root)`, never the current HEAD or a caller-selected SHA. The suite runner computes `protected_tree_digest` from current HEAD. `passed` is true exactly when tests_passed > 0 and tests_skipped == tests_failed == tests_errors == 0. The suite runner validates the suite_id-to-test mapping:
- STAGE1_OUTBOX: tests/quant_phase9/test_stage1_outbox_gate.py and tests/quant_phase9/test_intake.py
- LIQUIDATION_PARTIAL: tests/quant_phase9/test_liquidation_gate.py
- IMMUTABLE_SNAPSHOT: tests/quant_phase9/test_snapshot_replay_gate.py
- DETERMINISTIC_JEV: tests/quant_phase9/test_jev_gate.py

The sole gate command is:

    python -m quant_phase9.compatibility_gate --require-pass

The A1 wrapper may add only its fixed `--json-output` destination; the gate CLI accepts no `--expected-source-commit`, `--closure-commit`, or `--source-commit` option and no environment override for a SHA.

It reads exactly `artifacts/phase9/compatibility/stage1-outbox.json`, `liquidation-partial.json`, `immutable-snapshot.json`, and `deterministic-jev.json`. It resolves the closure commit itself with `resolve_phase9_compatibility_closure_commit(repo_root)`, recomputes the protected tree digest from current HEAD, and requires `artifact.source_commit == resolved_closure_commit` exactly for every suite artifact. An ancestor, merge-base, current HEAD, or other merely related commit is not sufficient. Missing file, invalid JSON, schema mismatch, invalid digest, unexpected command_id, invalid count, source_commit inequality, protected_tree_digest mismatch, required test skip, failure/error count, or passed=false makes the gate false. The CLI and callable gate accept no caller-provided commit SHA.

The aggregate JSON has exactly these required fields:
- schema: Literal["PHASE9_COMPATIBILITY_GATE_V1"]
- stage1_outbox_pass, liquidation_partial_pass, immutable_snapshot_pass, deterministic_jev_pass: bool
- tests_passed, tests_skipped, tests_failed, tests_errors: non-negative integer sums of the four suite records
- source_commit: full 40-character lowercase Git SHA-1
- protected_tree_digest: 64-character lowercase SHA-256 of the current protected path tree
- PHASE9_COMPATIBILITY_CLOSURE_PASS: bool
- passed: bool

`Phase9CompatibilityGateResult` serializes this exact aggregate schema. `PHASE9_COMPATIBILITY_CLOSURE_PASS` must equal `passed`. Both are true exactly when all four evidence schemas, artifact digests, exact closure commit equalities, protected tree digests, command IDs, counts, and suite passed flags validate; each tests_passed > 0; and total skipped, failed, and errors are zero. Exit 0 means pass; exit 2 means a suite/gate failure; exit 3 means missing or invalid evidence. Task 0.1–0.9 construct the closure; Tasks 1–9 run this exact gate command as their first executable command before any RED test.

## Canonical Interfaces

These definitions are the only cross-task interface source. All datetime parameters and values are timezone-aware UTC. Each signature uses full Python parameter names and types; task blocks reference these signatures without redefining them.

### Identity, event ID, and canonical serialization

EvaluationIdentityV1 fields are stage1_candidate_id: int, market: str, symbol: str, timeframe: Literal["15m", "1H", "4H"], evaluation_window_start: datetime, evaluation_window_end: datetime, policy_generation: str, and material_change_generation: str. The Spec identity preimage contains stage1_candidate_id, timeframe, evaluation_window_start, evaluation_window_end, policy_generation, and material_change_generation. market and symbol are validated context fields and are not included in the preimage. timeframe is mandatory in the preimage. evaluation_id_for(identity: EvaluationIdentityV1) -> UUID returns uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(identity_preimage_v1(identity))). Same identity repeats the same UUID; changing only timeframe changes the UUID. No truncated SHA-derived UUID is permitted.

The existing migrations/001_phase1_core.sql defines outbox_events.id as BIGSERIAL and has no event_id column. Keep id as the operational scan/cursor key. Stage1CandidateEventV1.event_id is TEXT containing the full 64-character lowercase SHA-256 hex required by the Spec’s SHA-256-derived event identity. Migration 016 adds a nullable event_id TEXT for legacy outbox rows and a partial unique index on (event_type, event_id) for non-null Phase 9 events. EventIdentityV1 fields are event_type: str, event_schema_version: str, screening_result_id: int, canonical_payload_digest: str. Its canonical preimage is canonical_json(EventIdentityV1); event_id is hashlib.sha256(preimage.encode("utf-8")).hexdigest(). Delivery attempts and delivery metadata are excluded. Tests prove repeatability, sensitivity to each identity field, 64 lowercase hex output, and one outbox event for a duplicate canonical event.

canonical_json(value: object) -> str; canonical_bytes(value: object) -> bytes; canonical_sha256(value: object) -> str; parse_canonical_json(data: bytes) -> object. Use UTF-8, sorted object keys, contract-defined array order, UTC ISO-8601 Z timestamps with microseconds, normalized decimal strings without exponent notation, explicit JSON null, and stable enum values. Reject floats, non-finite values, duplicate JSON keys, naive datetimes, unsupported values, and unknown schema versions.

### Snapshot, source, evidence, and decision models

EventIdentityV1 exact fields are event_type: str, event_schema_version: str, screening_result_id: int, canonical_payload_digest: str. event_id_for(identity: EventIdentityV1) -> str returns full lowercase SHA-256 hex over canonical_json(identity). Stage1CandidateEventV1 is immutable and self-contained: event_id, event_type, event_schema_version, stage1_candidate_id, screening_result_id, symbol, market, candidate_created_at, candidate_valid_until, stage1_policy_version, source_as_of, bounded source_refs/snapshot refs, canonical_payload_digest, created_at, and canonical_payload. Candidate IDs are the screening_results.id BIGINT. It has no raw provider body or secret.

SourceProjectionV1 carries projection_id, evaluation_id, source_phase, source_type, source_ref, symbol, market, event_time, observed_at, captured_at, processed_at, available_at, availability_status, freshness_status, quality_status, coverage_status, immutable canonical payload, source_schema_version, projection_version, and canonical_digest. event_time is source/exchange time; captured_at is receive/fetch time; processed_at is persist/process time. No clock substitutes for a missing event time.

EvaluationSnapshotV1 carries identity/evaluation_id, stage1_candidate_id, symbol, market, timeframe, evaluation_time, as_of, created_at, immutable candidate event projection, ordered source_projections, Stage 1/evidence/freshness/pattern/decision/TTL policy versions, code_version, and snapshot digest/hash. Every downstream object in an evaluation carries the same evaluation_id.

EvidenceItemV1 and EvidenceChainV1 retain the Spec’s missing/unknown/partial distinction, provenance, source times, evidence references, vetoes, evaluation_snapshot_hash, and input_snapshot_hash. EvidenceItemV1 types are exactly PRICE_STRUCTURE, OPEN_INTEREST_STRUCTURE, TRADE_FLOW, LIQUIDATION_CONTEXT, FUNDING_BASIS_POSITIONING, MARKET_REGIME, OPTIONS_CONTEXT, ONCHAIN_SPOT_MACRO. Missing/UNKNOWN is never NEUTRAL or zero.

PatternMatchV1 exact fields are pattern_match_id, evaluation_id, pattern_type, direction, status, required_evidence_ids, supporting_evidence_ids, conflicting_evidence_ids, missing, vetoes, pattern_policy_version. status is NOT_CONFIGURED|MATCHED|PARTIAL_MATCH|CONFLICTED|NOT_MATCHED. Tests compare the exact model field set.

DecisionCandidateV1 exact fields are decision_id, evaluation_id, stage1_candidate_id, symbol, market, timeframe, created_at, valid_until, eligible, direction_bias, confidence_band, matched_pattern, pattern_status, supporting_evidence_ids, conflicting_evidence_ids, degraded_evidence_ids, missing_evidence, veto_reasons, jev_review_id, reason_codes, short_summary, input_snapshot_hash, evidence_schema_version, pattern_policy_version, freshness_policy_version, decision_policy_version, ttl_policy_version, prompt_version, code_version, supersedes_decision_id. Reject forbidden fields entry_price, leverage, position_size, stop_loss, take_profit, order_type, account_balance, private_position, execution_instruction.

### Outbox, leases, admission, and persistence

Phase9OutboxWriter.emit(self, conn: Connection, *, event: Stage1CandidateEventV1) -> None. conn is owned by the Stage 1 persist transaction. It inserts a Phase9 outbox row with phase9_state=PENDING, attempt_count=0, attempt_limit=NULL and next_attempt_at=created_at. It acquires no connection and commits nothing.

persist_stage1_candidate_with_event(conn: Connection, *, run_id: int, screening_result: Stage1Result, candidate_created_at: datetime, candidate_valid_until: datetime | None, stage1_policy_version: str, source_as_of: datetime) -> int. It writes Stage 1 result and canonical A/B event atomically on the caller-owned connection and returns the screening_results identity via RETURNING. C/D emits no event.

AdmissionDispositionV1 is exactly ADMITTED|ALREADY_ADMITTED|EVENT_EXPIRED|INVALID_EVENT. `EventIdentityConflictError(RuntimeError)` means an existing canonical event identity is being reused with a different canonical payload digest.
`admit_event(conn: Connection, *, event: Stage1CandidateEventV1, identity: EvaluationIdentityV1, now: datetime) -> AdmissionDispositionV1` is caller-transaction-owned: it never commits, rolls back, or acquires a second connection. Its canonical identity is `(event_type, event_id)`, enforced by the partial unique database constraint; the stored canonical payload digest decides whether a duplicate is idempotent or conflicting. Exact truth table:
- A: invalid event schema, digest, or required fields -> `INVALID_EVENT`; no row mutation.
- B: `event.candidate_valid_until <= now` -> `EVENT_EXPIRED`; no admission row and no evaluation scheduled.
- C: no existing canonical identity and valid event -> insert `PENDING`; return `ADMITTED`.
- D: identity exists with the same canonical event digest in any of `PENDING`, `LEASED`, `RETRY_WAIT`, `ACKNOWLEDGED`, or `DEAD_LETTER` -> `ALREADY_ADMITTED`; no mutation, state regression, resurrection, or duplicate evaluation admission. In particular, acknowledged and dead-letter rows remain terminal.
- E: identity exists with a different canonical payload digest -> raise `EventIdentityConflictError`, causing the caller-owned transaction to fail/roll back; never overwrite the existing row.

`PHASE9_OUTBOX_MAX_ATTEMPTS_V1 = 3` is the fixed V1 total claim-attempt limit, including the first claim; no runtime config or public caller parameter can change it. OutboxEventStateV1 is exactly PENDING|LEASED|ACKNOWLEDGED|RETRY_WAIT|DEAD_LETTER. Migration 016 adds nullable `phase9_state` so pre-existing non-Phase9 outbox rows remain outside this state machine; every Phase9 event has a non-null state. It adds `attempt_count INTEGER NOT NULL DEFAULT 0`, `attempt_limit INTEGER NULL`, `next_attempt_at TIMESTAMPTZ NOT NULL` (backfill from created_at, then default now()), `lease_owner TEXT NULL`, `lease_expires_at TIMESTAMPTZ NULL`, `acknowledged_at TIMESTAMPTZ NULL`, `last_error_code TEXT NULL`, and `terminal_reason TEXT NULL`. Constraints enforce: PENDING has attempt_count=0/attempt_limit NULL and no lease/ack; LEASED has positive attempt_count <= positive attempt_limit, both lease fields non-null and no ack; RETRY_WAIT has positive attempt_count < attempt_limit and no lease/ack; ACKNOWLEDGED has positive attempt_count, positive attempt_limit, acknowledged_at non-null and no lease; DEAD_LETTER has positive attempt_count >= attempt_limit, nonempty terminal_reason, and no lease/ack. The state is null iff event_id is null for legacy rows. Preserve `id BIGSERIAL` as the existing operational scan cursor. Add partial unique `(event_type,event_id)` for non-null Phase9 IDs, a due-claim index `(phase9_state,next_attempt_at,created_at,event_id)`, and a lease-recovery index `(lease_expires_at,event_id)` where state=LEASED. `attempt_count` increments exactly when a lease is granted. `attempt_limit` is nullable only while an event is never claimed in PENDING; the first claim sets it to the fixed `PHASE9_OUTBOX_MAX_ATTEMPTS_V1` value 3 and it stays immutable. `next_attempt_at` is UTC and new rows are due at `created_at`.
`claim_pending(conn: Connection, *, consumer_name: str, limit: int, now: datetime, lease_until: datetime) -> Sequence[Stage1CandidateEventV1]`. Preconditions: limit > 0 and lease_until > now. Caller owns transaction. First atomically transition due RETRY_WAIT, expired LEASED, or malformed PENDING rows with attempt_count >= 3 to DEAD_LETTER, setting `terminal_reason=ATTEMPT_LIMIT_EXHAUSTED` and clearing lease fields; then select eligible PENDING/RETRY_WAIT/expired-LEASED rows ordered exactly `next_attempt_at ASC, created_at ASC, event_id ASC`, using PostgreSQL `FOR UPDATE SKIP LOCKED` (`ORDER BY ... LIMIT %s FOR UPDATE SKIP LOCKED`). A never-claimed PENDING row starts with attempt_count=0; first, second, and third claims transition 0→1, 1→2, and 2→3. Persist `attempt_limit=PHASE9_OUTBOX_MAX_ATTEMPTS_V1` on first claim; preserve it thereafter. Once attempt_count >= 3, no later claim returns the event; if it remains PENDING, RETRY_WAIT, or stale LEASED, transition it to DEAD_LETTER and do not return it. Set LEASED, owner, and expiry; increment attempt_count exactly once; return the event. Use the supplied connection; never commit, rollback, or acquire another connection.

The project has no existing lease-specific exception class; Phase 9 uses RuntimeError subclasses consistent with runtime state errors:
- Phase9LeaseError(RuntimeError)
- EventNotClaimedError(Phase9LeaseError)
- LeaseOwnershipError(Phase9LeaseError)
- LeaseExpiredError(Phase9LeaseError)
- AlreadyAcknowledgedError(Phase9LeaseError)

Claim disposition truth table: due PENDING with attempt_count=0 -> first LEASED claim (attempt 1); due RETRY_WAIT with attempt_count 1 or 2 -> next LEASED claim; LEASED with lease_expires_at > now -> not claimable; expired LEASED with attempt_count 1 or 2 -> stale reclaim with new owner/expiry and the next attempt count; due RETRY_WAIT or expired LEASED at attempt_count >= 3 -> DEAD_LETTER and not returned; malformed PENDING already at attempt_count >= 3 -> DEAD_LETTER and not returned; ACKNOWLEDGED and DEAD_LETTER are never returned. Thus attempts 1, 2, and 3 may be claimed, while attempt 4 is impossible. Row locking serializes dispositions.

Keep `event_id: str` as the SHA-256 text identity (64 lowercase hex) for ack/retry. Do not convert it to UUID or add a second event identifier: the separate `outbox_events.id BIGSERIAL` remains only the scan/cursor key.
ack(conn: Connection, *, event_id: str, consumer_name: str, now: datetime) -> None: LEASED + matching owner + active lease + unacknowledged -> ACKNOWLEDGED; wrong owner -> LeaseOwnershipError; expired lease -> LeaseExpiredError; PENDING/RETRY_WAIT/DEAD_LETTER/missing -> EventNotClaimedError; already ACKNOWLEDGED for the same event_id -> idempotent success, regardless of repeated caller. Duplicate ack has only this interpretation. Caller owns transaction; no commit/rollback.

The retry release API returns None and persists its disposition in OutboxEventStateV1; RETRY_SCHEDULED and RETRY_EXHAUSTED are not return values.
release_for_retry(conn: Connection, *, event_id: str, consumer_name: str, retry_at: datetime, error_code: str, now: datetime) -> None. Validate error_code against RetryableErrorCodeV1, exactly DB_SERIALIZATION_CONFLICT|AI_GATEWAY_TIMEOUT|AI_GATEWAY_RATE_LIMITED|AI_GATEWAY_UNAVAILABLE; require retry_at > now. For LEASED with matching owner, active lease, no ack and attempt_count < PHASE9_OUTBOX_MAX_ATTEMPTS_V1, set RETRY_WAIT, clear owner/expiry, set next_attempt_at=retry_at, and persist error_code. At attempt_count >= 3, set DEAD_LETTER, clear lease fields, and persist terminal failure reason; return None for both transitions. Wrong owner -> LeaseOwnershipError; expired lease -> LeaseExpiredError; PENDING/RETRY_WAIT/DEAD_LETTER/missing -> EventNotClaimedError; ACKNOWLEDGED -> AlreadyAcknowledgedError; retry_at <= now or a non-retryable code -> ValueError. The persisted attempt_limit is always the fixed V1 value 3 on first claim. Caller owns transaction; no commit/rollback.

The single `build_snapshot` signature is listed under Evidence, pattern, and decision public APIs; it passes the same caller-owned connection to `source_reader.read` and never commits, rolls back, or acquires another connection.
persist_evaluation_snapshot(conn: Connection, *, snapshot: EvaluationSnapshotV1) -> None. Immutable insert in caller-owned transaction; same evaluation_id and digest is idempotent success; same ID with different digest raises SnapshotIdentityConflictError; never UPDATE the snapshot payload.
SnapshotIdentityConflictError(RuntimeError) identifies immutable evaluation_id/snapshot_digest conflicts.
TX-SNAPSHOT is one short caller-owned REPEATABLE READ transaction: BEGIN; if admission/claim has not already been committed by the poller, perform that work on this same connection; call build_snapshot(conn, ...); call persist_evaluation_snapshot(conn, ...); COMMIT. Any error rolls back the complete snapshot transaction. A previously committed lease-only TX-A stays separate. After TX-SNAPSHOT, all Evidence/Pattern/optional Jev reads the durable snapshot only and never reselects mutable Phase 1–8 rows for that evaluation.

persist_final(conn: Connection, *, evaluation_id: UUID, snapshot_digest: str, evidence_items: Sequence[EvidenceItemV1], evidence_chain: EvidenceChainV1, pattern_matches: Sequence[PatternMatchV1], jev_review: JevReviewV1 | None, decision: DecisionCandidateV1, lifecycle_event: DecisionStatusEventV1, event_id: str, consumer_name: str, now: datetime) -> None. Require the already-durable snapshot for evaluation_id to have exactly snapshot_digest; accept no snapshot payload and never redefine it. In caller-owned final TX-B atomically insert Evidence, Chain, Pattern, Decision, Lifecycle, reference only an already-durable JevReview, and acknowledge event_id through the canonical ack contract on the same connection. Caller commits TX-B; any failure rolls back final rows and ack together. This is the sole persist_final definition.
persist_jev_review(conn: Connection, *, review: JevReviewV1) -> None. It commits only through its caller-owned transaction before a decision can reference the review.

### Policy manifest and approval

PolicyManifestV1 fields are schema: Literal["PHASE9_POLICY_MANIFEST_V1"], manifest_version: str matching SemVer MAJOR.MINOR.PATCH with no prerelease/build suffix, created_at: UTC datetime, policy_content: PolicyContentV1, manifest_digest: 64 lowercase SHA-256 hex. Digest is SHA-256 of canonical_bytes(policy_content) only; envelope metadata, approval timestamp, filesystem path, and runtime load time are excluded.

PolicyApprovalV1 fields are schema: Literal["PHASE9_POLICY_APPROVAL_V1"], manifest_version: SemVer MAJOR.MINOR.PATCH, manifest_digest: 64 lowercase SHA-256 hex, approval_status: APPROVED|REVOKED, approved_at: UTC datetime, approved_by: non-empty str, approved_commit: exactly 40 lowercase hexadecimal characters. approved_commit records the Git commit that produced the approval artifact. manifest_digest binds the approval to policy content. The Spec requires no runtime-HEAD equality or ancestry check; do not add either. Runtime validates status, version, recomputed digest, UTC timestamp, approved_by, and commit format.

Policy enums and records are fully typed. No dict[str, Any] or untyped mapping is allowed in policy core structures. Enums: PolicyDirectionV1=LONG|SHORT; PredicateOperatorV1=PRESENT|EQUALS|IN_SET|GT|GTE|LT|LTE|BETWEEN; PolicyUnitV1=NONE|SECONDS|COUNT|USD|BASE_ASSET|QUOTE_ASSET|RATIO|PERCENT|BASIS_POINTS|PRICE|SOURCE_NATIVE; MissingBehaviorV1=FAIL_CLOSED|PARTIAL_MATCH|NOT_CONFIGURED; ConfidenceBandV1=HIGH|MEDIUM|LOW|INSUFFICIENT; PolicyApprovalStatusV1=APPROVED|REVOKED; StaleBehaviorV1=FAIL_CLOSED|PARTIAL|NOT_CONFIGURED; PredicateEffectV1=BLOCK_MATCH|CAP_CONFIDENCE|RECORD_ONLY; DecimalNullBehaviorV1=REJECT|PRESERVE_NULL; RoundingModeV1=HALF_EVEN|HALF_UP|DOWN|UP; MaterialChangeFieldV1=DIRECTION|ELIGIBILITY|CONFIDENCE_BAND|MATCHED_PATTERN|HARD_VETO_SET|CORE_EVIDENCE|JEV_JUDGMENT; ConflictSeverityV1=NONE|LOW|MEDIUM|HIGH; JevUnavailableBehaviorV1=FAIL_CLOSED|DEGRADE; PolicyDataStatusV1=AVAILABLE|STALE|NOT_AVAILABLE|PARTIAL|ERROR|NOT_CONFIGURED; PolicyCoverageStatusV1=SOURCE_DECLARED_COMPLETE|PARTIAL|UNKNOWN|NOT_AVAILABLE; JevConflictClassV1=PATTERN_AMBIGUITY|DIRECTIONAL_PATTERN_CONFLICT|MATERIAL_AUXILIARY_CONTRADICTION|REQUIRED_HIGH_CONFLICT; HardVetoCodeV1=CORE_MARKET_DATA_INVALID|STAGE1_INVALIDATED|CORE_DIRECTIONAL_EVIDENCE_MISSING|DATA_CORRUPTION_OR_PROVENANCE_INVALID|EXTREME_REGIME_CONFLICT|EVIDENCE_TOO_STALE.

PredicateRuleV1 fields: predicate_id: str, source_phase: SourcePhase, source_type: str, evidence_type: str, accepted_semantic_codes: tuple[str, ...], operator: PredicateOperatorV1, threshold: Decimal | None, upper_threshold: Decimal | None, unit: PolicyUnitV1 | None, missing_behavior: MissingBehaviorV1. threshold is null only for PRESENT/EQUALS/IN_SET; upper_threshold exists iff BETWEEN; numeric operators require a known non-NONE unit.
PatternPolicyV1 fields: pattern_type: str, timeframe: Literal["15m", "1H", "4H"], direction: PolicyDirectionV1, required_predicates/supporting_predicates/contradicting_predicates/hard_conflict_predicates: tuple[str, ...], freshness_rule_ids/coverage_rule_ids: tuple[str, ...], confidence_ceiling: ConfidenceBandV1, jev_required_conflict_classes: tuple[JevConflictClassV1, ...], ttl_rule_id: str, revalidation_rule_id: str, approval_reference: str.
FreshnessRuleV1 fields: rule_id: str, source_phase: SourcePhase, source_type: str, maximum_age_seconds: positive int, required: bool, stale_behavior: StaleBehaviorV1.
CoverageRuleV1 fields: rule_id: str, source_phase: SourcePhase, source_type: str, accepted_coverage_statuses: tuple[PolicyCoverageStatusV1, ...], minimum_coverage_ratio: Decimal | None, ratio_unit: PolicyUnitV1 | None. Ratio and unit are both null or both present with unit RATIO.
HardVetoRuleV1 fields: veto_code: HardVetoCodeV1, predicate_ids: tuple[str, ...], precedence: positive int, applies_to_pattern_types: tuple[str, ...].
SoftDegradationRuleV1 fields: rule_id: str, source_phase: SourcePhase, source_type: str, triggering_statuses: tuple[PolicyDataStatusV1, ...], reason_code: str, confidence_ceiling: ConfidenceBandV1, required_predicate_effect: PredicateEffectV1.
ConfidenceCeilingRuleV1 fields: rule_id: str, predicate_ids: tuple[str, ...], maximum_band: ConfidenceBandV1, reason_code: str.
TTLRuleV1 and RevalidationRuleV1 both key by rule_id, pattern_type and timeframe Literal["15m", "1H", "4H"]; TTL has positive ttl_seconds; revalidation has positive cadence_seconds and positive max_runs_per_minute.
MaterialChangeRuleV1 fields: rule_id: str, semantic_fields: tuple[MaterialChangeFieldV1, ...], decimal_tolerance: Decimal | None, tolerance_unit: PolicyUnitV1 | None. Tolerance and unit are both null or both present with a compatible unit.
JevConflictRuleV1 fields: rule_id: str, conflict_class: JevConflictClassV1, minimum_severity: ConflictSeverityV1, review_required: bool, unavailable_behavior: JevUnavailableBehaviorV1.
NumericUnitRuleV1 fields: semantic_field: str, unit: PolicyUnitV1, source_phase: SourcePhase, source_type: str, verified_by_contract: bool.
DecimalRuleV1 fields: semantic_field: str, precision: non-negative int, input_unit: PolicyUnitV1, nullable_behavior: DecimalNullBehaviorV1.
RoundingRuleV1 fields: semantic_field: str, rounding_mode: RoundingModeV1, precision: non-negative int, unit: PolicyUnitV1.
HardVetoPrecedenceV1 fields: higher_veto_code: HardVetoCodeV1, lower_veto_code: HardVetoCodeV1, precedence: positive int.
PolicyContentV1 contains typed tuples: enabled_patterns, predicates, freshness_rules, coverage_rules, hard_veto_rules, soft_degradation_rules, confidence_ceiling_rules, ttl_rules, revalidation_rules, material_change_rules, jev_conflict_rules, numeric_units, decimal_rules, rounding_rules, hard_veto_precedence. Unknown enum/unit/status values are rejected.

policy_for(*, manifest: ApprovedPolicyManifestV1, pattern_type: str, timeframe: Literal["15m", "1H", "4H"], direction: PolicyDirectionV1) -> PatternPolicyV1 | None.
load_approved_policy_manifest(manifest_path: Path, approval_path: Path, *, code_version: str) -> ApprovedPolicyManifestV1. code_version is the full 40-character lowercase SHA-1 recorded in the snapshot; it is not compared for equality/ancestry with approved_commit.
This is the sole public production entry point that reads policy/approval artifacts and constructs ApprovedPolicyManifestV1. Raw JSON parsing/typed unapproved construction is private to policy.py and cannot be imported by runtime, PatternEngine, DecisionPolicy, or evaluation services. Those consumers accept only ApprovedPolicyManifestV1. A test-only AST boundary check rejects direct policy-file reads, JSON parsing, or private parser imports outside policy.py and tests.

### Evidence, pattern, and decision public APIs

build_snapshot(conn: Connection, *, identity: EvaluationIdentityV1, stage1_candidate: Stage1CandidateEventV1, as_of: datetime, source_reader: Phase9SourceReader, policy_manifest: ApprovedPolicyManifestV1, code_version: str) -> EvaluationSnapshotV1. This is the only build_snapshot signature. It calls source_reader.read(conn, ...) on the identical caller-owned connection and embeds the approved manifest version/digest in the immutable snapshot; it does not load or parse policy files itself.
Phase9SourceReader is a Protocol with read(self, conn: Connection, *, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...]. It reads bounded rows and never owns or commits a transaction.

project_liquidation(window_row: Mapping[str, object], health_row: Mapping[str, object] | None, *, as_of: datetime) -> SourceProjectionV1.
select_phase1(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase2(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase3(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase4(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase5(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase6(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase7(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].
select_phase8(conn: Connection, candidate: Stage1CandidateEventV1, timeframe: Literal["15m", "1H", "4H"], as_of: datetime) -> tuple[SourceProjectionV1, ...].

build_evidence(*, snapshot: EvaluationSnapshotV1, policy_manifest: ApprovedPolicyManifestV1) -> tuple[EvidenceItemV1, ...].
validate_evidence(*, snapshot: EvaluationSnapshotV1, evidence_items: Sequence[EvidenceItemV1], policy_manifest: ApprovedPolicyManifestV1) -> ValidationResultV1.
build_chain_draft(*, snapshot: EvaluationSnapshotV1, evidence_items: Sequence[EvidenceItemV1], validation: ValidationResultV1) -> EvidenceChainDraftV1.
match_patterns(*, evidence_items: Sequence[EvidenceItemV1], validation: ValidationResultV1, policy_manifest: ApprovedPolicyManifestV1, timeframe: Literal["15m", "1H", "4H"]) -> tuple[PatternMatchV1, ...].
build_decision_candidate(*, snapshot: EvaluationSnapshotV1, evidence_chain: EvidenceChainV1, pattern_matches: Sequence[PatternMatchV1], jev_review: JevReviewV1 | None, policy_manifest: ApprovedPolicyManifestV1, now: datetime) -> tuple[DecisionCandidateV1, DecisionStatusEventV1].

### Phase 6 AI Gateway and Jev contracts

Actual Phase 6 source contracts, read from src/quant_phase6/security.py, src/quant_phase6/prompts.py, and src/quant_phase6/ai.py:
- quant_phase6.security.AISafeContext(task_id: str, allowed_fields: Mapping[str, Any], context_hash: str); frozen dataclass with slots.
- quant_phase6.security.redact_sensitive(value: Any, *, strict: bool = False) -> tuple[Any, tuple[str, ...]].
- quant_phase6.security.build_safe_context(event: ExternalEvent, *, task_id: str, phase5_context: Mapping[str, Any] | None = None, max_bytes: int = 65_536) -> AISafeContext. Phase 9 context is not ExternalEvent; do not construct a fake ExternalEvent to call this function.
- quant_phase6.prompts.PromptEnvelope(prompt_id: str, prompt_version: str, schema_version: str, model_policy_version: str, system_instructions: str, untrusted_data: str).
- quant_phase6.prompts.PromptRegistry.render(self, prompt_id: str, context: AISafeContext, *, prompt_version: str | None = None, schema_version: str | None = None) -> PromptEnvelope.
- quant_phase6.ai.AIRequest(purpose: str, prompt_id: str, prompt_version: str, schema_version: str, model_policy_version: str, provider: str, model: str, envelope: PromptEnvelope, context_hash: str, timeout_seconds: float, max_output_bytes: int, estimated_cost: Decimal = Decimal("0.01")).
- quant_phase6.ai.StrictSchema(required: tuple[str, ...], allowed: tuple[str, ...], enums: Mapping[str, tuple[str, ...]]); this is the concrete top-level Phase 6 schema type passed to AIService.complete. It does not replace Phase 9 nested validation.
- quant_phase6.ai.AIService.complete(self, request: AIRequest, schema: Any, *, fallback_provider: str | None = None, evidence_validator: Callable[[Mapping[str, Any]], Any] | None = None, persist_result: Callable[[AIResult], None] | None = None) -> AIResult.

EvidenceTypeV1 is exactly PRICE_STRUCTURE|OPEN_INTEREST_STRUCTURE|TRADE_FLOW|LIQUIDATION_CONTEXT|FUNDING_BASIS_POSITIONING|MARKET_REGIME|OPTIONS_CONTEXT|ONCHAIN_SPOT_MACRO. EvidenceDirectionV1 is exactly BULLISH|BEARISH|NEUTRAL|MIXED|UNKNOWN; EvidenceStrengthV1 is exactly STRONG|MODERATE|WEAK|UNKNOWN; EvidenceFreshnessV1 is exactly FRESH|STALE|UNKNOWN|NOT_APPLICABLE; EvidenceQualityV1 is exactly VALID|PARTIAL|INVALID|UNKNOWN; EvidenceSourcePhaseV1 is exactly PHASE1|PHASE2|PHASE3|PHASE4|PHASE5|PHASE6|PHASE7|PHASE8. PatternMatchStatusV1 is exactly NOT_CONFIGURED|MATCHED|PARTIAL_MATCH|CONFLICTED|NOT_MATCHED.
Phase9JevEvidenceSummaryV1 is frozen and contains evidence_id: UUID, evidence_type: EvidenceTypeV1, semantic_code: str (must exist in its versioned evidence schema), direction: EvidenceDirectionV1, strength: EvidenceStrengthV1, availability_status: PolicyDataStatusV1, freshness_status: EvidenceFreshnessV1, quality_status: EvidenceQualityV1, coverage_status: PolicyCoverageStatusV1 | None, observed_at: datetime | None (UTC), source_phase: EvidenceSourcePhaseV1, source_type: str, interpretation: str (maximum 256 characters). Direction is a semantic label, never a numeric vote. It contains no raw source payload or free-form source metadata.
Phase9JevPatternSummaryV1 is frozen and contains pattern_type: str, direction: PolicyDirectionV1, status: PatternMatchStatusV1, pattern_policy_version: str.
Phase9JevPolicyVersionsV1 is frozen and contains evidence_schema_version: str, pattern_policy_version: str, decision_policy_version: str, freshness_policy_version: str, ttl_policy_version: str, prompt_version: str, code_version: str.
Phase9JevSafeContextV1 is frozen and contains schema: Literal["PHASE9_JEV_SAFE_CONTEXT_V1"], evaluation_id: UUID, evidence_chain_id: UUID, symbol: str, market: str, timeframe: Literal["15m", "1H", "4H"], as_of: datetime, requested_at: datetime, evaluation_snapshot_hash: 64 lowercase SHA-256 hex, market_regime: str | None, supporting_evidence: tuple[Phase9JevEvidenceSummaryV1, ...], conflicting_evidence: tuple[Phase9JevEvidenceSummaryV1, ...], missing_evidence_types: tuple[EvidenceTypeV1, ...], degraded_evidence: tuple[Phase9JevEvidenceSummaryV1, ...], pattern_summaries: tuple[Phase9JevPatternSummaryV1, ...], unresolved_conflict_codes: tuple[JevConflictClassV1, ...], policy_versions: Phase9JevPolicyVersionsV1, context_hash: Sha256Hex. Ordered tuples follow canonical evidence/pattern order; all timestamps are UTC; supporting/conflicting/degraded lists and pattern/unresolved lists are each <=16, missing_evidence_types <=8, and each evidence-ID reference list in downstream Jev output <=64. `context_hash` is the lowercase SHA-256 digest of canonical bytes for all fields except context_hash and must satisfy `Sha256Hex` validation.
build_phase9_jev_safe_context(*, snapshot: EvaluationSnapshotV1, evidence_items: Sequence[EvidenceItemV1], evidence_chain: EvidenceChainV1, pattern_matches: Sequence[PatternMatchV1], requested_at: datetime) -> Phase9JevSafeContextV1. It projects only these typed immutable inputs, binds snapshot hash/requested_at, and rejects evaluation/hash mismatch.
build_phase6_safe_context_for_phase9(*, context: Phase9JevSafeContextV1) -> AISafeContext. Define task_id as `phase9-jev:<evaluation_id>` and convert every typed Phase9 field to the exact JSON-safe primitive projection (UTC ISO strings, enum values, UUID strings, ordered arrays). First verify Phase9 `context_hash` against canonical bytes of its own fields excluding context_hash. Then build the Phase6 `allowed_fields` mapping including `task_id`, call `redact_sensitive(payload, strict=True)` and require unchanged payload plus no redaction paths, enforce the existing 65,536-byte ceiling, and calculate AISafeContext.context_hash using the exact Phase6 implementation encoding `json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")`. Construct the existing AISafeContext with MappingProxyType(safe); keep the Phase9 hash and Phase6 hash distinct because the latter includes task_id. Do not fabricate ExternalEvent or route Phase9 fields through generic Phase6 source selection.

JevReviewStatusV1 is exactly COMPLETED|NOT_CONFIGURED|NOT_AVAILABLE|TIMEOUT|INVALID|FAILED.
JevEvidenceAssessmentV1 fields: evidence_ids: tuple[UUID, ...], assessment: str with length <=256.
JevConflictNoteV1 fields: reason_code: str, evidence_ids: tuple[UUID, ...], detail: str with length <=256.
JevDegradationNoteV1 fields: reason_code: str, detail: str with length <=256.
JevReviewV1 exact fields are: evaluation_id: UUID, review_id: UUID, status: JevReviewStatusV1, reason_code: str | None, reason_detail: str | None (maximum 256 characters), evidence_chain_id: UUID, request_digest: 64-character lowercase SHA-256, response_digest: 64-character lowercase SHA-256 | None, provider: str | None, model: str | None, model_version: str | None, prompt_version: str, relation: CONSISTENT|MOSTLY_CONSISTENT|CONFLICTING|HIGHLY_CONFLICTING|INDETERMINATE | None, conflict_severity: NONE|LOW|MEDIUM|HIGH | None, dominant_context: FLOW_CONFIRMATION|FLOW_DIVERGENCE|CROWDING_RISK|REGIME_CONFLICT|LIQUIDATION_CONTEXT|PRICE_STRUCTURE_DOMINANT|MIXED|UNKNOWN | None, supporting_assessments: tuple[JevEvidenceAssessmentV1, ...], conflicting_assessments: tuple[JevEvidenceAssessmentV1, ...], unresolved_conflicts: tuple[JevConflictNoteV1, ...], degradation_notes: tuple[JevDegradationNoteV1, ...], referenced_evidence_ids: tuple[UUID, ...], reasoning_summary: str | None (maximum 512 characters), created_at: UTC datetime. COMPLETED requires the response fields and no failure detail; every non-COMPLETED status requires a bounded reason_code and may retain a secret-free reason_detail. NOT_AVAILABLE reason codes distinguish PROVIDER_UNAVAILABLE, CIRCUIT_OPEN, GATEWAY_UNAVAILABLE, BUDGET_BLOCKED, RATE_LIMITED, and PROVIDER_LIFECYCLE_NOT_CONFIGURED; defining CIRCUIT_OPEN as a reason code does not imply that Phase 6 currently has a circuit breaker.
Each nested object rejects unknown fields. Jev output is <=32 KiB; nesting depth <=4; each Evidence ID reference list has <=64 entries; supporting_assessments, conflicting_assessments, unresolved_conflicts, and degradation_notes each have <=16 entries; each assessment/detail text is <=256 characters; reasoning_summary is <=512 characters. Jev request JSON is <=64 KiB. Validator tests cover each exact boundary at limit and limit+1, nesting depth 4 and 5, unknown fields, invalid enums, invented Evidence IDs, and prohibited trade fields BUY, SELL, ENTRY, STOP, LEVERAGE, POSITION_SIZE, ORDER.

validate_jev_review_output(*, raw_output: object, allowed_evidence_ids: frozenset[str]) -> ValidatedJevReviewV1.
JevReviewRequestV1 is frozen and contains schema: Literal["PHASE9_JEV_REVIEW_REQUEST_V1"], review_id: UUID, evaluation_id: UUID, evidence_chain_id: UUID, safe_context: Phase9JevSafeContextV1, task_identifier: str, prompt_id: str, prompt_version: str, schema_version: str, provider: str, model: str, requested_at: datetime, evaluation_snapshot_hash: 64 lowercase SHA-256 hex, request_digest: 64 lowercase SHA-256 hex. The safe_context contains symbol, timeframe, market regime, pattern types, bounded supporting/conflicting/degraded/missing summaries and evidence-schema/prompt versions. requested_at is UTC. request_digest is SHA-256 of canonical bytes for every request field except request_digest; task_identifier must equal `phase9-jev:<evaluation_id>`, evaluation_snapshot_hash must equal the bound snapshot, and request IDs/digest bind to safe_context.
JevReviewOutputSchemaV1 is the existing Phase6 `StrictSchema` class with the exact required/allowed/enums structure; it must never be `Any` or a Phase9-invented substitute. Phase9 nested output validation remains separate.
build_jev_review_request(*, context: Phase9JevSafeContextV1, review_id: UUID, prompt_id: str, prompt_version: str, schema_version: str, provider: str, model: str) -> JevReviewRequestV1. Copy IDs, requested_at, snapshot hash, and version fields from the safe context; derive task_identifier as `phase9-jev:<evaluation_id>`; canonicalize and compute request_digest. Reject any caller input that disagrees with context-bound IDs/times/hashes.
review_with_jev(*, request: JevReviewRequestV1, prompt_registry: PromptRegistry, ai_service: AIService, output_schema: StrictSchema, allowed_evidence_ids: frozenset[str]) -> JevReviewV1. It internally calls build_phase6_safe_context_for_phase9(request.safe_context), so callers cannot pair a request with an unrelated AISafeContext. The only adapter flow is typed Phase9 context -> exact Phase6 AISafeContext -> PromptRegistry.render -> PromptEnvelope -> AIRequest -> AIService.complete(..., schema=output_schema, fallback_provider=None, evidence_validator=...) -> Phase9 nested schema/enum/ID/prohibited-field validation -> JevReviewV1 -> persist_jev_review in a committed transaction before any DecisionCandidate may reference it. Bind request/context digests to JevReview.request_digest and verify PromptEnvelope/AIRequest against the exact Phase6 contracts above. Phase9 has no HTTP client, provider client, queue, budget, rate limiter, or telemetry stack.

### Immutable persistence and replay

DataLayerReplayParentRefV1 fields: manifest_path: relative str with no .., replay_version: Literal["DATA_LAYER_REPLAY_V1"], parent_data_layer_bundle_hash: 64 lowercase hex, git_sha: full SHA matching existing ReplayManifest.git_sha.
Phase9ReplayArtifactRefV1 fields: path: relative str with no .., sha256: 64 lowercase hex.
Stage1CandidateFixtureV1 is frozen: schema: Literal["PHASE9_STAGE1_CANDIDATE_FIXTURE_V1"], candidate: Stage1CandidateEventV1, candidate_digest: 64 lowercase SHA-256 hex (canonical digest of candidate).
EvaluationSnapshotFixtureV1 is frozen: schema: Literal["PHASE9_EVALUATION_SNAPSHOT_FIXTURE_V1"], snapshot: EvaluationSnapshotV1, snapshot_digest: 64 lowercase SHA-256 hex (must equal snapshot.snapshot_digest).
PolicyManifestRefV1 is frozen: schema: Literal["PHASE9_POLICY_MANIFEST_REF_V1"], manifest_version: str, manifest_digest: 64 lowercase SHA-256 hex, approval_digest: 64 lowercase SHA-256 hex, manifest_artifact: Phase9ReplayArtifactRefV1, approval_artifact: Phase9ReplayArtifactRefV1. Both referenced artifacts are loaded and cryptographically checked through the single approved-policy loader contract.
RecordedJevReviewFixtureV1 is frozen: schema: Literal["PHASE9_RECORDED_JEV_REVIEW_FIXTURE_V1"], review: JevReviewV1, request_digest: 64 lowercase SHA-256 hex, response_digest: 64 lowercase SHA-256 hex | None. It is the only Jev input for replay. `review_mode` is exactly RECORDED|NO_REVIEW|REVIEW_NOT_AVAILABLE. RECORDED requires the fixture with review.status=COMPLETED and digest binding; REVIEW_NOT_AVAILABLE requires the fixture with a non-COMPLETED status and preserved reason_code/cause; NO_REVIEW requires recorded_jev_review=None, `no_review_reason=NOT_REQUIRED`, and no decision reference. All other combinations fail closed.
ExpectedPhase9HashesV1 is frozen and contains evaluation_snapshot_hash, evidence_items_digest, evidence_chain_digest, pattern_matches_digest, decision_candidate_digest, and input_snapshot_hash, all 64 lowercase SHA-256 hex. Replay compares every field after canonicalization; input_snapshot_hash includes the frozen evaluation_snapshot_hash plus the durable JevReview identity/content digest or the explicit no-review/unavailable sentinel required by the Spec.
Phase9ReplayManifestV1 is frozen and has exact fields: schema: Literal["PHASE9_REPLAY_V1"], version: Literal["v1"], parent: DataLayerReplayParentRefV1, stage1_candidate: Stage1CandidateFixtureV1, evaluation_snapshot: EvaluationSnapshotFixtureV1, policy_manifest: PolicyManifestRefV1, review_mode: Literal["RECORDED", "NO_REVIEW", "REVIEW_NOT_AVAILABLE"], no_review_reason: Literal["NOT_REQUIRED"] | None, recorded_jev_review: RecordedJevReviewFixtureV1 | None, expected_hashes: ExpectedPhase9HashesV1, migration_version: Literal["016"], code_version: 40 lowercase SHA-1. Every nested object has a versioned schema and rejects unknown keys; fixture/reason conditional invariants are exact above. No dict[str, Any], untyped JSON object, or untyped union exists in this manifest graph.
load_phase9_replay_manifest(path: Path, *, project_root: Path) -> Phase9ReplayBundleV1. The bundle contains the exact typed Phase9ReplayManifestV1, validated parent ReplayDataset, and approved policy; the original return annotation of only Phase9ReplayManifestV1 could not carry the parent and policy needed by replay. It calls existing quant_data_layer.replay.load_replay_dataset(manifest_path: Path, *, max_compressed_bytes: int = 2*1024*1024, max_uncompressed_bytes: int = 16*1024*1024, max_records: int = 20_000, project_root: Path | None = None) -> ReplayDataset and verifies dataset_sha256 against the typed parent hash. Every Phase9ReplayArtifactRefV1 path is resolved under project_root with symlink/traversal escape rejected and its raw file SHA-256 checked before typed parsing; policy and approval are then constructed only through load_approved_policy_manifest. It does not modify DATA_LAYER_REPLAY_V1 semantics.
replay_phase9(bundle: Phase9ReplayBundleV1) -> Phase9ReplayResultV1. The original ReplayDataset annotation had no Phase9 snapshot, policy or review fields. It runs stored Phase 9 canonical projections only and makes no provider or AI calls.

### Runtime observability and acceptance result interfaces

Phase9RuntimeConfig is a frozen typed configuration constructed by `quant_phase9.config.load_phase9_runtime_config`; fields are enabled: bool=false, max_inflight_evaluations: int=2 (1..2), max_queued_ids: int=32 (1..32), max_queued_bytes: int=8*1024*1024 (1..8 MiB), evaluation_timeout_seconds: int=30 (1..30), max_concurrent_jev_calls: int=1 (1..1), and max_revalidations_per_minute: int=16 (1..16). The outbox attempt ceiling is not a runtime config field: every Phase9 V1 consumer uses `PHASE9_OUTBOX_MAX_ATTEMPTS_V1 = 3`. Defaults for the remaining resource settings are explicit config defaults; runtime receives this validated object once at startup.

Add SourcePhase.PHASE9, SourceId.PHASE9_EVIDENCE_CHAIN="phase9.evidence_chain", and phase9.evaluations with ReplayClass.RECOVERABLE_REPLAYABLE and BackpressureAction.DEFER_TO_DURABLE_CURSOR. Preserve source cap 12 and use existing WorkAdmissionController/PostgresWriteAdmission. Do not add a second queue or container.

AcceptanceCommandKindV1 is exactly JSON_ARTIFACT|PYTEST_JUNIT|GIT_DIFF_CHECK|GIT_STATUS_CLEAN.
AcceptanceCommandSpecV1 is frozen and contains command_id: Literal["A1","A2","A3","A4","A5","A6","A7","A8","A9"], kind: AcceptanceCommandKindV1, command: str, required: Literal[True], artifact_path: str, parser_id: Literal["a1_compatibility_gate_v1","a2_pytest_junit_v1","a3_pytest_junit_v1","a4_pytest_junit_v1","a5_replay_v1","a6_secret_scan_v1","a7_runtime_acceptance_v1","a8_git_diff_check_v1","a9_git_status_clean_v1"].
AcceptanceCommandResultV1 exact fields:
schema: Literal["PHASE9_ACCEPTANCE_COMMAND_V1"]; command_id: Literal["A1","A2","A3","A4","A5","A6","A7","A8","A9"]; kind: AcceptanceCommandKindV1; parser_id: Literal["a1_compatibility_gate_v1","a2_pytest_junit_v1","a3_pytest_junit_v1","a4_pytest_junit_v1","a5_replay_v1","a6_secret_scan_v1","a7_runtime_acceptance_v1","a8_git_diff_check_v1","a9_git_status_clean_v1"]; command: str; source_commit: 40-character lowercase Git SHA-1; required: bool; exit_code: int | None; tests_passed: int; tests_skipped: int; tests_failed: int; tests_errors: int; artifact_path: str; output_digest: 64 lowercase SHA-256 hex; artifact_digest: 64 lowercase SHA-256 hex; passed: bool.
`artifact_path` identifies the raw parser input/captured output. `output_digest` is SHA-256 of its exact bounded raw bytes (for A8/A9, canonical bytes of the captured stdout/stderr record). The normalized command-result JSON is written separately to `artifacts/phase9/final/commands/A1.json` through `A9.json`; its `artifact_digest` is SHA-256 of canonical JSON for the normalized result excluding artifact_digest. The aggregate validates both digests and the result schema. Missing raw/result artifacts, schema mismatch, digest mismatch, or invalid count makes the command fail.

The exact A1–A9 parser entry points are `parse_a1_compatibility_gate_v1(json_path: Path, *, exit_code: int)`, `parse_a2_pytest_junit_v1(xml_path: Path, *, exit_code: int)`, `parse_a3_pytest_junit_v1(xml_path: Path, *, exit_code: int)`, `parse_a4_pytest_junit_v1(xml_path: Path, *, exit_code: int)`, `parse_a5_replay_v1(json_path: Path, *, exit_code: int)`, `parse_a6_secret_scan_v1(json_path: Path, *, exit_code: int)`, `parse_a7_runtime_acceptance_v1(json_path: Path, *, exit_code: int)`, `parse_a8_git_diff_check_v1(stdout: str, stderr: str, *, exit_code: int)`, and `parse_a9_git_status_clean_v1(stdout: str, stderr: str, *, exit_code: int)`. A2–A4 may delegate to one private JUnit parser but retain command-specific typed entry points. Each returns a typed AcceptanceCommandResultV1 input payload and fails closed on malformed, missing, oversized, or unexpected artifacts; output/diagnostic capture is bounded and secret-redacted.

Parser acceptance is exact: A1 requires `PHASE9_COMPATIBILITY_GATE_V1`, all four suite booleans true, closure `passed == PHASE9_COMPATIBILITY_CLOSURE_PASS == true`, valid closure source commit/protected digest, positive tests, and zero skips/failures/errors. A2–A4 require valid JUnit XML, exit 0, at least one collected test, and zero skips/failures/errors. A5 requires `PHASE9_REPLAY_RESULT_V1`, `passed=true`, `PHASE9_DETERMINISTIC_REPLAY_PASS=true`, `hashes_compared` exactly equal to the six ExpectedPhase9HashesV1 field names in their declared order, `hashes_match=true`, and provider_calls=ai_calls=0. A6 requires `PHASE9_SECRET_SCAN_V1`, `passed=true`, `secret_leak_found=false`, and findings_count=0. A7 requires `PHASE9_RUNTIME_ACCEPTANCE_V1`, `passed=true`, measured duration >=900 seconds, all listed runtime/persistence/failure-semantics gates true, no hard safety violation, no required skip, and a typed Jev status allowed by the final Jev rule. A8 requires exit 0 and empty stdout/stderr. A9 requires exit 0 and empty porcelain stdout; stderr must be empty after bounded normalization. Every parsed record binds the exact command_id/kind/parser_id, exit code, raw output digest, normalized result artifact digest, and counts. A1–A7 source_commit is the code SHA captured before A1; A8/A9 source_commit is the report-only descendant HEAD after report commit. The finalizer verifies ancestry and requires `git diff --name-only <A1-source_commit>..<A8-source_commit>` to equal exactly `[PHASE_9_IMPLEMENTATION_REPORT.md]`; no parser promotes unknown or missing state to pass.

The JUnit parser sums only leaf `<testsuite>` records (not a parent aggregate that would double count) and parses non-negative `tests`, `failures`, `errors`, `skipped`; `passed = tests - failures - errors - skipped`. It requires valid XML, exit_code=0, and zero failures/errors/skips for acceptance. Any missing suite or inconsistent/negative count is invalid.

The JSON command parsers require these exact versioned payloads: `Phase9ReplayAcceptanceArtifactV1(schema="PHASE9_REPLAY_RESULT_V1", passed: bool, PHASE9_DETERMINISTIC_REPLAY_PASS: bool, hashes_compared: tuple[Literal["evaluation_snapshot_hash","evidence_items_digest","evidence_chain_digest","pattern_matches_digest","decision_candidate_digest","input_snapshot_hash"], ...], hashes_match: bool, provider_calls: int, ai_calls: int)`; `Phase9SecretScanArtifactV1(schema="PHASE9_SECRET_SCAN_V1", passed: bool, secret_leak_found: bool, findings_count: int)`; and `Phase9RuntimeAcceptanceArtifactV1(schema="PHASE9_RUNTIME_ACCEPTANCE_V1", passed: bool, runtime_duration_seconds: int, PHASE9_RUNTIME_INTEGRATION_PASS: bool, PHASE9_FAILURE_SEMANTICS_PASS: bool, PHASE9_PERSISTENCE_AUDIT_PASS: bool, hard_safety_violation: bool, required_test_skips: int, PHASE9_REAL_JEV_INTEGRATION_STATUS: Literal["NOT_CONFIGURED","PASS","BLOCKED_EXTERNAL"]).` Parser semantics below further constrain these fields; unknown fields reject.

Phase9FinalAcceptanceResultV1 exact fields:
schema: Literal["PHASE9_FINAL_ACCEPTANCE_V1"]; source_commit: 40-character lowercase SHA-1; commands: tuple[AcceptanceCommandResultV1, ...]; required_commands_total: int; required_commands_passed: int; required_tests_passed: int; required_test_skips: int; required_tests_failed: int; required_test_errors: int; runtime_duration_seconds: int; PHASE9_IMPLEMENTATION_COMPLETE: bool; PHASE9_COMPATIBILITY_CLOSURE_PASS: bool; PHASE9_DETERMINISTIC_REPLAY_PASS: bool; PHASE9_FAILURE_SEMANTICS_PASS: bool; PHASE9_PERSISTENCE_AUDIT_PASS: bool; PHASE9_RUNTIME_INTEGRATION_PASS: bool; PHASE9_REAL_JEV_INTEGRATION_STATUS: Literal["NOT_CONFIGURED","PASS","BLOCKED_EXTERNAL"]; PHASE9_READY_FOR_PHASE10: bool; passed: bool.
The `Phase9ImplementationCompletionV1` control artifact is mandatory at `artifacts/phase9/final/implementation-complete.json` (C1; ignored generated evidence, not an A1–A9 command). Its exact fields are `schema: Literal["PHASE9_IMPLEMENTATION_COMPLETION_V1"]`, `plan_path: str`, `plan_digest: Sha256Hex`, `source_commit: GitSha`, `required_task_ids: tuple[Phase9TaskIdV1, ...]`, `completed_task_ids: tuple[Phase9TaskIdV1, ...]`, `task_commits: Mapping[Phase9TaskIdV1, GitSha]`, `generated_at: datetime` (timezone-aware UTC), and `passed: bool`. Unknown fields and duplicate JSON keys are rejected. The approved plan path is exactly `docs/superpowers/plans/2026-09-27-phase9-evidence-chain.md`; `plan_digest` is SHA-256 over the plan bytes read from `git show <source_commit>:<plan_path>`, after strict UTF-8 decoding and CRLF/CR-to-LF line-ending normalization only (no whitespace or Unicode normalization).

`Phase9TaskIdV1` is exactly the following 19 IDs in this order, matching every plan task heading including split Task 6: `TASK_0_1`, `TASK_0_2`, `TASK_0_3`, `TASK_0_4`, `TASK_0_5`, `TASK_0_6`, `TASK_0_7`, `TASK_0_8`, `TASK_0_9`, `TASK_1`, `TASK_2`, `TASK_3`, `TASK_4`, `TASK_5`, `TASK_6A`, `TASK_6B`, `TASK_7`, `TASK_8`, `TASK_9`. `required_task_ids` must equal this exact ordered tuple; `completed_task_ids` must contain each exactly once; `task_commits` must have exactly one valid `GitSha` for each ID and no extras. The artifact's derived pass condition is true iff completed and required ID sets are equal and lengths match, all 19 task commits are ancestor-or-equal to `source_commit`, `plan_digest` matches the approved plan at `source_commit`, and `source_commit` exactly matches the common code commit recorded by formal acceptance A1–A7. The parser recomputes this predicate; serialized `passed` must equal the recomputed value. Any missing/malformed/inconsistent C1 fails closed.

`parse_phase9_implementation_completion_v1(path: Path, *, repo_root: Path, formal_source_commit: GitSha) -> Phase9ImplementationCompletionV1` strictly parses C1, requires `plan_path` to equal the fixed approved path above, verifies the source commit object, computes the plan digest and task-commit ancestry against Git, verifies the exact task tuple and A1–A7 source commit, recomputes `passed`, and rejects a self-declared or mismatched boolean. Missing C1, invalid C1, or recomputed `passed=false` fails the final gate. The finalizer obtains `formal_source_commit` only from the common validated A1–A7 records; it is not a CLI or callable override. `PHASE9_IMPLEMENTATION_COMPLETE` has exactly one derivation: it equals `validated_completion_artifact.passed`; a missing/invalid C1 makes it false and the final gate fail.

The result contains exactly the nine command IDs A1–A9 once each; required_commands_total=9; every required command exit_code=0 and passed=true; required_test_skips=0; required_tests_failed=0; required_test_errors=0; all required artifact schemas/digests validate; runtime_duration_seconds >=900; all six Phase 9 gate flags including machine-derived PHASE9_IMPLEMENTATION_COMPLETE, plus PHASE9_READY_FOR_PHASE10, are true. source_commit is the code commit captured before A1; A1–A7 artifacts must name that exact commit. After PHASE_9_IMPLEMENTATION_REPORT.md is committed, A8/A9 run on its descendant commit; the finalizer requires git diff --name-only source_commit..HEAD to contain exactly PHASE_9_IMPLEMENTATION_REPORT.md, proving that only the measured report changed after runtime. Real Jev NOT_CONFIGURED is allowed only when no mandatory unresolved HIGH conflict exists. Smoke never sets PHASE9_RUNTIME_INTEGRATION_PASS. passed is true iff all these rules and PHASE9_READY_FOR_PHASE10 are true.

`evaluate_phase9_compatibility_gates(*, repo_root: Path, artifact_dir: Path) -> Phase9CompatibilityGateResult` is the only public compatibility-gate callable; it resolves and validates the closure commit internally.
`evaluate_phase9_final_acceptance(*, repo_root: Path, command_artifacts: Sequence[Path], runtime_duration_seconds: int) -> Phase9FinalAcceptanceResultV1` reads the fixed C1 path, derives the formal code source commit from A1–A7, and never accepts a manually supplied source SHA or completion boolean.
assert_schema_ready(conn: Connection, *, required_version: str) -> None.

## Nineteen Sequential Implementation Tasks

Tasks are sequential because later tasks consume earlier public contracts. Each task has a complete file list, interfaces, RED command, GREEN implementation, verification command, and a commit. Tasks 1–9 run the compatibility gate before the RED test.

### Task 0.1: Canonical values and UUIDv5 evaluation identity

**Files:**
- Create: src/quant_phase9/__init__.py
- Create: src/quant_phase9/contracts.py
- Create: src/quant_phase9/canonical.py
- Test: tests/quant_phase9/test_contracts.py
- Test: tests/quant_phase9/test_canonical.py

**Interfaces:**
- Consumes: EvaluationIdentityV1, EventIdentityV1, Stage1CandidateEventV1 from Canonical Interfaces.
- Produces: exact canonical serializer/parser, evaluation_id_for, event_id_for, and canonical event, source, snapshot, evidence, pattern, Jev, policy-reference, and decision record types.

- [ ] **Step 1: Write failing tests.** Assert timeframe 15m/1H/4H yields distinct UUIDv5 IDs for an otherwise identical identity; exact field sets; canonical key, array, UTC, Decimal, and null rules; reject float, duplicate key, naive datetime, and unknown schema.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_contracts.py tests/quant_phase9/test_canonical.py. Expected: FAIL because Phase 9 contracts are absent.
- [ ] **Step 3: Implement canonical contracts.** Implement the exact public serializers and EvaluationIdentityV1 preimage from Canonical Interfaces; do not add a second identity algorithm.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_contracts.py tests/quant_phase9/test_canonical.py. Expected: exit 0, all tests passed.
- [ ] **Step 6: Commit.** Stage only the five files above. Commit message: feat(phase9): define canonical identity contracts.

### Task 0.2: Additive migration 016

**Files:**
- Create: migrations/016_phase9_evidence_chain.sql
- Test: tests/quant_phase9/test_migrations.py

**Interfaces:**
- Consumes: Stage1CandidateEventV1, EvaluationSnapshotV1, EvidenceItemV1, EvidenceChainV1, PatternMatchV1, JevReviewV1, DecisionCandidateV1, DecisionStatusEventV1.
- Produces: eight Phase 9 tables, typed outbox event_id TEXT, claim/ack/lease fields and indexes, immutable audit trigger contract.

- [ ] **Step 1: Write failing tests.** Cover empty isolated DB migration, repeat idempotency, unique identities, exact outbox state columns/nullability/state CHECK constraints/indexes, legacy-row exclusion, exact bounds, immutable UPDATE/DELETE rejection, and mutable operational lease/state only.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_migrations.py. Expected: FAIL because migration 016 is absent.
- [ ] **Step 3: Implement migration.** Create exactly phase9_evaluations, phase9_evaluation_snapshots, phase9_evidence_items, phase9_evidence_chains, phase9_pattern_matches, phase9_jev_reviews, phase9_decision_candidates, phase9_decision_status_events. Add nullable event_id TEXT and phase9_state plus the exact state/attempt/lease/ack/error columns, constraints and indexes from Canonical Interfaces; preserve legacy rows outside the Phase9 state machine. Do not alter 001–015 or cascade-delete Phase 1–8 references. No Phase 9 audit deletion/retention job in V1.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_migrations.py. Expected: exit 0, migration/repeat tests passed.
- [ ] **Step 6: Commit.** Stage only migrations/016_phase9_evidence_chain.sql and tests/quant_phase9/test_migrations.py. Commit message: feat(phase9): add immutable evidence schema.

### Task 0.3: Atomic Stage 1 event emission

**Files:**
- Modify: src/quant_phase1/repositories.py
- Modify: src/quant_phase1/service.py
- Create: src/quant_phase9/intake.py
- Test: tests/quant_phase9/test_stage1_outbox_gate.py
- Test: tests/quant_phase9/test_intake.py
- Test: tests/test_repository_integration.py

**Interfaces:**
- Consumes: Phase9OutboxWriter.emit from Canonical Interfaces.
- Produces: persist_stage1_candidate_with_event from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert shared commit/rollback, deterministic full SHA-256 event ID, A/B status preservation, C/D no event, source upsert/delete independence, event payload cap failure without truncation, and one outbox identity for duplicate canonical input.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_stage1_outbox_gate.py tests/quant_phase9/test_intake.py tests/test_repository_integration.py. Expected: FAIL because transactional Phase 9 event emission is absent.
- [ ] **Step 3: Implement atomic writer.** Use the Stage 1 caller-owned connection and RETURNING identity; never acquire another connection or commit in the writer/helper.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_stage1_outbox_gate.py tests/quant_phase9/test_intake.py tests/test_repository_integration.py. Expected: exit 0, atomicity and idempotency tests passed.
- [ ] **Step 6: Commit.** Stage only the six files above. Commit message: feat(phase9): emit transactional Stage1 candidate events.

### Task 0.4: Lease, admission, acknowledgement, and retry recovery

**Files:**
- Modify: src/quant_phase9/intake.py
- Test: tests/quant_phase9/test_outbox_recovery.py

**Interfaces:**
- Consumes: admit_event, claim_pending, ack, release_for_retry and exact truth tables from Canonical Interfaces.
- Produces: durable admission, active-owner lease recovery, idempotent ack, and explicit retry terminal state.

- [ ] **Step 1: Write failing tests.** Assert admission cases: first valid event -> ADMITTED; same identity and digest repeated -> ALREADY_ADMITTED; repeats in ACKNOWLEDGED and DEAD_LETTER remain in those terminal states; same identity with a different digest raises EventIdentityConflictError; expired event -> EVENT_EXPIRED; invalid event -> INVALID_EVENT without mutation. Assert claim attempts 1, 2, and 3 are allowed and attempt 4 is DEAD_LETTER/never returned, plus crash/restart, stale lease owner, duplicate event/timeframe, missing TTL, empty timeframe config, durable rejection/defer, retry exhaustion, and one final identity. Assert every ack/retry branch and exact exception type.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_outbox_recovery.py. Expected: FAIL because the lease lifecycle is absent.
- [ ] **Step 3: Implement durable lease lifecycle.** TX-A claims, sets owner/lease, increments attempt and commits; evaluation runs outside a DB transaction; TX-B persists final rows and acknowledgement and commits. TX-B failure rolls both back. Retry uses the same evaluation identity and snapshot hash. `claim_pending` uses the fixed `PHASE9_OUTBOX_MAX_ATTEMPTS_V1 = 3`, persists 3 on first claim, and never accepts a per-caller/runtime override. Attempts 1–3 may be claimed; at attempt 3, retry release or stale-lease processing transitions to DEAD_LETTER, never silently dropping or issuing attempt 4.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_outbox_recovery.py. Expected: exit 0, all lease states and restart cases passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/intake.py and tests/quant_phase9/test_outbox_recovery.py. Commit message: feat(phase9): recover and deduplicate candidate intake.

### Task 0.5: Liquidation PARTIAL and gap contract

**Files:**
- Create: src/quant_phase9/liquidation.py
- Test: tests/quant_phase9/test_liquidation_gate.py

**Interfaces:**
- Consumes: Phase 4 liquidation window, runtime gap health, LIQUIDATION_SOURCE_CONTRACT_V1.
- Produces: project_liquidation(...) exact signature from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert PARTIAL_AGGREGATED stays PARTIAL, missing gap health is UNKNOWN, absent event is not zero, explicit complete interval may report zero, and known gap blocks required MATCHED.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_liquidation_gate.py. Expected: FAIL because the Phase 9 projection is absent.
- [ ] **Step 3: Implement gap-aware projection.** Preserve transport, availability, coverage, gap, historical watermark, source timestamp, and reason. Never promote or erase a gap.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_liquidation_gate.py tests/test_phase4_liquidation.py tests/test_phase4_runtime.py tests/test_phase4_persistence.py. Expected: exit 0, Phase 9 and Phase 4 tests passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/liquidation.py, tests/quant_phase9/test_liquidation_gate.py. Commit message: feat(phase9): preserve liquidation gap and coverage.

### Task 0.6: Phase 1–4 immutable projections

**Files:**
- Create: src/quant_phase9/sources/__init__.py
- Create: src/quant_phase9/sources/phase1.py
- Create: src/quant_phase9/sources/phase2.py
- Create: src/quant_phase9/sources/phase3.py
- Create: src/quant_phase9/sources/phase4.py
- Create: src/quant_phase9/snapshot.py
- Test: tests/quant_phase9/test_snapshot_replay_gate.py
- Test: tests/quant_phase9/test_sources.py
- Test: tests/quant_phase9/test_snapshot.py

**Interfaces:**
- Consumes: EvaluationIdentityV1, Stage1CandidateEventV1, ApprovedPolicyManifestV1, caller-owned Connection, Phase9SourceReader, and Phase 1–4 source contracts.
- Produces: select_phase1, select_phase2, select_phase3, select_phase4, build_snapshot(conn, ...), and persist_evaluation_snapshot exact signatures from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert event/availability cutoff, no timestamp substitution, expected closed bar, confirmed flow side only, bounded rows, mutation/delete replay invariance, all source reads use the exact caller connection, same-ID/same-digest insert is idempotent, same-ID/different-digest raises SnapshotIdentityConflictError, and snapshot writes never update an existing payload.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_snapshot_replay_gate.py tests/quant_phase9/test_sources.py tests/quant_phase9/test_snapshot.py. Expected: FAIL because source projections and immutable snapshot are absent.
- [ ] **Step 3: Implement read-only projections.** Inside one short caller-owned REPEATABLE READ TX-SNAPSHOT, read indexed bounded rows and source clocks/provenance using the same connection, build the snapshot, and persist its immutable canonical payload before commit. On any error roll back the entire TX-SNAPSHOT. Close it before AI or significant CPU work; replay reads only stored projections.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_snapshot_replay_gate.py tests/quant_phase9/test_sources.py tests/quant_phase9/test_snapshot.py. Expected: exit 0, projection and replay invariance tests passed.
- [ ] **Step 6: Commit.** Stage only the nine files above. Commit message: feat(phase9): freeze core evidence projections.

### Task 0.7: Phase 5–8 immutable projections

**Files:**
- Create: src/quant_phase9/sources/phase5.py
- Create: src/quant_phase9/sources/phase6.py
- Create: src/quant_phase9/sources/phase7.py
- Create: src/quant_phase9/sources/phase8.py
- Modify: src/quant_phase9/snapshot.py
- Test: tests/quant_phase9/test_snapshot_replay_gate.py
- Test: tests/quant_phase9/test_sources.py
- Test: tests/quant_phase9/test_snapshot.py

**Interfaces:**
- Consumes: EvaluationSnapshotV1 builder, Phase 5–8 source contracts.
- Produces: select_phase5, select_phase6, select_phase7, and select_phase8 exact signatures from Canonical Interfaces, composed in fixed source order.

- [ ] **Step 1: Write failing tests.** Assert Phase 6 NOT_CONFIGURED versus missing, old Phase 8 OI/volume timestamps stay old, unknown IV unit is not AVAILABLE, Phase 7 scope remains BTC/ETH where specified, and deleted source rows do not change replay.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_snapshot_replay_gate.py tests/quant_phase9/test_sources.py tests/quant_phase9/test_snapshot.py. Expected: FAIL because Phase 5–8 adapters are absent.
- [ ] **Step 3: Implement bounded auxiliary projections.** Cap at 16 rows/category; overflow fails closed without truncation. Preserve each metric source timestamp, status, unit, coverage, and provenance.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_snapshot_replay_gate.py tests/quant_phase9/test_sources.py tests/quant_phase9/test_snapshot.py. Expected: exit 0, all Phase 5–8 and replay tests passed.
- [ ] **Step 6: Commit.** Stage only the eight files above. Commit message: feat(phase9): freeze auxiliary evidence projections.

### Task 0.8: Deterministic Jev adapter and durable review

**Files:**
- Create: src/quant_phase9/jev.py
- Create: src/quant_phase9/jev_persistence.py
- Modify: src/quant_phase6/contract_v1.py
- Test: tests/quant_phase9/test_jev_gate.py
- Test: tests/quant_phase9/test_jev.py
- Test: tests/quant_phase9/test_jev_persistence.py
- Test: tests/test_phase6_contract_v1.py

**Interfaces:**
- Consumes: EvidenceChainV1, PatternMatchV1, Phase9JevSafeContextV1, Phase 6 AISafeContext, PromptEnvelope, AIRequest, AIService.complete, AIResult.
- Produces: safe-context builders, nested validator, JevReviewV1, and persist_jev_review from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert exact frozen Phase9JevSafeContextV1 fields/hash and Evidence direction/strength/status enums, real Phase6 AISafeContext mapping (including task_id) and independently correct Phase9/Phase6 hashes, strict redaction, typed JevReviewRequestV1 digest/IDs, existing StrictSchema passed as output_schema (never Any), Fake/Recorded common validation, exact nested schema/enums/limits/references, prohibited fields, status/reason, 64 KiB request and 32 KiB response bounds, exact AIRequest/PromptEnvelope construction, and JevReview transaction committed before decision reference.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_jev_gate.py tests/quant_phase9/test_jev.py tests/quant_phase9/test_jev_persistence.py tests/test_phase6_contract_v1.py. Expected: FAIL because Phase 9 adapter and validator are absent.
- [ ] **Step 3: Implement Phase 6 adapter.** Use the exact Phase 6 contracts and typed mapping in Canonical Interfaces; do not create transport, provider, queue, budget, rate-limit, retry, or telemetry code. Persist JevReview in a committed caller-owned transaction before any decision references it.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_jev_gate.py tests/quant_phase9/test_jev.py tests/quant_phase9/test_jev_persistence.py tests/test_phase6_ai.py tests/test_phase6_contract_v1.py tests/test_phase6_prompts.py tests/test_phase6_runtime_integration.py. Expected: exit 0, Phase 9 adapter and Phase 6 non-regression passed.
- [ ] **Step 6: Commit.** Stage only the seven files above. Commit message: feat(phase9): validate and persist deterministic Jev reviews.

### Task 0.9: Machine compatibility closure

**Files:**
- Create: config/phase9/compatibility-closure-v1.json
- Create: scripts/run_phase9_compatibility_suite.py
- Create: src/quant_phase9/compatibility_gate.py
- Create: tests/quant_phase9/test_compatibility_gate.py
- Modify: .gitignore

**Interfaces:**
- Consumes: four suite test mappings and CompatibilitySuiteEvidenceV1.
- Produces: four fixed JSON artifacts, PHASE9_COMPATIBILITY_GATE_V1, and python -m quant_phase9.compatibility_gate --require-pass.

- [ ] **Step 1: Write failing tests.** Assert exact suite IDs/paths, marker bytes/schema, unique first-add commit resolution and ancestry, frozen protected path set/tree digest, staged/unstaged protected-path edits make suite/gate fail while unrelated paths do not, every evidence schema field/digest/count, closure and protected-tree binding, four suite booleans, aggregate flag equality, missing/corrupt artifact, skips, failures/errors, and exit 0/2/3.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_compatibility_gate.py. Expected: FAIL because suite evidence and aggregate gate are absent.
- [ ] **Step 3: Implement suite runner and gate.** Add the exact static marker JSON; resolve its unique first-add commit with the specified git command and ancestry check; calculate the frozen protected-tree digest from tracked HEAD entries; bind both values into all suite evidence and aggregate validation. Use the exact command/artifact mapping above; calculate artifact digest excluding artifact_digest; ignore /artifacts/phase9/; do not infer pass from a bare pytest exit status.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_compatibility_gate.py. Expected: exit 0, suite schema and aggregate-gate tests passed.
- [ ] **Step 6: Commit.** Stage only config/phase9/compatibility-closure-v1.json, scripts/run_phase9_compatibility_suite.py, src/quant_phase9/compatibility_gate.py, tests/quant_phase9/test_compatibility_gate.py, and .gitignore. Commit message: test(phase9): enforce compatibility closure gate.

After this commit and before Task 1, run the four exact suite commands above in order, then run python -m quant_phase9.compatibility_gate --require-pass. Expected: all four artifacts name the Task 0.9 commit, and PHASE9_COMPATIBILITY_CLOSURE_PASS=true. This commit is the fixed closure source_commit for preconditions in Tasks 1–9.

Progression flag is exactly `COMPATIBILITY_GATE_REQUIRED_BY_POST_CLOSURE_TASKS=true`: Task 0.1–0.9 establish the closure; every implementation task after the closure commit (Tasks 1–9) must execute the gate before its first RED test. Do not describe this as a gate required by every task.

### Task 1: Typed policy and approval

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require PHASE9_COMPATIBILITY_CLOSURE_PASS=true and exit 0; otherwise stop before RED.
**Files:**
- Create: src/quant_phase9/policy.py
- Create: policies/phase9_policy_v1.json
- Create: policies/phase9_policy_v1.approval.json
- Create: docs/PHASE_9_POLICY_MANIFEST_REVIEW.md
- Test: tests/quant_phase9/test_policy.py
- Test: tests/quant_phase9/test_policy_access_boundary.py

**Interfaces:**
- Consumes: typed PolicyContentV1, PolicyManifestV1, PolicyApprovalV1.
- Produces: policy_for(...) and load_approved_policy_manifest(...) exact signatures from Canonical Interfaces; unapproved pattern is NOT_CONFIGURED.

- [ ] **Step 1: Write failing tests.** Assert nested typed records, enums/units/nulls, SemVer envelope, UTC created_at, digest preimage, approval format/binding/revoke, no bypass, and MATCHED requires ApprovedPolicyManifestV1. The AST boundary test proves `load_approved_policy_manifest` is the only public production loader and rejects raw manifest reads/parsing or private parser imports elsewhere.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_policy.py tests/quant_phase9/test_policy_access_boundary.py. Expected: FAIL because typed policy contracts and access boundary are absent.
- [ ] **Step 3: Implement policy contract.** Default policy stays DRAFT until a human-approved artifact exists; no environment cutoff or code-only numeric threshold.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_policy.py tests/quant_phase9/test_policy_access_boundary.py. Expected: exit 0, approval, typed schema, and unique-loader boundary tests passed.
- [ ] **Step 6: Commit.** Stage only the six files above. Commit message: feat(phase9): freeze typed policy and approval gate.

### Task 2: Evidence build and validation

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/evidence.py
- Create: src/quant_phase9/validator.py
- Test: tests/quant_phase9/test_evidence.py

**Interfaces:**
- Consumes: EvaluationSnapshotV1, ApprovedPolicyManifestV1, SourceProjectionV1.
- Produces: build_evidence, validate_evidence, and build_chain_draft exact signatures from Canonical Interfaces; EvidenceChainDraftV1 and ValidationResultV1.

- [ ] **Step 1: Write failing tests.** Assert missing OI/IV is null, UNKNOWN flow stays UNKNOWN, invalid provenance hard-fails, optional NOT_CONFIGURED degrades, and references/timestamps/units/freshness/coverage are exact.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_evidence.py. Expected: FAIL because evidence contracts are absent.
- [ ] **Step 3: Implement evidence generation.** Preserve source status/provenance and enforce the 64 EvidenceItem maximum.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_evidence.py tests/test_phase3_flow_windows.py tests/test_phase4_liquidation.py tests/test_phase8_context.py tests/test_phase8_freshness.py. Expected: exit 0, Phase 9 evidence and affected source contracts passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/evidence.py, src/quant_phase9/validator.py, tests/quant_phase9/test_evidence.py. Commit message: feat(phase9): build validated evidence chains.

### Task 3: Pattern matching without voting

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/patterns.py
- Test: tests/quant_phase9/test_patterns.py

**Interfaces:**
- Consumes: EvidenceItemV1, ValidationResultV1, ApprovedPolicyManifestV1.
- Produces: match_patterns(...) and policy_for(...) exact signatures from Canonical Interfaces; exact PatternMatchV1 fields.

- [ ] **Step 1: Write failing tests.** Assert six directional structures/statuses, 5 bullish versus 2 bearish anti-vote case, failed breakout does not flip SHORT, opposing credible directions conflict, and partial liquidation caps confidence.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_patterns.py. Expected: FAIL because pattern matching is absent.
- [ ] **Step 3: Implement fixed policy precedence.** Apply only reviewed predicates and the Spec precedence; MATCHED requires approved policy. Do not count or weight evidence.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_policy.py tests/quant_phase9/test_patterns.py. Expected: exit 0, approved-policy and pattern tests passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/patterns.py and tests/quant_phase9/test_patterns.py. Commit message: feat(phase9): evaluate versioned evidence patterns.

### Task 4: DecisionCandidate and append-only lifecycle

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/decision.py
- Test: tests/quant_phase9/test_decision.py

**Interfaces:**
- Consumes: EvaluationSnapshotV1, EvidenceChainV1, PatternMatchV1, JevReviewV1 | None, ApprovedPolicyManifestV1.
- Produces: build_decision_candidate exact signature from Canonical Interfaces and the exact DecisionCandidateV1 / DecisionStatusEventV1 field sets.

- [ ] **Step 1: Write failing tests.** Assert eligibility, Real Jev NOT_CONFIGURED with mandatory HIGH conflict fails closed, Jev cannot override veto/TTL/direction, expiry/supersede, exact model field set, and forbidden Phase 10 fields.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_decision.py. Expected: FAIL because decision policy is absent.
- [ ] **Step 3: Implement Python decision policy.** Python retains final authority, uses canonical UUIDv5 identity and final input digest, and appends lifecycle events without mutating DecisionCandidate.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_decision.py. Expected: exit 0, decision and lifecycle tests passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/decision.py and tests/quant_phase9/test_decision.py. Commit message: feat(phase9): construct auditable paper decisions.

### Task 5: Atomic final persistence

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/persistence.py
- Test: tests/quant_phase9/test_persistence.py

**Interfaces:**
- Consumes: the single persist_final(...) exact signature, a durable EvaluationSnapshotV1 identified by matching evaluation_id/snapshot_digest, optional already-committed JevReviewV1, and the event lease.
- Produces: atomic Evidence/Chain/Pattern/Decision/Status rows and fixed-snapshot reader.

- [ ] **Step 1: Write failing tests.** Inject failure at every insert; assert no partial final rows, Jev-before-reference, idempotent retry, supersede/event atomicity, and immutable tables.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_persistence.py. Expected: FAIL because Phase 9 persistence is absent.
- [ ] **Step 3: Implement atomic persistence.** Use the exact signature; caller transaction owns boundaries. Validate IDs, digests, and evaluation_id; never commit or rollback internally.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_persistence.py tests/quant_phase9/test_migrations.py tests/quant_phase9/test_jev_persistence.py. Expected: exit 0, persistence/migration/Jev ordering tests passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase9/persistence.py and tests/quant_phase9/test_persistence.py. Commit message: feat(phase9): atomically persist decision audit.

### Task 6A: Migration role and schema readiness

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Modify: src/quant_phase1/config.py
- Modify: src/quant_phase1/db.py
- Create: scripts/phase9_migrate.py
- Modify: .env.example
- Modify: docker-compose.local.yml
- Test: tests/quant_phase9/test_db_roles.py

**Interfaces:**
- Consumes: schema_migrations and required version 016.
- Produces: sole explicit migration runner plus assert_schema_ready from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert absent/old schema fails startup, runtime cannot execute DDL, migration/runtime roles are least-privilege, and DSN is absent from repr/errors/health.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_db_roles.py tests/quant_phase9/test_migrations.py. Expected: FAIL because migration/runtime role separation is absent.
- [ ] **Step 3: Implement the dedicated migration entrypoint.** QUANT_MIGRATION_DSN is read only by phase9_migrate.py; POSTGRES_DSN remains runtime-only; quant_migration owns DDL; quant_app remains least privilege. Existing runtime schema minimum stays unchanged; Phase 9 Engine requires 016 only when enabled.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_db_roles.py tests/quant_phase9/test_migrations.py. Expected: exit 0, role/readiness/migration tests passed.
- [ ] **Step 6: Commit.** Stage only src/quant_phase1/config.py, src/quant_phase1/db.py, scripts/phase9_migrate.py, .env.example, docker-compose.local.yml, tests/quant_phase9/test_db_roles.py. Commit message: feat(phase9): separate migration and runtime database roles.

### Task 6B: Remove runtime and acceptance startup DDL

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Modify: src/quant_phase1/service.py
- Modify: src/quant_phase1/entrypoints/collector.py
- Modify: src/quant_phase1/entrypoints/engine.py
- Modify: src/quant_phase2/runtime.py
- Modify: src/quant_phase6/runtime.py
- Modify: src/quant_phase7/runtime.py
- Modify: src/quant_phase8/runtime.py
- Modify: scripts/phase7_acceptance_db.py
- Test: tests/quant_phase9/test_runtime_migration_readiness.py
- Test: tests/test_phase4_runtime.py
- Test: tests/test_runtime.py
- Test: tests/test_phase2_runtime.py
- Test: tests/test_phase6_runtime_integration.py
- Test: tests/test_phase7_runtime_integration.py
- Test: tests/test_phase8_runtime_integration.py

**Interfaces:**
- Consumes: assert_schema_ready from Canonical Interfaces and scripts/phase9_migrate.py.
- Produces: listed runtime/acceptance entrypoints are readiness-only and DDL-free.

Verified non-test apply_migrations call sites to remove: quant_phase1/service.py:154; quant_phase1/entrypoints/collector.py:101,551,568,635,1097; quant_phase1/entrypoints/engine.py:388,463,479,598; quant_phase2/runtime.py:164,206; quant_phase6/runtime.py:143,413; quant_phase7/runtime.py:721,1674,1749; quant_phase8/runtime.py:74; scripts/phase7_acceptance_db.py:13,42–43. If the final read-only source scan finds another non-test runtime/acceptance call, stop and revise this plan before implementation.

- [ ] **Step 1: Write failing tests.** Assert every listed runtime/acceptance call path is DDL-free and missing schema fails startup closed.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_runtime_migration_readiness.py. Expected: FAIL because existing startup paths apply migrations.
- [ ] **Step 3: Implement read-only readiness.** Replace listed runtime calls with assert_schema_ready. Convert phase7_acceptance_db.py to read-only readiness/snapshot validation. Only phase9_migrate.py and migration-specific tests apply DDL.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_runtime_migration_readiness.py tests/test_runtime.py tests/test_phase2_runtime.py tests/test_phase4_runtime.py tests/test_phase6_runtime_integration.py tests/test_phase7_runtime_integration.py tests/test_phase8_runtime_integration.py. Expected: exit 0, readiness and affected lifecycle tests passed.
- [ ] **Step 6: Commit.** Stage only the 15 implementation/test paths in this Files section plus this plan update. Commit message: refactor(runtime): require schema readiness instead of startup migrations.

### Task 7: Bounded Engine lifecycle and observability

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/config.py
- Create: src/quant_phase9/runtime.py
- Modify: src/quant_phase9/intake.py
- Modify: src/quant_phase9/snapshot.py
- Modify: src/quant_phase1/entrypoints/engine.py
- Modify: src/quant_data_layer/backpressure.py
- Modify: src/quant_data_layer/observability.py
- Modify: .env.example
- Modify: docker-compose.local.yml
- Test: tests/quant_phase9/test_runtime.py
- Test: tests/quant_phase9/test_observability_contract.py
- Test: tests/quant_phase9/test_outbox_recovery.py

**Interfaces:**
- Consumes: validated Phase9RuntimeConfig, WorkAdmissionController, PostgresWriteAdmission, durable outbox, existing SourcePhase/SourceId/MAX_SOURCE_SNAPSHOTS/StreamContract/BackpressureAction.
- Produces: disabled-by-default Phase9EngineRuntime, Phase9 source/ID, phase9.evaluations RECOVERABLE_REPLAYABLE with DEFER_TO_DURABLE_CURSOR, source cap 12.

- [ ] **Step 1: Write failing tests.** Assert config defaults and validation bounds, no runtime limit can exceed the frozen ceilings, startup/cancel/drain, restart lease recovery, admission/queue limits, health fields, exact registry IDs and cap.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_runtime.py tests/quant_phase9/test_observability_contract.py. Expected: FAIL because Phase 9 runtime wiring is absent.
- [ ] **Step 3: Implement bounded lifecycle.** Reuse admission/backpressure and one aggregate health source. Defer canonical work to durable cursor; add no drop, second queue, unbounded task, or container.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_runtime.py tests/quant_phase9/test_observability_contract.py tests/test_runtime.py tests/test_phase2_runtime.py tests/test_phase6_runtime_integration.py tests/test_phase7_runtime_integration.py tests/test_phase8_runtime_integration.py tests/test_data_layer_replay.py tests/test_data_layer_replay_runner.py. Expected: exit 0, Phase 9 lifecycle and Phase 1–8/Data Layer regression passed.
- [ ] **Step 6: Commit.** Stage the listed files and this plan amendment. The intake/snapshot changes close two bugs exposed by the real restart test: expired events need durable terminal rows, and a caller-owned snapshot transaction must preserve the original validation error. Commit message: feat(phase9): wire bounded Engine lifecycle.

### Task 8: Typed Phase 9 replay and loader isolation

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: src/quant_phase9/replay.py
- Create: scripts/run_phase9_replay_v1.py
- Create: tests/fixtures/phase9/manifest.json
- Create: tests/fixtures/phase9/stage1_candidate.json
- Create: tests/fixtures/phase9/evaluation_snapshot.json
- Create: tests/fixtures/phase9/policy_manifest.json
- Create: tests/fixtures/phase9/policy_approval.json
- Create: tests/fixtures/phase9/recorded_jev_review.json
- Refresh: tests/fixtures/data_layer_replay_v1/manifest.json (only the component hash for the Task 7 backpressure source change; dataset SHA and records stay byte-for-byte identical)
- Modify: src/quant_phase9/jev.py (unify Jev chain identity with final persistence)
- Test: tests/quant_phase9/test_jev_gate.py
- Test: tests/quant_phase9/test_db_roles.py (isolate schema readiness from public schema)
- Test: tests/test_data_layer_backpressure.py (assert exact legacy Phase 1–8 inventory after SourcePhase gains PHASE9)
- Test: tests/quant_phase9/test_replay.py
- Test: tests/quant_phase9/test_replay_loader_isolation.py

**Interfaces:**
- Consumes: Phase9ReplayManifestV1 and DATA_LAYER_REPLAY_V1 through existing load_replay_dataset/load_manifest.
- Produces: load_phase9_replay_manifest and replay_phase9 exact signatures from Canonical Interfaces.

- [ ] **Step 1: Write failing tests.** Assert every nested versioned fixture model and unknown-field rejection, candidate/snapshot/policy/approval/Jev fixture digests, exact parent Data Layer hash through the existing loader, expected evidence/chain/pattern/decision/input-snapshot hashes, RECORDED/NO_REVIEW/REVIEW_NOT_AVAILABLE constraints and cause preservation, Phase 9 accepts only Phase9, existing loader accepts only Data Layer, source mutation stability, repeated hashes, and no provider/AI calls.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_replay.py tests/quant_phase9/test_replay_loader_isolation.py. Expected: FAIL because Phase 9 manifest/loader is absent.
- [ ] **Step 3: Implement isolated Phase 9 replay.** Validate parent with existing loader and require loaded dataset_sha256 to equal the typed parent hash. Do not alter the Data Layer loader/fixtures.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_replay.py tests/quant_phase9/test_replay_loader_isolation.py tests/test_data_layer_replay.py tests/test_data_layer_replay_runner.py. Expected: exit 0, Phase 9 and unchanged Data Layer replay tests passed.
- [ ] **Step 6: Commit.** Stage the listed files and this plan amendment. The parent fixture refresh preserves the raw replay dataset and updates only the hash of the changed registered stream component; the old digest otherwise rejects the valid post-Task-7 source. Commit message: test(phase9): add deterministic decision replay.

### Task 9: Final acceptance runner and safety contracts

**Precondition:** First executable command: python -m quant_phase9.compatibility_gate --require-pass. Require exact pass and exit 0 before RED.
**Files:**
- Create: scripts/run_phase9_acceptance.py
- Create: scripts/run_phase9_test_matrix.py
- Modify: scripts/run_phase9_replay_v1.py (accept the frozen A5 CLI output flags)
- Create: scripts/phase9_secret_scan.py
- Create: tests/quant_phase9/test_phase10_boundary.py
- Create: tests/quant_phase9/test_final_acceptance_contract.py
- Modify: tests/quant_phase9/test_observability_contract.py
- Create: PHASE_9_IMPLEMENTATION_REPORT.md

**Interfaces:**
- Consumes: all Phase 9 contracts, replay runner, registry, resource ceilings, compatibility/replay/test/security evidence.
- Produces: formal/smoke/runtime/finalize CLI, machine result schemas, mandatory C1 implementation-completion artifact, exact secret scanner command, boundary tests, measured completion report.

- [ ] **Step 1: Write failing tests.** Assert 900 seconds passes and 899 fails with INVALID_ACCEPTANCE_DURATION; smoke cannot set runtime pass; any required skip fails; exact forbidden field/import boundary; exact observability assertions; all nine command records, command kinds, parser IDs, JSON/JUnit artifacts, raw output digests, source-commit rules and result artifact digests are required and valid. Assert C1 missing/invalid/mismatched source or plan digest fails; C1 `passed=false` fails; the exact 19 task IDs are required once each; task commit map keys match exactly and all commits are ancestors of the A1–A7 source commit; completion `passed` is recomputed rather than trusted. Malformed XML/JSON and any A1–A9 gate mismatch fail closed; result `passed` equals `PHASE9_READY_FOR_PHASE10` and the frozen conjunction.
- [ ] **Step 2: Run tests to verify failure.** Run: python -m pytest -q tests/quant_phase9/test_phase10_boundary.py tests/quant_phase9/test_final_acceptance_contract.py tests/quant_phase9/test_observability_contract.py. Expected: FAIL because final machine contracts are absent.
- [ ] **Step 3: Implement acceptance and safety contracts.** Formal duration defaults to 900 and rejects shorter runs. The runner captures A1–A9 command exit/count/artifact evidence, validates every artifact digest, requires and validates C1 at `artifacts/phase9/final/implementation-complete.json`, derives `PHASE9_IMPLEMENTATION_COMPLETE` only from `validated_completion_artifact.passed`, and emits Phase9FinalAcceptanceResultV1; it never accepts manually supplied PASS/completion flags or a caller-supplied source SHA. Formal mode writes C1 from the exact 19 task IDs, their task commit map, the approved plan digest, and the source commit it captures before A1; the parser independently verifies every condition. Smoke never sets formal runtime pass. The secret scanner redacts matched values and scans the exact Phase 9 source/tests/policies/fixtures/logs/reports/prompts, tracked diffs, and generated artifacts for API_KEY, SECRET, PASSWORD, TOKEN, Authorization, credential URLs, and private account/position/order payload leakage. Complete PHASE_9_IMPLEMENTATION_REPORT.md with measured results only.
- [ ] **Step 4: Run tests to verify pass.** Run the Step 2 command. Expected: PASS.
- [ ] **Step 5: Task verification.** Run: python -m pytest -q tests/quant_phase9/test_phase10_boundary.py tests/quant_phase9/test_final_acceptance_contract.py tests/quant_phase9/test_observability_contract.py. Expected: exit 0, final machine gate and safety contract tests passed.
- [ ] **Step 6: Commit.** Stage the listed files and this plan amendment. The Task 9 code commit precedes formal measurement; after A1–A7, make one report-only commit for the measured report before A8/A9. Commit message: test(phase9): complete deterministic local acceptance.

## Final Acceptance Command IDs and Machine Aggregation

The formal runner records exactly nine command IDs, in this order. It stores bounded, secret-redacted command artifacts at artifacts/phase9/final/commands/A1.json through A9.json and the aggregate at artifacts/phase9/final/result.json. These files are ignored by /artifacts/phase9/. Each command artifact is AcceptanceCommandResultV1 and has a canonical SHA-256 digest excluding artifact_digest. The runner captures real subprocess exit status and parsed test counts; it does not accept a handwritten PASS.

- A1 compatibility gate: `python -m quant_phase9.compatibility_gate --require-pass --json-output artifacts/phase9/final/commands/A1.gate.json`; kind JSON_ARTIFACT, parser `a1_compatibility_gate_v1`, raw artifact path `artifacts/phase9/final/commands/A1.gate.json`.
- A2 Phase 9 focused tests: `python -m pytest -q tests/quant_phase9/ --junitxml=artifacts/phase9/final/commands/A2.junit.xml`; kind PYTEST_JUNIT, parser `a2_pytest_junit_v1`.
- A3 affected regression: `python scripts/run_phase9_test_matrix.py --scope affected --junitxml artifacts/phase9/final/commands/A3.junit.xml`; kind PYTEST_JUNIT, parser `a3_pytest_junit_v1`. The matrix runs the frozen affected paths with Phase 9 and Phase 8 disposable databases separately and merges their unmodified JUnit leaf suites.
- A4 local deterministic full repository regression: `python scripts/run_phase9_test_matrix.py --scope full --junitxml artifacts/phase9/final/commands/A4.junit.xml`; kind PYTEST_JUNIT, parser `a4_pytest_junit_v1`. It includes all non-opt-in tests, routes Phase 8 database tests to quant_phase8_test, and excludes only three explicitly opt-in public exchange contract probe files. Those eight live probes are outside required local acceptance, never counted as passed or silently marked skip.
- A5 deterministic replay: `python scripts/run_phase9_replay_v1.py --manifest tests/fixtures/phase9/manifest.json --require-pass --json-output artifacts/phase9/final/commands/A5.replay.json`; kind JSON_ARTIFACT, parser `a5_replay_v1`.
- A6 secret scan: `python scripts/phase9_secret_scan.py --root . --require-pass --json-output artifacts/phase9/final/commands/A6.secret-scan.json`; kind JSON_ARTIFACT, parser `a6_secret_scan_v1`.
- A7 formal runtime: `python scripts/run_phase9_acceptance.py --mode runtime --duration-seconds 900 --jev fake --stage1-fixture tests/fixtures/phase9/stage1_candidate.json --dsn-env TEST_POSTGRES_DSN --policy-manifest tests/fixtures/phase9/policy_manifest.json --approval-manifest tests/fixtures/phase9/policy_approval.json --result-json artifacts/phase9/final/commands/A7.runtime.json`; kind JSON_ARTIFACT, parser `a7_runtime_acceptance_v1`. This is explicitly fixture-driven local acceptance; the DRAFT production policy cannot be activated by fabricated human approval.
- A8 whitespace validation: `git diff --check`; kind GIT_DIFF_CHECK, parser `a8_git_diff_check_v1`, capture to `A8.capture.json`, pass only on exit 0 and empty stdout/stderr.
- A9 clean worktree verification: `git status --porcelain=v1 --untracked-files=all`; kind GIT_STATUS_CLEAN, parser `a9_git_status_clean_v1`, capture to `A9.capture.json`, pass only on exit 0 and empty stdout.

The acceptance runner captures each exact command, exit code, bounded redacted stdout/stderr, parsed counts/status, and designated raw artifact. A2/A3/A4 each produce JUnit XML; they never infer pass from a summary string or bare process exit. The runner then writes the normalized typed command-result record at `A1.json` through `A9.json`; A8/A9 capture records are separate `.capture.json` raw inputs and cannot collide with the normalized results.

Acceptance runner modes are frozen:
- formal mode captures the current full code SHA before A1, executes and records A1–A7 in order, and stops on a required command failure. After results are written to the ignored artifacts directory, update PHASE_9_IMPLEMENTATION_REPORT.md with measured results and commit that report.
- finalize mode runs A8 and A9 in order after the report commit, loads A1–A9 artifacts, validates schemas, digests, source commits, counts, and report-only ancestry, then emits the aggregate JSON. A9 passes only when its captured status output is empty. This two-step finalization lets the final machine result include both the committed report and a clean worktree.

Required test skips, test failures/errors, missing/invalid artifacts, command nonzero exit, source commit mismatch, runtime duration <900 seconds, or any required gate false forces aggregate passed=false. The required command count is exactly 9. The aggregate reports required_commands_total=9, required_commands_passed, required_tests_passed, required_test_skips, required_tests_failed, required_test_errors, runtime_duration_seconds, all required Phase 9 status flags, PHASE9_REAL_JEV_INTEGRATION_STATUS, PHASE9_READY_FOR_PHASE10, and passed. PHASE9_COMPATIBILITY_CLOSURE_PASS must be true. Real Jev NOT_CONFIGURED is allowed only when no mandatory unresolved HIGH conflict exists.

`PHASE9_IMPLEMENTATION_COMPLETE` has one machine source only: the finalizer parses mandatory C1 and assigns `validated_completion_artifact.passed`; it is never accepted as a caller flag. `PHASE9_READY_FOR_PHASE10` is derived, never caller-supplied: it equals the conjunction of all nine required command results passing, required_commands_passed == required_commands_total == 9, required_test_skips == required_tests_failed == required_test_errors == 0, runtime_duration_seconds >= 900, machine-derived PHASE9_IMPLEMENTATION_COMPLETE, PHASE9_COMPATIBILITY_CLOSURE_PASS, PHASE9_DETERMINISTIC_REPLAY_PASS, PHASE9_FAILURE_SEMANTICS_PASS, PHASE9_PERSISTENCE_AUDIT_PASS, PHASE9_RUNTIME_INTEGRATION_PASS, and the Jev rule (NOT_CONFIGURED only if there is no mandatory unresolved HIGH conflict, or PASS). `Phase9FinalAcceptanceResultV1.passed == PHASE9_READY_FOR_PHASE10` and both must equal that conjunction; mismatch is invalid and fails closed. Smoke can never satisfy the formal runtime term.

`FORMAL_ACCEPTANCE_PYTEST_COMMANDS_USE_JUNIT_XML=true`: only the required A2/A3/A4 formal acceptance pytest commands must emit their exact JUnit XML artifacts; the aggregate consumes parsed JUnit counts, not summary text. Ordinary task RED/GREEN/focused development tests do not require `--junitxml` unless that task explicitly specifies it.

### FROZEN AFFECTED REGRESSION

Run after Tasks 0.3, 0.5–0.8, 6B, 7, and any later change to those production semantics:

    python -m pytest -q tests/test_stage1.py tests/test_repository_integration.py tests/test_pipeline.py tests/test_runtime.py tests/test_phase2_contracts.py tests/test_phase2_normalization.py tests/test_phase2_persistence.py tests/test_phase2_runtime.py tests/test_phase3_contracts.py tests/test_phase3_flow_windows.py tests/test_phase3_persistence.py tests/test_phase4_contracts.py tests/test_phase4_liquidation.py tests/test_phase4_persistence.py tests/test_phase4_runtime.py tests/test_phase5_contracts.py tests/test_phase5_persistence.py tests/test_phase5_runtime.py tests/test_phase6_ai.py tests/test_phase6_contract_v1.py tests/test_phase6_prompts.py tests/test_phase6_runtime_integration.py tests/test_phase7_contracts.py tests/test_phase7_persistence.py tests/test_phase7_runtime_integration.py tests/test_phase8_contracts.py tests/test_phase8_persistence.py tests/test_phase8_runtime_integration.py tests/test_data_layer_replay.py tests/test_data_layer_replay_runner.py --junitxml=artifacts/phase9/final/commands/A3.junit.xml

### Full repository regression

    python -m pytest -q

## Final Self-Review

- Spec coverage: Tasks 0.1–0.9 implement GATE-1 through GATE-4 before feature work; Tasks 1–9 cover policy, evidence, patterns, decisions, persistence, migration-role separation, Engine runtime, Phase 9 replay, and acceptance. Replay, migration, and observability semantics remain unchanged.
- Step scan: all 19 tasks contain checkbox Steps 1–6; every RED/GREEN/verification command and expected result is explicit; every task stages only its Files list.
- Type consistency: evaluation and event identities have one algorithm each; outbox event_id is full SHA-256 text while outbox_events.id stays BIGSERIAL; lease APIs use str event_id and the listed exceptions/results; all later functions refer to Canonical Interfaces.
- Review Focus: the five listed risk classes each map to a named test in its owning task.
- Proportion: plan records decisions and exact tests/interfaces without embedding implementation bodies.
- Mechanical checks: 19 tasks; timeframe is in evaluation UUIDv5 preimage; four suite artifacts and aggregate closure flag are fixed; all requested public signatures, policy envelopes, Phase 6 adapter, numeric Jev limits, result schemas, command IDs, runtime duration, paths, caps, and task formats are specified.
- Final blocker patch matrix: B1 closure anchor/protected-tree digest; B2 admission state machine, lock order, attempts, and ack/retry truth tables; B3 same-connection snapshot read and immutable persistence plus the one final-persistence API; B4 typed safe context/request and actual Phase 6 StrictSchema adapter; B5 fully typed replay fixture graph and hash contract; B6 exact A1–A9 command/parser/JUnit/aggregation contracts; P1 single approved policy loader; P2 exactly one persist_final definition; P3 post-closure-only gate flag wording.
- Mechanical blocker assertions: `COMPATIBILITY_GATE_REQUIRED_BY_POST_CLOSURE_TASKS=true`; exactly one canonical `persist_final` definition; zero untyped Phase9 output-schema declarations; exactly one `build_snapshot` signature; 19 implementation tasks; A2/A3/A4 use JUnit XML; final `passed` equals `PHASE9_READY_FOR_PHASE10` and the frozen conjunction.
- No Phase 9 implementation authorization is implied by this plan. A separate Final Blocker Verification and human approval are required before implementation.

## Final Blocker Verification Contract

The next verification is plan/spec conformance only. It must confirm the B1–B6/P1–P3 items above, the exact 19-task count, no conflicting duplicate signatures, and the explicit status `IMPLEMENTATION_STARTED=false`. This plan revision itself does not run tests, migrations, replay, scanner, runtime, or external services. A passing plan verification only permits a separate human decision; it does not authorize Phase 9 implementation.

## Plan-Only Revision Finalization

This final compliance-fix round modifies only docs/superpowers/plans/2026-09-27-phase9-evidence-chain.md. Do not run tests, migrations, replay, secret scanner, runtime, ECS, Phase 10, push, or PR. Run only git diff --check and git status. Then explicitly stage only the plan and commit:

    git add docs/superpowers/plans/2026-09-27-phase9-evidence-chain.md
    git commit -m "docs(phase9): close final implementation plan blockers"
    git status

Record the full commit SHA and verify a clean worktree. Report plan-level Final Blocker Verification as ready for the separate review. Do not start implementation.
