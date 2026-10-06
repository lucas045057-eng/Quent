# Phase 1 Fast Runtime Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the already implemented paper-only Phase 1 on the existing Singapore Candidate ECS while persisting only to the isolated `quant` database in the existing Hangzhou PostgreSQL instance, then verify runtime, restart, failure recovery, safety, and resource boundaries.

**Architecture:** Singapore runs only `quant-collector` and `quant-engine`. A loopback-only local database port (`127.0.0.1:15432`) is connected through a temporary authenticated SSH tunnel to a loopback-only PostgreSQL bridge on Hangzhou; the bridge joins the existing PostgreSQL Docker network without changing the PostgreSQL container, volume, public port, or 随想记 services. The application remains `TRADING_MODE=paper` and uses only Bitget UTA v3 public REST/WebSocket APIs.

**Tech Stack:** Python 3.12, Docker Compose, PostgreSQL migrations/repositories already in the repository, Alibaba Cloud ECS Cloud Assistant, OpenSSH local forwarding, systemd, Bitget public REST/WS.

**Spec:** User-provided Phase 1 Fast Runtime Acceptance instructions pasted in `C:\Users\Admin\.codex\attachments\0ee34608-cdfe-43d4-b865-b77e7882fbbc\已粘贴的文本.txt`.

## Global Constraints

- Use only the existing Singapore ECS `i-t4n0mrjhd7w29t6k4tzj` in `ap-southeast-1`; do not create another Candidate ECS or test another Region.
- Keep `TRADING_MODE=paper`; no private Bitget API, API key, secret, passphrase, order, position, executor, or live trading path.
- Do not restart, rebuild, expose, remap, or alter the existing Hangzhou PostgreSQL container, its volume, its public ports, or 随想记 services.
- PostgreSQL is reachable only through loopback `127.0.0.1:15432`; never expose `0.0.0.0:5432` or `0.0.0.0:15432`.
- Singapore runs only `quant-collector` and `quant-engine`; no PostgreSQL, Redis, Kafka, or executor container.
- Do not mock advanced market data; OI, Funding, CVD, Liquidation, Long/Short, News, and AI remain unavailable.
- Collector memory limit is approximately 256 MB and engine memory limit approximately 384 MB; do not increase limits to consume the full 1 GiB host.
- Do not commit `.env`, passwords, SSH private keys, runtime logs, tokens, or any secret.
- Stop with `RESOURCE_BLOCKER` on OOM, restart loops, or sustained memory pressure; stop with `PHASE_1_FAST_RUNTIME_NOT_ACCEPTED` on any unmet acceptance condition.

## Review Focus

- Existing working-tree fixes must be preserved and justified before commit: Docker migration path, pip build resilience, server compose, and completion report.
- Tunnel isolation must be observable at each boundary: Hangzhou bridge container, Singapore loopback listener, SSH forwarding process, and PostgreSQL `quant`/`quant_app` authorization.
- Runtime correctness must be based on real Bitget data: all four timeframes, closed-bar filtering, interval-aware freshness, deterministic Stage1, and no advanced-data mock.
- Restart and fault transitions must be tested without restarting Hangzhou PostgreSQL or 随想记; a tunnel outage must produce `DEGRADED`, not false persistence success.
- Final claims require fresh evidence: resource stats, database counts, health, safety scan, Git diff/status, and exact skipped tests.

---

### Task 1: Freeze Phase 1 code and establish the execution ledger

**Files:**
- Inspect: `Dockerfile`, `src/quant_phase1/db.py`, `docker-compose.server.yml`, `PHASE_1_COMPLETION_REPORT.md`
- Inspect: `pyproject.toml`, `docker-compose.yml`, `migrations/001_phase1_core.sql`, `src/quant_phase1/entrypoints/collector.py`, `src/quant_phase1/entrypoints/engine.py`
- Modify only if required: the four known Phase 1 files above and `PHASE_1_COMPLETION_REPORT.md`
- Create/modify: `.superpowers/sdd/2026-09-20-phase1-fast-runtime-acceptance/progress.md`

**Interfaces:** The local branch remains `phase1`; existing DB migration fallback and server compose must remain available to deployment. No Phase 2 interface is introduced.

