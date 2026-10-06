# Data Layer V1 Hardening Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing Phase 1–8 data pipeline with observable bounded work, process-local compute admission, cross-process admission only for PostgreSQL transaction starts, deterministic replay, and staged runtime/resource evidence—without changing the established canonical or strategy semantics.

**Architecture:** Preserve the existing Collector + Engine + PostgreSQL topology. Introduce two independent process-local admission controllers (one in Collector, one in Engine) and a PostgreSQL session advisory-lock slot pool only around measured heavy/conflicting transaction classes. Add a shared health/error and A/B/C backpressure contract, then a frozen Phase 1–8 replay and ordered failure/runtime gates. No broker or always-on service is added.

**Tech Stack:** Existing Python application and pytest suite; PostgreSQL 16; Docker Compose; existing Phase 1–8 adapters, repositories, migrations and runtime reports. Any new implementation should use current project conventions and remain compatible with PostgreSQL 16.

**Spec:** `DATA_LAYER_V1_ARCHITECTURE_SPEC.md`, `DATA_LAYER_V1_ACCEPTANCE_SPEC.md`, `DATA_LAYER_V1_BASELINE_AUDIT.md`, `DATA_LAYER_V1_ROOT_CAUSE_ANALYSIS.md`, `DATA_LAYER_HARDENING_BACKLOG.md`.

**Frozen code baseline:** `PHASE8_CODE_BASELINE_SHA=73c157c7fcf259a9b7df3bfc535082afde0af343`.

## Global Constraints

- Execute strictly in Stage 0 → Stage 9 order below. A blocked/failed gate stops progression; do not skip ahead.
- Do not begin any hardening source change until Stage 0 has passed on the frozen Phase 8 baseline `73c157c7fcf259a9b7df3bfc535082afde0af343` with a database-enabled full regression and zero DB-dependent skips.
- Before and after each heavy stage (0, 6, 8, 9), record `df -h /`, `df -i /`, and `docker system df`. If free disk is below 15% before a stage, do not start it; if it crosses below 15% during a stage, stop further large image/DB/replay writes and report `WSL_STORAGE_PRESSURE=true`. Never auto-prune.
- Keep the formal Data Layer acceptance profile unchanged at PostgreSQL 768 MiB, Collector 256 MiB, Engine 384 MiB. Compose is profile-dependent: base development currently sets PostgreSQL 384 MiB, local/Phase 7 acceptance use 768 MiB, and server Compose has no PostgreSQL service. The isolated acceptance profile must explicitly declare 768/256/384; do not silently rewrite or raise the base development profile, convert caps into host reservations, or tune them from idle-only evidence.
- Keep `TRADING_MODE=paper`. No private API, order/position API, live executor, real orders, Phase 9, ECS, production DB, or deployment work.
- Do not add Kafka, Redis, a broker, another always-on service, or a heavy-worker container.
- Do not rewrite migrations 001–015. Add a migration only when a measured requirement cannot use existing bounded health/config contracts; any such migration must be additive, begin at 016, and pass fresh/repeat/upgrade tests.
- Preserve Phase 1–8 event identity, source provenance, UTC event-time semantics, missing-versus-zero, unknown-versus-known, no-future-leakage, cursor/transaction atomicity, Stage 1 eligibility, and Phase 8 context-only behavior.
- Never silently drop canonical/unrecoverable data, advance a cursor past unpersisted data, invent provider observations, or turn overload into a fresh/complete status.
- Keep credentials out of fixtures, manifests, logs, exceptions, health output, and reports. Raw payload capture must be bounded and opt-in only where the existing contract permits it.
- Do not run Docker prune or broad volume/image cleanup. Any disposable DB/artifact cleanup must be exact-ID, owner-tagged, dry-run-first, and limited to this project’s test artifacts.
- Each implementation task follows RED → GREEN → focused regression → review → commit. Do not combine multiple stages into one bulk implementation commit.

## Review Focus

