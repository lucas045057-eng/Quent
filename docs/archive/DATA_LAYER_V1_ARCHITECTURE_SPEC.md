# Data Layer V1 Hardening Architecture Specification

**Baseline:** `phase8` / `73c157c7fcf259a9b7df3bfc535082afde0af343`
**Architecture direction:** user-approved Option A — preserve Collector + Engine + PostgreSQL; no broker, new always-on service, or heavy-worker container.
**Status:** implementation approved by the user's current instruction; execute Stages 0–9 in order and stop before Phase 9.

## 1. Goals and non-goals

### Goals

- Bound aggregate Phase 1–8 processing without replacing source/provider limits.
- Coordinate only conflicting/heavy PostgreSQL transaction starts across Collector and Engine.
- Preserve canonical data, cursor, dedup, provenance, event-time, Stage 1 and Phase 8 contracts.
- Make queue pressure, admission wait, database wait, freshness and source lifecycle visible before tuning concurrency.
- Provide a deterministic Phase 1–8 replay and a staged failure/resource acceptance path.
- Bound Docker logs, runtime evidence, replay artifacts and disposable PostgreSQL storage without automatic Docker pruning.

### Non-goals

- No Phase 9, AI decision layer, risk/execution/order capability, private exchange API, wallet signing or live trading.
- No Kafka, Redis Streams, RabbitMQ, Kubernetes, new always-on service or heavy-worker container.
- No Phase 1–8 semantic changes, migration rewrite, retention deletion enablement, cap increase, or production deployment in the design stage.
- No cross-process scheduler for every CPU/RPC/parse task. Process-local compute admission and cross-process DB admission are separate layers.

## 2. Topology and admission boundary

```text
quant-collector process                         quant-engine process
  source-local provider limits                    source-local provider limits
  process-local LIGHT/MEDIUM/HEAVY queue          process-local LIGHT/MEDIUM/HEAVY queue
  (P1/P3/P4/P6/P7/P8 producer work)                (P2/P5/P6 AI/P7 context work)
             |                                                    |
             +------ cross-process DB transaction admission ------+
                                  |
                         PostgreSQL (768 MiB cap)
```

### Layer 1 — process-local workload admission

Collector and Engine each own a separate bounded admission controller. Neither asks the other process to schedule ordinary CPU, RPC, parsing or normalization work. Each controller admits bounded work by class and estimated count/bytes; source-local rate limit, concurrency and queue constraints remain in force.

- **LIGHT:** WebSocket receive/control, liveness/health, cursor reads and minimal message framing. It bypasses HEAVY permits so a BTC block cannot stop the socket reader. Existing task count and per-source transport bounds remain observable.
- **MEDIUM:** bounded REST snapshots, normal canonical batches, ordinary window/context work.
- **HEAVY:** BTC large-block parse/valuation, ETH log/receipt fanout, large Phase 1 recovery, Phase 4 recovery/rollup rebuild, Phase 8 full-chain reconciliation and large atomic DB batches.
- Work requests carry source/phase, class, request identity, estimated bytes, creation/deadline time, replayability class and cancellation ownership. The exact public Python names are to be fixed with tests in the implementation stage; they must not leak provider fields into business contracts.
- Use weighted fair service with aging, separate class capacities, and explicit live-vs-backfill policy. Do not choose numeric slots/weights in this design. `DATA_LAYER_REPLAY_V1` and resource evidence determine them.
- Backoff sleeps happen outside permits. A deferred request retains its source cursor/recovery identity; there is no unbounded retry task per event.
- On shutdown, stop new admission, cancel queued replaceable refreshes, allow owned atomic work to finish or roll back, await thread-backed operations, and leave uncommitted source cursor state unchanged.

### Layer 2 — cross-process PostgreSQL admission

This layer coordinates only transaction classes that conflict materially under the measured PG budget. It decides when a business write transaction may start; it does not split or redefine its atomic contents. Small/control writes may use a separately bounded lane. A checkpoint that is part of a source's atomic transaction remains in that same transaction and class.

The transaction classes to measure are: small health/status, medium canonical batch, large atomic source transaction, standalone checkpoint/control, context snapshot, and Options reconciliation. `LIGHT/MEDIUM/HEAVY` compute class and DB transaction class are related but not interchangeable.

#### Candidate comparison

