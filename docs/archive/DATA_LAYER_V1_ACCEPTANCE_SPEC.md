# Data Layer V1 Hardening Acceptance Specification

**Baseline:** `phase8` / `73c157c7fcf259a9b7df3bfc535082afde0af343`
**Purpose:** define ordered evidence gates for Phase 1–8 as one Data Layer.
**Execution status:** the current user instruction authorizes implementation, but each gate remains conditional on the preceding gate; no skipped/failed gate authorizes progression.

## Acceptance principles

- A check is `PASS`, `FAIL`, `SKIPPED` or `NOT_EXPOSED`; missing instrumentation is never inferred to be zero.
- A Phase-local feature pass is not a Data Layer acceptance pass.
- Run the full diagnostic matrix through soft source failures; stop unsafe writes only for a hard safety failure or the predeclared resource stop gate.
- Keep measured samples and projected estimates separate. Do not project 30/90/180/365-day growth from a short sample without labeling it as a projection.
- Keep current caps: PostgreSQL 768MiB, Collector 256MiB, Engine 384MiB. No increase is part of this acceptance.
- Compose caps are profile-dependent: base `docker-compose.yml` uses PostgreSQL 384MiB, local and Phase 7 acceptance use 768MiB, and server Compose has no PostgreSQL service. Gates 0 and 8–9 must use an isolated acceptance topology that explicitly pins PostgreSQL 768MiB / Collector 256MiB / Engine 384MiB; do not silently change the base development profile.
- All runtime acceptance is local/isolated, public/read-only, `TRADING_MODE=paper`; no ECS or production database is used.

## Ordered gates

### Gate 0 — final-SHA database regression (DL-12)

Run this before any hardening source-code change, against a disposable PostgreSQL instance and exact frozen Phase 8 SHA.

1. Verify branch/SHA/clean baseline and store an evidence manifest.
2. Fresh migration from empty database through 001–015; validate required tables/indexes/constraints and UTC behavior.
3. Repeat migration 001–015; assert no schema/data damage and no new migrations.
4. Upgrade path: apply 001–014, then 015; compare schema contract with fresh migration.
5. Run Phase 8 migration, persistence and replay DB suites with `TEST_POSTGRES_DSN`; expect zero skips and zero failures in DB-dependent tests.
6. Run full regression with `TEST_POSTGRES_DSN`; zero failures and zero DB-dependent skips. Live/opt-in public probes may remain intentionally skipped only when listed individually and not required by this gate.
7. Report test counts and all skips by exact test name/category; do not claim a prior pre-refinement DB run as final-SHA evidence.
8. Record PostgreSQL image/tag and actual version, migration version, DSN type only (not its value), disposable-row cleanup result, exact tests, skips, and failures. Remove only the uniquely owned disposable test database/container after the evidence is captured; no user or production rows are touched.

**Fail closed:** absent Docker/DSN, migration mismatch, DB-dependent skip, or schema error means Gate 0 is `BLOCKED/FAIL`; it is not a pass and no Stage 1–9 runtime gate starts.

### Gate 1 — observability contract

Use deterministic source fixtures to assert per-source metrics for queue depth/bytes/age, pending/active work, retries, cursor/backfill, freshness, last fetch/persist, sanitized error category, and per-process async task/admission counts. Assert DB pending/active transaction class and wait/hold durations. Every unavailable measurement is explicitly `NOT_EXPOSED`. Confirm bounded cardinality and no secrets, authenticated URLs, headers or large raw payloads.

### Gate 2 — unified health/error contract

Unit and integration tests exercise `NOT_CONFIGURED → INITIALIZING → AVAILABLE → DEGRADED → RATE_LIMITED → STALE/ERROR → recovery → SHUTTING_DOWN`. Verify source state is distinct from runtime lifecycle, fetch from persistence, cursor from freshness, and provider configuration from data availability. Verify sanitized error taxonomy and that cursor progress cannot be reported as unavailable without an explicit reason/state boundary.

### Gate 3 — process-local admission

Run deterministic mixed LIGHT/MEDIUM/HEAVY work in Collector and Engine separately. Verify:

- queue count and bytes never exceed configured bounds;
- LIGHT receive/health remains schedulable while HEAVY work is active;
- HEAVY work obeys measured class capacity; weights/aging prevent indefinite class starvation under the tested schedule;
- provider-specific rate/concurrency tests still pass independently;
- cancellation releases permits; retries/backoff hold no permits; shutdown drains or safely defers work;
- no numeric capacity is accepted unless resource replay and the current caps pass.

### Gate 4 — cross-process DB admission

Run at least one Collector and one Engine process against an isolated PostgreSQL instance. Verify two or more tested admission lanes/classes can progress; no all-work semaphore=1 is introduced. For advisory-lock implementation, use a dedicated idle/autocommit admission connection and a separate normal business connection. Assert the session slot is acquired before business `BEGIN`; admission waiting is bounded/cancellable and uses no transaction-scoped lock. The business transaction obtains a non-blocking per-slot transaction-lifetime fence before its first write; fence contention immediately rolls back/defer rather than waiting. Test explicit release, automatic release on connection/process death, injected admission-connection loss while a business transaction remains active (no overlapping writer), bounded timeout, slot reuse, cancellation, shutdown and mixed-class progress. Assert a database outage causes fail-closed deferred writes and no cursor advancement. Run Bitcoin chunk-N failure and checkpoint failure to prove the entire block transaction still rolls back.

**Fail:** lock leak, unbounded waiter connections, repeated starvation, timeout not visible, deadlock, transaction work before permit, or any atomicity/cursor regression. If advisory-lock fairness fails, return to architecture review; do not silently replace it with a second mechanism.

### Gate 5 — unified backpressure

