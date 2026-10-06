# Quant Paper V1 Runtime Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the documented pre-24H blockers by assembling the existing REAL_PUBLIC_DATA → Phase9 → Risk → durable Nautilus Local Paper path, with policy-driven TTL, explicit readiness, and PostgreSQL restart/reconciliation proof.

**Architecture:** Keep `quant_phase1` as the Stage1 producer, its durable Phase9 outbox and `Phase9EngineRuntime` as the evaluator, and `quant_realtime_paper` as the sole monitor/execution CLI. Add a default assembly that reads the approved policy/build revision, consumes only durable Phase9 outputs, calls the existing Risk/ExecutionStore contracts, and wraps the existing Nautilus LocalPaper journal/Sandbox rather than introducing a matching engine. Missing or ambiguous policy, TTL, candidate provenance, account state, quote, adapter, or reconciliation state remains a reason-coded no-execution outcome.

**Tech Stack:** Python 3.12, pytest, PostgreSQL/psycopg, Pydantic V2, existing Phase1/Phase9/Risk/Execution/Nautilus LocalPaper APIs.

**Spec:** User attachment `a6e229c8-d2c0-4360-9270-d7eca62d486d/已粘贴的文本.txt`; baseline context in `docs/STRATEGY_GAPS.md`.

## Global Constraints

- Continue only on `fix/rc24h-phase9-runtime-wiring`, starting at `2e4fad91fa7c4ab63bb8dd6127e453683795cb55`.
- Do not alter baseline RC `63711da00dc61bdf16c051e2698a9dfe809ed60a` or the validation worktree.
- Do not run A1–A9, formal RC acceptance, formal RC Docker build, or 24H.
- Do not create a production approval or select enabled patterns.
- `TRADING_MODE=paper`, `PAPER_ONLY=true`, `LIVE_ALLOWED=false`; no private exchange API or Live fallback.
- Do not infer LONG/SHORT from Stage1 A/B or price direction; do not synthesize candidates or guess thresholds/TTL.
- Keep market-data freshness rules, EvaluationSnapshot/source validity, Stage1 candidate intake TTL, Phase9 DecisionCandidate TTL, and ExecutionIntent TTL distinct; an absent required configured TTL means no execution.
- Database tests use only a new disposable loopback test PostgreSQL; prove `CANONICAL_DATABASE_USED=false` before mutations.
- Do not push, merge, tag, delete worktrees, or clean Docker history resources.

## Review Focus

- Stage1 category A/B with no candidate TTL must defer with `NO_TRADE_BY_MISSING_TTL`; test the outbox intake and monitor path.
- Asynchronous Phase9 completion, stale generations, or multiple timeframes must not pair a Stage1 row with another candidate; test exact durable identity and policy generation.
- Build revision missing/mismatched with approval must fail closed before Risk; test default assembly and CLI startup.
- Every crash point around reserve, submit, result, snapshot, and reconciliation must recover/query before retry; add PostgreSQL restart tests for each stated crash window.
- No policy pattern/TTL or incomplete data must never reach Risk/adapter; test distinct readiness flags and reason codes.

---

### Task 1: Define Paper V1 policy draft and explicit runtime readiness

**Files:**
- Create: `docs/phase9/PAPER_V1_POLICY_DRAFT.md`
- Modify: `src/quant_realtime_paper/gates.py`
- Modify: `src/quant_realtime_paper/runtime.py`
- Test: `tests/quant_realtime_paper/test_readiness.py`
- Test: `tests/quant_realtime_paper/test_runtime.py`

**Interfaces:**
- Produce `PaperV1ReadinessV1` with separate `process_ready`, `data_ready`, `stage1_ready`, `phase9_ready`, `policy_ready`, `risk_ready`, `paper_ready`, `reconciliation_ready`, `execution_ready`, `blockers`, and one of the requested `NO_TRADE_BY_*` outcomes.
- Draft each allowed pattern/timeframe with LONG/SHORT eligibility, evidence, freshness, coverage, vetoes, confidence, TTL, revalidation, material changes, and degradation columns. Mark every unsupported value `NEEDS_STRATEGY_OWNER_DECISION`.

