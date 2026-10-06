# Phase 3 Flow Enrichment Design Spec

**Status:** Approved for implementation from the Phase 2 accepted baseline

**Starting baseline:** `phase2` at `01846bee198a9a494657e5da249fb54a1b957415`

**Target branch:** `phase3`

**Trading mode:** `paper`

## 1. Goal and scope

Phase 3 adds public trade-stream ingestion and flow evidence to the already accepted Phase 1 and Phase 2 system. It provides non-directional volume context for all three supported exchanges and directional flow/CVD only for exchanges whose public aggressor semantics are explicitly confirmed.

In scope:

- Bitget, Bybit, and Hyperliquid public trade streams.
- Canonical trade normalization.
- Exchange capability declaration.
- Bounded deduplication and late-event handling.
- Bounded queues and observable backpressure.
- 1m flow windows and 5m/15m/1H/4H rollups.
- Bybit directional delta and rolling CVD.
- Cross-exchange non-directional volume.
- Cross-exchange directional flow only when the configured minimum of reliable sources is available.
- Dynamic subscriptions based on Phase 1 Universe and Stage1 A/B candidates.
- Stage1/Phase2 flow-context enrichment.
- Runtime health, recovery, retention, resource, and safety validation.

Out of scope:

- Order book, depth, DOM, bid/ask imbalance, liquidation, long/short ratio.
- News, AI, Evidence Chain, Stage2, Risk Engine.
- Orders, positions, private API, account WebSocket, API keys, and live trading.
- Binance, OKX, MEXC, Phase 4, or any new infrastructure service.

Phase 3 remains evidence/context only. It never emits BUY, SELL, LONG, SHORT, ORDER, or an executable signal.

## 2. Source semantics and capability model

Each adapter exposes an explicit immutable capability declaration. Business code consumes capabilities rather than embedding exchange-name conditionals.

```text
TradeSourceCapabilities:
  supports_public_trade_stream: bool
  supports_trade_id: bool
  supports_aggressor_side: bool
  supports_recent_trade_backfill: bool
  supports_directional_flow: bool
  supports_cvd: bool
```

Initial capabilities:

| Exchange | Public stream | Trade ID | Aggressor side | Recent backfill | Directional flow | CVD |
|---|---:|---:|---:|---:|---:|---:|
| Bitget | yes | yes, REST `execId` / WS `i` | no | bounded/contract-tested | no | no |
| Bybit | yes | yes, `i`/`execId` | yes, public `S` is side of taker | bounded | yes | yes |
| Hyperliquid | yes | yes, composite `block_time + coin + tid` | no | contract-tested | no | no |

Semantic rules:

- Bybit `Buy` maps to `aggressor_side=BUY`, `Sell` maps to `SELL`, with `side_source=EXCHANGE_PROVIDED`.
- Bitget preserves raw `buy`/`sell` but sets `raw_side_semantics=TRADE_SIDE_UNCONFIRMED_AGGRESSOR`, `aggressor_side=UNKNOWN`, and `side_source=UNKNOWN`.
- Hyperliquid preserves raw `B`/`A` or the actual source value but sets `raw_side_semantics=PUBLIC_TRADE_SIDE_UNCONFIRMED_AGGRESSOR`, `aggressor_side=UNKNOWN`, and `side_source=UNKNOWN`.
- `INFERRED` is forbidden in the initial Phase 3 implementation. No tick rule, price-change inference, or private fill substitution is permitted.
- Runtime observations and tests may validate schema, ordering, IDs, and coverage; they may not promote undocumented side semantics.

Source contracts:

- Bitget UTA public REST: `GET /api/v3/market/fills`, `category=USDT-FUTURES`, `symbol`, `limit<=100`, 20 requests/sec/IP; live rows use `execId`, `price`, `size`, `side`, and `ts`. Public WebSocket: `wss://ws.bitget.com/v3/ws/public`, topic `publicTrade`, `instType=usdt-futures`; live rows use `i`, `p`, `v`, `S`, `T`, with `L`/`isRPI` retained. These are source trade-side fields only and remain non-directional.
- Bybit public REST: `GET /v5/market/recent-trade`, `category=linear`, `symbol`, `limit<=1000`. Public WebSocket: `wss://stream.bybit.com/v5/public/linear`, topic `publicTrade.{symbol}`. Public payload `S` is the taker side; `T`, `s`, `v`, `p`, `i`, and `seq` are retained.
- Hyperliquid public WebSocket: `wss://api.hyperliquid.xyz/ws`, subscription `{method: subscribe, subscription: {type: trades, coin}}`. Public payload includes `coin`, `side`, `px`, `sz`, `hash`, `time`, `tid`, and `users`. REST `recentTrades` is only a bounded recovery candidate until the live contract test proves coverage and identity behavior.