For each Phase 1–8 stream, verify declared A/B/C class and overload behavior:

- A canonical/unrecoverable input never silently drops; if transport loss is unavoidable, explicit gap and non-available completeness are recorded.
- B recoverable input defers/pauses and resumes from last durable cursor/window without cursor skip or duplicate canonical identity.
- C replaceable output may coalesce/skip; status, freshness age and skip reason change and missing values remain missing.
- Transport/library buffers not measurable remain `NOT_EXPOSED`.

### Gate 6 — DATA_LAYER_REPLAY_V1

Freeze Git SHA, app and PostgreSQL image digests, migration version, dataset/config hashes and deterministic seed. Run three fresh disposable-schema/volume replays. Compare table row counts, canonical identity digests, cursor/checkpoint state, Stage 1-relevant outputs, context/aggregation/Options outputs and data-quality statuses. Results must be deterministic; duplicate identities, future leakage, unknown-to-zero conversion, or incomplete provenance fails the gate. Report fixture-derived results as fixture evidence, never live data.

### Gate 7 — failure matrix, restart, dedup, finality and reorg

The deterministic matrix includes provider timeout, 429/Retry-After, WS reconnect and gap, parser/contract failure, DB transient failure, DB transaction failure, checkpoint failure, queue saturation, heavy admission saturation, shutdown during heavy work, Bitcoin chunk N failure, Ethereum/Bitcoin in-window reorg, beyond-window reorg, Phase 8 stale WS/reseed, and Phase 6 provider-not-configured behavior.

Run at least three controlled run/stop/restart cycles. Each cycle validates migration idempotency, resubscription/reconnection, cursor restore, dedup, queue recovery/age reset, health recovery, DB consistency and no cursor regression. In-window reorgs reconcile; beyond-window conditions fail closed without unbounded history scans. Every scenario records expected/actual, source, error category, cursor and persistence result.

### Gate 8 — 15-minute integrated real-source gate

Only after Gates 0–7 pass. Run Phase 1–8 in an isolated local Compose project with public/read-only inputs, paper mode and the existing caps. Sample every 10 seconds or faster using bounded artifact output. Record all source health, cursor/freshness, queue/admission, DB transaction, PostgreSQL/cgroup, process memory/CPU, Docker logs, table/index growth and disk free space.
Direct public HTTPS/WSS egress is required: reject proxy environment variables, Docker client proxy configuration, Docker daemon proxies, and proxy variables in the Compose model. Record only the preflight state; never persist or print proxy values.

Soft 429, temporary provider errors, a single stale source or temporary `DEGRADED` do not stop the diagnostic run; continue independent sources until the fixed deadline and produce a FAILURE MATRIX. A failed source is not called accepted. On hard safety fault or resource stop threshold, stop unsafe ingestion and preserve the evidence gathered so far.

### Gate 9 — 60-minute soak and formal resource acceptance

Only after Gates 0–8 pass and thresholds/budgets are frozen before launch. Run the complete Phase 1–8 workload for 60 minutes with no cap changes. Record the same metrics and a clean shutdown. Resource disposition must distinguish:

- Collector/Engine/PostgreSQL `memory.current`, `memory.peak`, anon/file and `memory.events`;
- process tasks/RSS/PSS, CPU and restart/OOM counters;
- queue age/depth/bytes, active/pending work and admission wait;
- DB connections, active transactions/class, WAL/checkpoint data, transaction/write latency, database/table/index size and dead tuples;
- disk free percent/inodes, Docker volume/images, bounded logs/artifacts.

No OOM/oom-kill, cursor/data/atomicity failure, or silent loss is allowed. The predeclared sustained-memory and `memory.events:max` thresholds must pass under unchanged caps. If `memory.events:max` grows or a service crosses its frozen sustained warning/stop threshold, resource acceptance fails; classify the condition and preserve evidence instead of raising a cap. Disk free must remain at least 15%; stop before crossing that boundary. Long-term growth is measured only for the observed interval; any extrapolation is labeled `PROJECTED_ESTIMATE`.

## Hard stops and soft failures

| Class | Examples | Action |
|---|---|---|
| Hard safety stop | OOM/oom-kill; secret leakage; corrupted canonical data; duplicate canonical identity; cursor skip; Bitcoin/Ethereum transaction semantic violation; unrecoverable DB corruption; loss of cancellation ownership | Stop unsafe writers/runtime, preserve logs/metrics/manifest, mark acceptance failed. Do not resume automatically. |
| Resource stop gate | Sustained resource threshold exceeded, repeated cgroup max events, disk below 15%, or DB checkpoint/write pressure beyond the frozen threshold | Stop new heavy admissions first; collect safe monitor/health evidence; stop runtime if continued load risks data/host integrity; mark resource gate failed. |
| Soft diagnostic failure | 429, bounded timeout/retry exhaustion, single source stale/degraded, temporary admission timeout or provider outage | Keep the fixed deadline and continue independent sources. Record a row in FAILURE MATRIX; affected source may fail its source gate without aborting safe diagnostics. |

## Final result requirements

`DATA_LAYER_V1_ACCEPTED=true` may be reported only when every Gate 0–9 passes, all expected regression tests pass, no prohibited skip is hidden, three restarts and deterministic replay pass, all source/backpressure classes are observable or explicitly `NOT_EXPOSED`, security audit passes, resource/storage policy passes, and clean shutdown is confirmed. Any failed/blocked gate means `DATA_LAYER_V1_ACCEPTED=false` with exact blockers and no Phase 9 progression.

The final report includes frozen identities, gate results, exact tests and skips, FAILURE MATRIX, per-source progress, resource samples, measured DB/disk/log/artifact growth, cleanup ownership, secret audit, working-tree/commit state, and known limits. No ECS/production deployment is part of this acceptance.
