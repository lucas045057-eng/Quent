# Phase 4 Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public-only Liquidation, Long/Short, and Basis context to the accepted Phase 3 system while preserving Phase 1–3 semantics and paper-only safety.

**Architecture:** Add a focused `quant_phase4` package inside the existing collector and engine processes. Exchange adapters parse only their own official REST/WebSocket schemas and emit canonical Phase 4 objects; aggregation, cross-exchange comparison, persistence, and Stage1 enrichment consume only those canonical objects. Add one additive migration after `008`; do not add a service or modify prior migrations.

**Tech Stack:** Python 3.12, dataclasses, `Decimal`, `aiohttp`, `websockets`, `psycopg`, PostgreSQL `TIMESTAMPTZ`, pytest, pytest-asyncio, existing Compose deployment.

**Spec:** `PHASE_4_DESIGN_SPEC.md`

## Global Constraints

- Base HEAD is `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`; work only on branch `phase4`.
- `TRADING_MODE=paper` is mandatory.
- Only official public REST/WebSocket APIs are allowed; no API key, secret, passphrase, private API, order route, position route, or live executor.
- Bitget Classic v2 Long/Short must use a separate adapter from the existing Bitget UTA v3 adapters.
- Hyperliquid Liquidation and Long/Short remain `NOT_AVAILABLE` with reason `UNCONFIRMED_PUBLIC_SOURCE`.
- `MARK_INDEX` and `MARK_ORACLE` are separate basis types and are never averaged together.
- Phase 4 statuses are `AVAILABLE`, `STALE`, `NOT_AVAILABLE`, and `ERROR`; semantic uncertainty is a reason code.
- `RAW_LIQUIDATION_WAREHOUSE=NONE`; normalized liquidation records may be retained only for the configured 24-hour bounded window.
- Migrations `001–008` must not be changed; add `009_phase4_metrics.sql` only.
- The existing two application containers and resource ceilings remain: PostgreSQL 768 MiB, collector 256 MiB, engine 384 MiB.
- Phase 4 context never changes Stage1 eligibility, classification, or emits a trading decision.
- No Jakarta deployment or production migration is part of this plan; the code-complete boundary is `PHASE4_CODE_COMPLETE_RUNTIME_PENDING`.

## Review Focus

1. Bitget liquidation is server-side maximum-per-second aggregation, not an exhaustive event feed; the parser and aggregation tests must preserve `AGGREGATED_MAX_PER_SECOND` and `PARTIAL_AGGREGATED`.
2. Bitget v2 Long/Short and Bybit account-ratio values must retain account-holder semantics and must not be interpreted as position size or taker volume.
3. Basis must reject zero reference prices and timestamp skew above the configured limit, while accepting the exact boundary.
4. Restart and non-backfillable WebSocket gaps must yield explicit partial/unavailable context without duplicating persisted windows.
5. Raw payloads must be stripped at the persistence boundary and retention must delete bounded Phase 4 data without touching Phase 1–3 tables.

## File Map

### New files