| Candidate | Crash/automatic release | Lifetime, timeout and cancellation | Deadlock and shutdown | DB pressure and observability | Assessment |
|---|---|---|---|---|---|
| A. PostgreSQL session advisory-lock slot pool | PostgreSQL releases session locks when the dedicated admission backend connection closes; a process crash does not leave a lease row to expire. | A dedicated idle/autocommit admission connection owns the session slot. The actual business transaction uses a separate normal connection. Both remain owned until commit/rollback and permit release. Admission wait is bounded/cancellable and outside the business transaction; never hold a slot during provider backoff. | Acquire the session slot before business `BEGIN`; do not use a transaction-scoped lock to wait. Because a lost admission connection can release its slot while the separate business transaction is still active, the business transaction also takes a non-blocking transaction-lifetime fence for that slot before any business write. Fence contention rolls back immediately and reports deferred; it is not an admission wait queue. Stop waiters on shutdown; active business work is drained or rolled back. | Requires one dedicated admission connection plus the normal transaction connection while work is active, so connection budget and lock traffic must be measured. Export both session-slot wait/hold and transaction-fence state. Lock acquisition has no FIFO guarantee. | **Recommended first implementation**, subject to injected admission-connection loss, two-process fairness, and pressure tests. Lowest schema/operational overhead while preserving slot exclusivity across the required connection split. |
| B. Small lease/admission table | Expiring leases can recover after owner loss, but require heartbeat, expiry and fencing-token correctness to prevent a slow/stale holder overlapping a replacement. | Separate grant/renew/release traffic and a worker connection; every wait/cancel/shutdown path must revoke or let a lease expire. Lease duration must exceed enforced transaction timeout with a tested safety margin. | Stale leases, renewal races and fencing are additional failure modes. Shutdown must revoke owned leases; row-lock order needs a fixed rule. | Creates writes/reads on the database being protected; queue visibility is strong and priority/FIFO is implementable, but the coordinator itself can add contention. Requires additive schema and cleanup contract. | Do not choose unless advisory-slot fairness/observability fails acceptance or a hard need for durable cross-process queue order is demonstrated. |
| C. Per-process connection-pool / transaction-class scheduling only | No shared state to recover. | Simple per-process bounded pool; cancellation is local. | No cross-process coordination or global queue; each container can independently consume its full pool. | Least extra DB control traffic, but does not enforce the required shared transaction start budget. | Insufficient for the approved cross-process DB admission requirement; retain as a local complement, not the sole design. |

#### Recommendation and constraints

Use **Candidate A: a PostgreSQL session advisory-lock slot pool** on a dedicated admission connection, separate from the normal business transaction connection. Acquire one configured session slot with non-blocking `pg_try_advisory_lock` on an idle/autocommit admission connection; this is the only waiting/admission mechanism. After grant, the normal business connection begins the existing transaction and obtains a distinct per-slot transaction-lifetime fence with non-blocking `pg_try_advisory_xact_lock` before any business write. The fence is never used to wait: if unavailable, roll back immediately and defer. This prevents a new writer from overlapping an already active business transaction if its separate session-lock connection drops and PostgreSQL releases the session slot early. Commit/rollback the business transaction, then explicitly release the admission session slot in `finally`; closing the admission connection is the crash-release fallback. Driver transaction-state transitions and injected loss of either connection must be tested. Avoid indefinite blocking advisory-lock calls and never hold permits through RPC/provider backoff.

Advisory locks are not FIFO. Process-local weighted queues, bounded wait deadlines, aging/promotion, class-specific slot reservations where justified by replay, and a visible `DEFERRED/ADMISSION_TIMEOUT` result must prevent silent starvation. Acceptance must run both application processes concurrently and demonstrate progress for every continuously eligible class. If repeatable two-process starvation, slot/fence leakage, an overlap after injected admission-session loss, or unacceptable lock/connection pressure is observed, Candidate A fails its gate and the design returns for a reviewed move to Candidate B; do not quietly add a second coordination mechanism.

Use multiple class lanes/slots; never a single all-work semaphore. Numeric class/slot limits remain unset until deterministic replay and PostgreSQL evidence establish them. If PostgreSQL is unavailable, DB admission fails closed: no cursor advance, no bypass, and replayable work remains pending for recovery. The process-local source receiver/CPU admission does not depend on acquiring a DB lock.

## 3. Unified backpressure contract

Every data stream declares one of these classes in configuration/registry and exposes the class in health/metrics:

