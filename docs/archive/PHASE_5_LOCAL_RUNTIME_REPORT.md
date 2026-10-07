# Phase 5 Local Runtime Report

## Final status

`PHASE5_LOCAL_RUNTIME_ACCEPTED`

This is the final local Phase 5 acceptance. The historical pre-hardening
interruption is retained below as audit evidence. The current acceptance
passed the database, semantic, recovery, resource, regression, and 30-minute
stability gates. External source limitations remain explicit and are not
converted into synthetic availability. Phase 6 was not started.

## Scope and safety

- Environment: WSL2 local only, `/home/lucas045057/projects/quant`
- No ECS, Jakarta, Hangzhou, Singapore, Cloud Assistant, or remote database was accessed.
- No Phase 6 work was started.
- `TRADING_MODE=paper` remained enabled.
- No private API, order API, position API, live executor, or real order path was enabled.
- The unrelated pre-existing `agentops` container was not modified.

## Host persistence hardening acceptance

Acceptance timestamp: `2026-09-22T06:57:01Z` (UTC)

### Host lifecycle

- WSL distribution: Ubuntu, WSL2, kernel `6.6.87.2-microsoft-standard-WSL2`.
- PID 1: `systemd`; `systemctl is-system-running=running`.
- Docker and containerd: enabled and active.
- Docker server: 29.1.3.
- Windows scheduled task: `Quant-WSL-Keepalive`, trigger `AtLogOn` for the
  interactive user, action `wsl.exe -d Ubuntu --exec /bin/sleep infinity`.
- Task settings: `MultipleInstances=IgnoreNew`, `StartWhenAvailable=true`,
  no execution time limit.
- No `wsl.exe --shutdown` was used.

### Recovery and persistence results

| Check | Result | Evidence |
|---|---|---|
| Manual keepalive proof of concept | PASS | Ubuntu, systemd, Docker, and all Quant containers remained available for approximately 10 minutes after temporary terminals closed |
| Docker service restart | PASS | Docker/containerd active; all three Quant containers returned; PostgreSQL readiness accepted |
| WSL `Ubuntu` terminate/restart | PASS | `wsl --terminate Ubuntu` returned 0; scheduled task restarted Ubuntu; systemd/Docker/containers recovered |
| 15-minute closed-terminal persistence | PASS | 15/15 samples; task Running; Docker active; all containers `true/0/false` |
| 30-minute host stability | PASS | 30/30 samples from `06:27:36Z` through `06:57:01Z`; no restart or OOM observed |

### Current container policy and resources

| Service | Restart policy | Memory limit | Final state |
|---|---|---:|---|
| quant-postgres | `unless-stopped` | 768 MiB | running, restart 0, OOM false |
| quant-collector | `unless-stopped` | 256 MiB | running, restart 0, OOM false |
| quant-engine | `unless-stopped` | 384 MiB | running, restart 0, OOM false |

Final `docker stats --no-stream` sample was approximately PostgreSQL 430 MiB,
collector 176 MiB, and engine 300 MiB. The local WSL host reported 1.5 GiB
used of 31 GiB and 14 GiB used of a 1,007 GiB root filesystem. These host
figures are observations of the development machine, not a claim about an
ECS-sized production host.

### Tests after hardening

- Host safety/resource focused tests: `3 passed`.
- Phase 5, resource, and project safety suites: `92 passed`.
- Full regression: `529 passed, 11 skipped`.
- Compose render: PASS with exactly three services, paper mode, and the three
  expected `unless-stopped` policies.

The skipped tests remain the explicit live public API probes and tests that
require an externally configured `TEST_POSTGRES_DSN`; none were silently
converted into passes.

## Final local runtime acceptance

Acceptance window: `2026-09-22T07:21:25Z` through `2026-09-22T07:51:42Z`
(30 minutes 17 seconds, UTC).

### Database gate

- Production local database: `quant`.
- PostgreSQL: 16.15.
- Time zone: `UTC`.
- Current migration runner: first run `[]`, second run `[]`.
- `schema_migrations`: 001–010 plus the recorded `009_phase4_metrics.repair.v1`
  marker.
- Fresh isolated PostgreSQL integration database: first run 10 migrations,
  second run 0; duplicate Kline upsert remained one row; UTC round trip passed.
- Phase 3/4 PostgreSQL integration: `19 passed`.
- The pre-existing Phase 1 integration test contains a stale assertion for 9
  migrations; it was not modified. The fresh-database equivalent was run with
  the correct Phase 5 count of 10.