- `src/quant_phase4/__init__.py`: package boundary and public exports.
- `src/quant_phase4/contracts.py`: enums and validated canonical dataclasses.
- `src/quant_phase4/adapters/base.py`: public adapter protocols and strict parsing helpers.
- `src/quant_phase4/adapters/bitget_uta_v3.py`: Bitget UTA v3 liquidation and ticker parsing.
- `src/quant_phase4/adapters/bitget_classic_v2.py`: Bitget Classic v2 Long/Short REST parsing only.
- `src/quant_phase4/adapters/bybit_v5.py`: Bybit V5 liquidation, account-ratio, and ticker parsing.
- `src/quant_phase4/adapters/hyperliquid_public.py`: Hyperliquid basis parsing plus explicit unavailable metrics.
- `src/quant_phase4/basis.py`: the single centralized basis formula and skew validation.
- `src/quant_phase4/long_short.py`: account-ratio semantic validation and freshness handling.
- `src/quant_phase4/liquidation.py`: source identity, bounded deduplication, queue, and event-to-window aggregation.
- `src/quant_phase4/aggregation.py`: 1m liquidation windows and 5m/15m/1H/4H rollups.
- `src/quant_phase4/cross_exchange.py`: comparable-source grouping and coverage-aware context.
- `src/quant_phase4/persistence.py`: PostgreSQL writes, reads, idempotency, and retention.
- `src/quant_phase4/health.py`: component-level health transitions for liquidation, long-short, and basis.
- `src/quant_phase4/enrichment.py`: context-only Stage1 Phase 4 enrichment.
- `src/quant_phase4/runtime.py`: bounded Phase 4 runtime orchestration used by the existing collector.
- `migrations/009_phase4_metrics.sql`: additive Phase 4 schema.
- `tests/test_phase4_contracts.py`: canonical contract tests.
- `tests/test_phase4_basis.py`: formula, skew, and basis-type tests.
- `tests/test_phase4_long_short.py`: metric semantics and freshness tests.
- `tests/test_phase4_liquidation.py`: parser, side, units, dedup, gap, queue, and aggregation tests.
- `tests/test_phase4_cross_exchange.py`: comparable-source and coverage tests.
- `tests/test_phase4_persistence.py`: repository SQL and retention tests.
- `tests/test_phase4_enrichment.py`: Stage1 candidate-preservation tests.
- `tests/test_phase4_runtime.py`: restart, reconnect, backpressure, and status tests.
- `tests/test_phase4_migrations.py`: migration text and PostgreSQL integration tests.
- `tests/test_phase4_safety_resources.py`: paper-only, public-only, service-count, and memory-bound tests.
- `tests/contract/test_phase4_public_live.py`: opt-in official REST/WebSocket contract probes.
- `PHASE_4_COMPLETION_REPORT.md`: code-completion report, explicitly not runtime acceptance.

### Modified files

- `src/quant_phase1/config.py`: Phase 4 flags, endpoints, skew, queue, scheduler, and retention settings.
- `src/quant_phase1/entrypoints/collector.py`: instantiate and schedule the bounded Phase 4 runtime without adding a process.
- `src/quant_phase1/entrypoints/engine.py`: load and persist Phase 4 Stage1 context only.
- `.env.example`: paper-safe Phase 4 configuration names without credentials.

## Interfaces Between Tasks

The following signatures are the stable boundaries used by later tasks:

```python
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

class PublicLiquidationAdapter(Protocol):
    exchange: str
    source_channel: str
    def parse_ws_message(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Sequence[CanonicalLiquidation]:
        raise NotImplementedError

class LongShortAdapter(Protocol):
    exchange: str
    metric_type: LongShortMetricType
    async def fetch(
        self, symbol: str, period: str, *, received_at: datetime
    ) -> LongShortObservation:
        raise NotImplementedError

class BasisAdapter(Protocol):
    exchange: str
    async def fetch(self, symbol: str, *, received_at: datetime) -> BasisObservation:
        raise NotImplementedError

class Phase4Repository:
    def insert_liquidation_events(self, events: Iterable[CanonicalLiquidation]) -> int:
        raise NotImplementedError
    def insert_liquidation_windows(self, windows: Iterable[LiquidationWindow]) -> int:
        raise NotImplementedError
    def insert_long_short(self, rows: Iterable[LongShortObservation]) -> int:
        raise NotImplementedError
    def insert_basis(self, rows: Iterable[BasisObservation]) -> int:
        raise NotImplementedError
    def insert_cross_exchange(self, rows: Iterable[CrossExchangePhase4Context]) -> int:
        raise NotImplementedError
    def insert_stage1_enrichment(self, run_id: int, rows: Iterable[Stage1Phase4Enrichment]) -> int:
        raise NotImplementedError
    def load_latest_context(self, canonical_symbol: str) -> Phase4Context:
        raise NotImplementedError
    def cleanup(self, retention: Phase4Retention) -> None:
        raise NotImplementedError
```

## Implementation Tasks

### Task 1: Canonical contracts and strict validation

**Files:**
- Create: `src/quant_phase4/__init__.py`
- Create: `src/quant_phase4/contracts.py`
- Test: `tests/test_phase4_contracts.py`

