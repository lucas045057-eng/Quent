# Phase 1 Final Continuous Runtime Fix

## Goal

Convert the Phase 1 collector and engine from one-shot jobs into bounded, graceful, paper-only services with production Bitget UTA v3 public WebSocket ingestion, deterministic Stage1 classification, and explicit runtime health/recovery persistence.

## Steps

1. Extend configuration and canonical result contracts for service schedules, bounded buffers, explainability, and local runtime health.
2. Add failing tests for Stage1 A-cap and reason codes, service lifecycle/scheduler/shutdown, WebSocket event ingestion, outage recovery, and health checks.
3. Implement deterministic Stage1 hard filters with at most five A/DEEP_ANALYSIS results and explicit B/C/D outcomes.
4. Implement long-running collector and engine loops with SIGTERM/SIGINT cancellation, bounded event buffers, WS reconnect/resubscribe, closed-bar filtering, and REST gap recovery.
5. Extend PostgreSQL migrations/repository persistence for explainability and outage/recovery events without adding private/order/live-trading tables.
6. Update Compose health checks, restart policy, and paper-only runtime configuration.
7. Run local regression/resource tests, deploy to the existing Singapore candidate node through the existing secure DB tunnel, run two Stage1 cycles, and record measured evidence.
8. Update the completion report, commit only source/tests/docs/compose changes, and verify a clean working tree.

## Safety Constraints

- No Phase 2, private API, API keys, order/position route, executor, or live mode.
- No modification to production application/database schemas outside the isolated `quant` schema/database.
- No secrets, keys, `.env`, or runtime logs committed.
- Production defaults remain Universe 1h, Stage1 5m, and continuous ticker/kline ingestion; test overrides are explicit and documented.
