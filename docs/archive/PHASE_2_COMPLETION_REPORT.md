# PHASE 2 COMPLETION REPORT

Date: 2026-09-20  
Branch: `phase2`  
Baseline: Phase 1 `96e78812104e1841898884ed316e673077764aca`  
Trading mode: `paper`

## Final status

`PHASE_2_NOT_ACCEPTED`

### Blocker

- Docker CLI and Compose are installed, but the Docker daemon is unavailable in this environment. `docker version` reports the client and fails to reach `desktop-linux`; `docker info` cannot read server state.
- No local PostgreSQL service, `psql`, or `TEST_POSTGRES_DSN` is available. PostgreSQL migration, write/read, idempotency, restart, database-growth, and resource measurements therefore remain unverified.

No container, database, order, or live-trading result is claimed from this environment.

## Implemented scope

- Canonical UTC OI, funding, instrument, cross-exchange snapshot, and OI-change contracts.
- Explicit symbol registry for `BTC-USDT-PERP` and `ETH-USDT-PERP`; no string-replacement mapping.
- Public Bitget UTA v3 adapter only; no Classic v2 business adapter or fallback.
- Public Bybit V5 linear adapter.
- Public Hyperliquid `info` adapter for the first perpetual DEX.
- Unit-safe OI normalization and explicit funding interval normalization to an 8-hour comparison rate.
- Sparse aggregation with source counts, weighted funding, dispersion, divergence, and failure isolation.
- PostgreSQL migrations for `exchange_instruments`, `open_interest`, `funding_rates`, and `cross_exchange_derivative_snapshots`.
- Deterministic observation keys for retry/restart idempotency, including payload-hash fallback when a provider omits an event timestamp.
- Phase 2 polling is an opt-in task in the existing `quant-engine` process; no Kafka, Redis, extra PostgreSQL, or executor service.
- Phase 1 Stage1 classification is unchanged; derivatives are context only and cannot create an A/B/C/D result.

## Official public API contracts verified

The opt-in live contract suite passed: `3 passed`.

| Source | Actual public contract used | Result |
|---|---|---|
| Bitget UTA v3 | `GET /api/v3/market/instruments?category=USDT-FUTURES`; `GET /api/v3/market/tickers?category=USDT-FUTURES&symbol=...`; `GET /api/v3/market/current-fund-rate?category=USDT-FUTURES&symbol=...` | PASS |
| Bybit V5 | `GET /v5/market/instruments-info?category=linear`; `GET /v5/market/tickers?category=linear&symbol=...`; `GET /v5/market/open-interest?category=linear&symbol=...`; `GET /v5/market/funding/history?category=linear&symbol=...` | PASS |
| Hyperliquid | `POST https://api.hyperliquid.xyz/info` with `metaAndAssetCtxs`; funding history with `fundingHistory` | PASS |

Bitget details verified against live responses:

- `fundInterval="8"` is interpreted as `28,800` seconds.
- `quantityMultiplier` is retained as instrument size metadata.
- `fundingRateInterval="8"` is interpreted as an 8-hour interval.
- `nextUpdate` is not mislabeled as the current observation timestamp.
- Bitget ticker OI is retained as raw data but remains unnormalized because the public response does not provide a verified unit contract.

The initial Phase 2 runtime uses Bitget + Bybit + Hyperliquid. Binance and OKX remain contract-test gated; MEXC OI remains `NOT_AVAILABLE`.

## Real public adapter-chain probe

One live no-database cycle completed successfully:

- instruments: `6` configured rows (2 symbols × 3 sources)
- OI observations: `6`
- funding observations: `6`
- adapter errors: `0`
- Bybit and Hyperliquid OI normalized to USD
- Bitget OI raw values retained but excluded from USD aggregation until unit semantics are verified
- funding intervals: Bitget/Bybit `28,800s`, Hyperliquid `3,600s`, all normalized explicitly to 8-hour comparison rate

## Tests

- Focused Phase 2 unit/adapters/persistence/runtime tests: passed during implementation.
- Official live contract tests with `PHASE2_LIVE_CONTRACT=1`: `3 passed`.
- Full local regression before final report generation: `115 passed, 4 skipped`.
- Default skipped tests: three opt-in live-contract tests and PostgreSQL integration because `TEST_POSTGRES_DSN` was not configured. The live-contract tests were then run explicitly and passed.
- Docker runtime, PostgreSQL integration, restart, database retention/growth, `docker stats`, and RAM/CPU tests: `SKIPPED` because the Docker daemon and PostgreSQL connection were unavailable.

## Safety boundary

- `TRADING_MODE=paper` remains mandatory.
- No private API client, API key/secret/passphrase requirement, order endpoint, position endpoint, live executor, or real order path was added.
- No OI/Funding value is injected into Stage1 classification logic.
- No CVD, liquidation, long/short, news, AI, evidence chain, Stage2, risk runtime, order, or position capability was added.

## Database and deployment

Planned Phase 2 tables:

- `exchange_instruments`
- `open_interest`
- `funding_rates`
- `cross_exchange_derivative_snapshots`

Compose topology remains exactly three services: `postgres`, `quant-collector`, and `quant-engine`. Phase 2 runs inside `quant-engine` and uses the existing resource limits; no new service was introduced. Actual container RAM, CPU, database size, row counts, log size, and restart behavior are not measured because Docker was unavailable.

## Files added or changed

- `src/quant_phase2/`: contracts, symbols, normalization, funding, adapters, aggregation, persistence, enrichment, runtime.
- `src/quant_phase1/config.py`: opt-in Phase 2 settings, still paper-only.
- `src/quant_phase1/entrypoints/engine.py`: failure-isolated Phase 2 polling task in the existing engine.
- `migrations/004_phase2_derivatives.sql`
- `migrations/005_phase2_observation_idempotency.sql`
- `docker-compose.yml`: Phase 2 enabled in the existing paper engine only.
- `tests/test_phase2_*.py`, `tests/contract/test_phase2_live.py`, and Phase 2 audit documentation.

## Acceptance required before marking Phase 2 accepted

1. Start Docker daemon and a clean PostgreSQL database.
2. Apply migrations from empty and repeat them.
3. Run PostgreSQL insert/read and duplicate-key tests for instruments, OI, funding, and snapshots.
4. Start Compose with paper mode, run at least one complete cycle, and capture `docker stats`, RAM, CPU, row counts, DB size, and logs.
5. Restart collector and engine; verify reconnect, persistence idempotency, and health recovery.
6. Re-run the full suite with PostgreSQL integration enabled.

Until those steps are completed, the only valid status is `PHASE_2_NOT_ACCEPTED`.