1. **Canonical loss / cursor atomicity:** A/B/C overload behavior, BTC block atomic writes, ETH checkpointing, dedup, restart and reorg boundaries.
2. **Admission correctness:** permit lifetime, transaction ordering, connection/process crash release, starvation/fairness, cancellation, timeout visibility, and no global semaphore=1.
3. **Semantic integrity:** missing is not zero, unknown is not known, source/event/fetch/processed timestamps remain distinct, provenance survives, and derived outputs do not use future data.
4. **Failure classification:** hard safety failures stop unsafe writes; bounded provider errors remain diagnosable and do not unnecessarily stop independent safe work.
5. **Resource and secret safety:** unchanged caps, bounded queues/bytes/logs/artifacts/replay storage, exact cleanup ownership, sanitized diagnostics, and explicit `NOT_EXPOSED` for unknown telemetry.

---

## Task 0 — Stage 0: DL-12 final-SHA PostgreSQL regression (hard gate)

**Purpose:** establish a trustworthy database-enabled test baseline before touching runtime behavior.

**Files / evidence:** `DATA_LAYER_HARDENING_BACKLOG.md`; `PHASE_8_FEATURE_REPORT.md`; `tests/test_migrations.py`; `tests/test_phase8_migrations.py`; `tests/test_phase8_persistence.py`; `tests/test_phase8_replay.py`; `tests/test_phase8_runtime_integration.py`; `DATA_LAYER_V1_STAGE0_REPORT.md`; a run-scoped, ignored evidence directory with a manifest. Add a dedicated isolated DB test harness only if the existing test setup cannot safely provide fresh and upgrade databases; do not start Collector or Engine. Begin with the WSL storage guard and use only an ephemeral/disposable PostgreSQL instance.

**RED / verification:**

- [ ] Confirm the application/test sources correspond to the frozen SHA and record every skipped test by exact test ID and reason.
- [ ] Provision only an isolated PostgreSQL 16 test database with a unique run identity and the existing 768 MiB cap; expose it only to the local test environment. Do not use a production or persistent user database.
- [ ] Test migration 001–015 from empty, repeat 001–015 as a no-op, then exercise the existing Phase 7/Phase 8 upgrade path including an independent 001–014 → 015 upgrade. Assert schema contracts, UTC behavior, and no destructive/repeated migration effect.
- [ ] Run the Phase 8 DB suites with `TEST_POSTGRES_DSN`; fail the gate if absent or if a DB test skips.
- [ ] Run full regression with the same DSN; require zero failures and zero DB-dependent skips. Public live probes may be skipped only when individually identified and not part of this gate.
- [ ] Preserve a manifest containing SHA, PostgreSQL image/tag and actual version, migration state, DSN type only (never DSN value), row-cleanup result, exact commands, counts, skips, failures, and sanitized errors; no DSN/password in the manifest. The database is disposable and its test rows are removed with the isolated instance after evidence is captured.

**Pass condition:** migration and full regression evidence is complete at the frozen application SHA. Stop immediately on migration corruption, canonical identity regression, existing schema upgrade failure, or Phase 8 DB correctness failure. If blocked, stop before Stage 1 and report the exact blocker; do not alter production code to make the gate appear green.

**Commit boundary:** after a real PASS, update the Stage 0 report and close DL-12, then commit the independent evidence checkpoint as `test(data-layer): close final-sha database regression`. Do not commit a PASS if any DB-dependent test skipped. Any required test-harness change is committed separately and reviewed; run artifacts remain ignored and bounded.

## Task 1 — Stage 1: observability before scheduling

**Files:** new `src/quant_data_layer/observability.py` and tests `tests/test_data_layer_observability.py`; minimal integrations in `src/quant_phase1/entrypoints/collector.py`, `src/quant_phase1/entrypoints/engine.py`, and existing phase runtime health registries only where a stable measurement already exists. Prefer reusing current health persistence; do not add a migration unless the spec’s evidence gate requires it.

**Interfaces:** bounded-cardinality source/process/database snapshots with queue count/bytes/oldest age, pending/active, retries, cursor/backfill, freshness, last fetch/process/persist, normalized error, async task count, admission wait, DB transaction class/wait/hold, cgroup/process memory, CPU and resource events. Unknown measurements are `NOT_EXPOSED`, never zero by implication. Stage 1 is measurement-only: do not route work through a scheduler or change task timing, queue policy, or business behavior; capture the pre-admission baseline.

**TDD / verification:**

