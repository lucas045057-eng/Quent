# Quant Trading Dashboard V1 design

## Intent and authority

Build a local operations console that explains the real market→decision→risk→intent→execution→account evidence and reads existing Backtest/Paper state. The user explicitly authorizes Phase0 investigation followed by continuous implementation and final verification without design/plan approval pauses. This overrides the skill's normal approval handoffs; it does not authorize funds, Live, secrets, core rewrites or original DB writes.

Baseline: clean linked worktree, integration/nautilus-v1, 4b55633d0650ff232eb4fb176dccf652d49cdd66. Reuse this worktree and create milestone commits. Preserve original quant checkout and three original containers. See docs/DASHBOARD_V1_PROJECT_AUDIT.md for concrete sources and absent facts.

## Architecture

`Existing PostgreSQL / artifact files / verified local Paper heartbeat → independent bounded read services → filtered API DTO → React UI`. Backend package is src/dashboard/backend, frontend source dashboard/frontend. Python -m dashboard.backend supports serve/start/stop/status; scripts/start-dashboard.ps1 and stop-dashboard.ps1 invoke WSL from Windows. A single FastAPI service binds 127.0.0.1:3000 and serves API plus compiled Vite assets. No Paper controller or trading dependency is executed.

Lock stable dependencies in optional dashboard extra and frontend package-lock. Keep built frontend assets and a source/build hash stamp for startup without Node; developers rebuild using documented commands. Do not change existing required dependencies, core modules, migration files or protected compatibility paths.

## Data contract

Every response has schema DASHBOARD_API_V1, data, availability, observed_at, source references and warnings. Decimals cross JSON as strings; unavailable monetary/metric values are null. Lists are bounded (default100, max200); files max4MiB, catalog max256 files, logs last64KiB/file and max200 entries. Relative source refs remain traceable; DSNs/credentials/full exception messages never leave the server.

Current Paper requires heartbeat age <=5 seconds, nonfuture UTC timestamp, native_engine SandboxExecutionClient, network_order_routes=0, and verified matching live process (PID plus executable/module and ready-file identity). A PID reused by another process never proves RUNNING. Stopped/stale heartbeat contributes only historical state; current account and positions are null/unavailable. An explicitly selected historical session may display its recorded state with STALE/HISTORICAL labels. Missing start/restart/open timestamps remain null; real /proc start time is allowed with provenance.

PostgreSQL is optional and explicitly configured via QUANT_DASHBOARD_DSN and QUANT_DASHBOARD_SCHEMA. Local hosts only. Queries use psycopg READ ONLY transactions and identifier-safe schema names; connection/statement deadlines keep source failures bounded. Existing tables only, no migrations/DDL/writes. Missing tables are PARTIAL/not-ready; absent DSN is NOT_CONFIGURED; connection failure ERROR. DTO builders select actual persisted field names, never call Risk/Adapter/strategy functions.

Orders map actual intent identity/constraints and persisted native/status information, distinguishing constraint fields from native observations. Market orders have no invented limit price. Cumulative ExecutionResult is displayed as execution audit, not a sequence of individual trades; trade counts are null when exact trades are unavailable. Optional explicit trade records may populate the trade table with source refs. Separate accounts are never summed into one equity without a common explicit scope.

Decision detail joins immutable snapshot/evidence/chain/pattern/review/lifecycle and intents/results by UUID. Market, Screening, Evidence, Strategy, DecisionCandidate, Risk, ExecutionIntent, Nautilus steps show PASS/REJECT/WAITING/ERROR/NO DATA according to stored facts. An existing intent is recorded risk approval; absent intent is NOT_RECORDED, not fabricated rejection. Phase9 veto/missing/UNKNOWN/PARTIAL and original quality/provenance are preserved. Details show safe filtered source values, not invented numerical evidence.

Backtest catalog verifies the actual native acceptance report digest and creates content-derived view IDs per independent case. Preserve FIXTURE_DRIVEN_ACCEPTANCE and NOT_VALIDATED. Net, fees and signed funding reuse recorded values. Missing initial/final equity, times, trades or ratios stay null. Equity/Drawdown charts accept only actual timestamped equity points; drawdown is the bounded sampled running-peak calculation labelled as sampled, not intrabar maximum. Never synthesize a curve from terminal PnL or independent cases; Sharpe/Sortino require recorded metrics and are not guessed.

Logs merge selected actual artifact tails and typed DB audit events, filter level/module/search, strip secret keys, authorization, credentials in URLs, password/token assignments, JWT and PEM/private-key text. Strings are bounded; React renders text, never raw HTML. Failures are safe codes. Frontend tests use fixtures only; production has no mock provider or synthetic data fallback.

## Pages and visual behavior

Dark graphite surfaces, clear white typography, restrained teal active/positive and red risk/error accents. Persistent sidebar, LIVE DISABLED lock in header and overview, connection status, source freshness and fixture banner. Desktop layouts shrink at1366×768; tables scroll inside panels. Native loading, empty, source error and API-disconnected states are distinct. Overview and Paper refresh every3 seconds with request cancellation and no concurrent polling buildup; failed refresh keeps prior data explicitly stale. Backtest lists/details are on demand.

1. Overview: requested system/mode/Paper/Jev/funding/DB/reconcile status and eight account/activity cards, recent decisions and errors. System status separates Quant state from the Web process; N/A when Core cannot be observed.
2. Paper: process/session/heartbeat, current versus historical position/order/execution audit, independent reconciliation fields with unknown values preserved.
3. Decision Inspector: recent candidates, searchable selection, source/category/evidence/pattern/status and clickable eight-step chain, risk/intent linkage and safe raw JSON.
4. Backtest: actual runs/cases, metric table, equity/drawdown or honest missing-series panel, trades or unavailable explanation, actual PnL/fee/funding comparison.
5. Health: Quant Core, DB, Backtest, Paper, native adapter, funding, reconciliation, Jev, Live; measured dashboard/Paper resources and unavailable indicators for missing monitors.
6. Logs: level/module/query controls and event correlation IDs; source error/empty state.

## Local security and lifecycle

Only 127.0.0.1 bind; no remote host override. Host/Origin/Fetch-Metadata checks reject untrusted origins and DNS rebinding. No permissive CORS. API GET-only; unsafe methods fail. CSP script-src self, object/frame/base restrictions, nosniff, API no-store. Request validation and unexpected errors never echo input values or DB exception text.

Dashboard start/stop uses its own lock, PID + Linux start_ticks + instance identity; it cannot terminate Paper, Core or a process that reused the PID. Occupied port fails without killing another service. Runtime files are under ignored artifacts/dashboard; no secret is persisted. Scripts run hidden background helpers; stop gracefully terminates only the identified dashboard. Start is idempotent for its own live instance. Single foreground serve mode supports Ctrl+C.

## Verification

Baseline matrix must be fresh1698/0 before product implementation. Backend tests cover all requested routes, empty/DB failure/Paper stop/Jev NOT_CONFIGURED/Live DISABLED/API errors, typed projections, stale/current separation, readonly real PostgreSQL, redaction and resource/path bounds. Frontend tests cover all six pages, decision drill-down, no fabricated trades/curves, loading/error/offline, polling cancellation and log filters. Build/typecheck pass. Real browser QA at1366×768 and1920×1080, all routes, default actual artifact data and N/A state. Real Windows start/stop/restart verifies loopback3000 and ownership. Full original regression plus new tests, four anchored compatibility gates, core path hash comparison, secret scan and original DB/container identity verification. One final fresh whole-branch review, one concentrated Important/Critical fix round if necessary; minors logged. No push/merge/PR/tag/release or Live.