- [ ] **Step 1: Write tests** for independent readiness flags and the reason mapping for disabled policy, missing TTL, stale data, Risk rejection, Paper unavailable, and unhealthy reconciliation.
- [ ] **Step 2: Run** `pytest tests/quant_realtime_paper/test_readiness.py tests/quant_realtime_paper/test_runtime.py -q`; verify new cases fail first.
- [ ] **Step 3: Implement** the structured readiness model without changing freshness thresholds or enabling a policy.
- [ ] **Step 4: Write the policy draft** for `TREND_CONTINUATION` (1H/4H), `BREAKOUT_CONFIRMATION` (15m/1H), `LIQUIDATION_REVERSAL` (15m/1H); mark ungrounded parameters for owner decision.
- [ ] **Step 5: Re-run** the focused tests; verify no readiness condition collapses into an undifferentiated boolean.

### Task 2: Policy-derived Stage1 intake expiry and durable Phase9 bridge

**Files:**
- Modify: `src/quant_phase1/service.py`
- Modify: `src/quant_phase1/entrypoints/engine.py`
- Modify: `src/quant_phase9/intake.py` only if needed to preserve its existing schema and defer semantics
- Create: `src/quant_realtime_paper/phase9_bridge.py`
- Test: `tests/quant_phase9/test_stage1_outbox_gate.py`
- Test: `tests/quant_realtime_paper/test_phase9_bridge.py`

**Interfaces:**
- Add a separately typed `stage1_candidate_ttl_seconds` policy value and resolver returning `candidate_created_at + TTL`; keep it distinct from pattern `TTLRuleV1` (DecisionCandidate validity) and `RiskPolicyV1.intent_ttl_seconds` (ExecutionIntent validity). Return `None` and preserve `INTAKE_TTL_NOT_CONFIGURED` when the reviewed Stage1 intake TTL is absent.
- Add a durable reader keyed by Stage1 candidate ID, current policy generation, symbol, timeframe, and build revision; return typed `EvaluationSnapshotV1` and `DecisionCandidateV1` only when all identities match and the Phase9 evaluation is complete and active. Require distinct source-freshness, snapshot/evidence validity, pattern decision TTL, and execution TTL checks.
- Never assign direction from Stage1 category or market movement.

- [ ] **Step 1: Write tests** for policy-derived candidate expiry, absent TTL deferral, exact candidate/evaluation identity, stale revision, pending evaluation, and no direction synthesis.
- [ ] **Step 2: Run** the focused bridge/outbox tests and verify failure before implementation.
- [ ] **Step 3: Implement** the resolver at the Phase1→Phase9 boundary using the dedicated configured Stage1 intake TTL only; keep Phase1 calculation and market freshness semantics unchanged. Keep the existing pattern TTL for DecisionCandidate validity and the existing RiskPolicy TTL for ExecutionIntent validity, and report missing values with their own reason codes.
- [ ] **Step 4: Implement** the durable Phase9 projection reader using existing persistence tables/contracts, never an injectable fake as the default.
- [ ] **Step 5: Re-run** bridge tests and relevant Phase9 tests.

### Task 3: Default runtime assembly and explicit CLI readiness

**Files:**
- Create: `src/quant_realtime_paper/assembly.py`
- Modify: `src/quant_realtime_paper/cli.py`
- Modify: `src/quant_realtime_paper/runtime.py`
- Modify: `src/quant_realtime_paper/config.py`
- Test: `tests/quant_realtime_paper/test_runtime_assembly.py`
- Test: `tests/quant_realtime_paper/test_runtime.py`

**Interfaces:**
- `build_default_runtime(config, environ) -> RealtimePaperMonitor` loads the policy approval with `expected_commit` equal to `QUANT_BUILD_REVISION`, validates Risk config and the strict paper lock, constructs the durable Phase9 bridge, execution store, and Local Paper adapter, and refuses startup/marks readiness blocked on missing inputs.
- `quant-realtime-paper run/start` use this assembly by default; tests may inject dependencies, but production CLI must not require an undocumented injection point.

- [ ] **Step 1: Write tests** proving default assembly is invoked by `run`, approval/build mismatch and missing approval block it, and no fallback to fake/fixture runtime occurs.
- [ ] **Step 2: Run** the assembly/CLI tests and verify they fail before implementation.
- [ ] **Step 3: Implement** the assembly against existing Phase1 repository, Phase9 durable reader, `RiskPolicyV1`, `ExecutionStore`, and Nautilus Local Paper APIs.
- [ ] **Step 4: Test** all paper-lock combinations and reason-coded startup failures.
- [ ] **Step 5: Re-run** CLI and runtime unit tests.

### Task 4: Existing Nautilus Local Paper execution adapter and recovery

