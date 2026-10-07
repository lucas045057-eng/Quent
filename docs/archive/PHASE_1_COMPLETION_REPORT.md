# PHASE 1 FINAL CONTINUOUS RUNTIME REPORT

Date: 2026-09-20  
Branch: `phase1`  
Mode: `TRADING_MODE=paper`

## Final status

`PHASE_1_ACCEPTED`

Phase 2 was not started. No private API, order API, position API, live executor, or real order path was added.

## Implementation

Implemented and verified:

- Long-running collector and engine services with SIGTERM/SIGINT graceful shutdown.
- Configurable Universe 1h, Stage1 5m, ticker/Kline continuous scheduling.
- Bounded WS event and closed-candle buffers.
- Bitget UTA v3 public REST and WebSocket adapters.
- WS ticker and Kline ingestion for 5m/15m/1H/4H, closed bars only.
- WS reconnect/resubscribe and REST gap recovery.
- Canonical persistence, explainable Stage1 results, runtime health and recovery events.
- PostgreSQL migration serialization and snapshot/Kline retention.
- Docker restart policy, healthchecks, memory limits, and log rotation.

Key files:

- `src/quant_phase1/entrypoints/collector.py`
- `src/quant_phase1/entrypoints/engine.py`
- `src/quant_phase1/adapters/bitget_v3/rest.py`
- `src/quant_phase1/adapters/bitget_v3/websocket.py`
- `src/quant_phase1/runtime.py`
- `src/quant_phase1/service.py`
- `src/quant_phase1/stage1.py`
- `src/quant_phase1/repositories.py`
- `src/quant_phase1/healthcheck.py`
- `migrations/002_runtime_health_and_explainability.sql`
- `migrations/003_kline_source_exchange.sql`
- `docker-compose.server.yml`

## Bitget runtime contract

REST was verified against the current public v3 Market Data API:

- Instruments: `GET /api/v3/market/instruments?category=USDT-FUTURES`
- Tickers: `GET /api/v3/market/tickers?category=USDT-FUTURES`
- Candles: `GET /api/v3/market/candles?category=USDT-FUTURES&symbol=<symbol>&interval=<interval>&limit=<limit>`
- REST authentication: none.
- Rate limit used: 20 requests/second/IP with bounded retry/backoff for 429.

WebSocket was verified against:

- `wss://ws.bitget.com/v3/ws/public`
- Ticker subscription: `topic="ticker"`.
- Kline subscription: `topic="kline"` with `interval` equal to `5m`, `15m`, `1H`, or `4H`.
- WebSocket authentication: none.
- Ping: every 30 seconds.
- Current runtime topology: five public connections, one ticker group and one connection per Kline interval.

