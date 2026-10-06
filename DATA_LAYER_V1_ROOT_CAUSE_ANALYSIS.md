# Data Layer V1 Root-Cause Analysis

**Baseline:** `phase8` / `73c157c7fcf259a9b7df3bfc535082afde0af343`
**Status:** evidence-qualified; no production change made.

## Executive finding

The acceptance problem is not established as a single Bitcoin, Ethereum, Options, or PostgreSQL defect. The confirmed structural condition is that source-local limits and phase-local runtimes coexist without an aggregate workload admission contract or cross-process database-write admission. The measured PostgreSQL failure is real under active integrated load, but the available evidence does not isolate one exclusive cause. The defensible classification is **MIXED / ACTIVE-WORKLOAD PRESSURE**, not a proven memory leak.

## Findings and confidence

| Finding | Evidence | Confidence / boundary |
|---|---|---|
| Work can overlap across sources despite local bounds. | Collector creates independent Phase 1, 3, 4, 6, 7 and 8 tasks/supervisors; Engine independently runs Stage 1, Phase 2, Phase 6 and Phase 7 work. Phase 7 source supervisors run independently; Phase 8 periodic jobs have independent callbacks. | Confirmed architecture. It proves concurrency is possible, not that every measured peak was caused by a particular overlap. |
| No process-wide LIGHT/MEDIUM/HEAVY admission exists. | Source-level capacities and retry/rate limiters exist (e.g. Phase 1 recovery concurrency 8, Phase 3 per-symbol queues, Phase 4 event budgets, Phase 7 per-source concurrency 1, Phase 8 byte/message caps), but there is no common admission API in the runtime entrypoints. | Confirmed absence in the inspected code path. |
| PostgreSQL writes are not coordinated across Collector and Engine. | Phase 1 Collector, Phase 2 Engine, Phase 4 runtime, Phase 6 thread-offloaded persistence, Phase 7 source/context workers, and Phase 8 persistence open their own direct psycopg connections. There is no shared transaction-start gate or central writer queue. | Confirmed architecture. Historical overlap contribution to memory/checkpoint pressure still needs controlled ablation. |
| Per-source backpressure policies are materially different. | Phase 1 bounded deque evicts oldest on overflow (the live receive loop drains synchronously); Phase 3 rejects the newest trade on a full queue; Phase 4 records drop/gap/backlog conditions under several budgets; Phase 7 cursor recovery is bounded and atomic; Phase 8 bounds ingress and reseeds/reconciles. | Confirmed code behavior. Actual frequency of Phase 1 eviction is not established. Some WS sources cannot guarantee replay; these must be treated as loss-sensitive. |
| Health is not one cross-source contract. | Phase 1 uses a runtime-level tracker; Phase 3 uses `FlowStatus`; Phase 4 has `RUNNING/DEGRADED/STALE/ERROR/RECOVERED`; Phase 5 has `ContextStatus`; Phase 6 combines `DataStatus` and task reasons; Phase 7 persists per-source rows/stages; Phase 8 has its own lifecycle/status mapping. | Confirmed model fragmentation. A historical health/cursor disagreement is evidence that these fields need separate definitions, not proof that a specific source is currently corrupt. |
| PostgreSQL reached an active-workload resource gate. | Phase 7 report records a 5m10s integrated run at PostgreSQL cap 768MiB: sampled current 767.97MiB, peak 768.18MiB, `memory.events:max` +161,688; a ~22MiB checkpoint write took 39.197s; OOM and OOM-kill remained zero. | Confirmed report measurement; not rerun here. Acceptance blocked at that time. |
| Idle reclaimability does not clear the active gate. | In a separate quiescence sample, PostgreSQL current fell about 738.8MiB→148MiB over 120s; file fell about 719.0MiB→139.5MiB and `inactive_file` about 366.7MiB→1.6MiB, while anon stayed near 1.2MiB. | Confirms idle cache reclaimability for that sample only. Does not prove active-runtime safety. |
| Collector retained high but bounded memory under its latest Real-D window. | Phase 7 report records 15m10s, 92 samples: `memory.current` max 241.1MiB under 256MiB, `memory.peak` 247.6MiB, and no `memory.events:max`/OOM increments. | Confirms the specified Collector gate for that run, with limited headroom; it does not establish full Data Layer acceptance. |
| DB-enabled final-SHA regression evidence is incomplete. | Phase 8 report records the latest full run as 1,073/29/0 with 21 DB-dependent tests skipped; the previous full DB-enabled 1,093/8/0 preceded a probe-only pacing refinement. | Confirmed report gap. Must be the first hardening gate; no source behavior change should precede it. |
| DL-12 is missing from the current backlog. | `DATA_LAYER_HARDENING_BACKLOG.md` ends at DL-11; no DL-12 entry exists. | Confirmed documentation gap. Add DL-12 for final-SHA database regression. |
| Phase 3 PostgreSQL resource helper is stale/dead with respect to formal runtime acceptance. | `src/quant_phase3/resources.py::assess_resource_usage` contains a PostgreSQL 384MiB threshold. Search found no production caller; only `tests/test_phase3_safety_resources.py` refers to it. The formal Data Layer acceptance profile is PostgreSQL 768MiB, while base development Compose is 384MiB. | Confirmed test-only helper with profile-dependent meaning. It must be removed or explicitly deprecated before reuse; tests must pin the isolated acceptance profile, not claim one universal cap source. No code is changed in this design task. |
| PostgreSQL Compose cap is profile-dependent, not a single repository-wide value. | Local and Phase 7 acceptance profiles use 768MiB; base `docker-compose.yml` uses 384MiB; server Compose has no PostgreSQL service. Collector/Engine caps are 256/384MiB where defined. | Confirmed configuration difference. The new isolated Data Layer acceptance profile must explicitly retain the previously accepted 768/256/384 caps; do not silently raise or rewrite the base development profile. |
| Local disk is currently available, but storage policy is incomplete. | WSL is about 6% used with about 902GB available; Docker aggregate volumes about 44.93GB and images about 8.1GB. Base and server Compose variants configure `json-file` rotation at 10 MiB × 3 files; local and Phase 7 acceptance variants omit per-service rotation. Artifact/replay/temporary-DB budgets and cleanup ownership are not unified. | Confirmed at audit time. Active daemon logging defaults and effective per-container settings were not checked. Docker image reclaimable accounting was anomalous; no cleanup was attempted. |

