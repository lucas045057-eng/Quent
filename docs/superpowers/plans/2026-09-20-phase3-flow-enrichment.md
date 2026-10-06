# Phase 3 Flow Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public-trade flow context for Bitget, Bybit, and Hyperliquid to the accepted Phase 2 paper-only system, with directional delta/CVD only for Bybit's documented public aggressor-side contract, bounded runtime state, durable flow windows, gap/freshness evidence, and context-only Stage1 enrichment.

**Architecture:** Add a `quant_phase3` package behind exchange adapters and canonical contracts. Keep the existing two-process topology: collector owns public trade WebSockets, bounded ingestion, deduplication, window aggregation, and flow persistence; engine reads durable flow context and writes a separate Stage1 enrichment row. Reuse the existing PostgreSQL, migration runner, logging, health, and public-only configuration. Do not change Phase 1/2 canonical semantics or add a service.

**Tech Stack:** Python 3.12, dataclasses/Decimal, asyncio, aiohttp, websockets, psycopg 3, PostgreSQL, pytest/pytest-asyncio, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-20-phase3-flow-enrichment-design.md`

## Global Constraints

- Keep `TRADING_MODE=paper`; no private API, API key, order route, position route, executor, live path, or real order.
- Use only official public endpoints and public WebSocket channels. Do not mix Bitget v2 subscription schemas with v3 UTA schemas.
- Business logic consumes `CanonicalTrade` and immutable adapter capabilities, never raw exchange payloads or exchange-specific side meanings.
- Bybit public `S` is the only initial directional source. Bitget `buy/sell` and Hyperliquid public `side`/users remain raw evidence with `aggressor_side=UNKNOWN`.
- Unknown side contributes to total count/volume and never to buy/sell volume, delta, or CVD. Never infer a 50/50 split.
- All timestamps are timezone-aware UTC and retain exchange time, receive time, and process time.
- No unbounded queue, dedup map, raw-trade history, or per-trade INFO log. Preserve collector 256 MiB and engine 384 MiB limits.
- Migration 008 is additive and idempotent. Do not modify or delete Phase 1/2 history.
- Every task follows TDD: add the smallest failing test, run it and observe the expected failure, implement the minimum change, run targeted tests, then run the relevant regression subset and commit.
- Live contract probes are opt-in, public-only, and skipped with an explicit reason unless the required environment flag is set. A skipped live probe is never reported as passed.

## Review Focus

- Verify that source semantics, capability flags, identity keys, and persistence status cannot silently promote unknown sides into directional flow.
- Verify that reconnect/backfill and dedup share one identity path and that proven coverage limits produce `TRADE_GAP`/`PARTIAL`.
- Verify that cross-exchange counts distinguish volume-capable from directional-capable sources.
- Verify Stage1 category/direction remains unchanged by missing or partial flow context.
- Verify no new container, private import, order/position route, or live configuration path appears in the diff.

---

## Task 1 — Canonical trade contract and capability model

**Files:**

- Create `src/quant_phase3/__init__.py`.
- Create `src/quant_phase3/contracts.py`.
- Create `src/quant_phase3/capabilities.py`.
- Create `tests/test_phase3_contracts.py`.
- Create `tests/test_phase3_capabilities.py`.

**Interfaces:** `CanonicalTrade`, `TradeSourceCapabilities`, `TradeSide`, `FlowStatus`, `SideSource`, and validation/serialization helpers. The canonical trade must carry exchange, exchange symbol, optional canonical symbol, source trade ID, Decimal price/quantity/notional, raw side and raw-side semantics, aggressor side, timestamps, source channel, status, and raw payload/reference. `TradeSourceCapabilities` must expose all six approved capability flags.

- [ ] Write tests for UTC-only timestamps, positive price/quantity, nullable notional, stable string identity, status values including `PARTIAL`, and rejection of naive/invalid values.
- [ ] Write tests proving the initial capability matrix: Bybit directional/CVD true; Bitget and Hyperliquid public/non-directional only; no `INFERRED` capability.
- [ ] Run `python -m pytest tests/test_phase3_contracts.py tests/test_phase3_capabilities.py -q` and observe collection/import failures.
- [ ] Implement only the contract and immutable capability declarations.
- [ ] Run the targeted tests and commit `phase3: add canonical trade contracts`.

## Task 2 — Adapter protocol and common public parsing primitives

**Files:**

- Create `src/quant_phase3/adapters/__init__.py`.
- Create `src/quant_phase3/adapters/base.py`.
- Create `tests/test_phase3_adapter_base.py`.

**Interfaces:** A public-trade adapter protocol with `subscription(symbol)`, `parse_ws_message(payload, received_at)`, `parse_recent_trades(payload, received_at)`, `identity_key(trade)`, `capabilities`, and an optional bounded recovery method. Common helpers validate mappings, UTC milliseconds, Decimal fields, and public-only endpoint metadata.

- [ ] Test that adapters return canonical trades and never expose raw payloads to aggregation code.
- [ ] Test malformed payloads fail closed with a schema error instead of fabricating fields.
- [ ] Implement protocol and shared parsing helpers without an exchange branch in flow logic.
- [ ] Run targeted tests and commit `phase3: define public trade adapter protocol`.

## Task 3 — Bybit public trade adapter

**Files:**

- Create `src/quant_phase3/adapters/bybit.py`.
- Create `tests/test_phase3_bybit_adapter.py`.

**Interfaces:** `BybitPublicTradeAdapter` targets `wss://stream.bybit.com/v5/public/linear`, subscribes to `publicTrade.{symbol}`, parses `T,s,S,v,p,i,seq`, maps `S=Buy/Sell` to `BUY/SELL`, and uses `i`/`execId` as trade identity. REST recovery targets `GET /v5/market/recent-trade` with `category=linear`, symbol, and bounded limit.

