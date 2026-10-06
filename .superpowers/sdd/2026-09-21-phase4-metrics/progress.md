# SDD ledger — plan: docs/superpowers/plans/2026-09-21-phase4-metrics.md

## Setup

- Workspace: `.superpowers/sdd/2026-09-21-phase4-metrics`
- Branch: `phase4`
- Base: `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`
- Plan commit before execution: `0e63dcc`
- Spec commit: `e417053`
- Current HEAD before Task 1: `808293b`

## Preflight shared-file/interface scan

| Shared surface | Tasks | Check | Ruling |
|---|---|---|---|
| `src/quant_phase4/contracts.py` | 1, 3, 4, 5, 6, 7 | Tasks 3–7 consume Task 1 enums/dataclasses; no conflicting field names found. | Task 1 owns primitive contracts only. |
| `migrations/009_phase4_metrics.sql` | 2, 6, 7 | Task 2 owns schema; Task 6 maps repository SQL to it; Task 7 uses its enrichment key. | No table rewrite or prior migration change. |
| `src/quant_phase1/config.py` | 2, 3, 4, 5, 8, 9 | Settings are added once and consumed by adapters/runtime/tests. | Preserve Phase 1–3 defaults and paper guard. |
| `src/quant_phase4/adapters/base.py` | 3, 4, 5 | Basis, Long/Short, and Liquidation adapters share parser/transport helpers. | Transport is shared; version-specific fields remain adapter-local. |
| `src/quant_phase4/adapters/bybit_v5.py` | 3, 4, 5 | Bybit basis, ratio, and liquidation parsers share exchange envelope validation. | Keep metric parsers separate and preserve source semantics. |
| `src/quant_phase4/persistence.py` | 6, 7, 8 | Task 6 exposes repository methods; Tasks 7–8 call only those methods. | No direct SQL in cross-exchange or runtime modules. |
| Stage1 integration | 7, 8, 9 | Task 7 creates context/enrichment; Task 8 loads/persists it; Task 9 verifies preservation. | Phase 4 remains context-only. |
| `tests/contract/test_phase4_public_live.py` | 3, 4, 5, 9 | Tasks add source-specific probes to one gated live test file. | Live failures never trigger fallback. |

## Per-task self-consistency scan

| Task | Tests versus implementation | Files versus later touches | Result |
|---|---|---|---|
| 1 | Contract tests exercise the dataclasses and enums created by the task. | Creates only primitive contracts and package boundary. | Clean after moving derived context types to Task 7. |
| 2 | Config/migration tests cover defaults, constraints, timestamps, and idempotency. | Creates migration and settings consumed by Tasks 3–9. | Clean. |
| 3 | Basis tests cover formula, skew, zero reference, and parser fields. | Creates base plus basis adapters consumed by Tasks 4–5 and runtime. | Clean. |
| 4 | Ratio tests cover v2/v5 semantics, periods, and comparability. | Adds Long/Short adapters consumed by persistence and runtime. | Clean. |
| 5 | Liquidation tests cover source semantics, units, dedup, gaps, queue, and rollups. | Creates aggregation types consumed by Tasks 6–8. | Clean. |
| 6 | Repository tests cover idempotency, payload stripping, UTC, and retention. | Creates persistence consumed by Tasks 7–8. | Clean. |
| 7 | Context tests cover coverage isolation and candidate preservation. | Creates context/enrichment consumed by Tasks 8–9. | Clean. |
| 8 | Runtime tests cover lifecycle, boundedness, outage, and recovery. | Modifies existing entrypoints; Task 9 validates safety/regression. | Clean. |
| 9 | Safety/report/full-suite checks cover final code-completion gate. | Only final report/config/test artifacts. | Clean. |

## Rulings

- Ruling: move `LiquidationWindow`, `CrossExchangePhase4Context`, `Phase4Context`, and `Stage1Phase4Enrichment` ownership out of Task 1 — the plan's implementation order requires derived types to be defined beside their aggregation/enrichment behavior; the cost if wrong is interface churn between Tasks 1 and 7.

## Task status

