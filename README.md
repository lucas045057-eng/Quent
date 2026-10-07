# Crypto Perpetual Intelligence & Execution Platform

Quant Paper V2 now owns new research candidates over the existing Quant Core Phase1–9 data, persistence and lifecycle infrastructure. Python Risk Policy approves framework-neutral execution intents; the isolated adapter uses pinned NautilusTrader 1.231.0 for local backtests and Sandbox Paper order/position lifecycle.

The runnable local integration, research export, funding accounting, process recovery and acceptance commands are documented in [Nautilus integration](docs/NAUTILUS_INTEGRATION.md). Local acceptance is explicitly fixture-driven. Production strategy approval and Live trading are deferred.

V2 configuration, approval boundaries, Dashboard views and source activation blockers are documented in [Quant Paper V2](docs/QUANT_PAPER_V2.md).

## Existing public data foundation

Paper-only Bitget UTA v3 public market-data foundation.

The default public collection cycle uses `KLINE_FETCH_LIMIT=100` per symbol and timeframe so the 200-symbol universe stays within the Phase 1 collector memory budget; PostgreSQL owns the configured per-timeframe retention.

Phase 1 has no private API client, API key requirement, order route, position route, or live executor. Run tests with:

```text
python -m pytest -q
```

## Current cleanup baseline

See [cleanup report](AI_TECH_DEBT_CLEANUP_REPORT.md), [audit](AI_TECH_DEBT_AUDIT.md), [dependency map](MODULE_DEPENDENCY_MAP.md), [configuration authority](CONFIG_SOURCE_OF_TRUTH.md), [entrypoints](ENTRYPOINT_MAP.md), and [Nautilus retention](NAUTILUS_RETENTION_DECISION.md). Historical reports are indexed in [docs/archive](docs/archive/README.md). Cleanup preserves the current data and safety boundaries; Freqtrade is not implemented.