- [ ] Add fixture tests for exact subscription payload and no Bitget/Classic fields.
- [ ] Add tests for side mapping, timestamp conversion, Decimal parsing, ID retention, sequence ordering, duplicate message handling input, and unknown/missing side rejection.
- [ ] Add an opt-in official REST/WS contract test gated by `PHASE3_LIVE_CONTRACT=1` and `PHASE3_BYBIT_SYMBOL`; it must validate endpoint, response schema, public WS upgrade/subscription, fields/types, timestamps, IDs, and ascending order without asserting undocumented semantics.
- [ ] Run fixture tests red, implement the adapter, run targeted tests, and commit `phase3: add bybit public trade adapter`.

## Task 4 — Bitget UTA v3 public trade adapter

**Files:**

- Create `src/quant_phase3/adapters/bitget.py`.
- Create `tests/test_phase3_bitget_adapter.py`.

**Interfaces:** `BitgetUTA3PublicTradeAdapter` targets `wss://ws.bitget.com/v3/ws/public`, subscription `{instType:"usdt-futures",topic:"publicTrade",symbol}` and REST `GET /api/v3/market/fills` with `category=USDT-FUTURES`, symbol, and `limit<=100`. The verified REST schema is `side`, `execId`, `price`, `size`, `ts`; the verified WebSocket schema is `S`, `i`, `p`, `v`, `T` with `L`/`isRPI` retained. It sets `aggressor_side=UNKNOWN`, `side_source=UNKNOWN`, and the approved unconfirmed semantic marker.

- [ ] Add tests proving the exact v3 UTA subscription has `topic=publicTrade`, not a v2 channel or `candle*` channel.
- [ ] Add REST/WS fixture tests for `execId`, `price`, `size`, `side`, `ts`, `isRPI`, raw payload retention, and unknown aggressor side.
- [ ] Add an opt-in official contract test gated by `PHASE3_LIVE_CONTRACT=1` and `PHASE3_BITGET_SYMBOL`; it must fail explicitly on endpoint/schema mismatch and must not fall back to another endpoint.
- [ ] Implement, run targeted tests, and commit `phase3: add bitget uta v3 public trade adapter`.