| Class | Definition | Phase 1–8 mapping | Overload action |
|---|---|---|---|
| **A — CANONICAL_UNRECOVERABLE** | A source event cannot be proven recoverable from an authoritative cursor/window. | Phase 3 trade print streams; Phase 4 liquidation stream unless venue-specific complete replay is proved; any other source that lacks replay proof. | Never silently drop. Pause/defer downstream processing with bounded ingress where possible. If transport cannot be paused and data cannot be replayed, record an explicit gap and mark completeness `PARTIAL/ERROR`; do not claim `AVAILABLE`. |
| **B — RECOVERABLE_REPLAYABLE** | The source has a bounded authoritative cursor, window, or full reconciliation that reconstructs missed canonical state. | Phase 1 closed Klines/instrument catalog; Phase 7 BTC/ETH chain cursor and Binance Spot cursor; Phase 8 instrument/lifecycle reconciliation; Phase 6 source feed only when its adapter contract supports bounded time-window replay. | Defer/pause, then backfill/reconcile from the last durable cursor. Commit cursor with the data it covers. Recovery beyond the supported range fails closed. |
| **C — DERIVED_REPLACEABLE** | Latest state or output can be recomputed/refreshed without pretending an omitted interval was observed. | Phase 1 latest ticker, Phase 2 OI/Funding snapshots, Phase 4 Long/Short/Basis snapshots and recomputable rollups, Phase 5 context, Phase 6 AI/context result, Phase 7 enrichment/context, Phase 8 mark/ticker latest state and contexts/periodic chain summaries. | Coalesce, skip or defer a cycle. Persist/emit source age, skipped-cycle reason and `STALE/PARTIAL/NOT_AVAILABLE`; never fill missing data with zero or advance a source event cursor. |

Classification applies to the output stream, not merely a phase name. When source replay capability is unknown, classify conservatively as A until an official contract and tests prove B. Unexposed network-library buffers remain `NOT_EXPOSED`; they are not counted as empty.

## 4. Health and error contract

Keep lifecycle and data health distinguishable. The canonical source-health snapshot includes:

- `source_id`, `phase`, `configured`, `lifecycle_state`, `data_status`, `checked_at_utc`;
- `last_source_timestamp_utc`, `last_fetched_at_utc`, `last_processed_at_utc`, `last_persisted_at_utc`;
- `cursor_kind`, `cursor_value`, `cursor_updated_at_utc`, `freshness_age_seconds`;
- `queue_depth`, `queue_bytes`, `oldest_pending_age_seconds`, `pending_work`, `active_work`, `backfill_depth`, `retry_count`;
- `consecutive_failures`, `last_error_category`, `last_success_at_utc`, `admission_wait_seconds`, `db_transaction_class`;
- unknown/unavailable measurements explicitly carry `NOT_EXPOSED` or null plus a reason; never convert unknown to zero.

Canonical data/lifecycle states: `NOT_CONFIGURED`, `INITIALIZING`, `AVAILABLE`, `DEGRADED`, `RATE_LIMITED`, `STALE`, `ERROR`, `SHUTTING_DOWN`. `AVAILABLE` requires valid source read, contract/parser success, successful persistence when persistence is required, no unresolved gap, and freshness within that stream's contract. A transient provider error first moves an available source to `DEGRADED`; an active bounded 429 cooldown maps to `RATE_LIMITED`; expiry without a successful fresh observation maps to `STALE`; contract, integrity, atomicity or unrecoverable checkpoint faults map to `ERROR`. Shutdown lifecycle does not erase last data state. On restart, begin at `INITIALIZING`, reconcile cursor, then recover to `AVAILABLE` only after evidence.

Use a normalized category rather than exception class as the final diagnostic: `PROVIDER_RATE_LIMIT`, `PROVIDER_TIMEOUT`, `PROVIDER_CONTRACT`, `NETWORK`, `PARSER`, `DATA_QUALITY`, `PERSISTENCE`, `CHECKPOINT`, `BACKPRESSURE`, `ADMISSION_TIMEOUT`, `RESOURCE`, `CONFIGURATION`, `SHUTDOWN`, `UNKNOWN`. Preserve a sanitized exception type/code separately; never log credential-bearing URL, secret, auth header, raw large payload or private data.

Write a latest bounded source snapshot through the existing health persistence contract where feasible; do not append high-frequency queue samples to business tables. Runtime acceptance time series are bounded artifacts with explicit run identity and retention.

## 5. Observability before admission