- Task 1: pending
- Task 2: pending
- Task 3: pending
- Task 4: pending
- Task 5: pending
- Task 6: pending
- Task 7: pending
- Task 8: pending
- Task 9: pending

Task 1: fix round 1/5 addressed — `_require_positive` now rejects non-finite values; focused contract tests cover NaN and infinity; scoped re-review clean; commits `d8ef0f3..6d2bb46`.
Task 1: complete (commits `d8ef0f3..6d2bb46`, review clean).

Task 2: fix round 1/5 started — reviewer found unbounded Phase 4 retention/cadence values and silent invalid boolean parsing.
Ruling: cap Phase 4 event retention at 168 hours, all Phase 4 retention days at 365, timestamp skew at 3600 seconds, and REST cycle at 1–3600 seconds; reject unrecognized `PHASE4_ENABLED` values — these bounds preserve the required defaults while preventing accidental database growth or request storms; the cost if wrong is a later configuration migration if operational needs exceed these deliberate low-resource limits.
Task 2: fix round 1/5 addressed — bounds and strict boolean parsing added with tests; scoped re-review clean; commits `a61a845..c447164`.
Task 2: complete (commits `a61a845..c447164`, review clean; PostgreSQL runtime remains required because `TEST_POSTGRES_DSN` is absent).
Task 3: fix round 1/5 started — reviewer found adapter timestamp validation silently normalizes invalid inputs and Hyperliquid basis parser does not validate mark/oracle numeric fields.
Task 3: fix round 1/5 addressed — UTC entry validation and finite positive Hyperliquid mark/oracle validation added; scoped re-review clean; commits `dc0a163..aafa010`.
Task 3: complete (commits `dc0a163..aafa010`, review clean; live contract remains opt-in).
Task 4: fix round 1/5 started — reviewer found missing per-observation population semantics and fractional epoch milliseconds silently truncated by the shared parser.
Task 4: fix round 1/5 addressed — canonical population semantics are now required and compared per observation; Bitget and Bybit semantics remain distinct; fractional epoch milliseconds are rejected; scoped re-review clean; commits `c006a3e..453f407`.
Task 4: complete (commits `c006a3e..453f407`, review clean; live API probes remain opt-in and runtime acceptance is pending).
Task 5: started — liquidation adapters, strict units/coverage, stable deduplication, bounded queue/gap state, and deterministic timeframe rollups; brief at `task-5-brief.md`.
Task 5: complete (commit `c832b5a`, review clean; focused 22 passed/3 skipped, full 369 passed/10 skipped; no live API calls or deployment).
Task 6: started — PostgreSQL repository, idempotent Phase 4 persistence, bounded latest reads, and configured retention; brief at `task-6-brief.md`.
Task 6: fix round 1/5 started — reviewer found latest context reads were not scoped by metric type/period/basis type, allowing MARK_INDEX and MARK_ORACLE rows to mask each other.
Task 6: fix round 1/5 addressed — bounded latest reads now independently scope long/short by symbol+metric type+period and basis by symbol+basis type; regression tests added; scoped re-review clean; commits `2a5067f..c47e300`.
Task 6: complete (commits `2a5067f..c47e300`, review clean; PostgreSQL runtime remains required because `TEST_POSTGRES_DSN` is absent).
Task 7: started — coverage-aware cross-exchange context and Stage1 context-only enrichment; brief at `task-7-brief.md`.
Task 7: fix round 1/5 started — reviewer found liquidation comparability dropped event timestamps, ignored quantity units, and collapsed stale/reason status distinctions.
Task 7: fix round 1/5 addressed — liquidation compatibility now includes event timestamp/window and quantity unit; STALE/ERROR/NOT_AVAILABLE and reason codes are preserved; focused re-review clean; commits `ffa6ef8..d163c1d`.
Task 7: complete (commits `ffa6ef8..d163c1d`, review clean; focused 12 passed, full 386 passed/11 skipped).
Task 8: started — component health, bounded Phase 4 runtime, and explicit opt-in collector/engine integration; brief at `task-8-brief.md`.
Task 8: fix round 1/5 started — reviewer found liquidation WebSocket not actually started, REST adapters bypassing shared rate limiting/session lifecycle, unbounded window deduplication, stale recovery gap, and unbounded symbol materialization.
Task 8: fix round 1/5 addressed — public liquidation transport, shared REST session/limiter, bounded dedup/symbol handling, and stale transition tests added; scoped re-review found repository injection and stale-recovery correctness gaps; commits `57d8f5d..a4d9c66`.
Task 8: fix round 2/5 started — reviewer found collector did not inject Phase4Repository, leaving observations/windows memory-only, and stale REST responses were incorrectly promoted to RECOVERED.
Task 8: fix round 2/5 addressed — fresh repository provider injection and AVAILABLE-only recovery are implemented and independently reviewed clean; changes remain uncommitted because linked-worktree Git metadata is read-only. Default local regression: `398 passed, 6 failed, 11 skipped`; all 6 failures are network-blocked existing Bitget live probes.
Task 8: complete (commits `57d8f5d..666aa1e`, review clean; Task 8 + Phase 3 re-verification 26 passed; final commit `666aa1e`).
Task 9: started — safety/resource assertions, final local regression evidence, and `PHASE_4_COMPLETION_REPORT.md`; no deployment or runtime acceptance.
Task 9: fix round 1/5 started — reviewer found inaccurate plan-history metadata, incomplete private/trading route scan, incomplete destructive SQL detection, and superficial bounded-resource assertions.
Task 5: complete (commits `453f407..c832b5a`, tests: focused 22 passed/3 skipped; full 369 passed/10 skipped; diff check clean; live probes and PostgreSQL checks remain opt-in/runtime-required).