## Task 5 — Hyperliquid public trade adapter

**Files:**

- Create `src/quant_phase3/adapters/hyperliquid.py`.
- Create `tests/test_phase3_hyperliquid_adapter.py`.

**Interfaces:** `HyperliquidPublicTradeAdapter` targets `wss://api.hyperliquid.xyz/ws` with `{method:"subscribe",subscription:{type:"trades",coin}}`. It retains `coin,side,px,sz,hash,time,tid,users`, preserves buyer/seller raw information, sets unknown aggressor semantics, and forms the documented identity from block time + coin + tid. No private `WsFill`/`crossed` path is allowed.

- [ ] Add tests for exact subscription, `time` handling, raw users/B/A retention, composite identity, and refusal to claim identity coverage when block time is unavailable.
- [ ] Add opt-in public reconnect/missed-data contract fixtures for the snapshot acknowledgement and corresponding info recovery path; do not claim coverage without the documented fields.
- [ ] Implement, run targeted tests, and commit `phase3: add hyperliquid public trade adapter`.

## Task 6 — Bounded deduplication and event-time ordering

**Files:**

- Create `src/quant_phase3/dedup.py`.
- Create `src/quant_phase3/ordering.py`.
- Create `tests/test_phase3_dedup_ordering.py`.

**Interfaces:** `BoundedTradeDeduplicator` uses per-exchange TTL/LRU limits and a common adapter identity key. `EventTimeWindowRouter` accepts allowed lateness, emits on-time/late/gap events, and never silently rewrites a finalized immutable window.

- [ ] Test WS/REST duplicate collapse, bounded size, TTL expiry, per-exchange isolation, ascending event ordering, late event exclusion, and `PARTIAL`/`TRADE_GAP` marking.
- [ ] Implement bounded structures only; no historical raw trade list.
- [ ] Run targeted tests and commit `phase3: add bounded trade dedup and ordering`.

## Task 7 — Bounded queues and backpressure health

**Files:**

- Create `src/quant_phase3/queue.py`.
- Create `src/quant_phase3/health.py`.
- Create `tests/test_phase3_queue_health.py`.

**Interfaces:** `ExchangeTradeQueue`/`TradeQueueSet` provide bounded per-exchange or per-stream capacity, observable depth, dropped count, and `BACKPRESSURE_EVENT` records. Health is isolated per exchange and records connection, subscription, queue, recovery, and persistence status.

- [ ] Test saturation, explicit drop accounting, affected-window partial marking, exchange isolation, reconnect counters, and no unbounded growth.
- [ ] Implement bounded asyncio queues and compact health snapshots.
- [ ] Run targeted tests and commit `phase3: add bounded trade queues and health`.

## Task 8 — 1m event-time flow windows

**Files:**

- Create `src/quant_phase3/flow.py`.
- Create `tests/test_phase3_flow_windows.py`.

**Interfaces:** `TradeFlowWindowBuilder` aggregates total count/base/USDT-equivalent notional, buy/sell/unknown counts and volumes, average size, frequency, first/last exchange timestamps, source status, and processing timestamps. Unknown side is conserved, not split.

- [ ] Test exact minute bucketing, UTC boundaries, conservation `buy+sell+unknown=total`, directional fields only for capable sources, notional unavailable behavior, freshness, and partial windows.
- [ ] Implement 1m aggregation over canonical trades only.
- [ ] Run targeted tests and commit `phase3: add one minute flow aggregation`.

## Task 9 — 5m/15m/1H/4H rollups

**Files:**

- Create `src/quant_phase3/rollup.py`.
- Create `tests/test_phase3_rollups.py`.

