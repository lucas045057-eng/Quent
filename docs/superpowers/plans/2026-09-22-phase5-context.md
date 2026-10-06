# Phase 5 Context Implementation Plan

> **Execution mode:** SUBAGENT-DRIVEN, test-first, local WSL2 only.

## Goal

Implement the approved Phase 5 deterministic context layer on top of the existing Phase 1–4 canonical data model. The implementation must remain paper-only, public-data-only, additive, restart-safe, and context-only with respect to Stage1.

## Fixed baseline and boundaries

- Repository: `/home/lucas045057/projects/quant`
- Branch: `phase5`
- Base: `b3dd3741ae7719e1eaa469929db8e676c1a0a62e`
- Design commit: `5e8aca03ba32a25a5498472c4665b66ed23f0ee2`
- Phase 4 remains frozen at `PHASE4_EXTERNAL_SOURCE_GATE_PENDING`.
- Bybit 403 remains `EXCHANGE_ACCESS_LIMITATION`; do not modify the Phase 4 Bybit adapter.
- `TRADING_MODE=paper`; no private API, order, position, live executor, or real order path.
- No remote ECS, Jakarta, Hangzhou, Singapore, remote migration, or formal runtime-observation window.
- No Phase 6 work and no new permanent service/container.
- Migrations 001–009 are immutable; add only migration 010.

## Dependency graph

```text
Task 0 plan review
    -> Task 1 contracts/config boundaries
    -> Task 2 migration 010 + persistence primitives
    -> Task 3 closed-bar input/context calculations
    -> Task 4 breadth
    -> Task 5 regime
    -> Task 6 relative strength
    -> Task 7 sector taxonomy
    -> Task 8 sector context
    -> Task 9 Stage1 enrichment
    -> Task 10 retention/reliability
    -> Task 11 health/recovery/entrypoint wiring
    -> Task 12 full regression, safety/resource review, completion report
```

Tasks 3–9 are intentionally serial because they share canonical status, timestamp, calculation-version, and persistence contracts. Independent review is required after each task’s focused tests pass and before its commit. A reviewer may inspect and report only; the implementer applies any fixes and reruns tests.

## Global task protocol

Every task follows:

```text
TEST -> FAIL -> IMPLEMENT -> PASS -> INDEPENDENT REVIEW -> FIX -> RETEST -> COMMIT
```

For each task:

1. Write the smallest failing tests first.
2. Run focused tests and record the expected failure.
3. Implement only the approved design surface.
4. Run focused tests plus the relevant Phase 1–4 regression subset.
5. Spawn one independent read-only review of the task diff and contract.
6. Fix review findings without expanding scope.
7. Retest, run `git diff --check`, inspect safety-sensitive imports/routes, and commit.

Do not run multiple write agents against dependent files. The main agent owns integration and commit boundaries.

## Task 1 — Canonical contracts and configuration

**Depends on:** Task 0

**Files:**

- Add `src/quant_phase5/__init__.py`
- Add `src/quant_phase5/contracts.py`
- Add `src/quant_phase5/status.py` if needed to keep Phase 5 derived status separate from Phase 1–4 source status
- Modify `src/quant_phase1/config.py` only for Phase 5 names/defaults
- Modify `.env.example` with paper-safe, non-secret settings
- Add `tests/test_phase5_contracts.py`
- Add `tests/test_phase5_config.py`

**Contract requirements:**

- UTC-aware timestamps only;
- `AVAILABLE`, `PARTIAL`, `STALE`, `NOT_AVAILABLE`, `ERROR` for Phase 5 derived outputs;
- explicit source status mapping and precedence;
- missing is not zero and stale is not available;
- canonical leader, breadth, regime, RS, sector membership, sector context, and Stage1 enrichment types;
- no BUY/SELL/LONG/SHORT vocabulary;
- Decimal-compatible percentage/ratio fields, bounded evidence/reference objects, calculation version, and reason codes;
- reuse existing Phase 1–4 canonical contracts rather than duplicate raw source fields.

**Focused tests:** contract field/NULL rules, UTC, status precedence, missing/stale/error behavior, enum vocabulary, config validation, and paper-only safety.

## Task 2 — Migration 010 and persistence primitives

**Depends on:** Task 1

**Files:**

- Add `migrations/010_phase5_context.sql`
- Add `src/quant_phase5/persistence.py`
- Modify `src/quant_phase1/db.py` only to add Phase 5 migration schema introspection/transaction validation while preserving the existing runner behavior
- Add `tests/test_phase5_migrations.py`
- Add `tests/test_phase5_persistence.py`

**Schema:**

Create only the approved additive tables:

- `phase5_market_leader_context`
- `phase5_market_regime_snapshots`
- `phase5_relative_strength_snapshots`
- `phase5_sector_membership`
- `phase5_sector_context_snapshots`
- `stage1_phase5_context_enrichment`