- [ ] Record `git status`, `git diff`, `git diff --check`, and the list of tracked/untracked files.
- [ ] Review the four known uncommitted changes against the Phase 1 requirements; preserve Docker migration fallback, pip retry settings, server compose, and report content if they are required.
- [ ] Run the existing full local test suite before deployment and record its exact result.
- [ ] Run a safety scan that fails on private Bitget clients, order/position routes, executor imports, `TRADING_MODE=live`, secret literals, or Phase 2 modules.
- [ ] Commit only necessary Phase 1 changes after runtime validation; exclude `.env`, passwords, SSH keys, and logs.

### Task 2: Inspect Singapore and Hangzhou runtime boundaries

**Files:**
- No repository changes.
- Remote read-only checks on Singapore ECS and Hangzhou ECS/PostgreSQL host.

**Interfaces:** Cloud Assistant commands are one-shot diagnostic commands; no public SSH opening and no PostgreSQL restart.

- [ ] On Singapore record `uname -a`, `/etc/os-release`, `date -u`, `free -h`, `df -h`, `swapon --show`, `docker version`, `docker info`, and `docker compose version`.
- [ ] If Docker is missing, install only the official stable Docker Engine and Compose Plugin; do not install PostgreSQL, Redis, or Kafka.
- [ ] On Hangzhou read-only inspect `suixiangji-staging-api-1`, `suixiangji-staging-wealthmate-admin-api-1`, `suixiangji-staging-db-1`, Docker networks, PostgreSQL container network membership, current `quant` database/schema/user isolation, and public port bindings.
- [ ] Record baseline restart counts and health for all three 随想记 containers; abort if the PostgreSQL container or application is not already healthy.

### Task 3: Build the temporary loopback-only database bridge and tunnel

**Files:**
- Remote-only temporary files: a dedicated bridge compose/service definition on Hangzhou, a tunnel environment file on Singapore with mode `600`, and a systemd service for the tunnel.
- Do not modify the existing PostgreSQL container, its volume, or existing 随想记 compose files.

**Interfaces:**
- Hangzhou bridge exposes only `127.0.0.1:15432` on the Hangzhou host and forwards to the existing PostgreSQL service/container port 5432 on its Docker network.
- Singapore `DATABASE_URL` uses `host=127.0.0.1 port=15432 dbname=quant user=quant_app`.
- Tunnel service uses `ServerAliveInterval`, `ServerAliveCountMax`, and `ExitOnForwardFailure=yes`, with `Restart=always` or equivalent.

- [ ] Generate a temporary dedicated tunnel credential without exposing its private material in logs, Git, or command output; install only the required public key on the Hangzhou host through the existing secure management channel and record the cleanup action.
- [ ] Start the smallest bridge sidecar attached to the existing PostgreSQL Docker network, bound only to Hangzhou loopback; verify `ss -ltn` shows no `0.0.0.0:5432` or `0.0.0.0:15432`.
- [ ] From Singapore establish the active SSH local forward to Hangzhou loopback port 15432 and verify the listener is bound to `127.0.0.1` only.
- [ ] Run `SELECT 1;` and `SHOW timezone;` from Singapore through the tunnel; require `UTC` and successful `quant_app` access to `quant`.
- [ ] Verify `quant_app` cannot access `suixiangji` database/schema and do not query any user business table.
- [ ] Stop/restart only the tunnel service once to prove automatic recovery before starting Quant.

### Task 4: Deploy paper-only Quant services with resource limits

**Files:**
- Remote-only deployment directory on Singapore, based on repository `Dockerfile`, `docker-compose.server.yml`, and a mode-600 `.env`.
- Modify local files only if a verified Phase 1 runtime fix is required: `Dockerfile`, `src/quant_phase1/db.py`, `docker-compose.server.yml`, `PHASE_1_COMPLETION_REPORT.md`.

**Interfaces:**
- Services: `quant-collector`, `quant-engine` only.
- Environment: `TRADING_MODE=paper`, public Bitget REST/WS URLs, `DATABASE_URL` to `127.0.0.1:15432`, no private credentials.
- Limits: collector `256m`, engine `384m`; Docker log rotation bounded.