- [ ] Add failing tests for every required field, explicit unknown state, stable source/class cardinality, UTC timestamps, and secret/raw-payload sanitization.
- [ ] Implement immutable snapshot models and bounded sampling/export; do not append high-frequency queue samples to business tables.
- [ ] Verify Collector and Engine snapshots are process-local and distinguish source lifecycle from data freshness and persistence success.
- [ ] Run focused observability tests, Phase 1 lifecycle tests, Phase 6 runtime non-regression, and full regression with Stage 0’s DB setup.

**Commit boundary:** `feat(data-layer): expose bounded runtime observability`.

## Task 2 — Stage 2: unified health and error contract

**Files:** new `src/quant_data_layer/health.py` and `src/quant_data_layer/errors.py`; tests `tests/test_data_layer_health.py` and `tests/test_data_layer_errors.py`; integrations in the Collector/Engine health registries and `src/quant_phase1/healthcheck.py` as needed.

**Interfaces:** canonical lifecycle/data states and sanitized error categories defined in the Architecture Spec. Keep source configuration, read validity, processing, persistence, cursor, and freshness as distinct facts.

**TDD / verification:**

- [ ] Test legal transitions including `NOT_CONFIGURED`, `INITIALIZING`, `AVAILABLE`, `DEGRADED`, `RATE_LIMITED`, `STALE`, `ERROR`, recovery, and `SHUTTING_DOWN`; reject illegal transitions or require an explicit reason.
- [ ] Test error normalization for provider 429/timeout/contract, network, parser, persistence/checkpoint, backpressure/admission, resource, configuration, shutdown, and unknown failures.
- [ ] Prove exception text, URL credentials, auth headers, API keys, and raw large payloads cannot appear in health/log serialization.
- [ ] Test restart begins at `INITIALIZING` and cannot claim `AVAILABLE` until fresh source and required persistence evidence are restored.
- [ ] Run focused tests and Phase 1–8 health/lifecycle regressions.

**Commit boundary:** `feat(data-layer): unify source health and error states`.

## Task 3 — Stage 3: process-local work admission (Collector and Engine separately)

**Files:** new `src/quant_data_layer/admission.py`; tests `tests/test_data_layer_admission.py`; integration points in `src/quant_phase1/entrypoints/collector.py`, `src/quant_phase1/entrypoints/engine.py`, and phase runtimes under `src/quant_phase1` through `src/quant_phase8` only where heavy/replaceable work is scheduled.

**Interfaces:** separate Collector and Engine controller instances; LIGHT/MEDIUM/HEAVY requests carry phase/source, bounded estimated items/bytes, deadline, replay class, identity, and cancellation ownership. Preserve provider-specific rate/concurrency limiters. Use fair/aged class scheduling and multiple class lanes. Configurable conservative provisional defaults may be used to run tests, but they must be labeled provisional; no numeric value is a final capacity/weight until replay and resource evidence.

**TDD / verification:**

- [ ] Add deterministic tests proving the two processes/controllers do not share ordinary CPU/RPC permits and that work/bytes remain bounded.
- [ ] Test LIGHT health/WS control progresses while HEAVY parsing/recovery is admitted; test weighted progress without indefinite class starvation.
- [ ] Test permit cancellation/release, timeout/deferred outcome, retries sleeping outside permits, shutdown queue cancellation, active-work drain/rollback, and no detached unowned task.
- [ ] Test existing per-provider request-rate and concurrency contracts are unchanged.
- [ ] Route representative Phase 1 recovery, Phase 3/4 event processing, Phase 6 bounded work, Phase 7 block/receipt fanout, Phase 8 reconciliation, and Engine context work through the local controller without changing output semantics.
- [ ] Preserve Phase 3 queue-full semantics; expose gap/drop counters and ensure downstream flow context is `PARTIAL/GAP` when prints are not recoverable, without claiming complete directional flow.
- [ ] Run phase-focused regressions and the full DB-enabled regression.

**Commit boundary:** `feat(data-layer): bound collector and engine local work`.

## Task 4 — Stage 4: cross-process PostgreSQL write admission

**Files:** new `src/quant_data_layer/db_admission.py`; tests `tests/test_data_layer_db_admission.py`; transaction call sites/repositories in Phase 1–8 only for measured heavy/conflicting writes. Keep ordinary CPU/RPC scheduling out of this layer.