### Phase 1–4 and Phase 5 runtime evidence

- Phase 1 closed Klines continued during the window; 80 Kline rows were
  processed in the measured window.
- Phase 3 flow continued; 330 flow rows were processed in the measured
  window.
- BTC/ETH context rows existed for all approved timeframes. Available rows
  used real aligned closed bars; stale or missing inputs remained `STALE` or
  `NOT_AVAILABLE`.
- Latest observed BTC/ETH examples included `AVAILABLE` 1H/4H contexts and
  explicit `STALE`/`NOT_AVAILABLE` 5m/15m contexts when inputs were not usable.
- Regime status distribution: 56 `PARTIAL`, 36 `STALE`, 25
  `NOT_AVAILABLE`; coverage remained explicit (`0` to `0.65`), never a zero
  return or false `AVAILABLE`.
- Sector context status: 166 `NOT_AVAILABLE` rows with explicit coverage
  handling; static taxonomy and `UNKNOWN` membership were preserved.
- Latest screening run: 200 rows; matching Phase 5 enrichment: 200 rows.
- All Phase 5 enrichment rows remained `context_only=true`; latest Stage1
  candidates were not removed or rewritten.
- SQL semantic checks: future context timestamps `0`, status/freshness
  mismatches `0`, available closed-bar alignment violations `0`, duplicate
  Phase 5 natural keys `0`, RS arithmetic mismatches `0`.

### Recovery gates

- Collector/engine controlled restart: PASS; both recovered without volume
  deletion, restart count remained 0, and Phase 5 timestamps resumed.
- PostgreSQL outage: PASS; PostgreSQL was stopped for a bounded test, both
  application containers stayed alive with `DEGRADED` handling, PostgreSQL
  returned healthy, and the next engine cycle advanced Phase 5 leader rows
  `139 -> 144` and enrichment rows `5200 -> 5400`.
- No crash loop, OOM, duplicate burst, or unexplained shutdown occurred after
  recovery.

### Stability and growth

| Metric | Start | End | Result |
|---|---:|---:|---|
| Database size | 4,427,070,487 bytes | 4,613,495,831 bytes | measured delta +186,425,344 bytes |
| Phase 5 leader rows | 144 | 166 | continued |
| Phase 5 regime rows | 104 | 116 | continued |
| Phase 5 RS rows | 33,010 | 35,810 | continued |
| Phase 5 sector rows | 154 | 166 | continued |
| Phase 5 Stage1 enrichment | 5,400 | 6,600 | continued |
| Runtime health events | 56 | 62 | continued |

`MEASURED_SAMPLE`: +186,425,344 bytes over 30 minutes 17 seconds.
`PROJECTED_ESTIMATE`: approximately 8.8 GB/day if this short sample persisted;
this is not a measured 24-hour growth result. The projection is retained as a
capacity warning for longer retention review.

Peak observed container memory during the window was approximately PostgreSQL
212.4 MiB, collector 156.8 MiB, and engine 290.8 MiB. All stayed below the
768/256/384 MiB limits. Final measured Docker JSON log sizes were PostgreSQL
58,935 bytes, collector 96,835 bytes, and engine 16,414 bytes; these are
point-in-time samples, not long-term log-growth estimates.

### Tests and known external debt

- Phase 5 focused, safety, resource, and project tests: `92 passed`.
- Full regression: `529 passed, 11 skipped`.
- Explicit skips: 3 Phase 2 live probes, 2 Phase 3 live probes, 3 Phase 4
  live probes, and 3 tests requiring an externally configured PostgreSQL DSN.
- Bybit remains `EXCHANGE_ACCESS_LIMITATION` / `PHASE4_EXTERNAL_SOURCE_GATE_PENDING`.
  Runtime recorded degraded derivative cycles without crashing or promoting
  unavailable data to available.
- A later Phase 1 ticker/snapshot WebSocket reconnect limitation was observed;
  Klines, trade flow, health, and Phase 5 cycles continued, and missing/stale
  states were persisted explicitly. No mock market data was inserted.
- Legacy `docker-compose` 1.29.2 remains the local tool limitation; Compose
  rendering passed and no upgrade was introduced in this acceptance.

## Pre-hardening runtime configuration (historical)

