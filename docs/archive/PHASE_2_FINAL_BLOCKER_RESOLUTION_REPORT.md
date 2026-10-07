# PHASE 2 FINAL BLOCKER RESOLUTION REPORT

## Final status

`PHASE_2_ACCEPTED`

Phase 3 was not started. `TRADING_MODE=paper` remained enabled throughout the runtime acceptance. No private API, order API, position API, live executor, or real order was introduced or invoked.

## 1. Blocker resolution

### Original network blocker

The Hangzhou ECS remains unsuitable for Bitget and Bybit public market traffic:

- Bitget: DNS resolved, TCP connected, TLS was reset by the edge, and HTTPS returned no usable HTTP response.
- Bybit: DNS resolved, but TCP/TLS timed out or was unreachable.
- Hyperliquid and ordinary HTTPS comparators succeeded from the same host.
- The same behavior was reproduced from the external Docker network.

This is `HOST_FAIL_CONTAINER_FAIL`, not an adapter or API-version failure. The Hangzhou node was not modified and is not the active Phase 2 data node.

### Accepted runtime node

Phase 1's accepted Singapore Candidate ECS was used for Phase 2 runtime acceptance:

- Region: `ap-southeast-1`
- Zone: `ap-southeast-1a`
- Instance: `i-t4n0mrjhd7w29t6k4tzj` (`quant-candidate-node`)
- Type: `ecs.t5-lc1m1.small`, 1 vCPU / 1 GiB RAM / 40 GiB disk
- Billing: PAYG
- Public address: `[REDACTED]`
- Exchange traffic: direct official public HTTPS/WSS; no proxy, VPN, tunnel, or third-party gateway
- PostgreSQL access: existing loopback-only database tunnel to the already isolated `quant` database; no public port 5432

Candidate DNS, TCP 443, TLS, REST, WebSocket upgrade, ticker subscription, and `kline` interval subscription all passed for the required public exchanges.

## 2. Git and code audit

The audited implementation range was `a508c3d..f61b033`, followed by the Phase 2 blocker-resolution commits on branch `phase2`.

The local implementation source at code freeze was:

```text
Branch: phase2
Implementation HEAD: 9b5890c255449a10b6300209ba682f598f6ff9bc
```

The Hangzhou `/opt/quant-phase2` checkout remains clean at `f61b033aba0ba50b11906a32b7e53b68ff4e0f21` and was not used as the active runtime. The Singapore Candidate image was built from the local implementation HEAD above; the Candidate image archive SHA-256 was verified identically at source and destination.

Implemented blocker resolutions:

- Full Phase 1 persisted Universe is loaded from PostgreSQL when no explicit Phase 2 symbol override is supplied; the default is not BTC/ETH-only.
- Exchange-specific symbol registries map the Phase 1 canonical universe to Bitget, Bybit, and Hyperliquid symbols.
- Missing exchange instruments are recorded as explicit `instrument_missing` errors; no symbol or derivative observation is mocked.
- Each adapter has an independent request scheduler, rate limit, jitter, and existing 429 retry/backoff path.
- OI and Funding have independent freshness evaluation and retention settings.
- Bitget OI is fail-closed when the public ticker unit is not confirmed; raw payload is retained but is not promoted to normalized OI or weighted funding input.
- Weighted funding uses only eligible, normalized observations and the configured minimum source count.
- OI history is hydrated from PostgreSQL on restart so change windows are not reset to an empty in-memory state.
- Stage1 derivative information is persisted as context-only enrichment; it does not alter the deterministic Stage1 category.
- Runtime query indexes were added for the latest available market snapshot and kline paths.

## 3. Network and public API acceptance

| Exchange | Public REST | Public WebSocket | Result |
|---|---|---|---|
| Bitget UTA v3 | `https://api.bitget.com` | `wss://ws.bitget.com/v3/ws/public` | PASS |
| Bybit public v5 | `https://api.bybit.com` | public endpoint contract probe | PASS |
| Hyperliquid public | `https://api.hyperliquid.xyz/info` | public data path | PASS |

Bitget runtime used the v3 UTA contract only, including:

