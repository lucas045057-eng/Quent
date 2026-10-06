# Realtime Paper Readiness V1 design

Date: 2026-09-29

## Goal and non-goals

Add an operational long-running REALTIME PAPER readiness monitor that consumes the existing Phase1–8 public collectors and canonical PostgreSQL data, evaluates the existing Stage1 logic, records each observation/decision outcome durably, exposes readiness and source freshness in the existing Dashboard, and fails closed before any order unless the existing approved strategy, Risk, Paper, and reconciliation prerequisites are genuinely available.

This work does not create a market collector, OI/funding/trade-flow model, canonical feature pipeline, default strategy, policy approval, stop rule, risk limits, Live adapter, GPT call, or Jev configuration. Live remains disabled. `REAL_PUBLIC_DATA` and fixture/synthetic sources are separately typed; formal 24-hour acceptance requires only `REAL_PUBLIC_DATA` continuously for 24 hours.

## Audited baseline

- `CollectorService` already bootstraps Bitget UTA v3 public REST and runs public WS ticker/Kline, Phase3 trade-flow, and configured Phase4/6/7/8 context workers; its existing repositories persist into PostgreSQL.
- The engine runs Phase2 OI/funding when enabled, bootstraps once, and thereafter rebuilds Stage1 input through `Phase1Repository.load_latest_market_batch()` from canonical `symbols`, `market_snapshots`, and `klines`.
- Phase9 is disabled by default; if enabled, it requires a valid approved policy manifest. The checked-in pattern list is empty and Stage1 persists no validity deadline.
- Risk, intent creation, Nautilus, and fixture Paper are implemented in separate modules but have no autonomous Stage1-to-Paper production caller.
- The audited containers have no running `quant-collector`; canonical latest inputs cannot be assumed fresh.

## Design

1. Add `quant_realtime_paper` as an orchestration/observability sidecar. It opens PostgreSQL read-only, calls existing `Phase1Repository.load_latest_market_batch()` and `run_stage1()` for BTCUSDT/ETHUSDT, reads current collector/engine/Phase9/paper prerequisites, and never writes to canonical Phase1–8 tables.
2. Persist only operational session/cycle records to a local SQLite store: session identity/config/code/data provenance, counts, canonical source references/digests, Stage1 output, readiness result, risk outcome/reason, errors, and timestamps. Do not duplicate market-data, OI/funding, flow, or features tables. The SQLite store is a run ledger, not a market data layer.
3. Evaluate a deterministic fail-closed gate each cycle. Required checks include PostgreSQL reachable/read-only, current collector heartbeat, real public Bitget source provenance, fresh ticker and all Stage1 timeframes, wall/monotonic clock consistency, Phase9 enabled plus approved non-empty policy, Stage1 candidate validity, configured Risk policy, Paper engine, reconciled state, and Live disabled. Any failed hard check produces `PAPER NOT READY` / `DO NOT TRADE`. Stage1 observations are recorded even when the gate blocks.
4. Do not synthesize a Phase9 `DecisionCandidate`, `ExecutionIntent`, account, paper fill, or reconciliation status. With current policy prerequisites absent, record Phase9 as `NOT_CONFIGURED`, Risk as `REJECTED` with the blocking reason, and zero orders.
5. Add a GET-only Dashboard endpoint and a compact readiness/market/decision panel using the same canonical PG queries and operational SQLite ledger. Preserve the existing dashboard structure and local-only/read-only security.
6. Expose one CLI accepting `15m`, `24h`, `72h`, `7d` (or equivalent duration syntax), plus start/stop/status PowerShell wrappers. The runner starts/stops only its own process and never starts or restarts Docker services.
7. Acceptance distinguishes a short real-feed smoke from a formal 24h pass. The smoke reports `REAL_PUBLIC_DATA` only when live canonical Bitget source rows and the collector health heartbeat are current. Fixture tests can test safety logic but cannot pass real-data acceptance.

## Safety invariants

- `LIVE = DISABLED`; mode must be paper-only. No private API keys, authenticated exchange endpoints, or Live execution client.
- Stale, missing, invalid, negative, non-finite, duplicate, database, clock, health, or reconciliation signals block new paper orders.
- Since the current approved strategy, TTL, Risk runtime configuration, and Paper runtime linkage are missing, the V1 runner records `DO NOT TRADE` and does not call order submission.
- Existing services and canonical schema are not migrated or restarted by the runner.

## Verification

Unit tests cover source classification, all readiness hard stops, stale/invalid market data, clock jumps, duplicate cycle idempotency, persistent session resume/reconcile semantics, retry/backoff supervision, and prohibition of order submission under unready conditions. Dashboard tests cover truthful source provenance and stale/empty output. Integration/smoke tests use the existing canonical reader; only a run backed by live canonical Bitget rows can be labeled `REAL_PUBLIC_DATA`. Run the full original test matrix, Dashboard tests, new tests, build, and short real-feed acceptance if network/data availability permits.