**Files:**
- Create: `src/quant_nautilus/realtime_paper_adapter.py`
- Modify: `src/quant_nautilus/paper.py` only to accept an exact canonical real quote through existing Nautilus `QuoteTick` flow
- Modify: `src/quant_execution/persistence.py` only where durable status transitions/reconciliation require it
- Test: `tests/quant_nautilus/test_realtime_paper_adapter.py`
- Test: `tests/quant_execution/test_persistence.py`
- Test: `tests/quant_nautilus/test_paper_restart.py`

**Interfaces:**
- Implement the existing `ExecutionAdapter` protocol over the current `LocalPaper`/`SandboxSession`; use real bid/ask/size from canonical `REAL_PUBLIC_DATA`, never a fabricated quote or second matching engine.
- Keep `ExecutionStore.reserve`, owner fencing, durable submit state, result persistence, position persistence, and query-before-retry as the single execution ledger.
- Check durable account/position reconciliation before reserving each new order; unhealthy or unknown native state prevents execution.

- [ ] **Step 1: Add PostgreSQL tests** for all five crash windows: reserved intent, submitted without result, result without snapshot, snapshot persisted, and reconciled restart.
- [ ] **Step 2: Run** against the disposable DB and verify no same intent can submit twice.
- [ ] **Step 3: Implement** the adapter bridge using the pinned current Nautilus adapter/kernel and durable local journal; no Live/exchange client.
- [ ] **Step 4: Re-run** adapter, persistence, idempotency, and restart tests.

### Task 5: End-to-end readiness observability and dashboard/session persistence

**Files:**
- Modify: `src/quant_realtime_paper/runtime.py`
- Modify: `src/quant_realtime_paper/store.py`
- Modify: `src/quant_realtime_paper/acceptance.py`
- Test: `tests/quant_realtime_paper/test_runtime.py`
- Test: `tests/quant_realtime_paper/test_acceptance.py`

**Interfaces:**
- Persist each stage's correlation ID, UTC timestamp, status, and reason code without candidate sizing in `DecisionCandidateV1` and without CoT.
- Dashboard/status payload exposes the nine readiness flags and one precise no-trade classification; strategy no-trade remains distinct from policy-disabled, risk-rejected, stale-data, missing-TTL, and system-not-ready.

- [ ] **Step 1: Write tests** for all requested reason classes, event correlation/timestamp persistence, and Dashboard-visible readiness.
- [ ] **Step 2: Run** focused tests and verify the current generic `DO NOT TRADE` representation fails these assertions.
- [ ] **Step 3: Implement** status persistence and dashboard/readiness projection from the runtime's structured state.
- [ ] **Step 4: Re-run** runtime and acceptance-reporting unit tests (not formal RC acceptance).

### Task 6: Disposable loopback PostgreSQL integration verification

**Files:**
- Create: `tests/quant_realtime_paper/test_real_public_paper_db_integration.py`
- Modify: `tests/conftest.py` or a dedicated fixture module only for isolated test DB provisioning
- Test: Phase9 DB suites, execution persistence, Paper persistence, restart/reconciliation/idempotency suites

**Interfaces:**
- The test fixture provisions a fresh database named `quant_phase9_test` on loopback only, exports `TEST_POSTGRES_DSN`, verifies the connected database is not canonical, applies only the test schema/migrations, and destroys only that fixture after tests.

- [ ] **Step 1: Add fixture guard tests** that reject non-loopback hosts, canonical database names, and reused databases.
- [ ] **Step 2: Start** the disposable PostgreSQL and prove `SHOW TIME ZONE` is exactly `UTC`, `CANONICAL_DATABASE_USED=false`, and `TEST_POSTGRES_DSN` points only to the disposable DB.
- [ ] **Step 3: Run** Phase9 integration, execution persistence, Paper adapter, crash recovery, reconciliation, and idempotency tests; do not run A1–A9 or formal RC acceptance.
- [ ] **Step 4: Stop and remove** only the disposable test DB resource created by this task after capturing test evidence; preserve all pre-existing Docker resources.

### Task 7: Integrated verification and local branch completion

**Files:**
- All files listed above; no baseline worktree files

- [ ] **Step 1: Run** focused unit tests, all relevant Phase9/Risk/Nautilus/realtime tests, and the DB integration matrix.
- [ ] **Step 2: Inspect** `git diff --check`, exact changed paths, policy digest, branch/head, and the untouched baseline worktree.
- [ ] **Step 3: Confirm** no production approval, A1–A9, RC Docker acceptance, Live, or 24H session was created or started.
- [ ] **Step 4: Commit** the completed work locally on `fix/rc24h-phase9-runtime-wiring`; do not push, merge, or tag.
- [ ] **Step 5: Report** every completion flag and every remaining blocker; stop before formal validation.