**Interfaces:**
- Consumes: `quant_phase1.time.ensure_utc` and the existing Phase 2 `DataStatus` semantics.
- Produces: `DataStatus`, `LiquidationSide`, `QuantityUnit`, `SourceGranularity`, `CoverageSemantics`, `LongShortMetricType`, `BasisType`, `CanonicalLiquidation`, `LongShortObservation`, and `BasisObservation` types. Task 5 owns `LiquidationWindow`; Task 7 owns `CrossExchangePhase4Context`, `Phase4Context`, and `Stage1Phase4Enrichment`.

- [ ] **Step 1: Write failing contract tests.** Cover UTC enforcement for every timestamp, positive prices, nullable normalized quantity/notional, four statuses, explicit source fields, unknown side, `ACCOUNT_HOLDER_RATIO`, and `MARK_INDEX` versus `MARK_ORACLE`.

```python
def test_liquidation_requires_utc_and_preserves_coverage_semantics():
    row = CanonicalLiquidation(
        exchange="bitget",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        event_timestamp=UTC_NOW,
        received_at=UTC_NOW,
        processed_at=UTC_NOW,
        side=LiquidationSide.LONG,
        raw_side="buy",
        raw_quantity=Decimal("10"),
        quantity_unit=QuantityUnit.QUOTE_COIN,
        source="bitget_uta_v3_liquidation",
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE,
        reason=None,
    )
    assert row.source_granularity is SourceGranularity.AGGREGATED_MAX_PER_SECOND
    assert row.coverage_semantics is CoverageSemantics.PARTIAL_AGGREGATED
```

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_contracts.py -q`

Expected: FAIL because `quant_phase4.contracts` does not exist.

- [ ] **Step 3: Implement the minimal typed dataclasses.** Reject naive timestamps, reject negative values, require `raw_quantity`, `source`, `reason` for unavailable rows, and prevent normalized values on `NOT_AVAILABLE` or `ERROR` rows.

- [ ] **Step 4: Run the focused tests.**

Run: `python -m pytest tests/test_phase4_contracts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the contract boundary.**

```text
git add src/quant_phase4 tests/test_phase4_contracts.py
git commit -m "phase4: add canonical metric contracts"
```

### Task 2: Configuration and additive migration 009

**Files:**
- Modify: `src/quant_phase1/config.py`
- Modify: `.env.example`
- Create: `migrations/009_phase4_metrics.sql`
- Create: `tests/test_phase4_migrations.py`
- Create: `tests/test_phase4_config.py`

**Interfaces:**
- Consumes: Task 1 status strings and existing migration runner conventions.
- Produces: `Settings.phase4_enabled`, Phase 4 endpoint/rate settings, `phase4_*_retention_days`, `phase4_liquidation_event_retention_hours`, `phase4_max_basis_timestamp_skew`, `phase4_queue_capacity`, and the six Phase 4 tables.

- [ ] **Step 1: Write failing configuration and migration tests.** Assert paper-only defaults, positive validation, migration `009` only creates Phase 4 tables, all timestamps are `TIMESTAMPTZ`, all required unique constraints exist, and `001–008` are untouched.

```python
def test_phase4_defaults_are_bounded_and_paper_only():
    settings = Settings.from_env({"TRADING_MODE": "paper"})
    assert settings.phase4_enabled is False
    assert settings.phase4_liquidation_event_retention_hours == 24
    assert settings.phase4_max_basis_timestamp_skew > 0
```

- [ ] **Step 2: Run the tests and verify failure.**

Run: `python -m pytest tests/test_phase4_migrations.py tests/test_phase4_config.py -q`

Expected: FAIL because the Phase 4 settings and migration do not exist.

- [ ] **Step 3: Add configuration parsing.** Add the defaults from the spec, reject non-positive values, preserve existing `TRADING_MODE` and private-credential rejection, and do not change Phase 1–3 defaults.

- [ ] **Step 4: Add `009_phase4_metrics.sql`.** Create normalized short-retention liquidation events, liquidation windows, long-short observations, basis snapshots, cross-exchange contexts, and Stage1 Phase 4 enrichment with timestamp indexes and logical unique constraints. Do not store an unbounded raw payload column.

- [ ] **Step 5: Run focused tests.**

Run: `python -m pytest tests/test_phase4_migrations.py tests/test_phase4_config.py -q`

Expected: PASS; PostgreSQL-gated checks are reported as skips only when `TEST_POSTGRES_DSN` is absent.