- `/api/v3/market/instruments`
- `/api/v3/market/tickers`
- `/api/v3/market/current-fund-rate`
- WebSocket topic `ticker`
- WebSocket topic `kline` with `interval=5m`

The live subscription was:

```json
{"op":"subscribe","args":[{"instType":"usdt-futures","topic":"ticker","symbol":"BTCUSDT"},{"instType":"usdt-futures","topic":"kline","symbol":"BTCUSDT","interval":"5m"}]}
```

No Bitget Classic v2 subscription or private authentication was used.

Fresh Candidate contract probe results:

```text
BITGET_REST=True
BYBIT_REST=True
HYPER_REST=True
WS_SUBSCRIBED=True
WS_TICKER=True
WS_KLINE_5M=True
```

## 4. Real Phase 2 data chain

The accepted runtime exercised:

```text
Bitget / Bybit / Hyperliquid public APIs
  -> exchange adapters
  -> canonical symbol and observation contracts
  -> Universe Top200 from Phase 1 persistence
  -> OI/Funding normalization and independent freshness
  -> market snapshot persistence
  -> deterministic Stage1
  -> context-only derivative enrichment persistence
  -> health heartbeat
```

One real Universe cycle produced:

- `SYMBOLS=200`
- `OI=483`
- `FUNDING=483`
- `SNAPSHOTS=200`
- Explicit instrument-missing errors for symbols unavailable on a particular exchange; no fallback or fabricated rows

The persistent runtime later recorded:

- `weighted funding AVAILABLE=816`
- `STALE_OI=0`
- `STALE_FUNDING=0`
- `NOT_AVAILABLE_OI=0`
- `NOT_AVAILABLE_FUNDING=0`

Stage1 remains deterministic and fail-closed: stale or unavailable required data cannot produce a new valid derivative-enriched decision. Advanced data not available from a source remains explicitly unavailable.

## 5. PostgreSQL and migration acceptance

The isolated `quant` database was used. No existing Suixiangji business tables were modified.

Migration result:

- Migrations `001` through `007` applied successfully from the existing Phase 1 baseline.
- A repeat migration run returned no pending migrations and did not alter or corrupt data: `MIGRATION_REPEAT=[]`.
- Database timezone was verified as UTC.
- `exchange_timestamp`, `fetched_at`, and `processed_at` remain separate fields.

Persistence and idempotency:

- Canonical OI, Funding, cross-exchange snapshots, Stage1 results, enrichment rows, and health rows were written and read successfully.
- Duplicate observation insertion was checked inside a transaction and rolled back after the expected conflict check: `DUPLICATE_TX=ROLLBACK_AFTER_CONFLICT_CHECK`.
- Counts were unchanged by the duplicate test.
- Restart history hydration returned `HISTORY_KEYS=284` and `HISTORY_ROWS=2263`.

Retention validation:

```text
RETENTION_BEFORE=(4346, 3049, 1804)
RETENTION_AFTER=(4346, 3049, 1804)
RETENTION_POLICY=30d/90d/30d
```

No Phase 2 rows were old enough to delete during the measured run. The retention path executed successfully without destructive deletion of current data.

## 6. Runtime, restart, and outage recovery

### Measured sample

The measured continuous sample was approximately 5 minutes 31 seconds. It is reported as `MEASURED_SAMPLE`; no 30/90/180/365-day real-world growth claim is made.

Before sample:

- OI: 2,898
- Funding: 2,091
- Market snapshots: 1,204
- Enrichment: 400
- Database size: 668,040,215 bytes

After sample:

- OI: 3,380
- Funding: 2,409
- Market snapshots: 1,404
- Enrichment: 600
- Database size: 687,660,055 bytes
- Measured database delta: +19,619,840 bytes

Current runtime snapshot after subsequent cycles:

- `market_snapshots`: 411,414 rows
- `klines`: 26,550 rows
- `screening_results`: 9,600 rows
- `exchange_instruments`: 486 rows
- `open_interest`: 3,863 rows
- `funding_rates`: 2,732 rows
- `cross_exchange_derivative_snapshots`: 1,604 rows
- `stage1_derivative_enrichment`: 1,000 rows
- Database size: 700,202,007 bytes

### Restart test

