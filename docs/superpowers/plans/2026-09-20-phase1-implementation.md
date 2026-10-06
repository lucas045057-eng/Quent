# Phase 1 Foundation and Market Data Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the paper-only Phase 1 foundation that ingests real Bitget UTA v3 public market data, normalizes it into canonical contracts, computes local indicators and Stage1 results, persists safe historical data, and exposes health without any private API or order path.

**Architecture:** A small Python 3.12 application is split into contracts, configuration, adapters, market calculations, persistence, health, and service entrypoints. The collector owns Bitget REST/WS I/O and emits canonical observations; the engine owns closed-bar calculations, freshness, Universe, Stage1, and persistence. PostgreSQL is accessed through migration SQL and a narrow repository layer. Docker runs collector and engine only.

**Tech Stack:** Python 3.12, asyncio, `aiohttp`, `websockets`, `pytest`, PostgreSQL-compatible SQL migrations, Docker Compose, standard-library dataclasses/Decimal/zoneinfo.

**Spec:** `outputs/docs/pre-implementation-review.md`, `outputs/docs/architecture.md`, `outputs/docs/data-sources.md`, `outputs/docs/database-schema.md`, `outputs/docs/deployment.md`, `outputs/docs/phases.md`.

## Global Constraints

- `TRADING_MODE=paper` is the only Phase 1 runtime mode.
- Bitget Phase 1 is UTA v3 public REST/WS only; no API key, private API, order, position, or executor import path.
- REST instruments is fixed only after the real contract test confirms `/api/v3/market/instruments`; no silent fallback to `/api/v3/public/instruments`.
- v3 Kline subscription is `topic="kline"` with `interval="5m|15m|1H|4H"`; no v2 subscription schema.
- All database timestamps are UTC and keep `exchange_timestamp`, `fetched_at`, and `processed_at` separately.
- Kline freshness is interval-aware expected-closed-bar comparison with configurable ingestion grace, never simple candle age.
- Kline retention is configured by `KLINE_RETENTION_5M_DAYS`, `KLINE_RETENTION_15M_DAYS`, `KLINE_RETENTION_1H_DAYS`, and `KLINE_RETENTION_4H_DAYS`.
- OI/Funding raw fields may be retained but remain semantic `NOT_AVAILABLE`; no advanced-data mock is allowed.
- Stage1 cannot consume `STALE`, `ERROR`, or `NOT_AVAILABLE` required inputs.

## Review Focus

- API path/schema drift: live v3 instruments contract test must fail closed rather than fallback.
- v3 WS subscription shape: `topic` and `interval` must be sent exactly and reconnect must resubscribe.
- Kline boundary freshness: 10:30 UTC must treat 09:00–10:00 as the latest 1H closed bar.
- Raw advanced fields: `openInterest` and `fundingRate` must never become Stage1 inputs.
- Safety/resource boundary: no private/order imports; bounded REST/WS concurrency and retention configuration.

### Task 1: Project foundation and safety gates

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`
- Create: `src/quant_phase1/__init__.py`, `src/quant_phase1/entrypoints/collector.py`, `src/quant_phase1/entrypoints/engine.py`
- Create: `tests/test_project_safety.py`

**Interfaces:** entrypoints expose importable `main()` functions; safety tests inspect the source tree and configuration.

- [ ] Write failing tests for paper default, forbidden private/order/live modules, and package importability.
- [ ] Run `pytest tests/test_project_safety.py -q` and confirm failure because project files do not exist.
- [ ] Add the minimal project metadata, entrypoint stubs, safety configuration, and dependency declarations.
- [ ] Run the focused test, then the full test suite.

### Task 2: Config, UTC clock, and structured logging

**Files:**
- Create: `src/quant_phase1/config.py`, `src/quant_phase1/time.py`, `src/quant_phase1/logging.py`
- Create: `tests/test_config.py`, `tests/test_time.py`, `tests/test_logging.py`

**Interfaces:** `Settings.from_env()`, `UtcClock.now()`, `configure_logging()`. Settings include all four Kline retention values and four ingestion grace values.

- [ ] Write failing tests for defaults, paper-only validation, private-key rejection, UTC-aware timestamps, and secret redaction.
- [ ] Run focused tests and verify expected failures.
- [ ] Implement minimal immutable settings and UTC helpers.
- [ ] Run focused and full tests.

### Task 3: Canonical Data Contract

**Files:**
- Create: `src/quant_phase1/contracts.py`
- Create: `tests/test_contracts.py`

**Interfaces:** `Observation`, `Candle`, `Ticker`, `Instrument`, `DataStatus`, and `RawReference`. All adapters map into these types.

- [ ] Write failing tests for required fields, Decimal normalization, UTC timestamps, four statuses, raw preservation, and semantic `NOT_AVAILABLE` for OI/Funding.
- [ ] Run focused tests and verify failure.
- [ ] Implement dataclasses and validation without exchange-specific names in downstream types.
- [ ] Run focused and full tests.

### Task 4: REST contract tests and v3 adapter

**Files:**
- Create: `src/quant_phase1/adapters/bitget_v3/rest.py`, `src/quant_phase1/adapters/bitget_v3/parsers.py`, `src/quant_phase1/adapters/bitget_v3/rate_limit.py`
- Create: `tests/contract/test_bitget_rest_live.py`, `tests/contract/test_bitget_rest_parsers.py`, `tests/test_rate_limit.py`

**Interfaces:** `BitgetV3UtaRestClient.get_instruments()`, `.get_tickers()`, `.get_candles(symbol, interval, limit)`. The live contract test calls the current public endpoint and fails closed on non-00000 or schema drift.

- [ ] Write parser tests and a live smoke contract test for `/api/v3/market/instruments`, `/api/v3/market/tickers`, and `/api/v3/market/candles`.
- [ ] Run parser tests red; run the live test separately and record the actual response schema.
- [ ] Implement the adapter with explicit endpoint constants, 20 req/s/IP token bucket, 429 exponential backoff, timeout, and no endpoint fallback.
- [ ] Run all REST tests and confirm real instruments endpoint success before accepting the adapter.

### Task 5: WebSocket contract tests and v3 adapter

**Files:**
- Create: `src/quant_phase1/adapters/bitget_v3/websocket.py`
- Create: `tests/contract/test_bitget_ws_live.py`, `tests/test_bitget_ws_parser.py`, `tests/test_reconnect.py`

**Interfaces:** `BitgetV3UtaWebSocket.subscribe_ticker(symbol)`, `.subscribe_kline(symbol, interval)`, `.run()`, `.reconnect()`. Subscription payloads use v3 `topic` and `interval` exactly.

- [ ] Write parser tests proving the v3 ticker and kline payloads are accepted and v2 channel payloads are rejected.
- [ ] Run focused tests red.
- [ ] Implement connect, ping every 30 seconds, subscribe ack validation, snapshot parsing, reconnect backoff, and duplicate-safe resubscription.
- [ ] Run the live WS contract test against `wss://ws.bitget.com/v3/ws/public`, then run focused and full tests.

