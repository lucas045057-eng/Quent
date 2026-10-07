# Phase 3 Final Runtime Acceptance Report

**Acceptance timestamp:** 2026-09-21 04:45:56 UTC  
**Status:** `PHASE_3_ACCEPTED`  
**Region:** `ap-southeast-5`  
**Instance:** `i-k1a3kehc63k38qlzz661`  
**Trading mode:** `paper`

## 1. Deployment

- Application image: `quant-phase3:3f7f5aa`
- Collector and engine were rebuilt from commit `3f7f5aaadd35181140985f11a7b33e18234c803d7`.
- PostgreSQL container and data volume were preserved during application deployment.
- Final containers: collector, engine, and the existing Quant PostgreSQL container only.
- PostgreSQL has no host port mapping.

## 2. Public connectivity and contracts

All probes ran from the actual Quant engine container with public endpoints only and no proxy, VPN, tunnel, API key, secret, or passphrase.

| Exchange | REST | WebSocket | Result |
|---|---|---|---|
| Bitget UTA v3 | instruments, tickers, fills: HTTP 200; `code=00000` schemas | `wss://ws.bitget.com/v3/ws/public`; `publicTrade`; `kline` intervals `5m`, `15m`, `1H`, `4H` | PASS |
| Bybit V5 | recent trade: HTTP 200; `retCode=0` schema | `wss://stream.bybit.com/v5/public/linear`; `publicTrade.BTCUSDT` | PASS |
| Hyperliquid public | POST `https://api.hyperliquid.xyz/info`, `metaAndAssetCtxs`: HTTP 200 | `wss://api.hyperliquid.xyz/ws`; `trades/BTC` | PASS |

The first Hyperliquid REST attempt in the probe used GET and was discarded as a probe-method error. The corrected official POST contract passed.

## 3. Runtime data observed

Acceptance sample window: `2026-09-21T04:34:27Z` to `2026-09-21T04:45:56Z` (11m29s). Counts below are measured database totals, including the restored baseline.

| Data | Final count |
|---|---:|
| Symbols | 797 |
| Klines | 120,380 |
| Market snapshots | 1,613,130 |
| Market observations | 366,114 at the resource snapshot |
| Screening results | 15,400 |
| Trade-flow windows | 3,773 |
| CVD snapshots | 3,548 |
| Cross-exchange flow snapshots | 1,269 |
| Stage1 flow enrichment | 1,600 |

Trade-flow exchange distribution at the final probe: Bitget 2,160; Bybit 1,380; Hyperliquid 233. CVD was populated from Bybit public trades, as designed.

Integrity checks:

- Closed-kline non-AVAILABLE rows: `0`
- Kline duplicate key rows: `0`
- Trade-flow duplicate key rows: `0`
- Stage1 enrichment is context-only; no order, position, or live execution path was created.

## 4. Database and migrations

- `current_database() = quant`
- Runtime application connection: `TimeZone=UTC`, `search_path=quant`
- `quant_app` and `quant_owner`: superuser, createdb, createrole, replication, and bypassrls all `false`
- Migration runner: first run `[]`; second run `[]`
- Result: migration idempotency PASS
- Database size at final resource sample: `2,514,279,447` bytes (`2398 MB`)
- Largest table: `market_snapshots`, approximately `1.82 GB`

## 5. Restart and recovery

### Application restart

- Restarted only `quant-collector` and `quant-engine` using the runtime compose file.
- Both returned to healthy with zero container restarts and `OOMKilled=false`.
- PostgreSQL remained healthy and was not restarted by the application restart.
- After bootstrap/hydration, collector and engine heartbeats returned to `AVAILABLE`; final heartbeat ages were 5s and 26s.
- Flow, CVD, and Stage1 data continued increasing.

### PostgreSQL outage recovery

- Stopped only `postgres-quant-postgres-1` for the controlled test.
- During the outage, both application containers remained running.
- PostgreSQL became healthy after six 2-second polling intervals after restart.
- Post-recovery heartbeats were fresh: collector `2026-09-21T04:42:17Z`, engine `2026-09-21T04:42:25Z`.
- Flow windows increased from 3,649 to 3,671 and CVD snapshots from 3,400 to 3,452 across the recovery observation.
- No data volume was removed and no other service was restarted.

## 6. Resource validation

Measured host sample at `2026-09-21T04:44:02Z`:

- Host: 2 vCPU, 1.7 GiB RAM, load average `0.66 / 0.91 / 1.12`
- Available memory: approximately 832 MiB; no swap
- Root disk: 20 GiB, 7.6 GiB used, 42%
- Collector: 40.05 MiB / 256 MiB (15.65%)
- Engine: 105.8 MiB / 384 MiB (27.55%)
- PostgreSQL: 76.74 MiB / 768 MiB (9.99%)
- No OOM events observed
- Docker disk usage: 792.7 MB images, 278.5 MB containers, 3.176 GB active volume
- Application logs: collector 13,682 bytes; engine 1,163 bytes; PostgreSQL 40,254 bytes at sample time
- Collector and engine use `json-file` rotation of 10 MB × 3. The existing PostgreSQL container reported no Docker log rotation options; this is a long-term operations warning, not a runtime acceptance failure.

## 7. Safety and regression

- `TRADING_MODE=paper` verified in both application containers.
- No private API credentials present in application environment.
- No private API client, order route, position API, live executor, or real-order path detected by safety tests and source review.
- No real order was submitted.
- Live-flagged local regression: **234 passed, 2 skipped**, 9.51s.
- Skipped tests were PostgreSQL integration tests requiring local `TEST_POSTGRES_DSN`; target PostgreSQL integration was executed separately and passed.
- Targeted safety/resource suite: **18 passed, 2 skipped**; the two skips were the same local-DSN-gated integration tests.

## 8. Known issues and follow-up

- Public WebSocket connections occasionally emit a close-frame reconnect warning; automatic reconnect and recovery were observed and data flow continued.
- The restored database is already approximately 2.4 GB, dominated by `market_snapshots`; retention and disk monitoring remain required for long-term operation.
- PostgreSQL Docker log rotation should be made explicit in a future maintenance change if the existing PostgreSQL deployment is changed. It is not part of this acceptance change.

## 9. Git state

- Branch: `phase3`
- HEAD before this report: `3f7f5aadd35181140985f11a7b33e18234c803d7`
- Report commit: recorded in the final Git state below.
- Credentials, `.env` files, database passwords, API keys, and runtime log dumps are not included.