## 3. CanonicalTrade contract

The Phase 3 contract is exchange-neutral and UTC-only:

```text
CanonicalTrade:
  exchange: str
  exchange_symbol: str
  canonical_symbol: str | None
  trade_id: str
  price: Decimal
  quantity_base: Decimal
  notional_usd: Decimal | None
  aggressor_side: BUY | SELL | UNKNOWN
  raw_side: str | None
  raw_side_semantics: str
  side_source: EXCHANGE_PROVIDED | INFERRED | UNKNOWN
  exchange_timestamp: datetime
  received_at: datetime
  processed_at: datetime
  source_channel: str
  status: AVAILABLE | STALE | PARTIAL | NOT_AVAILABLE | ERROR
  raw_payload: mapping | None
```

Contract invariants:

- All timestamps are timezone-aware UTC.
- `price` and `quantity_base` are positive.
- `notional_usd` is calculated only when the price is a valid USD/USDT-equivalent market price; otherwise it is `NOT_AVAILABLE` rather than fabricated.
- `aggressor_side=UNKNOWN` contributes to `unknown_trade_count` and total volume, never to buy/sell volume or delta.
- `buy + sell + unknown = total` for both trade counts and base volume within each window, allowing `NOT_AVAILABLE` when required inputs are absent.
- `trade_id` is the adapter's source identity, not a timestamp/price/size fingerprint.

## 4. Identity, deduplication, ordering, and gaps

Each adapter provides an identity function:

- Bitget: `exchange + canonical_symbol + execId` for REST and `exchange + canonical_symbol + i` for public WebSocket. The adapter records the source-specific ID and does not silently fall back to timestamps.
- Bybit: `exchange + canonical_symbol + i`/`execId`; `seq` is retained for ordering diagnostics and does not replace the trade ID.
- Hyperliquid: `exchange + block_time + coin + tid`; the adapter must confirm how the stream exposes the documented block time. If it cannot construct the documented composite, it marks identity coverage insufficient and does not claim reliable gap recovery.

Deduplication uses a bounded per-exchange TTL/LRU cache. The cache has configurable maximum entries and TTL. It records duplicate counts and never grows without bound. REST recovery and WebSocket reconnects enter the same dedup path.

Window processing uses event time, processing time, and a configurable allowed-lateness interval. Once a window is finalized, a late event does not silently rewrite it. The initial policy is `PARTIAL`: the event is recorded in a `trade_gap_events` row, the affected window is marked partial, and the late event is excluded from the immutable finalized aggregate.

If a disconnect exceeds the proven REST recovery range, or if ID/ordering coverage is insufficient, the runtime emits `TRADE_GAP` and marks the affected window `PARTIAL`. It never reports a known gap as complete `AVAILABLE`.

## 5. Bounded stream runtime

The existing two-container topology is retained:

- `quant-collector` owns exchange public trade WebSockets, per-exchange connection state, subscription state, bounded queues, deduplication, and flow-window construction.
- `quant-engine` reads persisted flow snapshots beside the existing Stage1/Phase2 data and writes context-only flow enrichment.
- PostgreSQL remains the only persistence service. No Kafka, Redis, or additional service is introduced.

Candidate selection is refreshed from the persisted Phase 1 Universe plus Stage1 A/B candidates. The initial limit is configurable and defaults to `MAX_TRADE_STREAM_SYMBOLS=20`. BTC and ETH are test symbols, not the subscription universe.

Subscription lifecycle:

