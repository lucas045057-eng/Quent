# Data Layer V1 Baseline Audit

**Audit date:** 2026-09-26
**Repository baseline:** `phase8` / `73c157c7fcf259a9b7df3bfc535082afde0af343`
**PHASE8_CODE_BASELINE_SHA:** `73c157c7fcf259a9b7df3bfc535082afde0af343`
**Baseline worktree:** clean; no source changes were made during the audit.

## Scope and evidence rules

This is a read-only baseline audit of Phase 1–8 code, tests, runtime reports, migrations, Compose configuration, and the hardening backlog. Reported test and runtime measurements are historical evidence from their cited reports, not tests or runtime measurements rerun in this audit. Code-derived limits are distinguished from exchange-enforced limits; a configured client rate is not evidence of the provider's actual rate-limit threshold. Missing telemetry is labeled `NOT_EXPOSED` rather than inferred to be zero.

No production server, ECS, trading path, private API, or runtime was accessed or started. No database or Docker volume was modified or pruned.

## Git and test baseline

- Branch: `phase8`.
- HEAD: `73c157c7fcf259a9b7df3bfc535082afde0af343`.
- Worktree, diff, and `git diff --check`: clean at audit start.
- `PHASE_8_FEATURE_REPORT.md` records the latest post-smoke-refinement full regression as **1,073 passed, 29 skipped, 0 failed**. The 29 skips include 21 PostgreSQL-dependent tests skipped because `TEST_POSTGRES_DSN` was unavailable, and 8 opt-in live contract probes.
- The same report records an earlier database-enabled full regression as **1,093 passed, 8 skipped, 0 failed** before the final probe pacing-only change, and isolated Phase 8 database tests as **146 passed, 0 skipped, 0 failed**. Those results are not a substitute for a final-SHA database-enabled full regression.
- Phase 8 is feature-complete; the 15-minute/60-minute integrated runtime, final resource acceptance, and production retention enforcement remain deferred.

## Runtime/source inventory

The entries below describe code configuration and known reports. Where retries, provider limits, transport buffering, or freshness are not exposed by a common contract, they are explicitly marked `NOT_EXPOSED`.

