# Data Layer V1 Architecture Review

**Review date:** 2026-09-26
**Baseline:** `phase8` / `73c157c7fcf259a9b7df3bfc535082afde0af343`
**User-approved direction:** Option A, existing Collector + Engine + PostgreSQL topology.
**Design self-review:** `PASS_WITH_VALIDATION_GATES`.

## Decision record

Retain the existing three-service topology. Implement two distinct control layers:

1. **Process-local work admission:** one bounded, weighted admission controller in Collector and one in Engine. These govern only their own CPU/memory-intensive, backfill, parse, normalize and derived work. Existing provider-specific rate limits and concurrency remain in force. LIGHT receive/health/cursor work is not blocked behind HEAVY work.
2. **Cross-process DB write admission:** coordinate start of only measured heavy/conflicting transactions across the two processes using a dedicated session-lock admission connection, separate from each normal business connection. A non-blocking transaction-lifetime fence protects active work if the admission connection disappears; it is not used for waiting. Do not coordinate ordinary RPC, parsing or all CPU tasks cross-process. Preserve each repository transaction and cursor contract exactly.

No broker, always-on service, or new worker container is added. The database-admission implementation recommendation is a PostgreSQL session advisory-lock **multi-slot/class pool**, not a single global semaphore. It must acquire a slot before the business transaction, use the same DB session through commit/rollback, release explicitly, and rely on session close for crash recovery. Bounded local weighted queues, aging and observable `DEFERRED` timeouts address contention; advisory-lock FIFO is not assumed. If a two-process acceptance demonstrates repeated starvation or unacceptable lock pressure, stop and re-review Candidate B rather than adding a hidden fallback.

## Options considered

| Option | Complexity | Memory / CPU | Failure isolation | Operational burden | Migration cost | Fit |
|---|---|---|---|---|---|---|
| **A. Current topology + two-layer admission/scheduling** | Moderate, focused library and runtime wiring | No new process baseline; improves concurrency behavior without raising caps | Process failures remain separated at current Collector/Engine boundary; DB remains shared | Lowest ongoing operations; PostgreSQL availability is required for cross-process DB permits | Incremental and testable; preserves existing data paths | **Recommended.** Addresses measured gaps with the smallest topology change. |
| B. Split BTC/ETH/Options heavy workers + central persistence | High; new process APIs and persistence ownership | Extra container/process overhead; can isolate a 256MiB Collector peak but consumes scarce host memory and may add IPC copies | Better per-source process isolation | More images, health, restart and deployment controls | Requires source ownership and durable message transfer changes | Reconsider only if measured process-local resource isolation cannot pass under current topology. |
| C. Independent source workers + broker/message bus | Very high; delivery, ordering, replay, dedup and schema operations | Broker adds resident memory, disk and network buffers | Strong worker isolation if correctly operated | Highest; broker persistence, upgrades, monitoring, recovery and security | Largest; source cursor/transaction semantics must cross the bus | Rejected for V1: no evidence justifies Kafka/Redis/Rabbit, and the 2CPU/low-memory budget favors avoiding another service. |

## Review checklist

| Check | Result | Review note |
|---|---|---|
| No centralized mega-scheduler for all cross-process CPU/RPC/parse tasks | PASS | Collector and Engine each own process-local admission. |
| Source-local and global limits are separate | PASS | Provider rate/concurrency remains source-local; process admission controls aggregate local compute; DB admission controls only conflicting transaction starts across processes. |
| DB admission is independent of compute admission | PASS | A DB permit is acquired only for a DB transaction class that needs coordination; dedicated admission and business connections have separate lifecycles. |
| Admission-connection loss cannot release an active writer slot early | PASS WITH REQUIRED TEST | The business connection takes a non-blocking transaction-lifetime fence after admission and before writes; it is never used to wait. Injected disconnect/no-overlap evidence is mandatory in Stage 4. |
| No global semaphore = 1 | PASS | Multiple class lanes/slots are required; exact counts/weights are data-driven. LIGHT work does not consume a HEAVY slot. |
| Backpressure is classified A/B/C per stream | PASS | Loss-sensitive, replayable and replaceable streams have distinct overload behavior. Unknown replayability defaults conservatively to A. |
| Observability precedes scheduling | PASS | Queue age/bytes, pending/active, source/cursor/freshness and DB wait are prerequisites; unknowns are `NOT_EXPOSED`. |
| PostgreSQL pressure is treated correctly | PASS | Both idle reclaimability and active max-event/checkpoint evidence remain in the root cause record; no leak claim or cap increase. |
| Stale 384MiB helper and Compose profile variance are addressed | PASS WITH TASK | `assess_resource_usage` has only a unit-test caller. Its 384MiB value matches base development Compose but not the formal 768/256/384 acceptance profile. Future implementation removes/deprecates the helper and pins the isolated acceptance profile without rewriting the base profile. No code changed here. |
| Compose resource/log variants are accurately scoped | PASS WITH TASK | Local and Phase 7 acceptance use 768/256/384; base Compose uses 384/256/384; server Compose has only Collector/Engine. Base/server define 10 MiB × 3 `json-file` rotation; local/Phase 7 acceptance omit per-service rotation. The isolated acceptance topology must validate both caps and effective log rotation. |
| Docker/log/storage bounded without pruning user resources | PASS | Rotating logs, manifest-tagged bounded artifacts, replay caps, exact project-scoped disposable DB ownership and explicit dry-run cleanup. No auto prune. |
| DATA_LAYER_REPLAY_V1 covers Phase 1–8 | PASS | Manifest, all phase datasets, three-run identity/output comparison, no future leakage. |
| Soft failures do not stop the diagnostic matrix | PASS | Only data-safety/resource hard stops interrupt unsafe work; transient provider/source failures continue bounded diagnostics. |
| Bitcoin/Ethereum and domain semantics preserved | PASS | Transaction/cursor, dedup, finality/reorg, missing/unknown/provenance, Stage 1 and Options context-only invariants are explicit. |
| No broker or unneeded service | PASS | Existing topology retained. |

## Required validation gates before implementation acceptance

- Run DL-12 final-SHA DB regression first; final SHA must be exactly the frozen Phase 8 baseline or a separately reviewed documentation-only checkpoint.
- Prototype the advisory-lock slot pool against PostgreSQL and two application processes. Prove crash release, commit/rollback, cancellation, bounded acquisition timeout, slot reuse, class progress and no transaction-semantic change.
- Inject admission-connection loss while a business transaction is active; prove the per-slot non-blocking transaction fence prevents overlap without wrapping the admission wait in a transaction-scoped advisory lock.
- Use deterministic replay to choose class weights and slot counts. No numeric slot conclusion is made from isolated source limits.
- Prove every class makes progress under mixed sustained load; if this fails, Candidate A is not accepted as designed.
- Confirm per-source replayability for Phase 3/4 streams and Phase 6 adapters. Until proven, enforce canonical-unrecoverable behavior.
- Set the 60-minute resource thresholds and artifact/dataset budgets from measured Stage 0/replay artifacts before starting final acceptance.
- Verify the actual Docker log driver/rotation and exact disposable volume IDs at runtime acceptance; this design has not accessed or changed Docker runtime resources.

## Review conclusion

The design is internally consistent with the approved topology and the current evidence. Remaining uncertainties are explicit validation gates, not silent architectural assumptions. The user has now explicitly approved implementation; proceed through Stages 0–9 in order, with no refactor before Stage 0 passes and no progression after a failed/blocked gate. This approval does not authorize Phase 9, ECS changes, or trading functionality.