1. Load current eligible candidates.
2. Subscribe only symbols not already active.
3. Keep a symbol for at least `TRADE_MIN_SUBSCRIPTION_SECONDS`.
4. When a symbol leaves, start `TRADE_SUBSCRIPTION_COOLDOWN_SECONDS` before unsubscribe.
5. Record active subscriptions, subscribe count, unsubscribe count, and per-exchange reconnect count.

Every exchange has independent connection, subscription, queue, dedup, health, and recovery state. One exchange's failure produces degraded/partial flow for that exchange without stopping Phase 1, Phase 2, or the other exchanges.

The queue is bounded by `MAX_TRADE_QUEUE_SIZE`. On saturation, the runtime records a `BACKPRESSURE_EVENT` with exchange, symbol, queue depth/capacity, dropped count, and timestamp. It may drop only after recording the event and marking affected windows partial. An unbounded `asyncio.Queue`, set, dictionary, or raw-trade history is forbidden.

Raw trades are not persisted by default. An optional debug buffer is disabled by default, bounded, and short-retention only.

## 6. Flow aggregation and CVD

The aggregation graph is:

```text
CanonicalTrade
  -> bounded dedup
  -> event-time 1m window
  -> 5m / 15m / 1H / 4H rollups
  -> capability-aware flow snapshots
  -> cross-exchange volume/directional context
  -> Stage1/Phase2 flow enrichment
```

Each `TradeFlowWindow` contains exchange, canonical symbol, timeframe, open/close, buy/sell/unknown and total counts, base and USD volumes, delta fields when directional capability is available, first/last trade time, freshness, status, and processing timestamps.

For Bitget and Hyperliquid:

- total volume, notional volume, trade count, average size, and frequency are available when source data is fresh;
- buy/sell volume, delta, delta ratio, and CVD are `NOT_AVAILABLE` with reason `UNCONFIRMED_AGGRESSOR_SEMANTICS`.

For Bybit:

- directional buy/sell volume, delta, and delta ratio are available when source data is fresh and complete;
- CVD is a persisted rolling CVD derived from 1m delta;
- initial supported CVD horizons are 15m, 1H, 4H, and 24H;
- only `ROLLING_CVD` is implemented initially; no session CVD is introduced.

Cross-exchange snapshots distinguish:

- `volume_exchange_count`: sources with usable non-directional volume;
- `directional_exchange_count`: sources with fresh, complete, reliable aggressor semantics;
- volume totals across all available sources;
- weighted/median directional delta only from eligible directional sources;
- `cross_exchange_directional_status=INSUFFICIENT_DIRECTIONAL_SOURCES` when the configured minimum is not met.

With the initial capabilities, three exchanges may contribute volume while only Bybit contributes directional flow. The result must not be labeled multi-exchange directional confirmation unless the configured directional minimum is met.

## 7. Persistence and retention

Migration `008` adds the following idempotent tables without changing or deleting Phase 1/2 history:

- `trade_flow_windows`: one immutable finalized key per exchange, symbol, timeframe, and window open; upsert is allowed only for the same window identity before finalization.
- `cvd_snapshots`: exchange, symbol, CVD timeframe, window end, rolling value, source status, and processing metadata.
- `cross_exchange_flow_snapshots`: symbol, timeframe, snapshot time, per-exchange context, volume counts, directional counts, aggregate values, status, and reason.
- `trade_gap_events`: exchange, symbol, gap start/end, detection/recovery status, missing coverage reason, affected timeframe/window, and counts.
- `stage1_flow_enrichment`: Stage1 run and symbol identity, flow metrics, CVD metrics, capability/status fields, and processed timestamp. It is context-only and cannot change the Stage1 category.

All tables use UTC timestamps and explicit status/reason fields. No `raw_trades` table is created in the default design.

Retention is separately configurable:

```text
PHASE3_FLOW_RETENTION_1M_DAYS=7
PHASE3_FLOW_RETENTION_5M_DAYS=30
PHASE3_FLOW_RETENTION_15M_DAYS=90
PHASE3_FLOW_RETENTION_1H_DAYS=180
PHASE3_FLOW_RETENTION_4H_DAYS=365
PHASE3_CVD_RETENTION_DAYS=365
PHASE3_GAP_RETENTION_DAYS=90
PHASE3_CROSS_EXCHANGE_RETENTION_DAYS=180
PHASE3_ENRICHMENT_RETENTION_DAYS=90
```

