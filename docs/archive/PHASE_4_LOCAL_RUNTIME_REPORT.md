# Phase 4 Local Runtime Report

## Status

`PHASE4_EXTERNAL_SOURCE_GATE_PENDING`

This report covers the independent WSL2 local runtime only. No Jakarta,
Hangzhou, Singapore, remote PostgreSQL, or Phase 5 operation was performed.

The local core runtime is paper-only. The Bitget liquidation source gate is
now resolved after correcting a live-schema mismatch and observing real
events. The remaining Phase 4 public data gate is blocked by official Bybit
REST and WebSocket HTTP 403 responses in this environment. No substitute
endpoint, proxy, VPN, tunnel, or mocked advanced data was used.

## Repository and build

- WSL repository: `/home/lucas045057/projects/quant`
- Branch: `phase4`
- Starting baseline: `63b9cdda8214b4b11c1e45d926f114f84f13cbb8`
- Intermediate test-normalization commit: `e6b34bda0fa38d1b2524230566b41c4e0dc677e1`
- Bitget live-schema fix commit: `1ef8df27aa29e2d3f86a815d8cfd994521d23499`
- Final working tree before the report commit: clean after staging the intended
  local runtime changes; the final HEAD is recorded in the completion response.
- Runtime image tag: `quant-phase4:63b9cdd-local`
- Runtime image digest after the live-schema fix: `sha256:0ff3d81bd604cfab05d3154cb4c25b61585a2af1efe89fae00d0d801b7830ee7`
- Compose validation: passed
- Compose services: `quant-postgres`, `quant-collector`, `quant-engine`
- PostgreSQL image: `postgres:16-alpine`
- Host port mappings: none
- Trading mode: `paper`
- Database DSN: local Docker service `postgres`, database `quant`

The local profile is intentionally low-resource. It keeps the Top200 universe
logic while bounding live canonical candle retention, reducing trade stream
scope and queue/dedup capacities, and using configured per-timeframe retention
values. `.env.local` is ignored and was not committed.

## Implemented local changes

- Added `docker-compose.local.yml` with an independent PostgreSQL volume and
  explicit memory limits.
- Added local-only ignore rules for environment files, credentials, dumps and
  compressed SQL archives.
- Bounded the collector's live canonical Kline store to four recent bars per
  key; REST bootstrap remains independently configurable.
- Fixed Phase 4 JSONB persistence by adapting JSON payloads through psycopg's
  `Jsonb` wrapper.
- Fixed PostgreSQL `TEXT[]` persistence by passing lists rather than malformed
  tuple literals.
- Fixed Bitget UTA v3 liquidation update parsing: live update messages may omit
  `arg.symbol`; the adapter now validates and uses each required
  `data[].symbol` while preserving the requested channel contract.
- Made the Phase 4 upgrade-path assertion deterministic with `ORDER BY id`.
- Updated integration expectations for the nine-migration Phase 4 baseline.

## Database and migration acceptance

- Database: `quant`
- `current_database()`: `quant`
- PostgreSQL timezone: `UTC`
- First migration run on the empty local runtime database: migrations 001–009
- Second migration run: 0 new migrations
- Fresh isolated integration database (`quant_test_acceptance`): first-run,
  duplicate/idempotency, UTC, retention, Phase 3 and Phase 4 persistence tests
  all passed.
- Explicit PostgreSQL integration result: `20 passed`
- Runtime database has ten migration records because the Phase 4 repair marker
  is also recorded: 001–009 plus `009_phase4_metrics.repair.v1`.

## Runtime observation

- Observation window: 30 samples, approximately
  `2026-09-21T19:17:38Z` through `2026-09-21T19:47:57Z`
- Collector and engine remained running throughout the observation.
- Collector restart count: 0; OOM: false
- Engine restart count: 0; OOM: false
- PostgreSQL restart count: 0; OOM: false
- Final observed runtime counts:

