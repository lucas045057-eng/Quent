# PHASE 2 SERVER FINAL ACCEPTANCE REPORT

Date: 2026-09-20  
Target: Alibaba Cloud ECS in `cn-hangzhou` (public address redacted)  
Trading mode: `paper`  
Final status: `PHASE_2_NOT_ACCEPTED`

## 1. Git and deployment

- Server deployment directory: `/opt/quant-phase2`
- Branch: `phase2`
- HEAD: `f61b033aba0ba50b11906a32b7e53b68ff4e0f21`
- Working tree: clean
- Deployment used an isolated Git checkout; `/opt/quant-phase1` was not overwritten.
- Existing `.env` was copied without outputting its contents. Only non-secret Phase 2 settings were appended.
- `TRADING_MODE=paper` verified.
- No Phase 2 long-running containers were left running because the public API blockers made continuous acceptance unsafe.

## 2. Baseline and production protection

Before Phase 2 work:

- Suixiangji API: running
- Suixiangji admin API: running
- Suixiangji PostgreSQL: running and healthy
- Root filesystem: 40 GiB total, 17% used
- Host memory: 1.7 GiB total, approximately 851 MiB available at baseline
- Swap: 0 B
- Existing Quant deployment: Phase 1 artifact at `/opt/quant-phase1`, without Git metadata and without `quant_phase2`.
- Phase 1 database migrations before Phase 2: `001`, `002`, `003`

No ECS restart, Docker restart, PostgreSQL restart, Suixiangji restart, Security Group change, SSH change, or password change was performed.

## 3. Migration runtime

Migration from the existing Phase 1 schema succeeded:

```text
001_phase1_core.sql
002_runtime_health_and_explainability.sql
003_kline_source_exchange.sql
004_phase2_derivatives.sql
005_phase2_observation_idempotency.sql
```

Migration repeat returned no newly applied versions and did not fail. The following Phase 2 tables and indexes were verified in PostgreSQL:

- `exchange_instruments`
- `open_interest`
- `funding_rates`
- `cross_exchange_derivative_snapshots`
- lookup indexes and observation-key indexes

Phase 1 tables remained present. No `DROP`, `TRUNCATE`, database recreation, or volume deletion was executed.

## 4. Server API contract results

| Exchange | DNS/TCP/TLS | HTTP/schema | Result |
|---|---|---|---|
| Bitget UTA v3 | TLS reset | not available | `ERROR: ConnectionResetError / ClientConnectorError` |
| Bybit V5 | network error/timeout | not available | `ERROR: TimeoutError` |
| Hyperliquid public info | PASS, TLS 1.3 | PASS | `PASS` |

Hyperliquid runtime probe verified metadata, asset contexts, base-asset OI, mark price, funding, and funding history. It returned 178 instruments in the server probe.

The server could not complete the required three-exchange contract test because Bitget and Bybit were inaccessible from the ECS network.

## 5. Real Phase 2 cycle

Two one-shot real cycles were run using the server image and external PostgreSQL network. No mock data was used.

Latest cycle:

- Requested symbols: `BTCUSDT`, `ETHUSDT`
- Snapshot symbols: 2
- OI observations: 2
- Funding observations: 2
- Snapshots: 2
- Errors: Bitget instruments/tickers failed; Bybit instruments timed out; Hyperliquid continued successfully

Persisted totals after the measured sample:

- `exchange_instruments`: 2
- `open_interest`: 4
- `funding_rates`: 4
- `cross_exchange_derivative_snapshots`: 4

All persisted observations came from Hyperliquid because Bitget and Bybit were unavailable.

## 6. Normalization and aggregation

- Hyperliquid OI: `BASE_ASSET`; converted with `raw_open_interest × mark_price`; USD value was available.
- Hyperliquid funding: hourly interval (`3600s`), normalized explicitly to the 8-hour comparison rate.
- Bitget OI: no server observation due network failure. Code path retains raw OI and does not promote an unverified unit into USD aggregation.
- Bybit OI: no server observation due network failure.
- Latest snapshots: `NOT_AVAILABLE`, `INSUFFICIENT_CROSS_EXCHANGE_DATA`.
- Latest snapshot source counts: OI `1`, funding `1`.
- `oi_total_usd`: retained available-source total.
- `oi_weighted_funding`: `NULL`; it is correctly suppressed below the configured two-source minimum.
- Included weighted-funding sources: none.
- Excluded source: Hyperliquid, insufficient cross-exchange source count; Bitget and Bybit unavailable.
- Source divergence: not evaluated as a valid multi-source divergence because the minimum source count was not met.

## 7. OI change and freshness