### Task 6: Freshness and closed-bar logic

**Files:**
- Create: `src/quant_phase1/freshness.py`, `src/quant_phase1/market/closed_bars.py`
- Create: `tests/test_freshness.py`, `tests/test_closed_bars.py`

**Interfaces:** `expected_latest_closed_open(now, interval)`, `evaluate_kline_freshness(now, interval, latest_closed_open, grace)`, `accept_closed_candle(candle, now)`. Ticker freshness remains age-based.

- [ ] Write failing fixed-clock tests for interval boundaries, grace periods, missing theoretical latest bar, future bars, and the 10:30 UTC 1H example.
- [ ] Run focused tests red.
- [ ] Implement interval-aware comparison and closed-bar filtering.
- [ ] Run focused and full tests.

### Task 7: Indicators, structure, Universe, and Stage1 Basic

**Files:**
- Create: `src/quant_phase1/market/indicators.py`, `src/quant_phase1/market/structure.py`, `src/quant_phase1/universe.py`, `src/quant_phase1/stage1.py`
- Create: `tests/test_indicators.py`, `tests/test_structure.py`, `tests/test_universe.py`, `tests/test_stage1.py`

**Interfaces:** pure functions `compute_indicators()`, `classify_structure()`, `select_universe()`, and `evaluate_stage1()`. Inputs are canonical objects only.

- [ ] Write deterministic tests for EMA/ATR/spread/turnover, HH/HL/LH/LL, online USDT perpetual filtering, Top 200 ordering, missing/stale inputs, and no-advanced-data behavior.
- [ ] Run focused tests red.
- [ ] Implement only local deterministic calculations and Stage1 A/B/C/D basic outcomes.
- [ ] Run focused and full tests.

### Task 8: PostgreSQL migrations and persistence

**Files:**
- Create: `migrations/001_phase1_core.sql`, `src/quant_phase1/db.py`, `src/quant_phase1/repositories.py`
- Create: `tests/test_migrations.py`, `tests/test_persistence.py`

**Interfaces:** migration runner, `ObservationRepository`, `KlineRepository`, `Stage1Repository`, and `HealthRepository`. SQL uses UTC `TIMESTAMPTZ`, unique symbol/interval/open-time keys, raw references, and configured retention cleanup.

- [ ] Write failing SQL-structure and repository idempotency tests.
- [ ] Run tests red.
- [ ] Implement migration SQL and a narrow PostgreSQL repository with upsert semantics.
- [ ] Run tests against the available PostgreSQL service or a disposable local PostgreSQL container; verify restart idempotency and retention predicates.

### Task 9: Health, Docker, and resource tests

**Files:**
- Create: `src/quant_phase1/health.py`, `Dockerfile`, `docker-compose.yml`, `scripts/healthcheck.py`
- Create: `tests/test_health.py`, `tests/test_resource_limits.py`

- [ ] Write failing tests for component health, stale collector degradation, memory-limit declarations, and absence of executor/private services.
- [ ] Run tests red.
- [ ] Implement health aggregation, Docker collector/engine services, 256MB collector and 384MB engine limits, log rotation, and Postgres dependency.
- [ ] Run health, resource, and compose validation tests.

### Task 10: Full Phase 1 regression and completion report

**Files:**
- Modify: `outputs/docs/pre-implementation-review.md`, `outputs/docs/deployment.md`, `outputs/docs/database-schema.md`, `outputs/docs/phases.md`
- Create: `PHASE_1_COMPLETION_REPORT.md`

- [ ] Run the complete test suite, live REST/WS contract tests, Docker smoke test, PostgreSQL retention test, and resource measurements.
- [ ] Inspect Git diff for private/order/live code paths and confirm no Phase 2 modules were added.
- [ ] Record actual endpoint, test count/results, available and unavailable data, tables, services, resource limits, memory, database growth, known issues, branch, commit, and working-tree status.
- [ ] Stop and wait for human acceptance.