| Table | Rows |
|---|---:|
| `symbols` | 803 |
| `exchange_instruments` | 311 |
| `market_snapshots` | 146,448 |
| `market_observations` | 437,515 |
| `klines` | 82,332 |
| `open_interest` | 3,408 |
| `funding_rates` | 1,838 |
| `long_short_observations` | 2,989 |
| `basis_snapshots` | 2,800 |
| `trade_flow_windows` | 1,833 |
| `cross_exchange_flow_snapshots` | 561 |
| `cross_exchange_derivative_snapshots` | 2,200 |
| `cross_exchange_phase4_snapshots` | 2,958 |
| `stage1_derivative_enrichment` | 2,600 |
| `stage1_flow_enrichment` | 2,600 |
| `stage1_phase4_enrichment` | 2,400 |
| `screening_results` | 2,800 |
| `screening_runs` | 14 |
| `system_health` | 5 |
| `runtime_health_events` | 15 |
| `liquidation_events` | 0 |
| `liquidation_windows` | 0 |
| `cvd_snapshots` | 0 |

Phase 4 basis processing and typed unavailable/error persistence operated as
designed. Long/short remained unavailable/error when its official Bybit source
returned 403. Liquidation remained degraded/unavailable when no official
stream event was available. These were not replaced with synthetic data.

## Restart and outage checks

- Collector/engine controlled restart: services returned successfully; key
  counts were preserved and no duplicate Kline rows were observed.
- PostgreSQL short outage: only local `quant-postgres` was stopped and started;
  it returned to `healthy`, `current_database()` remained `quant`, timezone
  remained UTC, and the key counts were preserved.
- No volume deletion or database recreation was performed for the runtime
  database.

## Resource validation

Container limits:

- `quant-postgres`: 512 MiB
- `quant-collector`: 512 MiB
- `quant-engine`: 384 MiB

Final point-in-time sample:

- PostgreSQL: 316.2 MiB / 512 MiB
- Collector: 143.9 MiB / 512 MiB
- Engine: 227.4 MiB / 384 MiB
- Final observed peaks during the 30-minute window were approximately 345 MiB,
  146 MiB and 227.4 MiB respectively.
- Final container CPU sample: PostgreSQL 0.11%, collector 10.29%, engine 0.43%.
- WSL host at final check: 28 CPUs, 31 GiB memory, load average 0.25/0.19/0.18.
- PostgreSQL database size: `635 MB`
- Docker local volume size: `1.085 GB`
- Docker log byte samples from `docker logs`: collector 14,184 bytes, engine
  1,789 bytes, PostgreSQL 14,820 bytes. These are read-only CLI byte counts,
  not a long-term log-growth estimate.

## Public contract probes

### Bybit diagnostic

All probes used official public endpoints only, no credentials, no body, no
authentication and no proxy. `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` were
not present.

- DNS resolved `api.bybit.com` to `198.18.0.25` and
  `stream.bybit.com` to `198.18.0.73` in this WSL environment.
- Direct TLS to `api.bybit.com:443` succeeded with TLS 1.3, certificate
  verification `OK`, certificate `CN=*.bybit.com`.
- `GET https://api.bybit.com/v5/market/time`: 403 twice.
- `GET https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT`:
  403 twice.
- `GET https://api.bybit.com/v5/market/instruments-info?category=linear&symbol=BTCUSDT`:
  403 twice.
- All three responses were CloudFront HTML `Request blocked` responses with
  `server: CloudFront`, `x-cache: Error from cloudfront`, and SIN CloudFront
  POPs. This is classified as `EXCHANGE_ACCESS_LIMITATION`, not a project
  request-format bug.
- Direct `wss://stream.bybit.com/v5/public/linear` handshake: HTTP 403 from
  CloudFront; no subscription ACK was possible.

Bybit classification: `EXCHANGE_ACCESS_LIMITATION`.

### Bitget liquidation diagnostic

- Endpoint: `wss://ws.bitget.com/v3/ws/public`.
- DNS and direct TLS verification passed; no proxy/VPN/tunnel was used.
- Subscription payloads were the existing adapter contract with
  `instType=usdt-futures`, `topic=liquidation`, and bounded symbols
  `BTCUSDT`/`ETHUSDT`.
