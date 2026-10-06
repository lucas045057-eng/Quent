# Phase 4 Task 8 Report

## Result

Implemented the scoped reviewer fixes for bounded Phase 4 health and runtime
orchestration with explicit `PHASE4_ENABLED` opt-in integration in the existing
collector and context-only engine enrichment.

Review round 2 fixes bind the enabled collector to a fresh Phase4Repository
provider for each bounded REST or liquidation-window persistence operation.
The long-lived runtime no longer needs to retain a PostgreSQL connection.
Health persistence continues through the collector's existing per-cycle
connection. REST rows with `STALE`, `NOT_AVAILABLE`, or `ERROR` status never
produce `RECOVERED`; only fresh same-cycle `AVAILABLE` rows may recover a
component.

## Changed files

- `src/quant_phase4/health.py`
- `src/quant_phase4/runtime.py`
- `src/quant_phase4/adapters/base.py`
- `src/quant_phase4/adapters/bitget_classic_v2.py`
- `src/quant_phase4/adapters/bitget_uta_v3.py`
- `src/quant_phase4/adapters/bybit_v5.py`
- `src/quant_phase1/entrypoints/collector.py`
- `src/quant_phase1/entrypoints/engine.py`
- `tests/test_phase4_runtime.py`

## Behavior covered

- Component-level liquidation, long/short, and basis health snapshots with
  running, degraded, stale, error, and recovered transitions.
- Bounded liquidation queues, deduplication, window state, task registry, and
  symbol selection; no unbounded event or symbol cache.
- Public adapter ingestion, reconnect gap marking, resubscription lifecycle,
  stale REST preservation, database-outage degradation, and recovery.
- Bounded public Bitget UTA v3 and Bybit liquidation WebSocket transport with
  injectable connection/session factories, subscriptions, parsing through
  `ingest_liquidation`, reconnect/resubscribe, gap health, and clean shutdown.
- Settings-driven Phase 4 REST adapters using one runtime-owned session and
  the shared Phase 1 token bucket; Bitget Classic v2 long/short remains an
  isolated public adapter.
- Enabled collector runtime persistence uses an injectable fresh repository
  provider for liquidation windows and REST long/short/basis rows; disabled
  collector behavior remains unchanged.
- TTL/capacity-bounded recent window idempotency keys, STALE recovery on fresh
  REST data, and lazy bounded symbol consumption without iterable materialization.
- Idempotent `start()`/`stop()` and duplicate-window suppression across a
  runtime restart.
- Collector construction and startup are unchanged when Phase 4 is disabled.
  Enabled collector startup uses the same process and selected-symbol source.
- Engine enrichment loads Phase 4 context after the existing Phase 1–3 paths
  and persists only context/status data; the original Stage1 result remains
  unchanged.

## Verification

- Focused Task 8 plus Phase 3 regression: `26 passed`.
- Full suite: `398 passed, 6 failed, 11 skipped`.
- The 6 failures are existing live Bitget/pipeline contract probes blocked by
  the sandbox network restriction; deterministic tests remain green.
- Skips: 8 opt-in public exchange probes and 3 PostgreSQL-gated tests because
  `TEST_POSTGRES_DSN` is not configured.
- `git diff --check`: passed.

No Jakarta connection, deployment, service restart, production migration, or
runtime acceptance was performed. Phase 4 live probes and PostgreSQL runtime
acceptance remain opt-in/pending.