- [ ] **Step 6: Commit configuration and schema.**

```text
git add src/quant_phase1/config.py .env.example migrations/009_phase4_metrics.sql tests/test_phase4_migrations.py tests/test_phase4_config.py
git commit -m "phase4: add bounded configuration and migration"
```

### Task 3: Basis formula and public adapters

**Files:**
- Create: `src/quant_phase4/basis.py`
- Create: `src/quant_phase4/adapters/base.py`
- Create: `src/quant_phase4/adapters/bitget_uta_v3.py`
- Create: `src/quant_phase4/adapters/bybit_v5.py`
- Create: `src/quant_phase4/adapters/hyperliquid_public.py`
- Test: `tests/test_phase4_basis.py`
- Test: `tests/contract/test_phase4_public_live.py`

**Interfaces:**
- Consumes: Task 1 `BasisObservation`, existing Phase 2 `PublicHTTPAdapter`, and official response fixtures.
- Produces: `compute_basis(perpetual_price, reference_price, *, basis_type, perpetual_timestamp, reference_timestamp, max_timestamp_skew, source, received_at, fetched_at) -> BasisObservation` and public parser methods `parse_ticker` / `parse_asset_context`.

- [ ] **Step 1: Write failing basis tests.** Cover MARK_INDEX, MARK_ORACLE, zero reference price, timestamp skew at `limit-1`, `limit`, and `limit+1`, source status propagation, and rejection of mixed basis types.

```python
def test_basis_accepts_equal_timestamp_skew_limit():
    row = compute_basis(
        perpetual_price=Decimal("101"),
        reference_price=Decimal("100"),
        basis_type=BasisType.MARK_INDEX,
        perpetual_timestamp=UTC_NOW,
        reference_timestamp=UTC_NOW - timedelta(seconds=5),
        max_timestamp_skew=timedelta(seconds=5),
        source="bybit_v5_ticker",
        received_at=UTC_NOW,
        fetched_at=UTC_NOW,
    )
    assert row.status is DataStatus.AVAILABLE

def test_basis_rejects_skew_above_limit():
    row = compute_basis(
        perpetual_price=Decimal("101"),
        reference_price=Decimal("100"),
        basis_type=BasisType.MARK_INDEX,
        perpetual_timestamp=UTC_NOW,
        reference_timestamp=UTC_NOW - timedelta(seconds=6),
        max_timestamp_skew=timedelta(seconds=5),
        source="bybit_v5_ticker",
        received_at=UTC_NOW,
        fetched_at=UTC_NOW,
    )
    assert row.status is DataStatus.STALE
```