**Interfaces:** PostgreSQL session advisory-lock slot pool uses a dedicated admission connection, separate from the normal business transaction connection. Acquire one configured slot with non-blocking `pg_try_advisory_lock` on the idle/autocommit admission session; do not use a transaction-scoped lock to wrap admission waiting. After grant, the business connection begins its transaction and takes a distinct non-blocking per-slot transaction-lifetime fence before any business write. Fence contention causes immediate rollback/defer, never a wait. This fence prevents overlapping work if the admission session disappears while business work remains active. Commit/rollback the business transaction, explicitly unlock the admission slot in `finally`; PostgreSQL session close is the crash-release fallback. No permit across provider calls/backoff. No all-work semaphore=1. Numeric lane/slot counts are frozen only after Stage 6 replay evidence.

**TDD / verification:**

- [ ] First write two-process failing tests for slot contention, mixed-class progress, timeout reporting, and visibility of pending/active transaction class.
- [ ] Test independent admission/business connections, idle/autocommit lock acquisition, acquire-before-business-transaction ordering, success/failure rollback, explicit release, connection loss/process death release, slot reuse, bounded waiters, cancellation, and shutdown.
- [ ] Inject admission-session loss while a business transaction remains active; prove a competing writer cannot overlap because the non-blocking transaction-lifetime fence remains held. Verify this fence is never used to wait for admission.
- [ ] Test no DB availability means fail-closed deferred writes and absolutely no cursor advancement or admission bypass.
- [ ] Test lock order cannot deadlock and persistent eligible classes make progress; if repeatable starvation or slot leakage occurs, stop and request architecture review rather than silently switching to a lease table.
- [ ] Inject Bitcoin chunk-N and checkpoint failures and assert all data plus cursor roll back atomically; test Ethereum checkpoint atomicity remains unchanged.
- [ ] Run two actual app processes against an isolated PostgreSQL test instance, then focused and full DB-enabled regressions.

**Commit boundary:** `feat(data-layer): coordinate heavy postgres transaction starts`.

## Task 5 — Stage 5: unified A/B/C backpressure and overload behavior

**Files:** new `src/quant_data_layer/backpressure.py` and phase-stream registry; tests `tests/test_data_layer_backpressure.py`; bounded integration updates in Phase 1–8 producers/queues/recovery loops.

**Interfaces:** every persisted/derived stream declares `A CANONICAL_UNRECOVERABLE`, `B RECOVERABLE_REPLAYABLE`, or `C DERIVED_REPLACEABLE`; unknown replayability defaults conservatively to A. A never silently drops; B resumes only from durable cursor/window; C may coalesce/skip only with visible age/status/reason. Transport buffers that cannot be measured stay `NOT_EXPOSED`.

**TDD / verification:**

- [x] Create a table-driven test inventory covering every Phase 1–8 stream and assert it has exactly one classification and explicit overload behavior.
- [x] Saturate each class: prove A gap/completeness reporting, B cursor-safe recovery/dedup, C freshness/status/skipped-cycle behavior, and all queues/bytes remain bounded.
- [x] Prove missing values remain missing and no overload path invents zero, freshness, or successful completion.
- [x] Run targeted Phase 1 Kline, Phase 3 trade, Phase 4 liquidation, Phase 6 ingestion, Phase 7 cursor, Phase 8 sparse-state/reseed tests, plus full DB-enabled regression.

**Commit boundary:** `feat(data-layer): make overload behavior explicit across phases`.

## Task 6 — Stage 6: `DATA_LAYER_REPLAY_V1` fixture and deterministic replay

**Files:** new `src/quant_data_layer/replay/` manifest, loader and comparator; tests `tests/test_data_layer_replay.py`; versioned, compressed, hash-manifested fixtures under `tests/fixtures/data_layer_replay_v1/`; replay documentation/report template. Add a new additive migration (016+) only if a measured test/replay contract cannot fit existing schema; otherwise none.