After real data had been collected, both Quant containers were restarted without restarting PostgreSQL or Suixiangji:

- Containers returned to healthy state.
- Restart count remained 0 after recovery.
- OI count remained unchanged at the pre-restart count; no abnormal duplicate burst was observed.
- Migrations remained `001` through `007`.
- Health rows recovered to `AVAILABLE`.
- Collector automatically reconnected and WebSocket subscriptions resumed.

### Database outage and recovery

Only the Candidate's existing database tunnel service was stopped for the controlled outage test and then started again. PostgreSQL itself, Docker, and Suixiangji were not restarted.

- During outage: collector degraded as expected; engine remained running.
- After tunnel recovery: tunnel active, both Quant containers healthy, restart count 0, database reachable, and health rows `AVAILABLE`.

## 7. Resource validation

Latest Candidate read-only resource probe:

```text
quantphase2-quant-collector-1 | running | healthy | restart=0
quantphase2-quant-engine-1    | running | healthy | restart=0
collector: 13.15% CPU, 141.4 MiB / 256 MiB
engine:    0.00% CPU, 80.18 MiB / 384 MiB
host RAM: 959 MiB total, 313 MiB available at sample time
root disk: 5.2 GiB used, 33 GiB available, 14%
```

Compose uses only the two required Quant services on the Candidate. Log rotation is configured at 10 MiB per file, three files per service. Latest observed application log files were approximately 12 KiB for collector and 1 KiB for engine.

The existing Hangzhou PostgreSQL container remained healthy during the checks and was not restarted. No additional PostgreSQL, Redis, Kafka, load balancer, NAT gateway, peering, or CEN resource was created.

## 8. Safety scan

Passed:

- No Bitget private client.
- No order submit route.
- No position API.
- No API key, secret, or passphrase requirement.
- No live executor.
- No live-trading default path.
- `TRADING_MODE=paper`.
- No proxy, VPN, tunnel, or third-party gateway for exchange traffic.
- No real order was generated.
- Suixiangji containers were not restarted or modified.

The only tunnel used was the pre-existing loopback database tunnel required to reach the isolated PostgreSQL database. It was not used for Bitget, Bybit, or Hyperliquid traffic.

## 9. Tests

Final local regression:

```text
123 passed, 4 skipped in 6.04s
```

Skipped tests:

1. Bitget live OI/Funding contract probe: requires `PHASE2_LIVE_CONTRACT=1`; equivalent real public runtime probe passed on Candidate.
2. Bybit live OI/Funding contract probe: requires `PHASE2_LIVE_CONTRACT=1`; equivalent real public runtime probe passed on Candidate.
3. Hyperliquid live OI/Funding contract probe: requires `PHASE2_LIVE_CONTRACT=1`; equivalent real public runtime probe passed on Candidate.
4. Local PostgreSQL repository integration test: `TEST_POSTGRES_DSN` is not configured on the Windows development host; the required migration, persistence, idempotency, UTC, retention, outage, restart, and health checks passed against the isolated Candidate-connected PostgreSQL runtime.

Additional checks passed:

- `git diff --check`
- REST schema and real-response contract probes
- WebSocket subscription and reconnect probes
- 429 retry/backoff path tests
- UTC timestamp tests
- closed-bar and freshness tests
- deterministic Stage1 tests
- no advanced-data mock tests
- restart idempotency tests
- no-private/order/live safety tests
- migration and retention tests
- resource-limit tests

## 10. Known limitations and follow-up

- The original Hangzhou node still has the documented Bitget edge reset and Bybit reachability failure. It was not bypassed or modified. If it is to become an active exchange node, its network policy must be resolved separately.
- A finite set of Phase 1 symbols has no matching Bybit instrument. Those rows are represented as explicit unavailable/instrument-missing data and are not mocked.
- The database growth observation is a short `MEASURED_SAMPLE`; long-term growth must be re-estimated after a longer production-like window.
- The local Windows PostgreSQL integration test remains skipped because no local test DSN is configured. This does not invalidate the completed Candidate runtime integration evidence.
- Phase 3 remains out of scope and was not started.

## Final acceptance

```text
PHASE_2_ACCEPTED
```