| Phase / source | Protocol and cadence | Local bounds, recovery, and persistence | Health / shutdown / workload class |
|---|---|---|---|
| P1 Bitget UTA market data | REST bootstrap and hourly Universe refresh; continuous public WS; ticker/market persistence cycle about 5s. Client setting defaults to 20 REST requests/s; this is not a measured Bitget quota. | Universe default 200; event buffer capacity 2,000; bounded closed-Kline store; gap recovery concurrency 8 and at most `universe × 4 intervals` work items. REST gap recovery and restart bootstrap exist. Persistence: Phase 1 instruments, market snapshots/observations, klines, Stage 1 inputs/results. WS reconnect default 5s. | Collector-level `RUNNING/DEGRADED`; source-specific queue age is `NOT_EXPOSED`. Ticker snapshots are replaceable; closed bars/catalog are REST-recoverable. Medium; recovery batch may be heavy. |
| P2 derivatives | Bitget UTA, Bybit V5, Hyperliquid public REST; Engine poll default 300s. | Universe from persisted Phase 1, bounded by 200; per-symbol/provider calls are sequential within each adapter cycle; local history deque max 400. Direct Phase 2 persistence and retention cleanup. Cross-phase retry/admission and aggregate pending work are `NOT_EXPOSED`. | Runs as an Engine background cycle with error isolation; cancellation at Engine shutdown. OI/funding current snapshots are replaceable and missed intervals must remain visible as freshness gaps. Medium. |
| P3 public trade flow | Bitget UTA, Bybit, Hyperliquid public WS; dynamic subscriptions refreshed on the configured subscription cadence/cooldown. | Up to 20 symbols **per exchange**; one 2,000-item queue per active exchange-symbol (theoretical maximum 60 such queues); per-exchange dedup up to 10,000 entries / 300s TTL. Flow persistence cycle about 5s. Full queue rejects the incoming trade and records a dropped counter. WS reconnect default 5s; no complete runtime REST replay guarantee is established by the source runner. | Per-exchange `FlowStatus`, connection, reconnect/gap/drop counters. Queue age and aggregate bytes are `NOT_EXPOSED`. Live trade events are treated as canonical/unrecoverable unless a source-specific replay contract proves otherwise. Medium to heavy during bursts. |
| P4 liquidation | Bitget UTA v3 and Bybit v5 public WS; symbol set follows the Universe. | Per exchange-symbol queue default 2,000, plus global event budget 20,000 / 32MiB; bounded builder/finalized/rollup/recovery budgets. Dedup bounded to at most 10,000 entries/exchange. WS reconnect default 5s. Persistence runs through Phase 4 repository operations. | Phase 4 component registry has `RUNNING/DEGRADED/STALE/ERROR/RECOVERED`, timestamps and gap/drop counters. Transport backlog age is `NOT_EXPOSED`. Canonical liquidation events are loss-sensitive; source replayability must be proven per venue. Heavy during catch-up/rollup. |
| P4 Long/Short and Basis | Bitget Classic v2, Bybit v5 and Hyperliquid public REST for Long/Short; Bitget UTA v3 and Bybit v5 for basis; REST cycle default 60s. Phase 4 has its own 20 req/s TokenBucket and 15s session timeout. | Provider work is bounded by Universe and cycle; shared Phase 4 REST transport/limiter, but not shared with other phases. Direct REST observation persistence and retention cleanup. | Same Phase 4 registry; source error category/queue age is not common. Replaceable snapshots; medium. |
| P5 market context | No external provider. Engine reads Phase 1–4 data and computes context on the Stage 1 cycle (default 300s). | Bounded samples/windows and evidence bytes in settings; context/enrichment writes use Engine database cycles. No independent source backfill. | Phase 5 has a separate `ContextStatus` registry. Recomputable/replaceable output; medium, potentially heavy at full Universe. |
| P6 News / Macro / Unlock | Registry-bound public sources; ingestion cadence default 300s. Exact live protocol/provider set depends on configured registry and is not asserted by this inventory. | Per-kind collector loops; bounded raw payload (256KiB default), bounded AI inputs/outputs, AI queue default 32 and concurrency 2. Source/provider retry details are adapter-specific; aggregate pending work is `NOT_EXPOSED`. DB/source blocking operations are thread-offloaded with cancellation ownership. | Phase 6 uses its own `DataStatus` and task-reason model. AI provider absence is an explicit not-configured/unavailable outcome and must not block other phases. News windows may be replayable only where source contracts support it; otherwise gap is explicit. Medium. |
| P7 Bitcoin RPC | Bitcoin Core JSON-RPC; collector source cycle default 30s. Default 1 req/s, concurrency 1, timeout 8s, up to 3 attempts / 8s total retry-wait budget. | BTC response default 32MiB, hard cap 64MiB; bounded block backfill (12 blocks in latest resource report); single-block atomic persistence: chunks plus cursor in one commit. | Per-source stage/health/checkpoint diagnostics; source supervisor is owned and cancelled at shutdown. Cursor-recoverable, with bounded reorg lookup and fail-closed behavior beyond its window. Heavy. |
| P7 Ethereum RPC | Ethereum JSON-RPC; same 30s supervisor cadence, default 1 req/s and concurrency 1. | 8MiB response contract; latest report describes bounded 120-block backfill, logs cap 2,000, receipt candidate cap 2,000 and sequential batches up to 250. Events/checkpoint commit atomically. | Per-source health/checkpoint. Cursor-recoverable; finality/reorg acceptance remains a gate. Heavy for log/receipt fanout. |
| P7 Binance Spot | Public REST catch-up plus public WS trade stream. | Cursor/from-id REST recovery, bounded WS frame and closed-window persistence; reconnect/resubscribe owned by source supervisor. | Separate REST and WS health. Recoverable through cursor while venue contract supports it; otherwise gap is explicit. Medium. |
| P7 Engine context | DB reads/aggregation; cycle interval default 60s, window batch default 16. | Bounded SQL statement timeout and context batch; direct Phase 7 context persistence. | Separate Engine context health. Replaceable derived context; medium. |
| P8 Deribit Options | Public REST + one public WS connection. Instruments daily by default; chain summary hourly; mark/ticker/context snapshots every 900s. | REST minimum dispatch interval 1.25s, max 3 attempts, 8MiB response; REST full-chain record caps 2,048/underlying and 4,096 total; 64 ticker contracts/underlying; WS ingress max 256 messages / 16MiB, 1MiB/message; reconnect max 8 with 60s max delay. | Phase 8 lifecycle and health are runtime-local; WS state can reseed/reconcile after loss of trust. Full-chain reconciliation is heavy; mark/ticker/context refreshes are replaceable but freshness must change when skipped. Retention enforcement defaults false. |

## Workload classes

- **LIGHT:** WS receive and control frames, health heartbeat, cursor reads, small status updates. These must not wait behind a heavy block operation.
- **MEDIUM:** bounded REST snapshots, ordinary persistence batches, trade-window aggregation, normal Stage 1/context work, bounded option ticker/mark snapshots.
- **HEAVY:** Bitcoin large-block parsing/valuation and its atomic write, Ethereum log/receipt fanout and atomic checkpoint, Phase 1 gap-recovery batches, Phase 4 recovery/rollup rebuild, Options full-chain reconciliation, large Stage 1/history hydration.