**Interfaces:** manifest freezes Git SHA, app/PostgreSQL image digests, migration version, config hash without secrets, dataset hash, seed/version, UTC source/event/fetch/processed times, identity/provenance/status and explicit missing values. Cover Phase 1–8 inputs and their canonical, context, Stage 1-relevant, checkpoint and Phase 8 outputs as listed in the Architecture Spec. Synthetic fixture data is labeled fixture evidence, never live evidence.

**TDD / verification:**

- [x] Test manifest validation, hash mismatch rejection, secret omission, bounded decompression/record counts, explicit null/missing semantics, and deterministic seed handling.
- [x] Run the frozen fixture three times in fresh disposable schemas/volumes; compare table counts, canonical identity digests, cursors/checkpoints, quality states and derived outputs exactly.
- [x] Include >10,000-event Bitcoin block chunking, Ethereum block/log/receipt, Phase 3/4 event identity, Phase 6 not-configured AI path, and Phase 8 sparse ticker merge/options contexts with missing data.
- [x] Test future-event injection is rejected and no Stage 1 or Phase 8 semantic/eligibility output changes due to replay instrumentation.
- [x] Derive candidate work weights, admission slots, dataset/artifact bounds and sustained resource thresholds from these measured artifacts; record values and rationale before later runtime gates. If replay is not representative, do not freeze numeric settings.
- [x] Run the full DB-enabled regression after all replay runs.

**Commit boundary:** `test(data-layer): add deterministic phase one through eight replay` (or `feat(...)` only if a replay runner is required). Commit fixtures and manifests only after size budget/hash/sanitization checks.

## Task 7 — Stage 7: failure matrix, restart, dedup, finality and reorg

**Files:** `tests/test_data_layer_failure_matrix.py`; targeted phase tests for Bitcoin/Ethereum reorg/checkpoint, WS reconnect/gap, and Phase 8 stale reseed; `DATA_LAYER_V1_ACCEPTANCE_SPEC.md` only for reviewed clarifications; a bounded failure-matrix runner/report under `scripts/` if existing test tools cannot express the scenarios.

**TDD / verification:**

- [x] Add deterministic cases for provider timeout, 429/Retry-After, WS reconnect/gap, parser/contract failure, transient DB outage, transaction/checkpoint failure, queue/admission saturation, shutdown during heavy work, Phase 6 provider-not-configured, and Phase 8 stale WS/reseed.
- [x] Verify Bitcoin/Ethereum in-window reorg reconciliation and beyond-window fail-closed behavior with bounded query work; preserve existing source contracts.
- [x] Execute at least three controlled run/stop/restart cycles in isolated test containers. Each checks migration idempotency, reconnect/resubscribe, durable cursor restore, dedup, queue age reset, health recovery, DB consistency, and no cursor regression.
- [x] Verify Bitcoin and Ethereum finality/reorg rules independently: within-window reconciliation is correct; beyond-window lookup fails closed without an unbounded history scan.
- [x] Label OOM, secret/data corruption, atomicity/identity/cursor faults, and lost cancellation ownership as hard stops. Keep bounded provider errors and temporary stale/degraded state as soft diagnostics that do not stop independent safe work.
- [x] Run failure-matrix tests, replay again, and full DB-enabled regression.

**Commit boundary:** `test(data-layer): cover restart and failure recovery matrix`.

## Task 8 — Stage 8: 15-minute integrated public-source acceptance

**Files:** isolated acceptance Compose configuration `docker-compose.data-layer-acceptance.yml` (or a narrowly reviewed extension of existing acceptance Compose); `scripts/data_layer_acceptance.py`; tests `tests/test_data_layer_acceptance_runner.py`; `DATA_LAYER_V1_ACCEPTANCE_SPEC.md`; generated ignored run evidence. Do not use `docker-compose.local.yml`'s persistent user database volume.

**TDD / verification:**