Task 6: complete (commit `2a5067f`; tests: focused 8 passed/1 skipped; full 373 passed/11 skipped; diff check clean; PostgreSQL remains `RUNTIME_ACCEPTANCE_REQUIRED` without `TEST_POSTGRES_DSN`).

Task 8: complete (commit `57d8f5d`; focused Task 8 plus Phase 3 regression 18 passed; full 396 passed/11 skipped; diff check clean; live probes, PostgreSQL acceptance, and Jakarta runtime remain pending).

Task 9: complete (commit `495267c`; focused 7 passed; related Phase 1–3 regressions 38 passed; final full local suite 411 passed/11 skipped; opt-in combined public probes 11 passed/6 known network failures; diff check clean; Jakarta runtime pending).
Final review: self-review of committed Task 9 diff; only the requested test/report artifacts changed, migrations 001–008 and `.env.example` unchanged.
Final whole-branch review: CHANGES REQUESTED — critical persistence stubs and important runtime/recovery gaps identified; fix brief at `final-review-fix-brief.md`.
Final-review fix round 1/5 started — implement complete Phase 4 persistence, event/retention scheduling, population semantics storage, loaded-context engine integration, and gap recovery correctness before re-review.
Final-review fix round 1/5 addressed — commit `7d28bcf` added persistence/upserts, event/retention scheduling, population semantics, engine context integration, and gap gating; re-review found NULL idempotency, loaded liquidation reconstruction, migration upgrade, provenance, and report-gate issues.
Final-review fix round 2/5 started — resolve all P1/P2 findings from the scoped re-review before any completion claim.

Final-review fix round 3/5 addressed — made fresh migration 009 cross-exchange
timeframes `NOT NULL DEFAULT ''`; added recorded-009 reconciliation that
retains the greatest `id` per duplicate legacy `NULL` identity, normalizes the
survivor, and enforces the repaired key; kept persistence normalization and
all Phase 1–3 migration files unchanged.
Commit: `f123c409cf1517d3e8a4a07fd27f128f6be27904`
Test evidence: focused Phase 4 modules/public-probe gate `190 passed, 4 skipped`;
Phase 1–3 regression selection `221 passed, 2 skipped`; full local suite
`420 passed, 11 skipped`; `git diff --check` clean. PostgreSQL runtime acceptance,
live exchange probes, and Jakarta runtime remain gated because
`TEST_POSTGRES_DSN` and live opt-ins are unavailable; no external database or
deployment was used.