An operation's source-local rate/concurrency remains authoritative for that provider. Process-local admission is a second, independent control and is not a replacement provider limiter.

## Backpressure data classes

Classification is at data-product/stream granularity; one Phase can have multiple classes.

| Data class | Phase 1–8 mapping | Required overload behavior |
|---|---|---|
| A — canonical / not safely replayable | P3 live trade prints; P4 liquidation prints unless a venue-specific complete replay contract is proven | Never silently drop. Apply bounded ingress/backpressure; if the transport cannot pause and cannot replay, record a gap and make completeness unavailable/partial. |
| B — recoverable / replayable | P1 closed Klines and instrument catalog; P7 BTC/ETH chain events via cursor/backfill and Binance Spot via supported cursor; P8 instrument catalog/lifecycle through full reconciliation; P6 source events only where source provides bounded time-window replay | Defer or pause work; recover from durable cursor/window; do not advance checkpoint past unpersisted data. Reorg beyond a supported window fails closed. |
| C — derived / replaceable refresh | P1 latest ticker snapshot; P2 current OI/Funding snapshot; P4 Long/Short/Basis snapshot and rollups recomputable from retained inputs; P5 context; P6 AI/context output; P7 enrichment/context; P8 mark/ticker latest-state snapshots and option contexts/periodic chain summary | Coalesce, delay or skip the refresh when overloaded; update age/freshness/status and reason. Missing is never represented as zero or as a fresh value. |

For any stream whose replayability is not contractually established, use class A behavior until proven otherwise. No queue overflow may silently move a cursor or claim `AVAILABLE` completeness.

## Resource, storage, and deployment evidence

- Compose resource values are profile-dependent: `docker-compose.local.yml` and `docker-compose.phase7-acceptance.yml` use PostgreSQL **768MiB**, Collector **256MiB**, Engine **384MiB**; the base `docker-compose.yml` uses PostgreSQL **384MiB** with the same app caps; `docker-compose.server.yml` defines only the two app caps and no PostgreSQL service. The Data Layer acceptance topology must explicitly select the previously accepted 768/256/384 profile; do not silently raise the base development profile or treat these files as one universal cap source.
- No per-service CPU quota was found. Log rotation is inconsistent across Compose variants: `docker-compose.yml` and `docker-compose.server.yml` configure `json-file` with 10 MiB × 3 files; `docker-compose.local.yml` and `docker-compose.phase7-acceptance.yml` have no per-service rotation block. The active Docker daemon default was not verified. Runtime/acceptance artifact retention is not centrally defined.
- Latest storage check: WSL root filesystem about 6% used with about 902GB available; inode use about 1%. `docker system df` reported about 8.1GB images and 44.93GB local volumes in aggregate. No cleanup was run. Docker's negative image-reclaimable figure is treated as a reporting anomaly, not available capacity.
- Phase retention is configured independently by phase. Phase 8 suggests 7/30/90 days but enforcement is disabled. There is no measured long-duration Phase 1–8 production growth rate in this audit; short-window extrapolation is not a measured annual estimate.
- PostgreSQL test databases and replay artifacts need explicit size budgets and owner-tagged cleanup rules. No automatic prune/delete is permitted.

## Baseline audit conclusion

The existing Phase 1–8 features have meaningful source-local bounds, atomicity protections, and deterministic tests. They do not yet have an aggregate cross-source work budget, cross-process DB write admission, unified health/error semantics, or complete aggregate queue/backlog observability. PostgreSQL pressure is an active-workload acceptance blocker, not a proven leak. The next hardening gate is the final-SHA PostgreSQL regression, including migrations 001–015 and the full DB-enabled suite.

## Evidence files

- `PHASE_8_FEATURE_REPORT.md`
- `PHASE_7_LOCAL_RUNTIME_REPORT.md`
- `PHASE_6_LOCAL_RUNTIME_REPORT.md`
- `DATA_LAYER_HARDENING_BACKLOG.md`
- `docker-compose.local.yml`
- `src/quant_phase1/entrypoints/collector.py`
- `src/quant_phase1/entrypoints/engine.py`
- `src/quant_phase1/config.py`
- `src/quant_phase3/queue.py`
- `src/quant_phase4/runtime.py`
- `src/quant_phase6/runtime.py`
- `src/quant_phase7/runtime.py`
- `src/quant_phase8/runtime.py`