Enforce the design’s UTC `TIMESTAMPTZ`, checks, foreign keys, natural/replay identities, partial/expression unique indexes, Stage1 composite FK, and retention indexes. Do not add a raw-kline warehouse. Do not change 001–009.

**Migration validation:**

- empty database: 001–010 applies and records 010 once;
- existing 001–009 database: 010 applies once;
- repeat: `0 new migrations`;
- schema introspection detects a same-named incompatible object and rolls back without recording 010;
- partial-object and transaction-failure tests leave no destructive side effects.

The migration-runner tests cover the existing runner plus the new 010 schema contract. The runner must not record 010 when introspection fails and must not alter an incompatible same-named object.

**Commit boundary:** migration and repository tests pass; commit separately from calculation code.

## Task 3 — BTC/ETH closed-bar context

**Depends on:** Tasks 1–2

**Files:**

- Add `src/quant_phase5/market_context.py`
- Add `tests/test_phase5_market_context.py`

Consume existing canonical closed klines, volume, indicators, and market structure. Do not implement a collector or exchange adapter.

Implement for BTCUSDT and ETHUSDT at 5m/15m/1H/4H:

- bar-close `context_timestamp = bar_open_timestamp + interval duration`;
- interval-aware freshness and configured ingestion grace;
- return, EMA/slope trend, existing structure mapping, realized volatility, volume ratio/state;
- timestamp alignment, minimum windows, finite numeric validation, quality/source/missing evidence;
- no open candles, future bars, or cross-timeframe timestamp fabrication.

**Tests:** deterministic formulas, exact threshold boundaries, closed/open bar rejection, stale propagation, timestamp skew, missing windows, BTC/ETH resolution, and persistence idempotency.

## Task 4 — Market breadth

**Depends on:** Task 3

**Files:**

- Add `src/quant_phase5/breadth.py`
- Add `tests/test_phase5_breadth.py`

Use one immutable `universe_run_id` and its members for every breadth calculation. Persist `sample_size`, `available_count`, `missing_count`, `coverage_ratio`, ratios, evidence, and status.

Implement the approved neutral band and broad/narrow strength/weakness table. Missing members remain missing. Coverage below minimum is `NOT_AVAILABLE`; coverage meeting minimum with missing members is `PARTIAL`, never falsely `AVAILABLE`.

**Tests:** fixed universe snapshots, missing-not-zero, coverage boundaries, neutral band, broad/narrow boundaries, snapshot identity, and survivorship mismatch rejection.

## Task 5 — Market regime

**Depends on:** Task 4

**Files:**

- Add `src/quant_phase5/regime.py`
- Modify the Phase 5 contract only if needed for dimension status fields
- Add `tests/test_phase5_regime.py`

Persist direction, volatility, and breadth labels separately, plus `direction_status`, `volatility_status`, `breadth_status`, and support/conflict/missing evidence. Apply the approved full decision table and status precedence. Do not implement a scalar score, vote score, LLM, or trading action.

**Tests:** both leaders available, one leader missing, both missing, breadth missing/partial, narrow strength/weakness, range/mixed conflicts, stale/error precedence, and evidence completeness.

## Task 6 — Centralized relative strength

**Depends on:** Tasks 3–5

**Files:**

- Add `src/quant_phase5/relative_strength.py`
- Add `tests/test_phase5_relative_strength.py`

Expose one centralized formula:

```text
relative_return_pct = candidate_return_pct - benchmark_return_pct
```

Support BTC, ETH, and candidate-excluded equal-weight market benchmark at 15m/1H/4H only. Enforce same timeframe, exact aligned context timestamp, closed/fresh inputs, benchmark sample/coverage minimums, universe replay identity, and configuration thresholds. Do not duplicate formula logic in sector or Stage1 modules.

**Tests:** BTC/ETH self-comparison unavailable, candidate exclusion, benchmark missing/partial/stale, threshold boundaries, 5m non-persistence, and repeated calculations.

## Task 7 — Static sector taxonomy

**Depends on:** Task 1 and Task 2

**Files:**

- Add version-controlled `config/phase5_sector_map.csv`
- Add `src/quant_phase5/sector_taxonomy.py`
- Add `tests/test_phase5_sector_taxonomy.py`

Load only deterministic version-controlled mappings. Validate required columns, taxonomy version, symbol references, `UNKNOWN` fallback, effective interval ordering, and overlap rejection. No runtime network, AI, symbol-name guessing, or forced classification.

**Tests:** known mapping, unknown mapping, version changes, invalid rows, interval overlap, idempotent load, and Stage1 eligibility independence.

## Task 8 — Sector context

**Depends on:** Tasks 4, 6, and 7

**Files:**

- Add `src/quant_phase5/sector_context.py`
- Extend `tests/test_phase5_sector_context.py`