Final-review fix round 4/5 addressed — recorded-009 upgrade reconciliation now
partitions by `COALESCE(timeframe, '')` across the full legacy table, retaining
the greatest `id` for mixed `NULL`/blank identities before safely normalizing
survivors; non-empty legacy timeframe values remain distinct. Added structural
coverage and PostgreSQL-gated mixed-state coverage. Migrations 001–008 and the
fresh 009 null-safe schema remain unchanged. Runtime acceptance, live probes,
and Jakarta remain pending; no external database or deployment was used.

Final-review fix round 5/5 addressed — canonical liquidation events now remain
in bounded recovery state across repository outages; finalized windows are
acknowledged only after successful writes; disconnects mark builder gaps;
reconnect recovery requires a fresh accepted/persisted event; runtime persists
5m/15m/1H/4H rollups; both liquidation exchanges share bounded global event and
approximate-byte budgets; and persisted reason provenance restores recognized
`ReasonCode` values plus reason text. Added failure-path, rollup, dual-exchange,
global-budget, and provenance tests. Added the minimal `tests/__init__.py`
marker so the full suite resolves its existing sibling test import
deterministically.

Evidence: focused deterministic Phase 4 `199 passed, 1 skipped`; Phase 1–3
regression selection `221 passed, 2 skipped`; full local suite `429 passed, 11
skipped`; `git diff --check` clean. Gated skips are live public probes and
PostgreSQL tests without opt-ins/`TEST_POSTGRES_DSN`. Runtime acceptance remains
pending; no Jakarta connection, deployment, external migration, or Phase 5 work
was performed. Required commit: `phase4: close runtime integration gaps`.

New final-review cycle — implementation round 1 addressed all five whole-branch
findings on branch `phase4` from HEAD `6479387026950246c0f192031f1ba41005035a3d`:

- Planned refresh/shutdown worker cancellation is distinguished from genuine
  transport disconnects; only genuine disconnects create liquidation gaps and
  degraded recovery health.
- Recent persisted liquidation-window status/gap metadata is loaded alongside
  events, converted to bounded normalized context observations, and covered
  through repository, context, and engine enrichment tests.
- Phase 4 startup hydrates recent persisted 1-minute windows into a new runtime's
  builders and rollup history; the restart test uses a distinct runtime object.
- Global bounded accounting now includes queue/recovery events, compact builder
  state, finalized identities, persisted keys, and rollup windows. Raw payloads
  are not retained in aggregation state; builder/finalized/rollup/hydration
  limits are configurable and tested for both exchanges with a 200-symbol
  universe.
- Recorded-009 repair is marked once in `schema_migrations`, preventing the
  legacy reconciliation hot path from repeating on ordinary repository
  connections while preserving fresh and old-009 behavior.

Verified evidence for this round:

- Focused Phase 4 suite: `204 passed, 4 skipped`.
- Phase 1–3 regression selection: `230 passed, 7 skipped`.
- Full local suite: `434 passed, 11 skipped`.
- `git diff --check`: clean.

Skips remain gated public exchange probes and PostgreSQL tests without
`TEST_POSTGRES_DSN`; runtime/Jakarta acceptance remains pending. No external
database, Jakarta connection, deployment, migration, service restart, or Phase
5 work was performed. Required commit: `phase4: harden runtime lifecycle and
bounded recovery`.

New final-review cycle — implementation round 2 addresses the remaining
recovery, restart-hydration, and global builder-budget findings without
entering runtime acceptance or Phase 5:

- Added per-exchange receipt/source-time recovery watermarks so a pre-gap
  database retry remains degraded; recovery requires a newer accepted,
  durably persisted event.
- Startup hydration now rebuilds and persists missing closed 5m, 15m, 1H, and
  4H rollups from bounded persisted 1m windows, with durable rollup-key
  suppression across newly-created runtime instances and no raw payloads.
- Gap insertion now observes the global builder/window budget while retaining
  queue gap evidence and degraded component health under bounded admission.