Admission/scheduling implementation is blocked until these signals are available or marked `NOT_EXPOSED`:

- Per source: queue count/bytes/age; pending/active count; backfill depth; retry count; durable cursor; source/data freshness; last valid read and last successful persistence; last normalized error category; reconnect/gap/drop counters.
- Per process: async task count; LIGHT/MEDIUM/HEAVY active/pending counts and oldest wait; process RSS/PSS where available; cgroup current/peak/anon/file/events; CPU and restart/shutdown outcome.
- Database: pending write admissions by class; active transaction class/count; lock wait/hold/timeout; connections; transaction duration/rows/bytes; database and table/index sizes; WAL/checkpoint duration and buffers; cgroup anon/file/events; dead-tuple/vacuum evidence.
- Host/storage: free bytes/percent/inodes, Docker image/container/volume usage, bounded log/artifact bytes.

Metrics must be cardinality-bounded by registered source and class. Do not label by instrument/event ID at high cardinality. Health rows contain the latest summary; periodic acceptance samples are written to per-run NDJSON/CSV and rotated/retained under the policy below.

## 6. PostgreSQL and resource strategy

Keep the accepted Data Layer runtime/acceptance profile unchanged: PostgreSQL 768MiB, Collector 256MiB, Engine 384MiB. Compose files are not a universal single source today: `docker-compose.local.yml` and `docker-compose.phase7-acceptance.yml` use that profile, the base `docker-compose.yml` uses PostgreSQL 384MiB, and `docker-compose.server.yml` has no PostgreSQL service. The isolated Data Layer acceptance topology must explicitly declare and validate 768/256/384; do not silently raise the base development profile or infer a cap from whichever Compose file happens to be selected. Do not convert caps into simultaneous host reservations without checking host memory headroom. No resource value is tuned from an idle-only sample.

The experiment matrix in `DATA_LAYER_V1_ROOT_CAUSE_ANALYSIS.md` distinguishes `LEAK`, `BACKLOG`, `STEADY_WORKING_SET`, `WORKLOAD_PEAK`, `FILE_CACHE`, `WAL_CHECKPOINT_PRESSURE`, `MULTI_SOURCE_OVERLAP`, and `MIXED`. Evaluate PostgreSQL `shared_buffers`, `work_mem`, `maintenance_work_mem`, checkpoint settings, `max_wal_size`, autovacuum and connection limits only in isolated test databases after writer scheduling evidence exists; this design authorizes no production tuning.

### Stale Phase 3 resource rule

Audit confirmed `src/quant_phase3/resources.py::assess_resource_usage` is referenced by its unit test but has no production caller and contains a PostgreSQL 384MiB threshold, conflicting with the formal 768MiB Data Layer acceptance profile (though matching the base development Compose value). Treat it as stale acceptance code, not a universal resource source. In implementation, remove it if it remains unused or explicitly deprecate it; add a test ensuring the isolated formal acceptance profile explicitly uses 768/256/384 and that the base development profile is not silently rewritten. Do not run the stale helper as a production resource gate.

## 7. Database, Kline, retention and storage policy

- Preserve phase-owned canonical schemas/tables and existing migrations. Use additive migration only if a measured requirement cannot fit existing bounded health metadata/admission control; migrations 001–015 are immutable.
- No raw full payload logging. Keep per-phase retention configuration and distinguish source retention from evidence retention; storage deletion remains opt-in and owner-scoped.
- Phase 8 retention stays provisional at ticker/snapshot 7d, context 30d, lifecycle 90d with enforcement disabled until measured database/index growth and backtest needs are reviewed.
- Run retention in bounded batches, measure table/index sizes and dead tuples, observe vacuum/checkpoint load, and never assume `DELETE` immediately returns disk to the filesystem.
- Docker logs use a per-service rotating driver/config, proposed default `local` driver with `max-size=10m` and `max-file=3` (or an equivalent tested daemon policy). No compose service may omit an effective bounded log policy in the acceptance topology. Verify actual driver via `docker info` at runtime acceptance.
- Runtime applications log to stdout/stderr; acceptance artifacts are isolated under a quant-owned run directory with a manifest and size cap. Retain a bounded rolling set (proposed latest 5 runs / 30 days) and remove only manifest-tagged Data Layer artifacts through an explicit dry-run-first cleanup command. No Docker prune or blanket volume cleanup.
- Replay fixtures are versioned, compressed, hashed and budgeted. The per-dataset cap must be frozen from measured DATA_LAYER_REPLAY_V1 generation before the first full replay; over-cap generation fails rather than silently truncating. The runner records disk free space and stops before available filesystem space falls below 15%.
- Disposable PostgreSQL test volumes use a unique run/project name and manifest; no cleanup targets production or unrelated volumes. Cleanup is explicit, dry-run by default, and validates project ownership and exact volume IDs before removal.