- Branch: `phase5`
- Pre-report HEAD: `731867ca5eb2e3af57162f75e31566f011845019`
- Working tree was clean before this report was added.
- Docker client/server: 29.1.3
- Compose available locally: legacy `docker-compose` 1.29.2
- Required Quant services: exactly `quant-postgres`, `quant-collector`, `quant-engine`
- No host PostgreSQL port was published.

| Service | Image | Memory limit | Restart policy |
|---|---|---:|---|
| quant-postgres | postgres:16-alpine | 768 MiB | no |
| quant-collector | quant-phase5:f55a21b-local | 256 MiB | on-failure |
| quant-engine | quant-phase5:f55a21b-local | 384 MiB | on-failure |

## Migration and database checks

- First migration replay: `[]` (zero new migrations)
- Second migration replay: `[]` (zero new migrations)
- Database: `quant`
- PostgreSQL timezone: `UTC`
- Migration records: 001 through 010 present, including the recorded Phase 4 repair marker.
- Database size measured at one sample: `3,248 MB`.
- Local volume `quant_quant_local_pgdata` was preserved; no volume was deleted.

Measured row counts at approximately `2026-09-22T04:54Z`:

| Table | Rows |
|---|---:|
| symbols | 803 |
| market_snapshots | 823,702 |
| market_observations | 2,444,514 |
| klines | 106,994 |
| screening_results | 17,400 |
| cross_exchange_flow_snapshots | 3,980 |
| phase5_market_leader_context | 24 |
| phase5_market_regime_snapshots | 12 |
| phase5_relative_strength_snapshots | 4,800 |
| phase5_sector_membership | 794 |
| phase5_sector_context_snapshots | 16 |
| stage1_phase5_context_enrichment | 800 |
| runtime_health_events | 29 |

The local Phase 5 cycle used existing canonical database data. No mock OI,
funding, CVD, liquidation, long/short, news, or AI data was inserted. Real
coverage gaps were represented as unavailable/stale values.

## Phase 5 gate results

| Gate | Result | Evidence |
|---|---|---|
| closed-kline and interval handling | PASS | deterministic tests and runtime persistence |
| BTC/ETH context paths | PASS | deterministic tests |
| breadth, regime, relative strength, sectors | PASS / DEGRADED | tests passed; runtime rows persisted with real availability states |
| timestamp, skew, freshness | PASS | deterministic tests |
| persistence and retention contracts | PASS | Phase 5 persistence/retention tests |
| Stage 1 non-interference | PASS | full regression and runtime cycles |
| Bybit degraded handling | PASS | runtime recorded degraded Phase 2 status without mock promotion |
| controlled database outage recovery | PASS once | explicit stop/start recovered readiness, migrations, and a Phase 5 cycle |
| sustained runtime stability | ENVIRONMENT INTERRUPTED | WSL session exit stopped PostgreSQL after the held session |
| 30-minute observation | PASS IN HELD SESSION / FINAL FAIL | 60 heartbeats had no stop event; post-session extension found PostgreSQL stopped |

## Resource observations

Measured with `docker stats` and `docker inspect`:

- Collector: 41.37 MiB / 256 MiB at one sample; later 55.21 MiB / 256 MiB.
- Engine: 48.62 MiB / 384 MiB at one running sample.
- PostgreSQL: no valid steady-state sample at the final check because it had
  already exited; stopped-container output was 0B.
- Sample CPU: collector 0.36% and engine 2.30%; later collector 1.23%.
- Inspected containers had `OOMKilled=false` and `RestartCount=0` at the
  corresponding inspections.
- Docker JSON log byte size was unavailable from the WSL Docker path; it is
  reported as unavailable rather than fabricated. Log content was inspected.

## Pre-hardening restart and stability evidence (historical)

PostgreSQL logs repeatedly contained `received fast shutdown request`, followed
by clean shutdown. Representative observations:

- Started `04:49:19Z`, stopped `04:51:49Z`, exit `0`, OOM `false`.
- Started `04:54:34Z`, stopped `04:54:52Z`, exit `0`, OOM `false`.
- Earlier sample: started `04:46:53Z`, stopped `04:47:18Z`, exit `0`, OOM `false`.

At the final sample (`2026-09-22T04:55:33Z`), PostgreSQL and engine were
exited, while collector had restarted and was running in a degraded state.
Application logs showed `server closed the connection unexpectedly` and `the
database system is shutting down`.

The observed signal was not an OOM or PostgreSQL crash. Its source was not
identified by the available local Docker event history. Because acceptance
requires all three services to remain healthy, this is a blocker.

## Tests

