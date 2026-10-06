# Phase 8 Options Context — Feature Acceptance Report

**Outcome:** `PHASE8_FEATURE_COMPLETE=true`
**Final integrated runtime/resource acceptance:** `PHASE8_FINAL_RUNTIME_ACCEPTANCE_DEFERRED=true`
**Next gate:** `DATA_LAYER_V1_HARDENING_PENDING=true`
**Branch:** `phase8`
**Task 15 smoke checkpoint:** `871a8f2029fd5500b24289c700b1765c9f2248fd`

Phase 8 Feature Acceptance is complete for the approved context-only scope. The full test suite passed, the bounded unauthenticated public-source smoke passed, and the Phase 8 database tests ran against a disposable loopback PostgreSQL instance. This report does not claim 60-minute runtime, final resource-budget acceptance, or production deployment acceptance.

## Scope and safety

- Phase 8 remains a collector-owned, optional options-context pipeline. `TRADING_MODE=paper` remains required.
- No Stage 1 eligibility or output changes; no BUY/SELL/LONG/SHORT signal, RiskEngine, Executor, private API, order API, position API, or live-trading path was added.
- REST and WebSocket source adapters use the official public Deribit API. The short smoke made no authenticated request and did not persist or print raw market payloads.
- Migration `015_phase8_options_context.sql` is additive. Migrations 001–014 are unchanged from the Task 14 baseline.
- Migration 015 defines four Phase 8 tables: `phase8_option_instruments`, `phase8_option_instrument_events`, `phase8_option_market_snapshots`, and `phase8_option_context_snapshots`. The public smoke did not persist data, so it added **0 live rows**. Deterministic replay runs only in a disposable isolated schema, asserts that all four row counts and the canonical digest are unchanged after a second replay, then drops that schema. Exact synthetic replay row counts were not emitted by the test runner and are not claimed here.
- Retention configuration and cleanup contract exist, but `retention_enforcement=false`; no collection data was deleted.

## Feature and data-contract status

- Instrument metadata is seeded/reconciled through `public/get_instruments`; supported index names are discovered with `public/get_index_price_names`.
- Lifecycle channels are subscribed on the public WebSocket. No creation/state event arrived during the short smoke window; per the approved contract this is not a failure.
- Full-chain markprice uses dynamically derived `markprice.options.{index_name}` channels. The smoke validated both initial full seeds and the current message schema.
- `public/get_book_summary_by_currency` remains the REST source for full-chain OI/volume snapshots. Incremental ticker is subscribed only for the bounded, deterministic ticker universe.
- The measured bounded universe was `PARTIAL` by design: BTC 220 eligible / 64 selected; ETH 156 / 64 selected. The smoke subscribed to one selected ticker per underlying, not all 128 candidates.
- IV/Greek source values remain exact source-provided values with provenance and conservative unit status. Unverified IV/Greek units are not normalized or used for unit-dependent available contexts. Dealer GEX is not implemented.
- Missing source fields remain missing/partial/unavailable; no missing value was filled with zero.
- Context calculations retain event-time/source timestamps and do not use future observations.

## Task 15 public smoke evidence

Final instrumented smoke after the REST dispatch-spacing fix: **PASS**, started `2026-09-26T06:23:16.908246Z`, elapsed **14,612 ms**.

| Check | Result |
|---|---|
| REST request count | 5 / maximum 5; one index-name request, BTC/ETH instruments, BTC/ETH book summary |
| REST result | 5/5 HTTP 200; canonical parsing and full summary coverage passed |
| REST inter-call timer gaps | 2,096.255 / 1,612.954 / 1,606.168 / 1,781.720 ms. The probe waits at least 1.25 seconds after the previous response completes; this stronger gate guarantees actual next-request starts are at least 1.25 seconds apart despite dispatch overhead. A loopback HTTP regression test measures server-observed arrival times. |
| Instrument / summary rows | BTC 998 / 998; ETH 856 / 856 |
| REST response bytes / latency | Index names 4,112 B / 845 ms; BTC instruments 871,533 B / 356 ms; ETH instruments 746,531 B / 350 ms; BTC summary 445,835 B / 527 ms; ETH summary 378,344 B / 366 ms |
| WebSocket | One public connection to `wss://www.deribit.com/ws/api/v2` |
| Subscription acknowledgement | One exact successful ACK for 8 requested lifecycle, markprice, and bounded ticker channels |
| Markprice | Dynamic `btc_usd` and `eth_usd` channels; complete schema-valid seeds: BTC 998, ETH 856 |
| Ticker | Two schema-valid initial snapshots; seven subsequent change messages |
| Sparse merge | `LIVE_SPARSE_UPDATE_OBSERVED=true`; an observed live sparse update preserved omitted state |
| Deterministic merge | `DETERMINISTIC_STATE_MERGE_PASS=true`; source-contract fixture tests and deterministic replay passed |
| Lifecycle events | 0 observed in the short window; accepted under the approved rule |
| Rate limiting / retries | No 429 observed; no retry was performed |
| Bounds | 8 MiB REST body, 1 MiB WebSocket frame, one connection, 75-second global deadline |