Compute sector return and breadth only from mapped members with the selected universe/timeframe. Enforce minimum sector members and coverage. Persist aggregate sector rows without candidate-specific fields; compute candidate-vs-sector relation in the Stage1 enrichment contract. Use `UNKNOWN` and `NOT_AVAILABLE/PARTIAL` exactly as designed.

**Tests:** sector arithmetic, member/missing counts, minimum thresholds, unknown mapping, candidate relation, timestamp identity, and no single-token sector inference.

## Task 9 — Stage1 Phase5 context-only enrichment

**Depends on:** Tasks 3–8

**Files:**

- Add `src/quant_phase5/enrichment.py`
- Add `tests/test_phase5_enrichment.py`

Write only `(screening_run_id, symbol)` rows that reference an existing screening result via the composite FK. Compare complete Stage1 results with Phase 5 disabled/enabled and prove equality for category, reason, reason codes, inputs, indicators, structure, status, and identity.

Scenarios must include missing BTC, missing ETH, unavailable RS, unknown sector, unavailable regime, stale input, and partial breadth. None may delete or demote an existing candidate.

## Task 10 — Retention, bounded memory, and reliability

**Depends on:** Tasks 2–9

**Files:**

- Add `src/quant_phase5/retention.py`
- Add `src/quant_phase5/reliability.py` if needed
- Add `tests/test_phase5_retention.py`
- Add `tests/test_phase5_reliability.py`

Implement configured per-timeframe retention and 30-day enrichment retention with indexed, bounded batches. Do not delete parent screening history. Use bounded windows, bounded caches, bounded evidence, batch reads/writes, finite retry/backoff, and no whole-history load.

**Tests:** boundary timestamps, cleanup idempotency, retention indexes, duplicate cycles, bounded retry, PostgreSQL unavailable/recovered, stale recovery, and no duplicate burst after recovery.

## Task 11 — Health, restart, and local integration wiring

**Depends on:** Tasks 9–10

**Files:**

- Add `src/quant_phase5/health.py`
- Add `src/quant_phase5/runtime.py`
- Modify only `src/quant_phase1/entrypoints/engine.py` for the Phase 5 integration point and existing config wiring; do not add a service
- Modify `docker-compose.local.yml` to the fixed acceptance limits: PostgreSQL 768 MiB, `quant-collector` 256 MiB, and `quant-engine` 384 MiB. Do not raise these limits or add a container.
- Add `tests/test_phase5_runtime.py`
- Add `tests/test_phase5_safety_resources.py`

Wire the context cycle into the existing engine process. Rebuild bounded loaded context after restart from canonical PostgreSQL rows. Preserve source statuses, avoid infinite queue/retry, and publish health transitions. Keep collector unchanged except for configuration surfaces if strictly required.

**Tests:** restart recovery, loaded-context rebuild, database outage/recovery, stale propagation, health heartbeat, exact Compose memory ceilings, measured container memory under those ceilings, service count, paper-only/public-only scan, Phase 4 semantics, and no Phase 6 imports.

## Task 12 — Whole-branch review and completion gate

**Depends on:** all implementation tasks

**Files:**

- Add `PHASE_5_COMPLETION_REPORT.md`
- Add/adjust only Phase 5 tests/docs required by review findings

Run:

- focused Phase 5 tests;
- Phase 1–4 regression suites;
- PostgreSQL integration tests;
- deterministic, safety, and resource tests;
- `git diff --check`;
- final independent whole-branch review from base `b3dd3741...` to final HEAD.

The final review must classify Critical/Important/Minor findings and mark Ready to merge only with Critical=0 and unresolved Important=0. Verify no Phase 4 adapter changes, no private/order/live path, no remote activity, no new service, and no migration edits before 010. Existing live-probe skips caused by the known external network limitation must remain explicitly separated from deterministic code regressions and must not be converted to false failures or hidden skips.

The completion report must include implementation plan commit, migration 010, all Phase 5 components, tests, resource constraints, known limitations, Phase 4 Bybit external debt, final HEAD, and working-tree status.

## Commit boundaries

1. Task 0: plan only.
2. Task 1: contracts/config.
3. Task 2: migration/persistence.
4. Task 3: leader context.
5. Task 4: breadth.
6. Task 5: regime.
7. Task 6: relative strength.
8. Task 7: taxonomy.
9. Task 8: sector context.
10. Task 9: Stage1 enrichment.
11. Task 10: retention/reliability.
12. Task 11: health/runtime wiring.
13. Task 12: completion report and any review fixes.

Every commit must leave the working tree clean and record focused test results. Do not squash or rewrite the Phase 4 base.

## Stop condition

Only after all tasks, reviews, regression, integration, safety/resource tests, completion report, and clean working tree are complete may the branch report:

```text
PHASE5_CODE_COMPLETE_RUNTIME_PENDING
```

Stop immediately at that state. Do not begin formal Phase 5 runtime acceptance or Phase 6.