These are low-resource initial defaults, not final production commitments. Retention is executed by the existing bounded runtime and tested against an isolated database.

## 8. Freshness, outage, and recovery

Phase 3 freshness is independent of Phase 1 kline freshness. It tracks at least:

- last trade age;
- last complete window age;
- source status;
- gap/partial state;
- queue/backpressure state.

States are `AVAILABLE`, `STALE`, `PARTIAL`, `NOT_AVAILABLE`, and `ERROR`. Low-liquidity symbols do not become `ERROR` merely because no trade arrived for a few seconds; thresholds are configurable and based on the symbol's active subscription policy.

During a database outage, the runtime keeps only bounded active windows and the configured recovery buffer. It marks persistence degraded and records a gap/partial condition if closed windows cannot be durably written. It never accumulates unlimited raw trades in RAM. On recovery, it persists new complete windows and resumes health heartbeats.

## 9. Stage1/Phase2 enrichment boundary

The data path is:

```text
Phase 1 Stage1
  -> Phase 2 derivative context
  -> Phase 3 flow context
```

Flow enrichment may expose:

```text
delta_1m
delta_5m
delta_15m
delta_ratio_5m
delta_ratio_15m
cvd_15m
cvd_1h
cvd_4h
cross_exchange_delta_ratio
flow_divergence
flow_status
```

Missing Phase 3 data does not remove a Phase 1/2 candidate. It is represented as `FLOW_NOT_AVAILABLE` or the more specific capability/gap reason. No category, direction, trade signal, order, or risk decision is produced.

## 10. Resource and deployment constraints

- Keep `quant-collector` at the Phase 2 256 MiB memory limit.
- Keep `quant-engine` at the Phase 2 384 MiB memory limit.
- Do not add containers or raise limits before measuring.
- Emit `RESOURCE_WARNING` if collector usage is sustained above 220 MiB.
- Any OOM, restart loop, or unbounded memory path is a Phase 3 acceptance blocker.
- Logs are summary-only at INFO: connect, disconnect, subscribe, unsubscribe, reconnect, gap, backpressure, window summary, and critical errors. Per-trade logging is DEBUG and disabled by default.
- Internal health snapshots expose stream connectivity, event count/rate, duplicate count, unknown-side count, gap count, queue depth/capacity, backpressure count, window count, CVD count, and last event/window timestamps without per-symbol metric explosion.

## 11. Test and acceptance boundary

Implementation is incremental and follows the approved order:

1. CanonicalTrade
2. Exchange capability model
3. Bybit adapter and contract tests
4. Bitget adapter and contract tests
5. Hyperliquid adapter and contract tests
6. Bounded dedup
7. Ordering/lateness
8. Bounded queue
9. Backpressure
10. 1m aggregation
11. 5m/15m/1H/4H rollups
12. Bybit CVD
13. Persistence
14. Dynamic candidate subscription
15. Gap detection/recovery
16. Freshness
17. Cross-exchange volume
18. Cross-exchange directional capability
19. Stage1 flow enrichment
20. Full local regression
21. Singapore runtime acceptance

Tests must cover source schema, side handling, IDs, ordering, duplicates, REST/WS dedup, gap coverage, late events, conservation, rollups, Bybit CVD, restart recovery, dynamic subscriptions, bounded queues, backpressure, exchange isolation, migration, retention, database outage, restart, resource limits, and safety.

Phase 3 is accepted only when:

- Bitget, Bybit, and Hyperliquid real public trade streams pass.
- All three schemas and non-directional volume aggregation pass.
- Bybit aggressor semantics, directional flow, and CVD pass.
- Bitget and Hyperliquid remain safely excluded from directional flow unless a future official semantic change is documented and separately tested.
- Dedup, ordering, gap, freshness, backpressure, failure isolation, persistence, retention, outage recovery, restart recovery, and resource tests pass.
- Phase 1 and Phase 2 regressions pass.
- Singapore runtime has no OOM or restart loop and keeps `TRADING_MODE=paper`.
- No private API, order route, position route, live executor, real order, or Phase 4 work exists.
