# Phase 2 Derivatives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add public, exchange-neutral Open Interest and Funding Rate enrichment for the verified Phase 2 source set without changing Phase 1 Stage1 behavior or adding any trading capability.

**Architecture:** Exchange adapters parse only their own official REST/WebSocket payloads into canonical OI/Funding observations. A normalization layer converts only when contract metadata makes the unit explicit. A cross-exchange layer retains every source, computes disagreement and weighted funding only from fresh normalized observations, and exposes an enrichment context to the existing Stage1 result without changing its classification rules.

**Tech Stack:** Python 3.11+, dataclasses, Decimal, aiohttp, psycopg, PostgreSQL migrations, pytest, Docker Compose.

**Spec:** `docs/phase2-data-source-audit.md` and the user-provided Phase 2 scope.

## Global Constraints

- `TRADING_MODE=paper` remains mandatory.
- No private API, API key, order, position, live executor, or real order route.
- Business code consumes canonical contracts only; raw exchange fields stay inside adapters and raw payload storage.
- Initial real source set is Bitget UTA v3 + Bybit V5 linear + Hyperliquid first perp DEX.
- Binance and OKX remain contract-test gated until ambiguous units/fields pass live probes.
- MEXC is `NOT_AVAILABLE` and is not force-enabled.
- Missing values remain `NOT_AVAILABLE`; never substitute zero.
- `exchange_timestamp`, `fetched_at`, and `processed_at` remain distinct UTC timestamps.
- Phase 1 Stage1 runs unchanged; derivative data is enrichment context only.

## Review Focus

- A raw OI value with an unknown unit must not become USD notional: normalization tests cover missing multiplier/price/unit.
- A non-8-hour funding interval must not be compared as an 8-hour raw rate: interval and `NOT_COMPARABLE` tests cover this.
- One exchange outage must not invalidate other exchanges: failure-isolation tests cover independent errors and minimum-source status.
- Missing sources must not be inserted as zero: cross-exchange snapshot tests cover sparse input and divergence.
- Historical windows must report actual timestamps: OI change tests cover 5m/15m/1H/4H/24H and actual-window metadata.

---

### Task 1: Canonical contracts and symbol registry

**Files:**
- Create: `src/quant_phase2/contracts.py`
- Create: `src/quant_phase2/symbols.py`
- Create: `tests/test_phase2_contracts.py`

**Interfaces:**
- Produces `OIObservation`, `FundingObservation`, `InstrumentMetadata`, `CanonicalSymbol`, `CrossExchangeSnapshot`, and `Status` values for all later tasks.

- [ ] Define frozen UTC-aware contracts with raw and normalized fields, source endpoint, raw reference, status, and timestamp triplet.
- [ ] Define explicit exchange-symbol mappings; reject string-replace-only mappings and non-crypto/RWA instruments.
- [ ] Add failing tests for raw preservation, UTC validation, missing normalization, and explicit symbol mapping.
- [ ] Run `pytest tests/test_phase2_contracts.py -q` and observe the intended failures before implementation.
- [ ] Implement the minimal contracts and mappings.
- [ ] Run the focused tests and the Phase 1 contract tests.

### Task 2: Normalization and funding semantics

**Files:**
- Create: `src/quant_phase2/normalization.py`
- Create: `src/quant_phase2/funding.py`
- Create: `tests/test_phase2_normalization.py`
- Create: `tests/test_phase2_funding.py`

**Interfaces:**
- Consumes Task 1 contracts and instrument metadata.
- Produces base/quote/USD normalization, normalized 8h funding where comparable, funding classification, and OI change results.

- [ ] Add red tests for contract-count to base, base to USD, raw preservation, missing inputs, dynamic interval, predicted versus realized funding, and `NOT_COMPARABLE`.
- [ ] Implement exchange-specific method names in the contract, never a hidden universal multiplier.
- [ ] Implement OI changes for 5m/15m/1H/4H/24H with requested and actual windows.
- [ ] Run focused tests and then the full local suite.

### Task 3: Official adapters and contract probes

**Files:**
- Create: `src/quant_phase2/adapters/base.py`
- Create: `src/quant_phase2/adapters/bitget.py`
- Create: `src/quant_phase2/adapters/bybit.py`
- Create: `src/quant_phase2/adapters/hyperliquid.py`
- Create: `src/quant_phase2/adapters/binance.py`
- Create: `src/quant_phase2/adapters/okx.py`
- Create: `tests/contract/test_phase2_live.py`
- Create: `tests/test_phase2_adapters.py`

**Interfaces:**
- Each adapter exposes `fetch_instruments()`, `fetch_oi()`, `fetch_funding()`, and `fetch_history()` into canonical contracts only.
- Binance/OKX adapters return `NEED_VERIFICATION`/`NOT_AVAILABLE` rather than guessing when the live schema is ambiguous.

- [ ] Add fixture tests for each verified schema and explicit bad-schema/error mapping.
- [ ] Add opt-in live probes with no credentials; skip only when `PHASE2_LIVE_CONTRACT=1` is absent.
- [ ] Verify Bitget UTA v3, Bybit V5 linear, and Hyperliquid official public responses.
- [ ] Record Binance/OKX field gates and MEXC unavailability in the audit/report.

### Task 4: Cross-exchange snapshot and failure isolation

**Files:**
- Create: `src/quant_phase2/cross_exchange.py`
- Create: `tests/test_phase2_cross_exchange.py`

**Interfaces:**
- Consumes canonical observations from Task 3.
- Produces sparse `CrossExchangeSnapshot`, source disagreement flags, source counts, OI-weighted funding, median/range/dispersion, and minimum-source statuses.

- [ ] Add red tests for missing/stale/error source, divergence, sparse snapshots, and weighted funding exclusions.
- [ ] Implement per-exchange isolation and configurable minimum source counts.
- [ ] Keep all source observations unchanged under the aggregate snapshot.

### Task 5: PostgreSQL persistence and Phase 1 enrichment boundary

**Files:**
- Create: `migrations/004_phase2_derivatives.sql`
- Modify: `src/quant_phase1/repositories.py`
- Create: `src/quant_phase2/persistence.py`
- Create: `src/quant_phase2/enrichment.py`
- Create: `tests/test_phase2_persistence.py`

**Interfaces:**
- Adds `exchange_instruments`, `open_interest`, `funding_rates`, `cross_exchange_derivative_snapshots`, and enrichment references without altering Phase 1 historical data.
- Enrichment consumes a Phase 1 candidate and returns context only; it never changes Stage1 A/B/C/D rules.

- [ ] Add migration-from-empty/repeat and idempotent write/read tests.
- [ ] Persist source, endpoint, raw payload/reference, normalized values, status, and timestamp triplet.
- [ ] Add configurable sampling/dedup/retention controls and measured row counts.

### Task 6: Runtime, resources, safety, and final acceptance

**Files:**
- Modify: `src/quant_phase1/entrypoints/collector.py`
- Modify: `src/quant_phase1/entrypoints/engine.py`
- Modify: `docker-compose.server.yml`
- Create: `tests/test_phase2_runtime.py`
- Create: `PHASE_2_COMPLETION_REPORT.md`

- [ ] Add bounded derivative polling and per-exchange failure state without Kafka, Redis, or extra PostgreSQL.
- [ ] Run Phase 1 regression, Phase 2 tests, public live contract probes, restart/recovery, 429/timeout/bad-schema tests, and safety scans.
- [ ] Measure row counts, DB growth, RAM, CPU, and health without claiming long-term projections as measured facts.
- [ ] Commit on `phase2`, keep `phase1` unchanged, and confirm clean status.