A preliminary harness run stopped after five successful REST responses because it incorrectly rejected the expected `PARTIAL` status of a bounded sample. It opened no WebSocket. The gate was corrected to accept only a non-empty, trustworthy `AVAILABLE`/`PARTIAL` selection for both assets; the final instrumented smoke then passed. This was a local harness-gate correction, not a provider/network failure.

## Test results

| Suite | Result |
|---|---|
| Task 15 source-probe focused tests, after pacing fix | 13 passed |
| All `tests/test_phase8_*.py` with isolated PostgreSQL | 146 passed, 0 skipped, 0 failed |
| Migration / persistence / replay focused database tests | 30 passed, 0 skipped, 0 failed |
| Phase 1–7 regression (`tests/test_phase8_*.py` excluded) | 947 passed, 8 skipped, 0 failed |
| DB-enabled full regression before the standalone probe pacing refinement, isolated PostgreSQL configured to UTC | **1,093 passed, 8 skipped, 0 failed** |
| Post-refinement full `.venv/bin/pytest -q` in the current WSL environment | **1,073 passed, 29 skipped, 0 failed**; 8 live-contract opt-ins and 21 PostgreSQL-dependent cases skipped because `TEST_POSTGRES_DSN` was unavailable |

The eight intentional live-contract skips are official public probes gated by explicit opt-in environment variables: Phase 2 (3, `PHASE2_LIVE_CONTRACT=1`), Phase 3 (2, `PHASE3_LIVE_CONTRACT=1`), and Phase 4 (3, `PHASE4_LIVE_CONTRACT=1`). They were not enabled as part of this options-only smoke. The post-refinement run also skipped 21 database-backed cases because the WSL Docker daemon was unavailable and `TEST_POSTGRES_DSN` was unset; these are not represented as passes: Phase 3 persistence (1), Phase 4 persistence (1), Phase 6 persistence (1), Phase 6 runtime integration (1), Phase 7 persistence (1), Phase 8 migration (1), Phase 8 persistence (12), Phase 8 replay (1), and repository integration (2). The preceding DB-enabled full run and isolated Phase 8 migration/persistence/replay suites passed before the standalone probe-only pacing refinement; no database code changed afterward. One existing Phase 7 `aiohttp.BasicAuth` deprecation warning remains at `src/quant_phase7/runtime.py:259`; it is unrelated to Phase 8.

The shared repository migration integration test was made self-isolating with a per-test schema and now checks the complete current migration-file list. This fixes its former dependency on a pristine shared database and stale hard-coded count; no Phase 1–7 runtime or migration SQL was changed.

## Security, migration, and resource audit

- Secret scan: `SECRET_LEAK_FOUND=false`. No credential-bearing URL, API key, authentication header, or secret value was added to tracked files or reports.
- `.env.local`: ignored and untracked; its contents were not read or changed.
- REST smoke allowlist contains only `public/get_index_price_names`, `public/get_instruments`, and `public/get_book_summary_by_currency`; no private/order/position route is present.
- Migration 001–014 diff: none. Migration 015 passed the isolated PostgreSQL migration, schema, persistence, idempotency, and replay tests.
- Existing local Compose caps remain unchanged: PostgreSQL **768 MiB**, collector **256 MiB**, engine **384 MiB**. No service or production container was changed for this feature acceptance.
- Disposable test PostgreSQL was bound to loopback, capped at 512 MiB / 0.5 CPU, configured with UTC, and stopped after testing. Existing `quant-postgres`, `quant-engine`, and `agentops` containers remained running.

## Deferred acceptance and known limits

The following are deliberately deferred to **Data Layer V1 Hardening** and are not acceptance claims in this report:

- 60-minute Phase 1–8 runtime acceptance, production collector/PostgreSQL resource characterization, and final long-run storage growth.
- Global admission control and global database scheduling.
- Three restart cycles and final runtime recovery acceptance.
- Production retention-budget approval and retention enforcement.

The short source smoke proves the current documented public contract and schema for the captured window only; it does not establish long-duration availability, public rate-limit thresholds, or production-scale resource behavior. Continue to treat unverified IV/Greek units as non-normalized and unavailable for unit-dependent contexts. The final post-refinement full test pass ran without a PostgreSQL DSN; its database-dependent skips are listed above. Isolated PostgreSQL acceptance passed in the preceding full run, but was not rerun after the standalone REST smoke pacing change.

## Completion

`PHASE8_FEATURE_COMPLETE=true`
`PHASE8_FINAL_RUNTIME_ACCEPTANCE_DEFERRED=true`
`DATA_LAYER_V1_HARDENING_PENDING=true`

No Phase 9 work, Data Layer V1 Hardening, ECS operation, or trading operation was started.
