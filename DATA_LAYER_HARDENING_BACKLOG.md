# Data Layer V1 Hardening Backlog

**Program state (2026-09-27):** The final one-shot acceptance sprint is closed.

```text
DATA_LAYER_V1_IMPLEMENTATION_COMPLETE=true
DATA_LAYER_V1_DETERMINISTIC_ACCEPTANCE_PASS=true
DATA_LAYER_V1_LIVE_ACCEPTANCE_PASS=false
LIVE_ACCEPTANCE_BLOCKED_BY_ENVIRONMENT=true
```

Overall Data Layer V1 acceptance is **not** granted. The live gate remains
blocked by Deribit host connectivity: the IPv4 DNS answer was public, but the
host-side TLS/connectivity probes timed out; the DNS safety preflight also
observed a non-public AAAA answer. The Phase 8 REST/catalog probe was therefore
not run. Stage 8 and Stage 9 were intentionally not run. This is recorded as an
environment blocker, not a production-code defect.

No further acceptance remediation loop or automatic Stage 8/9 rerun is
scheduled. Reopening requires a new explicit instruction and fresh environment
evidence. Phase 9 remains out of scope.

The PostgreSQL `FILE_CACHE_PRESSURE` observation from a non-formal diagnostic
container remains diagnostic evidence only. No formal Stage 8/9 PostgreSQL
resource verdict was produced, and no PostgreSQL tuning or cap change is
authorized here. The formal profile remains PostgreSQL 768 MiB, Collector 256
MiB, and Engine 384 MiB.

DL-01–DL-13 below are retained engineering/future-hardening backlog; their
presence does not reopen this one-shot acceptance sprint or change its final
status.

## DL-01 — PostgreSQL active-write resource pressure

Investigate the active integrated workload under the existing PostgreSQL
768 MiB cap, including checkpoint/write latency, cgroup `memory.events:max`,
page-cache reclaim/refault behavior, and bounded persistence batches. Evidence
must retain both sides: idle quiescence reduced cgroup current from about
738.8 MiB to 148.0 MiB in 120 seconds, while a later 5m10 integrated retest
reached 767.97 MiB current / 768.18 MiB peak, increased `memory.events:max` by
161,688, and logged a roughly 22 MiB checkpoint write taking 39.197 seconds.
OOM/OOM-kill remained zero. This is active-workload pressure, **not** a proven
memory leak; idle cache reclaimability does not by itself pass the active
resource gate.

## DL-02 — Global multi-source workload admission

Define a shared admission budget for overlapping Phase 1–8 workloads,
including large Bitcoin blocks, Ethereum logs/receipts, Binance Spot, earlier
phase collectors/engines, and Phase 8 option-chain collection. Admission must
prevent one source's recovery work from multiplying concurrent load without
changing each source's correctness or cursor contract.

## DL-03 — Unified backpressure and queue budget

Inventory bounded queues, in-memory buffers, transport queues, batch sizes,
and pending work across all enabled sources. Establish an explicit aggregate
budget, overflow behavior, and observable backlog age/depth. Unobservable
transport buffers must remain marked `NOT_EXPOSED`, not inferred as empty.

## DL-04 — Database writer scheduling

Coordinate writes across collectors and engines to bound concurrent
transactions and checkpoint pressure. Preserve source-local atomicity:
Bitcoin block event chunks and cursor commit, Ethereum event persistence and
checkpoint, and equivalent source progress must continue to commit or roll
back together.

## DL-05 — Unified source health lifecycle

Define a cross-source health state machine with at least
`NOT_CONFIGURED`, `INITIALIZING`, `AVAILABLE`, `DEGRADED`, `RATE_LIMITED`,
`STALE`, and `ERROR` (or an explicitly mapped equivalent). Include last
successful observation, consecutive failures, cursor/data freshness, and a
sanitized error category. Do not collapse configuration presence into data
availability.

## DL-06 — Bitcoin and Ethereum finality acceptance

Add deterministic and bounded real-source acceptance for confirmation/finality
semantics, cursor advancement, and persisted event status. Finality must not be
inferred from mere head observation.

## DL-07 — Reorg fault injection and bounded recovery

Test reorgs inside the supported recovery window and beyond the existing
bounded old-event-ID query window. In-window changes must reconcile correctly;
out-of-window conditions must fail closed and be visible, never silently
reported as reconciled.

## DL-08 — Restart recovery

Run at least three controlled restart cycles. Verify migration idempotency,
source resubscription/reconnection, checkpoint recovery, data deduplication,
health recovery, and absence of cursor regression or skipped data.

## DL-09 — Integrated deterministic replay

Provide deterministic replay fixtures and bounded replay for Phase 1–8 after
Phase 8 feature completion. Replay must preserve event-time/no-future-leakage
rules and make persistence/cursor outcomes reproducible.

## DL-10 — 60-minute integrated real-source soak

After the resource and writer controls are in place, run a 60-minute integrated
observation with bounded sampling and explicit stop gates. Record actual
resource, backlog, cursor, freshness, health, and database-growth evidence;
do not extrapolate long-term growth from a short sample without labeling it as
a projection.

## DL-11 — Formal resource budget and acceptance

Reassess PostgreSQL, Collector, and Engine together under Phase 1–8 workload
overlap. Keep the formal runtime acceptance profile at 768/256/384 MiB until
a separately reviewed change is justified; do not silently alter the base
development Compose PostgreSQL cap of 384 MiB. Define sustained thresholds,
OOM gates, CPU, disk/database growth, log retention, and graceful-stop
criteria before the final acceptance run.

## DL-12 — Phase 8 final-SHA database regression — CLOSED

Before any Data Layer refactor, run the exact frozen Phase 8 SHA with a
disposable PostgreSQL instance and `TEST_POSTGRES_DSN`: fresh migrations
001–015, repeat migrations, upgrade 001–014 to 015, Phase 8 DB integration,
and full regression. The DB gate must fail closed if Docker/DSN is unavailable
or if any database-dependent test is skipped. The latest post-refinement full
suite evidence (1,073 passed, 29 skipped, 0 failed) included 21 DB-dependent
skips; the preceding DB-enabled full suite (1,093 passed, 8 skipped) preceded
the final probe pacing refinement. Those do not replace the final-SHA gate.

**Closure evidence (2026-09-26):** frozen application sources matched SHA
`73c157c7fcf259a9b7df3bfc535082afde0af343`; PostgreSQL 16.15 fresh 001–015
and independent 001–014→015 upgrade passed; both repeat runs applied 0
migrations; focused Phase 8 database suites 36 passed; full regression 1094
passed, 8 opt-in public live probes skipped, 0 failed, with 0 database-dependent
skips. See `DATA_LAYER_V1_STAGE0_REPORT.md`. Stage 0 evidence checkpoint is
separate from and does not imply overall Data Layer V1 acceptance.

## DL-13 — Retire stale PostgreSQL 384 MiB resource helper

`src/quant_phase3/resources.py::assess_resource_usage` has no production
caller and contains a PostgreSQL 384 MiB threshold. That matches base
development Compose but not the formal 768/256/384 MiB runtime acceptance
profile. Remove the unused helper or explicitly deprecate it before it can be
reused. Add a regression test pinning the isolated acceptance profile while
proving the base development profile is not silently rewritten. This item is
design/backlog only until implementation is separately approved.