- Phase 5 focused tests: `83 passed`
- Full regression: `528 passed, 11 skipped`
- No test failures.

Skipped tests:

- 3 Phase 2 live public API probes: live contract flag not enabled
- 2 Phase 3 live public API probes: live contract flag not enabled
- 3 Phase 4 live public API probes: live contract flag not enabled
- 1 Phase 3 persistence integration: `TEST_POSTGRES_DSN` not configured
- 1 Phase 4 persistence integration: `TEST_POSTGRES_DSN` not configured
- 1 repository integration: `TEST_POSTGRES_DSN` not configured

The skipped live and external-DSN tests are explicit skips, not silently
converted into passes.

## Historical known issues and blockers (pre-hardening)

### Blocker

1. The WSL runtime/session exits after the observation shell ends. Docker then
   stops PostgreSQL cleanly; because PostgreSQL has restart policy `no`, it does
   not return, while engine/collector behavior follows their configured
   policies. The runtime owner/host-level trigger is not visible in the
   available Docker event history.

### Warnings

1. The local compose file uses legacy `docker-compose` because the local
   environment did not provide the v2 `docker compose` command.
2. The local bootstrap role `quant` reports PostgreSQL superuser capability;
   a production-like local acceptance should use the least-privilege runtime
   role before deployment.
3. Live public API probes and an independent integration DSN were not enabled,
   so this report does not claim live external-source acceptance.
4. Long-term database and log growth were not projected from a stable 30-minute
   run. The database size above is a measured sample only.

## Historical re-acceptance requirements (superseded)

1. Provide a stable WSL/Docker runtime owner or supervised environment that
   remains alive after the acceptance shell exits; do not rely on a temporary
   interactive keepalive as an acceptance substitute.
2. Re-run migration idempotency, outage recovery, resource sampling, and a
   complete 30-minute observation.
3. Re-run the full regression and verify a clean working tree.

Phase 6 was not started.

## PostgreSQL fast-shutdown root-cause diagnostic

### Evidence

- PostgreSQL container: `2d1a2ebc7c8c099f4c82558ea9f2eca42ea97f6f561449f8512b83b385f07987`.
- Ten historical PostgreSQL shutdown requests were present between
  `2026-09-22T04:18:08Z` and `2026-09-22T04:54:52Z`.
- Every shutdown was `received fast shutdown request`, followed by a clean
  checkpoint and `database system is shut down`.
- Exit code was `0`; `OOMKilled=false`; no PANIC, corruption, disk error, or
  out-of-memory evidence was found. The shutdown-time FATAL messages were
  connection termination consequences of the shutdown.
- Docker historical events were unavailable. During the held observation,
  `/tmp/quant-postgres-events.log` contained only health-probe `exec_*` events;
  no `stop`, `kill`, `die`, `restart`, or `destroy` event occurred.
- The held observation ran from approximately `05:15:31Z` to `05:45:29Z`.
  All 60 heartbeats saw PostgreSQL accepting connections, collector and engine
  running, restart count zero, and no OOM.
- A read-only extension immediately after the held session ended observed
  PostgreSQL `Exited (0)` while engine and collector had been restarted.
- WSL systemd boot/session evidence showed user sessions entering
  `exit.target` between tool sessions; a new WSL boot restarted dockerd.
  PostgreSQL's `restart=no` policy then left it stopped.

### Root-cause classification

- `POSTGRES_INTERNAL_CRASH_EVIDENCE = NONE`
- `TEST_HARNESS_BUG = NOT_FOUND` in tracked project scripts/tests/fixtures.
- `COMPOSE_LIFECYCLE_BUG = NOT_PROVEN`; no compose down/stop occurred during
  the held observation.
- `DOCKER_DESKTOP_RESTART = NOT_PROVEN`.
- `MANUAL_STOP = NOT_PROVEN`.
- `WSL_RUNTIME_RESTART = EVIDENCE_SUPPORTED`.
- `ENVIRONMENT_RUNTIME_INTERRUPTION = YES`.
- Final status remains `PHASE5_RUNTIME_SHUTDOWN_CAUSE_UNRESOLVED` because the
  host-level actor that terminates the WSL session is not observable from the
  available Docker history, and the runtime is not stable after the session
  ends.

### Code and fix record

- Phase 5 market logic, formulas, migration 010, and runtime code were not
  modified during this diagnostic.
- No test-harness or Compose fix was justified by evidence.
- No rebuild or fix commit was created.
- Existing migration replay and full regression results remain valid.
