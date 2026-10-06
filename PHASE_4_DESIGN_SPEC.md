# CRYPTO PERPETUAL QUANT V1.0 — PHASE 4 DESIGN SPEC

Status: Design approved for specification drafting; implementation not started

Date: 2026-09-21

Base branch: `phase3`

Base HEAD: `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`

Target branch: `phase4`

Trading mode: `paper`

## 1. Goal and boundaries

Phase 4 adds three public-data context families:

1. Liquidation
2. Long / Short
3. Basis

The output remains descriptive context for future evidence processing. It does
not create a trading decision, vote score, order, position, risk action, or
live execution path.

Phase 1, Phase 2, and Phase 3 semantics remain unchanged. Phase 4 reuses the
existing collector, engine, scheduler, WebSocket supervision, PostgreSQL,
health events, and Stage1 context boundary. No new daemon or container is
created.

Out of scope:

- AI expansion, Evidence Chain, Stage 2, or Phase 5
- private API, API keys, account WebSocket, order API, or position API
- live executor, real orders, TP/SL, or position management
- Binance, OKX, or MEXC runtime adapters
- anonymous Hyperliquid liquidation or long/short inference
- long-term raw liquidation warehousing

## 2. Architecture

The runtime remains:

```text
Official REST / WebSocket
        |
        v
quant_phase4 adapters
        |
        v
canonical contracts and status propagation
        |
        +--> bounded queues / dedup / gap tracking
        |
        +--> liquidation windows
        +--> long-short observations
        +--> basis snapshots
        +--> cross-exchange comparable context
        |
        v
PostgreSQL migration 009+
        |
        v
Stage1 Phase 4 context-only enrichment
```

The Phase 4 package is additive:

```text
src/quant_phase4/
    contracts.py
    adapters/
        base.py
        bitget_uta_v3.py
        bitget_classic_v2.py
        bybit_v5.py
        hyperliquid_public.py
    liquidation.py
    long_short.py
    basis.py
    aggregation.py
    cross_exchange.py
    persistence.py
    health.py
    enrichment.py
```

The Bitget v2 Classic adapter is isolated from the existing Bitget UTA v3
adapter. Business logic receives only Phase 4 canonical objects and never
reads a version-specific response field.

The deployment remains exactly two Quant application services:

- `quant-collector`
- `quant-engine`

The resource ceilings remain:

- PostgreSQL: 768 MiB
- collector: 256 MiB
- engine: 384 MiB

## 3. Official source matrix

Every source must pass a live schema contract test before it is enabled. A
contract failure stops that source; it does not select an alternate endpoint
silently.

| Exchange | Metric | Official source | Auth | Intended fields / semantics | Frequency or history | Design status |
|---|---|---|---|---|---|---|
| Bitget | Liquidation | UTA v3 WS `wss://ws.bitget.com/v3/ws/public`; subscription `instType=usdt-futures`, `topic=liquidation` | Public | `symbol`, `side`, `price`, `amount`, `ts`; `buy` means liquidated long, `sell` means liquidated short; amount is quote coin | 1-second server aggregation; highest quantity per side and symbol only | Enabled after contract test |
| Bitget | Long / Short | Classic v2 REST `GET /api/v2/mix/market/long-short` | Public | `longRatio`, `shortRatio`, `longShortRatio`, `ts`; account-holder / holder-count ratio semantics, not global position size | Official period parameter; documented 1 request/sec/IP | Enabled through isolated Classic v2 adapter |
| Bitget | Basis | UTA v3 REST `GET /api/v3/market/tickers?category=USDT-FUTURES` | Public | `markPrice`, `indexPrice`, `ts` or the contract-tested exchange response timestamp | Current snapshots; periodic persistence | Enabled after contract test |
| Bybit | Liquidation | V5 public WS `wss://stream.bybit.com/v5/public/linear`; topic `allLiquidation.{symbol}` | Public | `T`, `s`, `S`, `v`, `p`; `Buy` means liquidated long, `Sell` means liquidated short | 500 ms push; exchange-declared all-liquidation stream | Enabled after contract test |
| Bybit | Long / Short | V5 REST `GET /v5/market/account-ratio` | Public | `buyRatio`, `sellRatio`, `timestamp`, requested `period`; all position holders account ratio | Official periods only: 5min, 15min, 30min, 1h, 4h, 1d; global public REST limit documented as 600 requests/5 seconds/IP | Enabled after contract test |
| Bybit | Basis | V5 REST `GET /v5/market/tickers?category=linear&symbol=...` | Public | `markPrice`, `indexPrice`, response `time` | Current snapshots; mark/index kline endpoints may be added only if independently contract-tested | Enabled after contract test |
| Hyperliquid | Liquidation | No verified anonymous public market liquidation source in current scope | Public-only runtime | No promotion from trades, user fills, or clearinghouse state | N/A | `NOT_AVAILABLE`, `UNCONFIRMED_PUBLIC_SOURCE` |
| Hyperliquid | Long / Short | No verified anonymous public market long/short source in current scope | Public-only runtime | No inferred market ratio | N/A | `NOT_AVAILABLE`, `UNCONFIRMED_PUBLIC_SOURCE` |
| Hyperliquid | Basis | Public `POST https://api.hyperliquid.xyz/info` with `metaAndAssetCtxs` | Public | `markPx` and `oraclePx`; basis is `MARK_ORACLE`, never `MARK_INDEX` | Current context; timestamp contract must be validated | Optional after contract test |