**Interfaces:** `FlowRollupBuilder` combines finalized 1m windows into configured `5m`, `15m`, `1H`, and `4H` windows with explicit completeness/partial state and deterministic processing time.

- [ ] Test interval boundaries, missing-minute partial status, no double counting, restart replay idempotency, and no closed-window mutation by late trades.
- [ ] Implement rollups from persisted/accepted 1m aggregates, not raw trades.
- [ ] Run targeted tests and commit `phase3: add flow timeframe rollups`.

## Task 10 — Bybit directional delta and rolling CVD

**Files:**

- Create `src/quant_phase3/cvd.py`.
- Create `tests/test_phase3_cvd.py`.

**Interfaces:** `BybitCVDBuilder` consumes only Bybit windows whose capability/status proves reliable aggressor semantics. It calculates delta as buy base volume minus sell base volume and persists rolling CVD for 15m, 1H, 4H, and 24H. Bitget/Hyperliquid CVD is unavailable with an explicit reason.

- [ ] Test positive/negative/zero delta, unknown-side exclusion, partial/stale blocking, rolling horizon boundaries, restart hydration, and absence of CVD for non-directional sources.
- [ ] Implement deterministic rolling state serialization/hydration without inferred sides.
- [ ] Run targeted tests and commit `phase3: add bybit directional flow and cvd`.

## Task 11 — Migration 008 and flow repositories

**Files:**

- Create `migrations/008_phase3_flow.sql`.
- Create `src/quant_phase3/persistence.py`.
- Extend `tests/test_migrations.py`.
- Create `tests/test_phase3_persistence.py`.

**Interfaces:** Add idempotent tables `trade_flow_windows`, `cvd_snapshots`, `cross_exchange_flow_snapshots`, `trade_gap_events`, and `stage1_flow_enrichment`. Repository methods persist finalized windows, CVD, cross-exchange snapshots, gap/backpressure evidence, and context-only enrichment with conflict keys for restart idempotency.

- [ ] Add SQL contract tests for UTC `timestamptz`, explicit status/reason, required identity/idempotency keys, retention indexes, no `raw_trades`, and no order/position tables.
- [ ] Add isolated PostgreSQL tests, gated by `TEST_POSTGRES_DSN`, for empty-database migrations, repeat migrations, insert/read, duplicate upserts, UTC round trip, and retention deletion. Without the DSN, report the integration test as skipped rather than passing.
- [ ] Implement repository serialization with Decimal/Enum/dataclass-safe JSON and no raw exchange fields in business columns.
- [ ] Run SQL/isolated tests and commit `phase3: add flow persistence migration and repository`.

## Task 12 — Phase 3 configuration and retention

**Files:**

- Extend `src/quant_phase1/config.py` without changing paper/private API guards.
- Create `tests/test_phase3_config.py`.

**Interfaces:** Add validated configuration for `MAX_TRADE_STREAM_SYMBOLS`, subscription minimum/cooldown, `MAX_TRADE_QUEUE_SIZE`, dedup limits/TTL, lateness/grace, per-timeframe flow retention, CVD/gap retention, directional minimum, and Phase3 enablement. Defaults match the approved low-resource spec.

- [ ] Test defaults, positive bounds, invalid values, retention overrides, `TRADING_MODE=live` rejection, and private credential rejection.
- [ ] Implement backward-compatible settings fields and normalized timeframe names.
- [ ] Run Phase1 config regression plus targeted tests and commit `phase3: add flow runtime configuration`.

## Task 13 — Dynamic candidate subscriptions

**Files:**

- Create `src/quant_phase3/subscriptions.py`.
- Create `tests/test_phase3_subscriptions.py`.

**Interfaces:** `TradeSubscriptionManager` reads Phase 1 Universe plus Stage1 A/B candidates, caps the union at `MAX_TRADE_STREAM_SYMBOLS`, applies minimum dwell/cooldown, and emits exchange-specific subscribe/unsubscribe actions. BTC/ETH are fixtures, not a production fallback universe.