## 8. Deterministic Replay V1 contract

`DATA_LAYER_REPLAY_V1` is a new integrated Data Layer fixture/replay, not a renamed Phase 6/7 replay. Its manifest freezes:

- Git SHA, application image digest, PostgreSQL image digest, migration version, config hash (secrets omitted), dataset hash, deterministic seed and replay version;
- UTC event/source/fetch/processed timestamps, source ID, canonical identity, provenance, status/quality fields and explicit missing values;
- per-run table counts, canonical identity digests, cursor/checkpoint state, data-quality statuses, context outputs, aggregation outputs, Phase 8 Options outputs and Stage 1-relevant outputs.

Fixtures cover P1 Klines/Stage 1 inputs; P2 OI/Funding; P3 trades/flow; P4 liquidation/Long-Short/Basis; P5 market context; P6 news/macro/unlock and provider `NOT_CONFIGURED`; P7 a realistic >10,000-event BTC block, Ethereum block/log/receipt, Binance Spot; P8 BTC/ETH calls/puts, multiple expiries/strikes, OI/volume/IV/Greeks, missing data, sparse ticker merge and lifecycle. Fixture provenance is explicit; synthetic fixture data is never presented as live-source evidence.

Run the same frozen replay at least three times in disposable isolated PostgreSQL schemas/volumes; compare exact output contracts and deterministic digests. No full-table hash over live multi-million-row production data is required.

## 9. Acceptance architecture and stop policy

1. **Stage 0 — final-SHA DB regression:** before any hardening refactor, run fresh 001–015 migration, repeat migration, 001–014 upgrade to 015, Phase 8 DB integration, then full regression with a configured `TEST_POSTGRES_DSN`. Database tests must fail/abort preflight if DSN is absent; they cannot silently skip in this gate.
2. **Stages 1–5 — evidence and controls:** observability, common health/error contract, per-process admission, cross-process DB admission, then unified backpressure. After each stage run focused, affected phase tests and full regression; preserve source transaction boundaries.
3. **Stage 6 — Replay V1:** three deterministic repetitions with frozen identity and exact output comparison.
4. **Stage 7 — failure/restart/finality:** inject bounded source, queue, DB and shutdown failures; at least three run/stop/restart cycles; Bitcoin/Ethereum finality and in/out-of-window reorg behavior.
5. **Stage 8 — 15-minute integrated real-source gate:** paper-only, public/read-only sources, observable per-source/DB/queue/resource state. Soft failures continue diagnostics and appear in a FAILURE MATRIX.
6. **Stage 9 — 60-minute soak and formal resource acceptance:** only after Stages 0–8 pass. Keep caps unchanged; apply hard resource stop thresholds frozen before launch; report actual sample and projections separately.

**Hard stop:** OOM/oom-kill, secret leak, data corruption, transaction semantic violation, duplicate canonical identity, cursor skip, unrecoverable DB corruption, or loss of safe cancellation. Preserve evidence and stop unsafe writes.
**Soft failure:** one 429, transient provider/network failure, one stale source, temporary `DEGRADED`, or a bounded admission timeout. Continue independent diagnostics and other sources until the fixed run deadline; report the failure matrix without calling the affected source accepted.

## 10. Correctness invariants

- Bitcoin: one block = one DB transaction = N bounded event chunks + cursor update + one commit; any chunk/checkpoint/cursor failure rolls back the block and cursor.
- Ethereum event data and checkpoint remain atomic; supported reorgs reconcile within the bounded window; beyond-window ambiguity fails closed and is visible.
- Canonical identity/dedup remains source-defined; queue pressure does not create duplicate identities or silently skip cursor data.
- `missing != zero`; `UNKNOWN != known`; provenance and unit/status metadata are retained; event-time prevents future leakage.
- Stage 1 eligibility/output is unchanged. Phase 8 remains context-only and never feeds risk, order or executor paths.
- `TRADING_MODE=paper`; no private API key, order/position API or Live Executor is introduced.