## Causal model to test

The following are system-level hypotheses supported by architecture and timing, not yet uniquely proven:

1. **Multi-source overlap amplifies active working set.** A Bitcoin large-block cycle, Ethereum logs/receipts, Spot flow, Phase 1 recovery, Options chain reconciliation, and derived work have independent local policies. A local maximum multiplied by many sources can exceed the safe aggregate envelope.
2. **Transaction overlap amplifies PostgreSQL checkpoint/reclaim pressure.** Independent connections from both application containers allow large and ordinary transactions to start together. PostgreSQL's cache is reclaimable at idle, but the active run showed repeated `memory.events:max` growth and slow checkpoint writes.
3. **Backlog can masquerade as leak or steady-state growth.** Queue counts are bounded in several places, but not all byte sizes, queue ages, in-flight work, and pending retries are aggregated. A stable Python object baseline does not exclude a transport backlog or DB write backlog.
4. **Health aggregation can hide the locus of failure.** A configured source, successful fetch, canonical processing, durable persistence, cursor progress, and freshness are currently reported by different phase-specific mechanisms. A single top-level status is insufficient to determine which stage is degraded.
5. **Acceptance behavior has been too gate-local.** Prior runtime tests often stopped when a mandated blocker was reached. Final acceptance must distinguish hard safety stops from soft provider/source failures and continue collecting independent evidence after soft failures.

## PostgreSQL characterization matrix

Future formal-acceptance-profile experiments must change one factor at a time and keep caps at PostgreSQL 768MiB / Collector 256MiB / Engine 384MiB:

| Experiment | Controlled factor | Required observations | Interpretation target |
|---|---|---|---|
| Idle baseline and quiescence | Stop new application writes; keep the test PostgreSQL alive. | cgroup current/peak, anon/file/inactive_file, `memory.events`, RSS, cache/refault counters. | Distinguish reclaimable file cache from retained anon/working set. |
| Single writer class | Run one bounded small, medium, or large transaction class at a time. | active connections/transactions, bytes/rows, transaction duration, WAL/checkpoint timing, cgroup stats. | Establish class-specific cost and safe batch boundary. |
| Controlled overlap | Pair selected classes, then compare against isolated runs. | Same metrics plus overlap timeline and writer-admission wait. | Attribute additive/super-additive pressure to transaction overlap. |
| Source replay overlap | Replay BTC, ETH, Spot, recovery, and Options separately and in bounded combinations. | per-source queue depth/age, active class/slots, bytes, CPU, memory, DB wait and cursor outcomes. | Separate workload peak, backlog, and steady working set. |
| Repeated fixed workload | Repeat identical deterministic cycles without changing data identity. | post-cycle anon/PSS baseline, cgroup max events, DB size/WAL/dead tuples. | Detect retained-object growth separately from transient peaks and database growth. |

No tuning of `shared_buffers`, `work_mem`, checkpoint values, autovacuum, or connection limits is authorized by this design task. Tuning experiments require isolated disposable databases and independent review.

## Required root-cause closure criteria

Do not declare a leak from `memory.current` alone. A causal report must label evidence as one or more of: `LEAK`, `BACKLOG`, `STEADY_WORKING_SET`, `WORKLOAD_PEAK`, `FILE_CACHE`, `WAL_CHECKPOINT_PRESSURE`, `MULTI_SOURCE_OVERLAP`, or `MIXED`; show the timeline and controlled comparison; state what is `NOT_EXPOSED`; and show OOM/max-event, transaction, cursor and data-quality results. Keep measured sample and projections separate.

## Preservation invariants

- Bitcoin block events and their cursor remain one atomic transaction across all bounded persistence chunks.
- Ethereum data/checkpoint semantics, canonical identity and dedup remain unchanged.
- `missing != zero`; `UNKNOWN != known`; provenance and event time remain explicit; no future leakage is introduced.
- Stage 1 eligibility and Phase 8 context-only scope remain unchanged.
- No queue overflow or admission timeout may silently advance a cursor or report complete/available data.