- ACKs were received for each subscription.
- A real update revealed the live schema: `arg` contains `instType/topic`
  without `arg.symbol`; the instrument is in `data[].symbol`.
- The minimal adapter fix was committed as
  `1ef8df27aa29e2d3f86a815d8cfd994521d23499`; it keeps required row-level
  symbol validation and does not change liquidation semantics.
- Post-fix 60-second gate: 2 ACKs, 2 real rows, 2 parsed rows, zero parser
  errors, status `AVAILABLE`.
- Post-fix 5-minute bounded observation: 3 connections, 2 reconnects, 6 ACKs,
  10 real rows, 10 parsed rows, zero parser errors, 17 ping/17 pong pairs, and
  2 server disconnects recovered by the bounded reconnect loop.
- Preserved metadata: `source_granularity=AGGREGATED_MAX_PER_SECOND`,
  `coverage_semantics=PARTIAL_AGGREGATED`.

Bitget classification: `PASS`.

- Bitget v3 REST instruments/tickers/candles: passed.
- Bitget v3 WebSocket ticker/Kline: passed.
- Hyperliquid public REST info contract: passed.
- Bitget liquidation WebSocket after the parser fix: passed with real events.
- The existing 15-second combined live test can still fail when Bitget emits no
  event in that short window; it was not weakened or changed.
- Phase 4 basis/long-short probes depending on Bybit: failed as a consequence
  of the official Bybit 403.

Earlier aggregate public probes were `5 passed, 5 failed` for the combined
Bitget/Bybit/Phase 3/Phase 4 suite, plus `1 passed` for the isolated
Hyperliquid probe. The remaining failure class is the Bybit external access
limitation, not a silently skipped test.

## Test results

- Full default regression after the Bitget fix: `445 passed, 11 skipped`
- Explicit fresh-database PostgreSQL integration: `20 passed`
- Phase 4, liquidation and runtime safety regression: `71 passed`
- Collector bounded-store and persistence tests: passed
- Migration idempotency: passed; second run returned no new migrations
- Bitget live liquidation parser gate: passed with real official messages

The 11 default skips are the intentionally gated live public probes and tests
whose database DSN is not set in the default process environment. The
database-gated tests were explicitly rerun against the fresh local acceptance
database and passed.

## Safety checks

- `TRADING_MODE=paper`: confirmed in collector and engine containers.
- No private API credentials were configured.
- No order API, position API, or live executor was enabled.
- No real order path was exercised.
- No public PostgreSQL port was exposed.
- No remote environment was connected or modified.
- No Jakarta migration or Phase 5 activity was performed.
- No proxy, VPN, tunnel, unofficial endpoint, or hard-coded CDN IP was used.

## Blockers to local acceptance

1. Bybit official public REST and WebSocket access returns HTTP 403 from
   CloudFront in the current WSL network environment. Phase 4 long/short and
   related cross-exchange completeness cannot be accepted as available.

During this diagnostic, local PostgreSQL also received normal Docker
`fast shutdown request` signals and exited with code 0, without OOM. It was
manually restarted and returned healthy; this is recorded as a local Docker
lifecycle issue, separate from the Bybit source gate. A later read-only check
also observed all three local `quant-*` containers receiving the same normal
shutdown at `2026-09-22T01:34:58Z`; all exited 0 with `OOMKilled=false`.
No volume or database data was deleted. This Docker lifecycle issue must be
resolved before any future full runtime acceptance, but it does not change the
Bybit source classification.

The runtime remains safe to inspect in paper mode, but the above blockers mean
the final state is deliberately:

`PHASE4_EXTERNAL_SOURCE_GATE_PENDING`

Next minimum retry condition: repeat the same direct Bybit public REST/WS
probes from a network path where the official endpoints no longer return
CloudFront 403, then rerun the Bybit-dependent Phase 4 gates. No Phase 5 work
should start until that external source gate is resolved and the failed probes
are rerun successfully.