- 5m OI change: `NOT_AVAILABLE`, insufficient local history
- 15m OI change: `NOT_AVAILABLE`, insufficient local history
- 1H OI change: `NOT_AVAILABLE`, insufficient local history
- 4H OI change: `NOT_AVAILABLE`, insufficient local history
- 24H OI change: `NOT_AVAILABLE`, insufficient local history
- No exchange historical OI series was substituted for local history.
- The current runtime does not yet provide independent OI and funding freshness thresholds/status transitions for server acceptance.
- `STALE` exclusion was not server-validated.

## 8. Stage1 and Universe

- Phase 1 Stage1 source behavior was not changed.
- Phase 2 enrichment boundary tests passed locally, but the server runtime did not produce a continuous Stage1 enrichment stream.
- The current runtime configuration uses the Phase 2 configured BTC/ETH symbols and does not yet consume the full Phase 1 Universe Top-N mapping. This is an acceptance gap.
- 5m/15m/1H/4H/24H history persistence across restart was not accepted.

## 9. PostgreSQL measured sample

- Database size immediately after migration and before Phase 2 observations: `493,452,311` bytes.
- Database size after the measured Phase 2 sample: `537,164,823` bytes.
- Measured sample growth: `43,712,512` bytes. This short interval includes normal database activity and is not a long-term Phase 2 projection.
- OI rows: 4
- Funding rows: 4
- Snapshot rows: 4
- Migration repeat: PASS
- Real insert/read through repository and count queries: PASS
- Full duplicate/idempotency, retention, restart persistence, and DB-outage recovery: NOT RUN

## 10. Resource validation

After the one-shot runtime sample, existing containers remained healthy:

- `suixiangji-staging-api-1`: running
- `suixiangji-staging-wealthmate-admin-api-1`: running
- `suixiangji-staging-db-1`: running, healthy

Measured Docker stats:

- Quant collector: not running
- Quant engine: not running
- Quant bridge: approximately 13.54 MiB
- PostgreSQL: approximately 240.3 MiB
- Host available memory: approximately 860 MiB
- Disk: 40 GiB total, 17% used
- Swap: 0 B

Continuous Phase 2 RAM/CPU, restart-loop, and long-term resource behavior were not accepted because the required public API path was unavailable.

## 11. Restart and DB recovery

- Quant collector/engine restart test: NOT RUN
- REST/WS reconnect: NOT ACCEPTED
- PostgreSQL outage/recovery: NOT RUN
- Suixiangji restart: not performed
- PostgreSQL restart: not performed

## 12. Local regression and skipped tests

Local final regression on commit `f61b033`:

```text
116 passed, 4 skipped
```

Skipped tests:

1. `test_bitget_uta_v3_public_oi_and_funding_contract` — skipped unless `PHASE2_LIVE_CONTRACT=1`; server probe attempted but Bitget was inaccessible. Acceptance gap remains.
2. `test_bybit_v5_public_oi_funding_and_instrument_contract` — skipped unless `PHASE2_LIVE_CONTRACT=1`; server probe attempted but Bybit timed out. Acceptance gap remains.
3. `test_hyperliquid_public_info_contract` — skipped unless `PHASE2_LIVE_CONTRACT=1`; server manual probe covered the same public contract successfully.
4. `test_postgres_migrations_and_kline_upsert_are_restart_idempotent` — skipped because `TEST_POSTGRES_DSN` is not configured locally; server migration and real Phase 2 persistence were partially covered, but the complete integration test remains an acceptance gap.

## 13. Safety scan

- `TRADING_MODE=paper`: PASS
- Private Bitget/Bybit/Hyperliquid trading API: not present in Phase 2 implementation
- API secrets/passphrases: not added
- Order API: absent
- Position API: absent
- Live Executor: absent
- Real orders/positions: none
- Existing Suixiangji/PostgreSQL services: unaffected and healthy

## Blockers

1. Bitget public TLS/HTTP is reset from the current ECS.
2. Bybit public network access times out from the current ECS.
3. Three-exchange server contract acceptance therefore fails.
4. Continuous collector/engine acceptance was not safe to declare because the required Phase 1 Bitget path is unavailable.
5. Full Phase 1 Universe mapping is not yet wired into the Phase 2 runtime; current runtime uses configured BTC/ETH symbols.
6. Independent Phase 2 OI/Funding freshness handling and server validation are incomplete.
7. Stage1 derivative enrichment is tested as a boundary but not integrated into a continuous server result stream.
8. Restart persistence, DB outage recovery, retention, and continuous resource acceptance remain unverified.

No Phase 3 work was started.

`PHASE_2_NOT_ACCEPTED`