References: [Bitget Market Data](https://www.bitget.com/docs/catalog/market/market-data), [UTA Tickers](https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel), [UTA Candlesticks](https://www.bitget.com/docs/uta/websocket/public/Candlesticks-Channel), [UTA Quick Start](https://www.bitget.com/docs/uta/quick-start), [UTA upgrade guide](https://www.bitget.com/docs/classic/uta-api-upgrade-guide).

REST and WebSocket contract probes passed: HTTP/TLS, WebSocket upgrade, ticker subscription, all four Kline intervals, closed-bar filtering, reconnect and resubscribe.

## Candidate runtime

- Region: Singapore, `ap-southeast-1`
- ECS: `quant-candidate-node`, `ecs.t5-lc1m1.small`
- Capacity: 1 vCPU / 1 GiB RAM / 40 GiB disk
- Billing: Pay-As-You-Go
- OS: Ubuntu 24.04
- Services: `quant-collector`, `quant-engine`
- PostgreSQL: existing isolated `quant` database/schema/user through the existing loopback-only secure tunnel; no PostgreSQL container was added.
- No VPC peering, NAT Gateway, CEN, Redis, Kafka, load balancer, or executor was added.

Final service state: both containers healthy, runtime state `RUNNING`, collector and engine health heartbeats available.

## Real data and Stage1

Final Candidate read-only measurement:

- Instruments/symbols available: 797
- Universe: Top 200
- Klines: 18,307
- Klines by interval: 5m=9,285; 15m=4,474; 1H=2,475; 4H=2,073
- Kline sources: `bitget_v3_rest`=18,080; `bitget_v3_ws`=227
- Market snapshots: 193,238
- Stage1 results: 5,800
- Latest result distribution after restart: A/DEEP_ANALYSIS=5, B/WAIT_TRIGGER=113, C/NO_EDGE=75, D/REJECT=7
- Latest runtime health: collector and engine `AVAILABLE`, `runtime_state=RUNNING`, `buffer_dropped=0`

Two default 5-minute Stage1 cycles after the database connection-lifecycle fix completed successfully. The first produced 200 available inputs; the second produced zero high-confidence candidates and emitted `no_high_confidence_candidate`, without forcing an A result.

Stage1 real inputs are limited to price, bid/ask, volume, turnover, closed 5m/15m/1H/4H candles, local indicators, and market structure.

`NOT_AVAILABLE` and out of scope: semantic OI, semantic Funding, CVD, liquidation, Long/Short, News, AI, Stage2, Evidence Chain, Risk Engine, Order, Position, and Live Trading. Raw ticker fields that resemble OI/Funding remain raw payload only and are not strategy inputs.

## PostgreSQL

Migration set: `001_phase1_core.sql`, `002_runtime_health_and_explainability.sql`, `003_kline_source_exchange.sql`.

Core tables:

`schema_migrations`, `symbols`, `market_observations`, `klines`, `market_snapshots`, `universe_runs`, `universe_members`, `screening_runs`, `screening_results`, `system_health`, `runtime_health_events`, and `outbox_events`.

Migration-from-empty, repeat migration, UTC timestamps, canonical write/read, Stage1 write/read, health write/read, duplicate-safe Kline upsert, and retention checks passed. No order or position tables exist.

Snapshot retention is configurable with `MARKET_SNAPSHOT_RETENTION_DAYS` and defaults to 7 days. Kline retention defaults remain 5m=30 days, 15m=90 days, 1H=180 days, 4H=365 days.

## Health, outage, and restart tests

- Local healthcheck passes only for a fresh `RUNNING` state file and fails for `DEGRADED`/missing state.
- Candidate tunnel outage: only the Candidate `quant-db-tunnel.service` was stopped; both local health files became `DEGRADED`, persistence stopped, `outage_started_at` was recorded in memory, and recovery events were persisted after reconnect.
- Tunnel recovery: tunnel became active, both health files returned `RUNNING`, containers returned healthy, and the engine recovery event recorded the outage duration.
- Restart: both services were restarted after real data existed. Containers returned healthy, Kline count changed only by newly received bars (no abnormal duplicate growth), Stage1 results remained explainable, and WS counters resumed.
- Collector reconnect evidence: public WS close-frame events were observed and automatically recovered; health recovery events and REST gap recovery remained active.

## Resources

Final measured sample:

- Collector: 93.76 MiB / 256 MiB, 36.62% of limit
- Engine: 52.32 MiB / 384 MiB, 13.62% of limit
- Host: 959 MiB RAM, 395 MiB available, no swap
- CPU sample: collector 31.54%, engine 0.00%
- Disk: 40 GiB volume, 4.8 GiB used, 33 GiB available
- PostgreSQL database: 351 MB
- Docker JSON logs: 24 KiB collector, 4 KiB engine in the measured sample
- Docker log policy: 10 MiB per file, 3 files per service
- No OOM event and no restart loop observed.

Growth classification:

- `MEASURED_SAMPLE`: the database increased from approximately 197 MB to 351 MB during the observed runtime window while market snapshots were being collected.
- `PROJECTED_ESTIMATE`: no 30/90/180/365-day actual growth claim is made; longer retention projections require a longer production-like sample and PostgreSQL vacuum/reuse measurements.

## Safety scan

Passed local source and deployment scans:

- No Bitget private client.
- No private API key/secret/passphrase requirement.
- No order submit route.
- No position API.
- No Live Executor.
- `TRADING_MODE=paper` enforced by configuration.
- No real orders were generated.
- No `.env`, credential, private key, or runtime log was committed.

## Tests

- Local regression: `84 passed, 1 skipped`.
- The only local skip was `tests/test_repository_integration.py` because the Windows workspace did not expose `TEST_POSTGRES_DSN`; Candidate PostgreSQL integration was run separately and passed.
- Added coverage includes lifecycle/scheduler/shutdown, WS-to-canonical conversion, closed-bar-only persistence, reconnect/resubscribe, A-capacity, reason codes, outage/recovery, healthcheck, bounded buffers, migration race prevention, memory/resource limits, and safety constraints.

## Known issues and Phase 2 preconditions

- Bitget public WS occasionally closes a connection without a close frame. This is handled by reconnect, resubscribe, and REST gap recovery; it creates short-lived recovery events but did not leave the runtime degraded.
- PostgreSQL and market-snapshot growth must be monitored with a longer measured sample before increasing retention or universe size.
- Phase 2 must not begin until a separate human approval is given. Advanced data, AI, evidence chain, risk, orders, positions, and live trading remain intentionally absent.

## Git

- Branch: `phase1`
- HEAD: recorded after the final commit below
- Required report and runtime changes are committed.
- Working tree: clean after commit.

`PHASE_1_ACCEPTED`
