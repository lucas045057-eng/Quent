# Phase 3 Runtime Acceptance Gate

Phase 3 is public-data, context-only, paper mode. These commands never read or require an API key, secret, passphrase, order route, or position route.

## Local regression

```text
python -m pytest -q
```

The expected local skips are explicit:

- `tests/contract/test_phase2_live.py` skips unless `PHASE2_LIVE_CONTRACT=1`.
- `tests/contract/test_phase3_public_live.py` skips unless `PHASE3_LIVE_CONTRACT=1`.
- PostgreSQL integration skips unless `TEST_POSTGRES_DSN` points to an isolated test database.

A skip is not a pass. Runtime acceptance must list every skip.

## Official public contract probes

Use only public endpoints and set symbols explicitly when required:

```text
PHASE3_LIVE_CONTRACT=1
PHASE3_BYBIT_SYMBOL=BTCUSDT
PHASE3_BITGET_SYMBOL=BTCUSDT
PHASE3_HYPERLIQUID_COIN=BTC
python -m pytest tests/contract/test_phase3_public_live.py -q
```

The probes validate HTTP status, response envelope, fields/types, timestamps, source IDs, subscription envelopes, WebSocket upgrade, and first public data payload. They do not infer undocumented side semantics. Any endpoint/schema conflict fails the adapter contract test; no alternate endpoint is selected.

## PostgreSQL integration

Set `TEST_POSTGRES_DSN` to a disposable isolated database, then run:

```text
python -m pytest tests/test_phase3_persistence.py -q
```

The test applies all migrations twice, validates UTC round trips, inserts/reads flow windows, verifies duplicate idempotency, and exercises retention cleanup. Do not point it at the Suixiangji business database.

## Runtime safety

```text
TRADING_MODE=paper
PHASE3_ENABLED=1
python -m pytest tests/test_phase3_runtime.py tests/test_phase3_recovery_freshness.py tests/test_phase3_safety_resources.py -q
```

Phase 3 remains context-only. `STALE`, `PARTIAL`, `NOT_AVAILABLE`, and `ERROR` flow data cannot become a new signal. Bitget and Hyperliquid public side fields remain unknown aggressor semantics; Bybit is the only directional/CVD source in this phase.

Retention covers all Phase 3 tables: timeframe-specific flow windows, CVD, gap events, cross-exchange snapshots (`PHASE3_CROSS_EXCHANGE_RETENTION_DAYS`), and Stage1 flow enrichment (`PHASE3_ENRICHMENT_RETENTION_DAYS`).