- [ ] Test deterministic ranking/capping, candidate disappearance, dwell time, cooldown, duplicate canonical mapping, and no subscription when the source capability is false.
- [ ] Implement lifecycle state only; no hard-coded fallback that masks missing persisted universe.
- [ ] Run targeted tests and commit `phase3: add dynamic trade subscriptions`.

## Task 14 — Collector trade runtime and reconnect/recovery

**Files:**

- Create `src/quant_phase3/runtime.py`.
- Extend `src/quant_phase1/entrypoints/collector.py` through a narrow Phase3 runtime hook.
- Extend `src/quant_phase1/entrypoints/engine.py` only for flow-context read/enrichment orchestration.
- Create `tests/test_phase3_runtime.py`.

**Interfaces:** `Phase3TradeRuntime` owns independent public WS tasks per exchange, subscriptions, queues, dedup, 1m/rollup flow builders, reconnect, bounded REST/WS recovery, persistence callbacks, and health callbacks. It must not own Phase1 ticker/kline state or Phase2 OI/funding semantics.

- [ ] Test start/stop, exchange isolation, reconnect and resubscribe, REST/WS recovery through the same dedup path, bounded backlog, and restart state hydration.
- [ ] Add opt-in live public contract tests for Bybit, Bitget, and Hyperliquid subscription/response/reconnect/backfill coverage; skipped sources remain non-directional/unavailable at runtime.
- [ ] Integrate the runtime into existing two-container entrypoints behind a Phase3 config flag, preserving Phase1/2 schedules and paper-only behavior.
- [ ] Run targeted runtime tests and commit `phase3: integrate bounded trade runtime`.

## Task 15 — Gap recovery and Phase 3 freshness

**Files:**

- Create `src/quant_phase3/recovery.py`.
- Create `src/quant_phase3/freshness.py`.
- Create `tests/test_phase3_recovery_freshness.py`.

**Interfaces:** Recovery records proven coverage range, attempts bounded public backfill, emits `TRADE_GAP` when coverage/identity is insufficient, and marks affected windows `PARTIAL`. Freshness evaluates last trade, last complete window, source/gap/queue state, and configurable grace; no signal may consume stale/partial flow.

- [ ] Test fresh low-liquidity symbols, stale thresholds, disconnect within/over recovery coverage, missing Hyperliquid composite identity, DB outage bounded buffer, and stale/partial blocking.
- [ ] Implement explicit statuses/reasons and persistence of gap evidence.
- [ ] Run targeted tests and commit `phase3: add trade gap recovery and freshness`.

## Task 16 — Cross-exchange flow and capability-aware aggregation

**Files:**

- Create `src/quant_phase3/cross_exchange.py`.
- Create `tests/test_phase3_cross_exchange.py`.

**Interfaces:** `CrossExchangeFlowSnapshot` separately reports `volume_exchange_count` and `directional_exchange_count`, aggregates total volume from all fresh sources, and aggregates directional delta only from fresh complete directional sources. Below the configured minimum it emits `INSUFFICIENT_DIRECTIONAL_SOURCES`.

- [ ] Test all three sources contributing volume, only Bybit contributing directional flow, no unknown-side promotion, minimum-source blocking, and per-exchange outage isolation.
- [ ] Implement deterministic weighted/median directional aggregation only from eligible sources.
- [ ] Run targeted tests and commit `phase3: add capability-aware cross exchange flow`.

## Task 17 — Stage1/Phase2 context-only enrichment

**Files:**

- Extend `src/quant_phase3/persistence.py` for enrichment reads/writes.
- Extend `src/quant_phase2/enrichment.py` only at the context boundary if required.
- Extend `src/quant_phase1/entrypoints/engine.py` for the separate flow enrichment write.
- Create `tests/test_phase3_enrichment.py`.