- [ ] **Step 2: Run the focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_basis.py -q`

Expected: FAIL because `compute_basis` and the adapters do not exist.

- [ ] **Step 3: Implement one centralized Decimal formula.** The function computes `absolute_basis = perpetual - reference`, `basis_bps = absolute / reference * 10000`, and `basis_pct = absolute / reference * 100`; it refuses zero or unavailable reference prices.

- [ ] **Step 4: Implement source parsers.** Bitget UTA v3 reads only contract-tested `markPrice`, `indexPrice`, and exchange timestamp fields; Bybit V5 reads only `markPrice`, `indexPrice`, and response `time`; Hyperliquid reads `markPx` and `oraclePx` and emits `MARK_ORACLE` only when the timestamp contract is valid.

- [ ] **Step 5: Add deterministic source-schema tests and opt-in live probes.** Live probes must be gated by `PHASE4_LIVE_CONTRACT=1`, use public endpoints only, assert the official response envelope and fields, and never silently fall back to another version or endpoint.

- [ ] **Step 6: Run tests.**

Run: `python -m pytest tests/test_phase4_basis.py tests/contract/test_phase4_public_live.py -q`

Expected: deterministic tests PASS; live tests explicitly skip unless enabled.

- [ ] **Step 7: Commit basis implementation.**

```text
git add src/quant_phase4/basis.py src/quant_phase4/adapters tests/test_phase4_basis.py tests/contract/test_phase4_public_live.py
git commit -m "phase4: add basis contracts and public adapters"
```

### Task 4: Long / Short adapters and semantic comparability

**Files:**
- Modify: `src/quant_phase4/adapters/bitget_classic_v2.py`
- Modify: `src/quant_phase4/adapters/bybit_v5.py`
- Modify: `src/quant_phase4/adapters/hyperliquid_public.py`
- Create: `src/quant_phase4/long_short.py`
- Test: `tests/test_phase4_long_short.py`
- Modify: `tests/contract/test_phase4_public_live.py`

**Interfaces:**
- Consumes: Task 1 `LongShortObservation` and Task 2 validated settings.
- Produces: `BitgetClassicV2LongShortAdapter`, `BybitV5LongShortAdapter`, `UnavailableLongShortAdapter`, and `validate_long_short_comparability(rows) -> ComparableLongShortGroup`.

- [ ] **Step 1: Write failing semantic tests.** Assert Bitget uses `/api/v2/mix/market/long-short`, preserves `longRatio`, `shortRatio`, `longShortRatio`, and maps to `ACCOUNT_HOLDER_RATIO`; assert Bybit uses `/v5/market/account-ratio`, preserves `buyRatio`, `sellRatio`, `timestamp`, and `period`; assert neither is interpreted as taker volume or global position quantity.

```python
def test_bitget_classic_ratio_is_account_holder_ratio():
    row = BitgetClassicV2LongShortAdapter(
        base_url="https://api.bitget.com",
        session=recording_session,
    ).parse_response(BITGET_LONG_SHORT_FIXTURE, received_at=UTC_NOW)
    assert row.metric_type is LongShortMetricType.ACCOUNT_HOLDER_RATIO
    assert row.long_value == Decimal("0.61")
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_long_short.py -q`

Expected: FAIL because the adapters and semantic validator do not exist.

- [ ] **Step 3: Implement the isolated Bitget Classic v2 adapter.** Reuse only the public HTTP transport; keep the v2 path, parameters, response fields, rate limiter, and parser inside this adapter. No v2 field may be referenced by cross-exchange or Stage1 code.

- [ ] **Step 4: Implement the Bybit V5 account-ratio adapter.** Preserve official period values and response timestamps; reject missing `buyRatio`, `sellRatio`, or `timestamp` rather than substituting zero.

- [ ] **Step 5: Implement Hyperliquid unavailable behavior.** Return a typed `NOT_AVAILABLE` observation with `UNCONFIRMED_PUBLIC_SOURCE`; do not scan addresses, consume user fills, or infer ratios from trades.

- [ ] **Step 6: Implement comparability.** Group only identical metric type, population semantics, period, canonical symbol, and compatible timestamp. Otherwise return `INSUFFICIENT_COMPARABLE_SOURCES` with separate source rows intact.

- [ ] **Step 7: Add official live contract assertions.** Validate response fields, periods, public authentication, and documented rate behavior for both Bitget v2 and Bybit V5 under the live flag.

- [ ] **Step 8: Run tests and commit.**

Run: `python -m pytest tests/test_phase4_long_short.py tests/contract/test_phase4_public_live.py -q`

Expected: deterministic tests PASS; live tests explicitly skip without the live flag.

```text
git add src/quant_phase4 tests/test_phase4_long_short.py tests/contract/test_phase4_public_live.py
git commit -m "phase4: add long short adapters and semantic gates"
```

### Task 5: Liquidation adapters, units, deduplication, and bounded aggregation

**Files:**
- Modify: `src/quant_phase4/adapters/bitget_uta_v3.py`
- Modify: `src/quant_phase4/adapters/bybit_v5.py`
- Modify: `src/quant_phase4/adapters/hyperliquid_public.py`
- Create: `src/quant_phase4/liquidation.py`
- Create: `src/quant_phase4/aggregation.py`
- Test: `tests/test_phase4_liquidation.py`
- Modify: `tests/contract/test_phase4_public_live.py`

**Interfaces:**
- Consumes: Task 1 liquidation contract and Task 2 queue settings.
- Produces: `BitgetUTA3LiquidationAdapter`, `BybitV5LiquidationAdapter`, `UnavailableLiquidationAdapter`, `liquidation_identity_key(event)`, `BoundedLiquidationDeduplicator`, `BoundedLiquidationQueue`, `LiquidationWindowBuilder`, and `rollup_liquidation_windows(windows, timeframe)`.

- [ ] **Step 1: Write failing parser tests.** Cover Bitget `buy -> LONG`, `sell -> SHORT`, quote-coin amount, aggregated coverage metadata; Bybit `Buy -> LONG`, `Sell -> SHORT`, executed size, bankruptcy price, and no fabricated notional when units are unresolved; Hyperliquid unavailable status.

```python
def test_bitget_amount_is_quote_coin_and_not_complete_market_total():
    rows = adapter.parse_ws_message(BITGET_LIQUIDATION_FIXTURE, RECEIVED)
    assert rows[0].quantity_unit is QuantityUnit.QUOTE_COIN
    assert rows[0].coverage_semantics is CoverageSemantics.PARTIAL_AGGREGATED
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_liquidation.py -q`

Expected: FAIL because the adapters and aggregation types do not exist.

- [ ] **Step 3: Implement strict source parsers.** Accept only the documented envelopes and fields; preserve raw side and source metadata; never infer side from order-side intuition.

- [ ] **Step 4: Implement stable identity and deduplication.** Use exchange, symbol, source timestamp, side, price, quantity, and any source event identity. The identity must distinguish same-timestamp opposite sides and legitimate records with different quantity or price; `received_at` is never an identity field.

- [ ] **Step 5: Implement bounded queue and gap state.** Queue insertion returns false on capacity exhaustion and records backpressure. A WebSocket disconnect with no official backfill produces a gap/partial state rather than synthetic events.

- [ ] **Step 6: Implement 1m aggregation and rollups.** Aggregate observed source records only; name fields `observed_*` where coverage could be partial. Calculate notional only from verified units and multipliers. Produce 5m, 15m, 1H, and 4H rollups without storing an unbounded raw feed.

- [ ] **Step 7: Add live WebSocket contract probes.** Validate Bitget UTA v3 `topic=liquidation` and Bybit `allLiquidation.{symbol}` subscription/first payload without API credentials.

- [ ] **Step 8: Run tests and commit.**

Run: `python -m pytest tests/test_phase4_liquidation.py tests/contract/test_phase4_public_live.py -q`

Expected: deterministic tests PASS; live tests explicitly skip without the live flag.

```text
git add src/quant_phase4 tests/test_phase4_liquidation.py tests/contract/test_phase4_public_live.py
git commit -m "phase4: add liquidation ingestion and aggregation"
```

### Task 6: PostgreSQL persistence, idempotency, and retention

**Files:**
- Create: `src/quant_phase4/persistence.py`
- Test: `tests/test_phase4_persistence.py`
- Modify: `tests/test_phase4_migrations.py`

**Interfaces:**
- Consumes: Task 1 canonical rows, Task 2 migration, and Task 5 windows.
- Produces: `Phase4Repository` methods from the interface section plus `Phase4Retention`.

- [ ] **Step 1: Write failing repository tests.** Use the existing recording-connection style to verify SQL table names, stable conflict keys, omission of `raw_payload`, explicit UTC values, and deterministic cleanup statements.

```python
def test_liquidation_persistence_strips_raw_payload():
    repository.insert_liquidation_events([event_with_debug_payload()])
    sql, rows = connection.cursor_value.calls[0]
    assert "raw_payload" not in sql.lower()
    assert rows[0].source_granularity == "AGGREGATED_MAX_PER_SECOND"
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_persistence.py -q`

Expected: FAIL because `Phase4Repository` does not exist.

- [ ] **Step 3: Implement batch upserts.** Use `ON CONFLICT` on the migration-defined logical keys. Persist normalized fields, source references, status, reason, and coverage metadata; omit complete raw payloads.

- [ ] **Step 4: Implement latest-context reads.** Fetch only bounded latest rows per canonical symbol and metric type, with no full-table scan. Missing data returns typed `NOT_AVAILABLE` context.

- [ ] **Step 5: Implement retention.** Delete by timestamp using configured event/window/observation/enrichment retention values; do not touch Phase 1–3 tables.

- [ ] **Step 6: Run deterministic and optional PostgreSQL tests.**

Run: `python -m pytest tests/test_phase4_persistence.py tests/test_phase4_migrations.py -q`

Expected: PASS; PostgreSQL integration is marked `RUNTIME_ACCEPTANCE_REQUIRED` when `TEST_POSTGRES_DSN` is absent.

- [ ] **Step 7: Commit persistence.**

```text
git add src/quant_phase4/persistence.py tests/test_phase4_persistence.py tests/test_phase4_migrations.py
git commit -m "phase4: add idempotent persistence and retention"
```

### Task 7: Coverage-aware cross-exchange context and Stage1 enrichment

**Files:**
- Create: `src/quant_phase4/cross_exchange.py`
- Create: `src/quant_phase4/enrichment.py`
- Test: `tests/test_phase4_cross_exchange.py`
- Test: `tests/test_phase4_enrichment.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5, and 6 canonical reads.
- Produces: `build_phase4_context(liquidations, long_short_rows, basis_rows, processed_at) -> CrossExchangePhase4Context` and `enrich_stage1_phase4(result, context, processed_at) -> Stage1Phase4Enrichment`.

