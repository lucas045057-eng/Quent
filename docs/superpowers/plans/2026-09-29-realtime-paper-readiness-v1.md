# Realtime Paper Readiness V1 implementation plan

The user explicitly authorized continuous execution through implementation and acceptance. This is a working record, not an approval stop.

1. Complete source-first audit; publish `docs/current-strategy-map.md` and `docs/STRATEGY_GAPS.md` before runtime/core changes. (Done.)
2. Implement a small `quant_realtime_paper` orchestrator that reuses Phase1Repository + `run_stage1`, reads only existing canonical PostgreSQL data, and uses a local operations-only SQLite ledger for sessions/cycle decisions. No new collector or market-data schema.
3. Add a fail-closed readiness/safety gate. Do not instantiate or call Nautilus order submission while policy, validity, Risk, paper, or reconciliation prerequisites are absent; persist exact rejection reason.
4. Add read-only Dashboard API + existing-page UI linkage for BTC/ETH canonical quote/Kline freshness, source class, session uptime, last decision, risk block, and readiness. Do not restart the user's running dashboard service.
5. Add CLI and Windows start/stop/status scripts. They control only the new runner process. Support bounded smoke and 24h/72h/7d durations.
6. TDD: add focused failing tests before implementation for gates, source typing, persistence/idempotence, clock and recon safety, API data truthfulness, and long-run session semantics.
7. Run new unit tests, Dashboard tests/build, existing integration and full project test matrix. Do not edit/delete/skip/xfail old tests or lower Risk.
8. Perform a short REAL_PUBLIC_DATA acceptance only against the existing Collector and canonical PostgreSQL path. Report actual duration and observed cycles. Do not call a short run a 24h pass. If the existing collector/feed or other hard prerequisite is unavailable, report the exact block and leave formal 24h acceptance unclaimed.
9. Write `docs/realtime-paper-readiness-report.md`, inspect diff, commit changes, verify clean worktree and branch/head. Do not push.