- Added isolated regression coverage for all three findings.

Evidence for this round:

- Focused Phase 4: `207 passed, 4 skipped`.
- Phase 1–3 regression selection: `221 passed, 2 skipped`.
- Full local suite: `437 passed, 11 skipped`.
- `git diff --check` clean.

Runtime/Jakarta acceptance remains pending; no external database, deployment,
migration, service restart, or Phase 5 work was performed. Required commit:
`phase4: harden recovery watermarks and restart hydration`.

Final-review fix round 3/5 addressed the remaining migration hot-path and
large-restart idempotency findings without entering runtime acceptance or
Phase 5:

- Phase 4 migration/recorded-009 repair is now an explicit one-time collector
  startup action; event/window repository scopes no longer run migrations or
  acquire the migration advisory lock.
- Restart rollups use durable unique-key upsert in bounded startup batches;
  the bounded in-memory key cache is only an optimization and cannot create
  retry-queue backpressure when hydrated identities exceed queue capacity.
- Hydrated rollup grouping is indexed once per timeframe to keep the
  dual-exchange/default-200/240-minute restart deterministic and bounded.
- Added migration-scope and deterministic 27,600-rollup scale coverage.

Evidence: focused Phase 4 `209 passed, 1 skipped`; Phase 1–3 regression
selection `221 passed, 2 skipped`; full local suite `439 passed, 11 skipped`;
`git diff --check` clean. Runtime/Jakarta acceptance remains pending; no
external database, deployment, migration, service restart, or Phase 5 work
was performed. Required commit:
`phase4: remove migration hot path and scale restart idempotency`.

Follow-up final-review implementation fix — both P1 findings are addressed
without deployment, external migration, Jakarta access, or Phase 5 work:

- Window persistence uses separate bounded retry classes for 1m sources and
  derived rollups. Source minutes are acknowledged before rollups are
  generated; failed rollup identities remain bounded rebuild work backed by
  retained normalized 1m rows, and retry-capacity overflow is explicit health
  degradation rather than a silent drop.
- Hydration records a bounded per-stream durable closed-minute high-watermark.
  Late events at or below that watermark are rejected before persistence when
  their finalized identity has been compacted, while newer minutes continue
  through normal aggregation. The scale regression covers both exchanges,
  200 symbols, 240 minutes, finalized-key eviction, and a late duplicate.
- Added deterministic tiny-budget persistence and scale hydration integrity
  assertions; no raw payload was added to long-lived state.

Evidence for this fix: focused Phase 4 `210 passed, 1 skipped`; Phase 1–3
regression selection `221 passed, 2 skipped`; full local suite `440 passed,
11 skipped`; `git diff --check` clean. Runtime/Jakarta acceptance remains
pending; no external database, deployment, migration, service restart, or
Phase 5 work was performed. Required commit:
`phase4: make rollups lossless and hydration safe`.

Final-review fix round 6 — global rollup rebuild recovery is now bounded and
cursor-progressing without entering runtime acceptance or Phase 5:

- Global fallback retains the advanced retry range returned by
  `_retry_one_rollup_marker`, plus durable exchange/symbol/timeframe cursor
  semantics and the overall rebuild range needed when moving to the next
  timeframe or source scope.
- Durable source-scope discovery uses bounded keyset pagination and the
  runtime consumes one scope at a time; all retained 1-minute scopes are not
  materialized in memory.
- Added deterministic coverage for multiple scopes, multiple bounded chunks,
  timeframe transitions, complete rollup rebuild, and bounded scope pages.
- `PHASE_4_COMPLETION_REPORT.md` was reduced to this verified marker/rebuild
  evidence and keeps runtime/Jakarta acceptance explicitly pending.

Verification evidence: focused Phase 4 `214 passed, 1 skipped`; Phase 1–3
regression selection `221 passed, 2 skipped`; full local suite `444 passed,
11 skipped`; `git diff --check` clean. Runtime/Jakarta acceptance remains
pending; no external database, deployment, migration, service restart, or
Phase 5 work was performed. Required commit:
`phase4: make global rebuild recovery bounded`.