- [ ] **Step 1: Write failing cross-exchange tests.** Assert Bitget partial liquidation coverage is not added to Bybit all-liquidation totals, different Long/Short metric types return `INSUFFICIENT_COMPARABLE_SOURCES`, and `MARK_INDEX` is isolated from `MARK_ORACLE`.

- [ ] **Step 2: Run tests and verify failure.**

Run: `python -m pytest tests/test_phase4_cross_exchange.py tests/test_phase4_enrichment.py -q`

Expected: FAIL because the context builder and enrichment function do not exist.

- [ ] **Step 3: Implement coverage-aware context.** Include source count, comparable count, missing count, stale count, source granularity, coverage semantics, and reason codes. Do not emit a vote score or LONG/SHORT/BUY/SELL decision.

- [ ] **Step 4: Implement Stage1 context-only enrichment.** Preserve the existing Stage1 result and symbol even when all Phase 4 metrics are unavailable. Set enrichment state to available, partial, or unavailable without changing eligibility or classification.

- [ ] **Step 5: Persist integration rows through `Phase4Repository`.** Use `(screening_run_id, symbol)` idempotency and keep the Phase 4 context separate from existing Phase 2 and Phase 3 enrichment.

- [ ] **Step 6: Run tests and commit.**

Run: `python -m pytest tests/test_phase4_cross_exchange.py tests/test_phase4_enrichment.py -q`

