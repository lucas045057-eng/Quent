# Data Layer V1 — Stage 8 Local Runtime Acceptance

## Purpose and safety boundary

Stage 8 measures the integrated Phase 1–8 public/read-only runtime after Stages 0–7 pass. It is local-only and paper-only. It does not connect to ECS, use exchange private/account/order APIs, place orders, or configure an AI provider. The isolated runtime consists of exactly one collector, one engine, and one PostgreSQL instance in a uniquely named Compose project. No host ports are published; a project-scoped bridge network provides required public egress. The existing local database and its volume are never referenced.

The collector and engine receive only an explicit allowlist of public-source settings from `.env.local`. `TRADING_MODE` and `POSTGRES_DSN` from that file are ignored; Compose forces `paper` and a new project-local database. Proxy variables and unrecognized/private API keys are not passed. The only secret-shaped public-source setting admitted is the configured Phase 7 Bitcoin RPC credential; it is used solely for read-only public-chain RPC and is never included in artifacts or logs.

## Runtime window and sampling

The measured window starts once all three services are ready and the isolated database has completed startup migrations. Its deadline is monotonic and fixed at 900 seconds. Samples are scheduled at offsets 0, 10, …, 890 seconds. A sample is never started at or after the deadline. No catch-up bursts are allowed: if a sample overruns a slot, skipped slots are counted and the next sample uses the next future slot. Any skipped slot makes the 10-second sampling gate fail.

Each bounded sample records UTC receive time, service/container state, cgroup and process memory, cgroup CPU counters, host aggregate CPU, PostgreSQL database size and transaction counters, low-cost table activity estimates, health/status ages, numeric ingestion-cursor observations, disk headroom, rotated-log sizes, and artifact size. Health details are projected through the existing allowlist; market payloads, raw health details, endpoint URLs, credentials, and headers are never persisted. Each JSONL record is limited to 64 KiB and the complete sample artifact to 5 MiB. Log inspection reads at most the final 128 KiB per service and records only size/audit booleans.

## Resource and lifecycle contract

The isolated Compose profile pins both memory and memory+swap limits to PostgreSQL 768 MiB, collector 256 MiB, and engine 384 MiB. It does not change other Compose profiles. Every service uses `json-file` rotation at 10 MiB × 3. The runtime reports actual cgroup/process/host measurements and database/log/artifact growth; it does not infer long-term growth from a 15-minute sample.

The run stops early only for a hard safety condition: disk free below 15%, any new cgroup OOM/OOM-kill event, a numeric cursor rollback, detected credential/header leakage, or an artifact/measurement safety-limit violation. Stale, partial, unavailable, rate-limited, or ordinary network-error sources are recorded with bounded categories and do not cause retries outside the application’s configured bounded policy; independent safe measurements continue until the fixed deadline. A graceful interrupt stops sampling and still performs bounded shutdown.

Shutdown is ordered engine → collector → PostgreSQL. The runner verifies final container state and checks that the collector/engine exited cleanly. It removes only containers and the network carrying the exact generated project and ownership labels. The uniquely named PostgreSQL volume is preserved for evidence/recovery; no prune or broad cleanup is allowed.

## Acceptance gates

All gates are reported independently as `PASS`, `FAIL`, `SKIPPED`, or `NOT_EXPOSED`; source observations also preserve `AVAILABLE`, `PARTIAL`, `STALE`, `NOT_AVAILABLE`, `NOT_CONFIGURED`, and `ERROR` distinctions.

Required gates:

1. Compose preflight validates exactly the three approved services, unique project/volume ownership, no host ports, paper mode, exact memory caps, bounded logs, and no private/order/position environment variables.
2. PostgreSQL is `quant`, uses UTC, is reachable only through the isolated project network, and has every migration file present exactly once. Database samples use read-only transactions and statement/connect timeouts.
3. Collector and engine remain running without restart/OOM throughout the measured window and have fresh runtime health. PostgreSQL remains healthy.
4. Phase 1 proves live persistence by observing new market snapshots, closed klines, and Stage1 results during the window. An empty-but-running pipeline is not a pass.
5. Each Phase 2–8 phase must expose either new bounded table activity or a current explicit health status. Phase health is evaluated by state, not by requiring every public provider to remain continuously available:
   - `AVAILABLE` passes.
   - `NOT_CONFIGURED` passes only for an explicitly optional source when health proves `configured=false` (and, for Phase 6 ingestion, `source_count=0` with `SOURCE_NOT_CONFIGURED`). Missing configuration evidence fails closed.
   - `DEGRADED`/`PARTIAL` passes as `PASS_WITH_DEGRADATION` only when reason and `data_quality=PARTIAL` are exposed and there is no corruption, duplicate identity, atomicity violation, cursor skip/rollback, or silent canonical loss; any required recovery semantics must also be evidenced.
   - Unexplained `STALE`, persistent/unrecoverable `ERROR`, or `NOT_EXPOSED` fails.
   Phase 6 News/Macro/Unlock and the AI provider are optional/config-driven; an unconfigured source remains explicitly `NOT_CONFIGURED` and is not synthesized or treated as available.
6. Phase 4 liquidation stream gaps are durable data-quality degradation, not an instruction to wait for a random future liquidation. The gap and watermark remain visible after recovery; the phase may be `PARTIAL` only when the gap reason, watermark, and timestamp are exposed. Acceptance verifies gap detection, silent-loss prevention, downstream PARTIAL propagation, and continuation after a later event.
7. Phase 7 aggregates child-source state without allowing one transport failure to hide behind unrelated table activity or permanently poison the whole phase. Per source, one or two consecutive transient transport/rate-limit failures are `DEGRADED`/`PARTIAL`; the third consecutive failed cycle marks that child `ERROR`. A successful cycle resets that child’s count and restores its `AVAILABLE` state. At the phase aggregate, a failed child remains visible and the phase is `PARTIAL` when another public transport is currently `AVAILABLE` and every failed child is explicitly categorized as transport/rate-limit with `data_quality=PARTIAL` and a reason. The phase is `ERROR` when no public transport is usable or any child has an unexplained, parser, persistence, or data-quality error. Cursor/table activity alone never upgrades a failed child to `AVAILABLE`.
8. No hard safety condition occurs; disk headroom remains at least 15%; the sampling schedule has no missed intervals; numeric cursors do not decrease; and secret/log/artifact audits pass.
9. Collector and engine shut down cleanly before PostgreSQL; the isolated database remains readable through final measurements.

The Stage 8 gate passes only if all required safety and activity gates pass and each source gate is either `PASS` or evidence-backed `PASS_WITH_DEGRADATION`. Soft provider failures are recorded in the failure matrix and are not, by themselves, hard stops. OOM, secret leakage, data corruption, atomicity violation, duplicate canonical identity, cursor skip/rollback, unreported canonical loss, unrecoverable database corruption, missing Phase 1 persistence, and unexplained `STALE` remain failures. `NOT_EXPOSED` and `NOT_CONFIGURED` without explicit optional-source evidence cannot be promoted to success. Stage 9 may start only after Stage 8 reports a full pass and Stages 0–7 remain accepted.

## Evidence and privacy

Run evidence is written under the ignored `.superpowers/sdd/2026-09-26-data-layer-v1-hardening/stage8/<run-id>/` directory with restrictive permissions. The report contains counts, statuses, bounded measurements, error categories, and opaque cursor identifiers only. It must not include full URLs, raw payloads, API keys, authorization headers, DSNs, or exception text. An inability to safely inspect a metric is reported as `NOT_EXPOSED` or `ERROR`; it is never synthesized.