- [ ] Transfer only non-secret Phase 1 source/config artifacts to Singapore using a secure management channel; verify checksums and ownership.
- [ ] Create `.env` on Singapore with mode `600`; verify no private API variables or secret values are present.
- [ ] Run `docker compose config` and a safety scan before starting services; require no PostgreSQL/Redis/Kafka/executor service.
- [ ] Start collector and engine, then immediately record `free -h`, `docker stats --no-stream`, container status, health output, and log sizes.
- [ ] Stop deployment and report `RESOURCE_BLOCKER` if OOM, restart loop, or sustained pressure is observed.

### Task 5: Verify real Bitget-to-PostgreSQL Phase 1 flow

**Files:**
- No new application feature files; update only `PHASE_1_COMPLETION_REPORT.md` with measured evidence.

**Interfaces:** Real public v3 endpoints and existing canonical pipeline; no mocks and no advanced-data promotion.

- [ ] Verify REST HTTP 200 for `/api/v3/market/instruments?category=USDT-FUTURES` and `/api/v3/market/tickers?category=USDT-FUTURES`.
- [ ] Verify WS TLS/HTTP 101 at `wss://ws.bitget.com/v3/ws/public`, ticker subscription, and Kline subscriptions using `topic=kline` with intervals `5m`, `15m`, `1H`, `4H`.
- [ ] Run long enough to complete Universe Top200 and one complete deterministic Stage1 A/B/C/D cycle; record actual UTC runtime duration.
- [ ] Verify only closed Klines persist and expected-closed-bar freshness is used; verify stale inputs block Stage1.
- [ ] Verify real database writes for instruments, tickers/observations, all four Kline timeframes, market snapshots, Stage1 results, and system health.
- [ ] Record row counts without dumping raw market payloads, and verify advanced semantic observations remain `NOT_AVAILABLE` rather than mocked.

### Task 6: Restart and tunnel-fault acceptance

**Files:**
- Remote-only service restart/fault commands; update `PHASE_1_COMPLETION_REPORT.md`.

- [ ] After real data exists, restart only `quant-collector` and `quant-engine`; do not restart Hangzhou PostgreSQL, 随想记, or either ECS host.
- [ ] Verify REST recovery, WS reconnect/resubscribe, tunnel reconnect, DB reconnect, Kline idempotency, Stage1 consistency, and health recovery.
- [ ] Stop only the Quant tunnel service; require Quant health transition `RUNNING → DEGRADED` and failed persistence to remain visible rather than falsely successful.
- [ ] Restore the tunnel; require `DEGRADED → RUNNING`, DB reconnect, and continued collection.
- [ ] Recheck the three 随想记 containers and compare restart counts with the baseline; any Quant-attributed change is a blocker.

### Task 7: Final evidence, report, and Git封版

**Files:**
- Modify: `PHASE_1_COMPLETION_REPORT.md`
- Modify only if needed: design/deployment docs that contradict measured Phase 1 runtime behavior.

- [ ] Record ECS identity/specification/billing/disk/public bandwidth, measured runtime duration, Bitget results, row counts, DB size, container memory, host free memory, CPU, restart counts, and costs.
- [ ] Record all skipped tests explicitly, known issues, architecture limitations, and safety scan results.
- [ ] Run the full local regression suite and any live contract tests that are safe and applicable; do not re-test the old Hangzhou Bitget edge directly.
- [ ] Run `git diff --check`, inspect the final diff, verify no `.env`, password, SSH private key, secret, or runtime log is staged.
- [ ] Commit necessary Phase 1 runtime/report fixes on branch `phase1`; verify `git status` is clean and record HEAD.
- [ ] Stop with `PHASE_1_FAST_RUNTIME_ACCEPTED` only if every acceptance condition has fresh evidence; otherwise stop with `PHASE_1_FAST_RUNTIME_NOT_ACCEPTED` and list blockers.

## Self-Review Coverage

- Code freeze and known-file review: Task 1.
- Singapore host/Docker/resource preflight: Task 2.
- PostgreSQL isolation and loopback-only tunnel: Task 3.
- Paper-only service composition and memory limits: Task 4.
- Real REST/WS/closed-bar/freshness/Stage1/persistence evidence: Task 5.
- Restart, reconnect, degraded/recovery, and 随想记 isolation: Task 6.
- Safety, secrets, cost, Git, and final acceptance report: Task 7.