Expected: PASS.

```text
git add src/quant_phase4/cross_exchange.py src/quant_phase4/enrichment.py tests/test_phase4_cross_exchange.py tests/test_phase4_enrichment.py
git commit -m "phase4: add coverage-aware context enrichment"
```

### Task 8: Health, runtime orchestration, and existing entrypoint integration

**Files:**
- Create: `src/quant_phase4/health.py`
- Create: `src/quant_phase4/runtime.py`
- Modify: `src/quant_phase1/entrypoints/collector.py`
- Modify: `src/quant_phase1/entrypoints/engine.py`
- Test: `tests/test_phase4_runtime.py`

**Interfaces:**
- Consumes: Tasks 3–7 adapters, queue, repository, settings, existing `PeriodicScheduler`, `Phase3PublicStreamRunner` patterns, and existing health persistence.
- Produces: `Phase4Runtime.start()`, `Phase4Runtime.stop()`, `Phase4Runtime.ingest_liquidation(exchange, payload, received_at)`, `Phase4Runtime.run_rest_cycle(symbols, processed_at)`, and component-level health snapshots.

- [ ] **Step 1: Write failing runtime tests.** Cover disabled-by-default startup, bounded queue, reconnect status transitions, PostgreSQL outage degradation, recovery after reconnect, no duplicate windows after restart, and no changes to Phase 1–3 tasks when Phase 4 is disabled.

- [ ] **Step 2: Run tests and verify failure.**

Run: `python -m pytest tests/test_phase4_runtime.py -q`

Expected: FAIL because the Phase 4 runtime and health registry do not exist.

- [ ] **Step 3: Implement component health transitions.** Track `liquidation`, `long_short`, and `basis` at component level with `RUNNING`, `DEGRADED`, `STALE`, `ERROR`, and `RECOVERED`; do not create one health row per symbol/message.

