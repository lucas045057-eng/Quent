# Quant Trading Dashboard V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user chose autonomous inline execution; no per-task approval handoff.

**Goal:** Ship a local browser operations console reading real Quant/Nautilus state without core changes or Live routes.

**Architecture:** Independent bounded read services produce safe DTOs from PostgreSQL, native artifact reports and verified local Paper heartbeat. FastAPI serves React/Vite static assets plus GET-only API at127.0.0.1:3000. No trading/lifecycle calls; a separate launcher owns only the dashboard process.

**Tech Stack:** Python3.12, FastAPI0.141.1, uvicorn0.54.0, httpx0.28.1 test client; React19.3.0, TypeScript, Vite8.3.1, Recharts3.10.1, Vitest5.0.2 (frontend transitive versions locked after peer compatibility check).

**Spec:** docs/superpowers/specs/2026-09-29-dashboard-v1-design.md

## Global Constraints

- Baseline4b55633, branch integration/nautilus-v1; preserve existing core sources, required dependencies, migrations and original DB/containers.
- 127.0.0.1:3000 only; GET-only API, LIVE DISABLED permanently, Jev NOT_CONFIGURED baseline.
- Every response schema DASHBOARD_API_V1; Decimal strings, unavailable=null, source refs, availability and UTC observed_at.
- Poll3 seconds; Paper current heartbeat<=5 seconds plus process identity; histories stay explicitly historical/stale.
- File4MiB/catalog256/list200/log-tail64KiB limits; READ ONLY DB transaction, connection2 seconds and statement1.5 seconds.
- No fabrication of curve/trades/risk rejection/zero metrics; fixture acceptance explicitly labelled.
- Required local tests zero skips/xfail; existing1698 must remain green. Only disposable loopback DB for tests.

## Review Focus

- A stale heartbeat or reused PID must not produce current positions/RUNNING; Task1 tests and Task5 process ownership tests.
- Cumulative fill/result duplicates must not become new trades/PnL; Task2 exact projection tests.
- Missing curve or independent case reports must not produce invented equity/drawdown; Task2/Task4 tests.
- Source JSON/URL/error/log containing credentials must remain redacted through detail and errors; Task1/Task2/Task4 tests.
- A non-dashboard process on the port or reused PID must survive stop; Task5 real process tests.

### Task 0: Investigation, design and baseline

**Files:** docs/DASHBOARD_V1_PROJECT_AUDIT.md; this spec/plan; test artifacts only.
**Interfaces:** Consumes existing baseline; produces precise source map and core-path digest inventory.
- [ ] Self-review spec and plan for scope, contradictions, field names and missing proof; record user-authorized autonomous handoff.
- [ ] Commit docs as docs(dashboard): define read-only local operations console.
- [ ] Create named disposable postgres16.15-bookworm on127.0.0.1:55442 with quant_phase9_test/quant_phase8_test; record exact identity, never use original containers.
- [ ] Run existing full matrix with original isolated Python environment; expect1698 passed0skipped0failed0errors before product code.

### Task 1: Safe read services and basic API

**Files:** src/dashboard/__init__.py, backend/{__init__,config,models,security,files,db,paper,service,app}.py; pyproject dashboard extra; tests/dashboard/{test_sources,test_api,test_db}.py.
**Interfaces:** produces DashboardConfig.from_env(), FileCatalog(root).scan(), DatabaseReader(config).snapshot(), PaperReader(config).snapshot(), DashboardService(config).overview()/health()/positions()/orders()/trades(), create_app(config)->FastAPI. Lists use limit<=200; paths are relative bounded catalog refs.
- [ ] RED tests import within test functions and require health/overview/positions/orders/trades/paper routes, null empty cards, safe DB errors, NOT_CONFIGURED/DISABLED, cross-origin/method denial, secret/path bounds, current vs stale Paper and real readonly PostgreSQL.
- [ ] Run focused pytest, confirm missing dashboard feature fails.
- [ ] Implement config/DTO/security/catalog/readonly reader/process reader/basic routes; do not call core mutation functions.
- [ ] Run focused tests and source isolation checks; expect allpass/0skip.
- [ ] Commit feat(dashboard): add bounded read-only operations API.

### Task 2: Decision, Backtest and event projections