Bitget UTA v3 explicitly uses `topic` plus fields such as `symbol` and
`interval`; Classic v2 uses a different channel model. The Phase 4 adapters
preserve this separation. See the [Bitget UTA migration guide](https://www.bitget.com/docs/classic/uta-api-upgrade-guide).

The Bitget liquidation channel is an aggregated source: the official
documentation states that it pushes once per second and keeps only the maximum
liquidation quantity for each side and symbol in that second. See the [Bitget
Liquidation Channel](https://www.bitget.com/docs/uta/websocket/public/Liquidation-Channel).

Bybit documents `allLiquidation.{symbol}`, 500 ms pushes, and its `Buy`/`Sell`
position-side semantics in [All Liquidation](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation).

Hyperliquid's public WebSocket documentation lists liquidation under user-event
structures, not as an anonymous market liquidation feed. The runtime therefore
does not infer market-wide liquidation from user data. See [Hyperliquid
subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

## 4. Canonical status and time contract

All Phase 4 contracts use UTC-aware timestamps and the following fields:

```text
exchange
exchange_symbol
canonical_symbol
metric
source_endpoint / source_channel
exchange_timestamp
fetched_at
received_at
processed_at
status
raw_reference
raw_payload
reason_code
```

`status` is one of:

```text
AVAILABLE
STALE
NOT_AVAILABLE
ERROR
```

`UNCONFIRMED_SEMANTICS` and `UNCONFIRMED_PUBLIC_SOURCE` are reason codes,
not substitute numeric values. Missing values remain null. Zero is never used
to represent missing data.

- `exchange_timestamp`: timestamp supplied by the exchange for the event or
  source snapshot.
- `fetched_at`: local UTC time at which a REST response was obtained.
- `received_at`: local UTC time at which a WebSocket message or REST response
  was received by the process.
- `processed_at`: local UTC time after parsing and normalization.

## 5. Liquidation contract

```text
CanonicalLiquidation:
    event_id
    exchange
    exchange_symbol
    canonical_symbol
    event_timestamp
    received_at
    processed_at
    side                 # LIQUIDATED_LONG, LIQUIDATED_SHORT, UNKNOWN
    raw_side
    raw_side_semantics
    price
    raw_quantity
    quantity_unit        # QUOTE_COIN, BASE_ASSET, CONTRACTS, UNKNOWN
    quantity_base
    notional_usd
    source_endpoint
    source_channel
    source_granularity
    coverage_semantics
    status
    raw_reference
    raw_payload
```

The source coverage fields are mandatory:

```text
Bitget:
    source_granularity = AGGREGATED_MAX_PER_SECOND
    coverage_semantics = PARTIAL_AGGREGATED

Bybit:
    source_granularity = ALL_LIQUIDATIONS_STREAM
    coverage_semantics = EXCHANGE_DECLARED_ALL_LIQUIDATIONS
```

Bitget `event_count` is the count of normalized source records, not the true
number of market liquidation events. Bitget `sum(amount)` is not described as
the total liquidation amount for the second. Cross-exchange code never adds
Bitget and Bybit values into a field named `global_liquidation_total`.

`notional_usd` is populated only when price, quantity unit, contract
multiplier, and quote semantics are all verified. Otherwise the raw quantity
is preserved and the notional remains null.

The persisted event table contains normalized source records only and is
bounded by retention. `RAW_LIQUIDATION_WAREHOUSE=NONE` remains the default;
complete unbounded raw messages are not persisted.

## 6. Long / Short contract

```text
CanonicalLongShortObservation:
    exchange
    exchange_symbol
    canonical_symbol
    metric_type
    period
    long_value
    short_value
    ratio
    exchange_timestamp
    fetched_at
    received_at
    processed_at
    source_endpoint
    status
    raw_reference
    raw_payload
```

The initial metric type is:

```text
ACCOUNT_HOLDER_RATIO
```

It is used only for:

- Bitget `longRatio`, `shortRatio`, `longShortRatio` after the live contract
  confirms its account-holder semantics.
- Bybit `buyRatio`, `sellRatio` after the live contract confirms its all
  position-holders account-ratio semantics.

It must not be renamed to `GLOBAL_POSITION`, `TOP_TRADER_POSITION`, or any
other semantically stronger type. Any other metric type is stored separately.

Bitget and Bybit enter the same cross-exchange comparable group only after the
contract tests confirm semantic equivalence. Otherwise both observations are
persisted independently and the cross-exchange result is
`INSUFFICIENT_COMPARABLE_SOURCES`.

Official source periods are preserved. A source that does not provide 1m data
is not resampled or fabricated into a 1m observation.

## 7. Basis contract

```text
CanonicalBasisObservation:
    exchange
    exchange_symbol
    canonical_symbol
    basis_type
    perpetual_price
    reference_price
    absolute_basis
    basis_bps
    basis_pct
    exchange_timestamp
    fetched_at
    received_at
    processed_at
    max_timestamp_skew
    timestamp_skew
    source_endpoint
    status
    raw_reference
    raw_payload
```

Basis types are never mixed:

```text
Bitget / Bybit:
    MARK_INDEX
    absolute_basis = mark - index
    basis_bps = (mark - index) / index * 10000
    basis_pct = (mark - index) / index * 100

Hyperliquid:
    MARK_ORACLE
    absolute_basis = mark - oracle
    basis_bps = (mark - oracle) / oracle * 10000
    basis_pct = (mark - oracle) / oracle * 100
```

`MAX_BASIS_TIMESTAMP_SKEW` is configurable and enforced centrally. A basis
observation is not calculated when the two source timestamps exceed the limit,
the source units differ, or either input is not `AVAILABLE`.

## 8. Aggregation and cross-exchange rules

Liquidation aggregation uses a bounded 1m base window and supports 5m, 15m,
1H, and 4H rollups. It records source-record counts, long/short counts,
reliably convertible notional, largest source record, source exchange count,
coverage semantics, and status. It never claims total market liquidation when
source coverage is not equivalent.

Long/Short cross-exchange comparison requires the same `metric_type`, period,
canonical symbol, compatible timestamp, and `AVAILABLE` status.

Basis cross-exchange comparison requires the same `basis_type`, economic asset,
compatible units, and timestamp skew within the configured bound.

All cross-exchange outputs include:

```text
exchange_count
comparable_exchange_count
missing_sources
stale_sources
coverage_semantics
source_granularity
status
reason_code
```

Allowed descriptive contexts include `LIQUIDATION_ACTIVITY_ELEVATED`,
`LONG_LIQUIDATION_PRESSURE`, `SHORT_LIQUIDATION_PRESSURE`,
`POSITIVE_BASIS`, `NEGATIVE_BASIS`, `BASIS_EXPANSION`,
`BASIS_COMPRESSION`, and `INSUFFICIENT_DATA`. None is a trading decision.

## 9. Database migration and retention

Migration `009_phase4_metrics.sql` is additive and idempotent. It must not
alter or rewrite migrations `001–008` or any large Phase 1–3 table.

The migration adds only the tables required for Phase 4:

- `liquidation_events`
- `liquidation_windows`
- `long_short_observations`
- `basis_snapshots`
- `cross_exchange_phase4_snapshots`
- `stage1_phase4_enrichment`

Required uniqueness is based on canonical logical identity:

- liquidation deduplication key per exchange/source/event identity
- liquidation window `(exchange, canonical_symbol, timeframe, window_open)`
- long/short `(exchange, canonical_symbol, metric_type, period, exchange_timestamp)`
- basis `(exchange, canonical_symbol, basis_type, exchange_timestamp)`
- Stage1 `(screening_run_id, symbol)`

Initial configuration defaults:

```text
PHASE4_LIQUIDATION_EVENT_RETENTION_HOURS=24
PHASE4_LIQUIDATION_1M_RETENTION_DAYS=7
PHASE4_LIQUIDATION_5M_RETENTION_DAYS=30
PHASE4_LIQUIDATION_15M_RETENTION_DAYS=90
PHASE4_LIQUIDATION_1H_RETENTION_DAYS=180
PHASE4_LIQUIDATION_4H_RETENTION_DAYS=365
PHASE4_LONG_SHORT_RETENTION_DAYS=90
PHASE4_BASIS_RETENTION_DAYS=90
PHASE4_CROSS_EXCHANGE_RETENTION_DAYS=90
PHASE4_ENRICHMENT_RETENTION_DAYS=90
```

These values are configurable and must be validated as positive. Retention
uses timestamp indexes and bounded deletes; it does not perform a full-table
rewrite.

## 10. Runtime, health, and recovery

The collector owns public ingestion, bounded queues, adapter reconnects,
deduplication, window aggregation, and Phase 4 persistence. The engine loads
latest Phase 4 context for Stage1 enrichment.

Health distinguishes:

- `liquidation_collector`
- `long_short_collector`
- `basis_collector`

Each reports `RUNNING`, `DEGRADED`, `STALE`, `ERROR`, or `RECOVERED` through
the existing health system without per-message spam.

For streams without an official backfill endpoint, a disconnect creates an
explicit gap and marks affected context partial or unavailable. Reconnect does
not claim that missing events were recovered. REST sources resume from the
next verified snapshot and preserve logical timestamp idempotency.

PostgreSQL outages leave the bounded queues and process alive, mark health
degraded, and resume persistence after reconnect. Phase 1–3 ingestion must not
be stopped by a Phase 4 source failure.

## 11. Stage1 boundary

Phase 4 data is persisted as a separate context-only enrichment. A symbol can
remain a Stage1 candidate when one or more Phase 4 metrics are unavailable.
The enrichment reports full, partial, or unavailable context; it never removes
an existing Phase 1–3 candidate and never changes the Stage1 base
classification rules.

No single-indicator rule is permitted:

- positive basis does not imply short
- long/short extreme does not imply reversal
- liquidation spike does not imply long

## 12. Tests and acceptance gates

Before an adapter is enabled, contract tests must validate its real official
response envelope, required fields, types, timestamps, units, side semantics,
rate behavior, and public authentication requirement.

Unit tests cover:

- Bitget v2 / v3 version isolation
- Bybit and Hyperliquid parsing
- liquidation side and coverage semantics
- quantity units and notional eligibility
- long/short metric types
- basis formulas and timestamp skew
- status propagation
- aggregation and deduplication

Integration and failure tests cover:

- migration from an existing database
- repeat migration idempotency
- persistence duplicate handling
- collector and engine restart
- WebSocket disconnect and gap reporting
- REST error and stale handling
- PostgreSQL outage and recovery
- bounded queue pressure and memory
- Stage1 partial enrichment
- Phase 1–3 regression and paper-only safety

Phase 4 can be accepted only when all of the following pass:

```text
PHASE1_REGRESSION_PASS
PHASE2_REGRESSION_PASS
PHASE3_REGRESSION_PASS
LIQUIDATION_GATE_PASS
LONG_SHORT_GATE_PASS
BASIS_GATE_PASS
PHASE4_ENRICHMENT_PASS
MIGRATION_PASS
MIGRATION_IDEMPOTENCY_PASS
RESTART_RECOVERY_PASS
POSTGRES_OUTAGE_RECOVERY_PASS
BACKPRESSURE_PASS
RETENTION_PASS
RESOURCE_ACCEPTANCE_PASS
PAPER_TRADING_SAFETY_PASS
```

The final status is either `PHASE_4_ACCEPTED` or
`PHASE_4_NOT_ACCEPTED`. Phase 5 is outside this specification.

## 13. Safety invariants

- `TRADING_MODE=paper` is required.
- No private API credentials are read or required.
- No order, position, or live-executor module is added.
- No real order is generated or submitted.
- No source fallback is silent.
- No undocumented semantics are promoted to canonical values.
- No mock Liquidation, Long/Short, Basis, AI, or advanced-data observation is
  introduced.