- [ ] **Step 4: Implement bounded runtime orchestration.** Reuse the existing collector process, dynamic Universe/Stage1 candidates, scheduler, public WebSocket lifecycle, REST rate limiter, bounded queues, and repository. Use no unbounded symbol/event cache.

- [ ] **Step 5: Integrate collector.** Instantiate `Phase4Runtime` only when `PHASE4_ENABLED=1`, hydrate bounded windows on startup, run liquidation stream and REST cycles as tasks, persist Phase 4 health, and stop all tasks cleanly with the existing collector shutdown path.

- [ ] **Step 6: Integrate engine.** After existing Stage1/Phase 2/Phase 3 context loading, load the latest Phase 4 context and persist `stage1_phase4_enrichment`; never alter Stage1 classification or invoke any order path.

- [ ] **Step 7: Run tests and commit.**

Run: `python -m pytest tests/test_phase4_runtime.py tests/test_phase3_runtime.py tests/test_phase3_recovery_freshness.py -q`

Expected: Phase 4 tests PASS and Phase 3 runtime tests remain PASS.

```text
git add src/quant_phase4/health.py src/quant_phase4/runtime.py src/quant_phase1/entrypoints/collector.py src/quant_phase1/entrypoints/engine.py tests/test_phase4_runtime.py
git commit -m "phase4: integrate bounded runtime and health"
```

### Task 9: Safety, resources, report, and full local regression

**Files:**
- Modify: `.env.example`
- Create: `tests/test_phase4_safety_resources.py`
- Create: `PHASE_4_COMPLETION_REPORT.md`

**Interfaces:**
- Consumes: all prior tasks and the accepted Phase 1–3 regression suites.
- Produces: code-completion evidence only; no Jakarta deployment or runtime acceptance claim.

- [ ] **Step 1: Write failing safety/resource tests.** Assert two application services only, unchanged memory ceilings, `TRADING_MODE=paper`, no credentials, no private/order/position/withdrawal/leverage routes, no raw warehouse, bounded queue settings, and no changes to migrations `001–008`.

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/test_phase4_safety_resources.py -q`

Expected: FAIL for missing Phase 4 safety assertions.

- [ ] **Step 3: Implement only test/report/config updates.** Do not increase Docker limits or add a service. Record the exact configured defaults and safety scan results in the completion report.

- [ ] **Step 4: Run the full local suite.**

Run: `python -m pytest -q`

Expected: all deterministic Phase 1–4 tests PASS; only explicitly gated live exchange and PostgreSQL tests may skip, with each skip reason listed.

- [ ] **Step 5: Run the opt-in public contract suite when network is available.**

Run: `$env:PHASE4_LIVE_CONTRACT='1'; python -m pytest tests/contract/test_phase4_public_live.py -q`

Expected: each source is reported independently as PASS, NOT_AVAILABLE, or contract failure; no source is silently substituted.

- [ ] **Step 6: Fill `PHASE_4_COMPLETION_REPORT.md`.** Include base/design/final commits, files, migration/idempotency result, source matrix, unavailable sources, persistence, retention, reliability, Stage1 behavior, tests/skips, safety scan, known issues, and the explicit statement that Jakarta Runtime Acceptance remains pending.

- [ ] **Step 7: Commit report and final code-completion artifacts.**

```text
git add .env.example tests/test_phase4_safety_resources.py PHASE_4_COMPLETION_REPORT.md
git commit -m "phase4: complete local regression and code report"
```

- [ ] **Step 8: Verify the code-completion gate.**

Run: `git status --short --branch; git log --oneline --decorate -12; python -m pytest -q`

Expected: branch `phase4`, clean working tree, no code-level blocker, and a complete local test result. Only then report `PHASE4_CODE_COMPLETE_RUNTIME_PENDING`; otherwise report `PHASE4_CODE_NOT_COMPLETE` and list the blocker.

## Execution Order and Stop Boundary

Execute Tasks 1–9 in order. After every task, run its focused test command and
commit the independently testable result. Run Phase 1–3 regression tests after
Tasks 2, 5, 8, and 9 to catch compatibility regressions early.

This plan ends at local code completion. It does not deploy to Jakarta, apply
production migrations, restart the accepted Phase 3 runtime, execute Phase 4
Runtime Acceptance, or enter Phase 5.