**Interfaces:** Enrichment exposes `delta_1m`, `delta_5m`, `delta_15m`, `delta_ratio_5m`, `delta_ratio_15m`, Bybit CVD horizons, cross-exchange context, divergence/status/reason. Missing data is `FLOW_NOT_AVAILABLE` or a specific gap/capability reason. It cannot alter Stage1 category, direction, signal, order, or risk.

- [ ] Test available, unavailable, stale, partial, and mixed-source rows; assert existing Stage1 results are byte-for-byte category/direction stable.
- [ ] Implement separate `stage1_flow_enrichment` persistence and never write a signal/order/position.
- [ ] Run targeted tests plus Phase1/Phase2 enrichment regressions and commit `phase3: add context-only stage1 flow enrichment`.

## Task 18 — Health, logs, Docker, and resource checks

**Files:**

- Extend `src/quant_phase3/health.py`/runtime logging as needed.
- Extend `docker-compose.yml` and `docker-compose.server.yml` only for existing service environment/config mounts; do not add services or raise limits.
- Create `tests/test_phase3_safety_resources.py`.

- [ ] Test summary-only INFO events, DEBUG-only per-trade logs, health heartbeat fields, memory-limit declarations, two-container topology, paper mode, and no private/order/live symbols/imports.
- [ ] Implement resource warnings above 220 MiB collector usage and bounded counters without new infrastructure.
- [ ] Run static safety scans and compose config validation, then commit `phase3: preserve bounded paper-only deployment`.

## Task 19 — Full local regression and official contract-test gate

**Files:**

- Extend `tests/test_phase3_regression.py`.
- Extend `README.md` or create `docs/phase3-runtime-acceptance.md` with exact commands and skip semantics.

- [ ] Run all unit/contract tests, migration SQL tests, safety scans, and existing Phase1/Phase2 suite.
- [ ] Run official public REST/WS contract tests with explicit environment flags where network access is available; record endpoint, subscription, schema, timestamps, IDs, ordering, reconnect, 429/backoff, and recovery results. Do not convert skipped probes into passes.
- [ ] Verify no private API import, order route, position API, live executor, or real-order path.
- [ ] Commit `phase3: record local regression and contract test gate`.

## Task 20 — Singapore runtime acceptance and completion report

**Files:**

- Create/update `PHASE_3_COMPLETION_REPORT.md`.
- Add only required tests/docs/report changes; never commit credentials, `.env`, passwords, tokens, or log dumps.

- [ ] Before claiming completion, run `superpowers:verification-before-completion`: inspect diff, run full regression, validate branch/HEAD/status, and capture evidence for every acceptance claim.
- [ ] On the Singapore candidate/runtime host, verify public Bitget/Bybit/Hyperliquid REST/WS connectivity, collector/engine/PostgreSQL runtime, migrations, flow counts/windows, Bybit CVD, unknown-side behavior, gaps/freshness, dynamic subscriptions, restart/reconnect, resource usage, database/log growth, and safety scan. Keep `TRADING_MODE=paper`.
- [ ] Separate measured sample from projected retention/resource estimates; list every skipped test and blocker.
- [ ] Only output `PHASE_3_ACCEPTED` if all required runtime evidence passes. Otherwise output `PHASE_3_NOT_ACCEPTED` with blockers and stop; do not enter Phase 4.

## Commit and verification cadence

After each task:

1. `python -m pytest <targeted tests> -q`
2. `python -m pytest <affected regression subset> -q`
3. `git diff --check`
4. `git status --short`
5. Commit only the task's files with the commit message specified above.

Before final acceptance:

1. `python -m pytest -q`
2. `docker compose config` (where Docker is available)
3. Static scans for private API/order/live executor paths
4. `git diff --check`
5. `git status --short --branch`
6. Review `PHASE_3_COMPLETION_REPORT.md` against the approved spec and this plan.