- [ ] Test fixed overall deadline, bounded sample/response/log artifact size, safe interrupt/shutdown, no credentials in outputs, and failure-matrix row generation.
- [ ] Verify acceptance topology keeps paper mode and the exact formal PostgreSQL 768 MiB / Collector 256 MiB / Engine 384 MiB caps; has bounded log rotation for every service. Base/server currently specify `json-file` 10 MiB × 3; local and Phase 7 acceptance variants currently omit per-service rotation and must be covered or superseded by this isolated compose file. Do not alter the base development PostgreSQL 384 MiB profile as an incidental harmonization.
- [ ] Verify the isolated project uses unique named resources and loopback-only test access where a host port is needed; do not contact ECS or any private/order endpoint.
- [ ] Run only after Stages 0–7 pass: 15-minute Phase 1–8 public/read-only integrated run, recording every 10s or faster source, queue/admission, DB, cgroup/process, CPU, storage, log and artifact measure.
- [ ] Continue safe independent diagnostics through bounded 429/timeouts/stale sources; stop unsafe writes only on hard-safety or predeclared resource stop conditions. Emit PASS/FAIL/SKIPPED/NOT_EXPOSED for every source/gate and a FAILURE MATRIX.
- [ ] Verify clean shutdown, health recovery and no data/cursor/atomicity issue; run full regression after the acceptance runner changes.

**Commit boundary:** `feat(data-layer): add bounded local runtime acceptance harness`; runtime evidence remains ignored and is summarized only in the later completion report.

## Task 9 — Stage 9: 60-minute soak and formal resource/storage acceptance

**Files:** `DATA_LAYER_V1_HARDENING_REPORT.md`; acceptance thresholds/config in the isolated harness; `docker-compose.local.yml` and `docker-compose.phase7-acceptance.yml` only for reviewed log-rotation/resource-contract harmonization; Phase 3 stale resource helper only as described below. Do not change memory caps.

**TDD / verification:**

- [ ] Before launch, freeze sustained warning/stop thresholds and disk/replay/log/artifact budgets from Stage 6 and Stage 8 measurements; require at least 15% disk free headroom.
- [ ] Remove `src/quant_phase3/resources.py::assess_resource_usage` if still unused, or explicitly deprecate it. Add a regression proving the isolated formal acceptance profile pins 768/256/384, cannot reintroduce the stale acceptance threshold, and does not silently rewrite the base development 384 MiB setting. No Phase 3 business semantics change.
- [ ] Verify each Compose variant used for acceptance bounds logs; actual Docker daemon/container logging settings are recorded, not assumed. Add exact manifest ownership and dry-run-first cleanup tests for disposable volumes/artifacts; never run Docker prune or broad cleanup.
- [ ] Run the full Phase 1–8 60-minute local isolated soak under unchanged caps; collect per-service `memory.current/peak`, anon/file, `memory.events`, task/RSS/PSS, CPU/restarts, queue and admission measures, DB connections/transactions/WAL/checkpoint/table/index growth, disk/inodes, Docker volume/log/artifact sizes.
- [ ] Require zero OOM/oom-kill, no new `memory.events:max` pressure beyond the frozen acceptance rule, no cursor/data/atomicity/silent-loss fault, clean shutdown, all stage gates pass, and all unknown signals marked `NOT_EXPOSED`.
- [ ] Clearly separate measured interval growth from any labeled `PROJECTED_ESTIMATE`; never claim unobserved 30/90/180/365-day growth as measured.
- [ ] Run final full DB-enabled regression and secret scan; verify branch/HEAD/worktree and include every skip/blocker in the completion report.

The final report `DATA_LAYER_V1_HARDENING_REPORT.md` must record `PHASE8_CODE_BASELINE_SHA`, `DATA_LAYER_V1_DESIGN_SHA`, `FINAL_HARDENING_SHA`, Stage 0–9 status, test/skip matrix, database and migration results, observability/health/admission/backpressure evidence, replay hashes, failure matrix, three restart results, finality/reorg, 15-minute and 60-minute gates, Collector/Engine/PostgreSQL resources, storage, secret audit, known limits, and remaining debt.

**Commit boundary:** `docs(data-layer): record v1 hardening acceptance` only when every Gate 0–9 has real evidence and all tests pass. If any gate is blocked, do not commit a false completion state and do not begin Phase 9.

## Final stop condition

Stop after Stage 9 and issue `DATA_LAYER_V1_ACCEPTED=true` only if every acceptance gate passes. Otherwise issue `DATA_LAYER_V1_ACCEPTED=false`, list exact failed/blocked gates and evidence, preserve safe artifacts, and wait for direction. In either case, do not begin Phase 9, deployment, ECS changes, or trading functionality.