**Files:** backend/{decisions,backtests,events}.py; service/app extension; tests/dashboard/{test_decisions,test_backtests,test_events}.py.
**Interfaces:** produces DecisionReader(reader).list()/detail(decision_id), BacktestReader(catalog).list()/detail(run_id), EventReader(catalog,db).list(level,module,q), consumes Task1 bounded sources and safe DTO envelope.
- [ ] RED real persisted decision graph tests assert actual reasons/quality/versions; absent intent Risk NOT_RECORDED; linked intent APPROVED proof; duplicate result has no invented trade.
- [ ] RED backtest tests verify native report hash/fixture label, no invented curve/Sharpe, real timestamped sampled drawdown, corrupt/out-of-root denial. Event tests require level/module/query and secret stripping.
- [ ] Implement typed read projections and detail routes with bounded joins; real failures degrade one source without losing others.
- [ ] Run all backend tests, expect pass/0skip; commit feat(dashboard): expose decision evidence and historical backtest audit.

### Task 3: Overview, Paper and Decision frontend

**Files:** dashboard/frontend/{package.json,package-lock.json,tsconfig.json,vite.config.ts,index.html,.gitignore}; src/{main,App,api,types,styles,components,pages} modules; vitest setup/tests.
**Interfaces:** React App consumes Task1/2 DASHBOARD_API_V1 endpoints; usePolling(url,3000) cancels requests and exposes loading/error/stale; sidebar uses hash routes.
- [ ] Install locked compatible frontend dependencies in Windows workspace copy; configure tests before behavior implementation.
- [ ] RED component tests require three pages, six nav links, LIVE disabled, data/empty/error/loading, current/historical distinction and clickable decision chain with safe detail.
- [ ] Implement graphite/teal terminal layout, reusable status/metric/table/source/empty components and three main pages.
- [ ] Run frontend tests/typecheck; commit feat(dashboard): build overview paper and decision workspace.

### Task 4: Backtest charts, Health and Logs

**Files:** frontend pages/{Backtests,Health,Logs}.tsx, components/{Charts,JsonPanel}.tsx and tests; CSS refinement.
**Interfaces:** consumes backtest/health/events detail DTO; charts get actual timestamped series only.
- [ ] RED tests require run selection, recorded metrics, empty curve/trades message, real series chart, health components, log filters, safe raw info and offline polling behavior.
- [ ] Implement three pages and Recharts visuals; no fixture API client in production.
- [ ] Run all frontend tests and build; verify6 routes and1366px layout bounds; commit feat(dashboard): add backtest health and event visualization.

### Task 5: Static bundle and Windows lifecycle

**Files:** backend/{__main__,lifecycle}.py; scripts/{start-dashboard,stop-dashboard}.ps1; frontend build-stamp helper/build output; tests/dashboard/test_launcher.py; docs/DASHBOARD_V1_OPERATIONS.md.
**Interfaces:** CLI serve/start/stop/status; Windows -Distro/-RepoPath/-PythonPath/-Port defaults point to installed local project/env,3000. Own instance record includes PID/start_ticks/UUID and no DSN.
- [ ] RED real process tests for start/health/static SPA routes/stop/idempotent start; occupied port and reused PID survive; host cannot bind0.0.0.0.
- [ ] Implement guarded launcher/loopback serve, scripts and compiled static assets; verify source/build hash stamp.
- [ ] Run actual Windows launcherstart/stop/restart and inspectport3000; document foreground/oneclick/configuration/failures.
- [ ] Commit feat(dashboard): ship loopback Windows start stop and static application.

### Task 6: Full verification and final review

**Files:** final report and ignored evidence/ledger only unless verified Important fixes.
**Interfaces:** all Task APIs/build/launcher; original source digests and full regression baseline.
- [ ] Backend tests +frontend tests/typecheck/build +full original matrix +compatibility36 +secret scan, zero required skips; report original and added counts separately.
- [ ] Real browser QA six pages at1366×768 and1920×1080, actual artifacts/default no-data; source traceability, local bind and no Live/secret/core mutation checks.
- [ ] Dispatch one fresh whole-branch reviewer using executing-plans/requesting-code-review; sort findings; one concentrated Important/Critical fix round RED→GREEN/full verification, minors logged.
- [ ] Commit measured final docs; clean source; remove only verified disposable test DB after workersstop. Preserve original containers/DB and integrationworktree; no push/merge/PR/tag.
- [ ] Deliver actual URL/launch/stop commands, page/API/component map, proof counts, Git state, safety boundaries and exhaustive rulings/minors; keep the dashboard runnable with real local sources.
