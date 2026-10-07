# Phase 7 Local Runtime Acceptance Report

> **Current disposition (2026-09-25 Asia/Shanghai):**
> `PHASE7_RESOURCE_ACCEPTANCE_BLOCKED`. The latest full acceptance attempt is
> recorded at the end of this report and supersedes earlier “runtime pending”
> statements. It stopped early on the configured 256 MiB Collector resource
> gate; no 60-minute soak or Phase 8/9 work was started.

## Result

`PHASE7_SOURCE_CONFIG_READY`

Memory remediation prerequisite: `PHASE7_ENGINE_MEMORY_REMEDIATED`.
The earlier live-runtime gate remains pending until an operator configures an
approved read-only Chain provider; this task only implements that contract.

## FINAL LOCAL RUNTIME RE-ACCEPTANCE POST MEMORY REMEDIATION

This acceptance was limited to the local WSL2 environment at
`/home/lucas045057/projects/quant`. No remote ECS, remote PostgreSQL, private
exchange API, order API, live executor, or Phase 8/9 work was used.

The previous runtime acceptance was not accepted because of engine memory. The
memory remediation is now complete under the unchanged 384 MiB limit: the
database hydration set is bounded, the new image is deployed, and the engine
completed a 30-minute local stability window. Full Phase 7 live chain/spot
acceptance remains a separate, unstarted gate.

## Initial repository checkpoint (before remediation)

- Branch: `phase7`
- HEAD: `05c33f1a111d36be5b63030f0b535c82dda35faf`
- Working tree before this report: clean
- `TRADING_MODE`: `paper` in the local runtime configuration
- Phase 4 external-source state preserved: `PHASE4_EXTERNAL_SOURCE_GATE_PENDING`
- Phase 6 provider state preserved: `PHASE6_AI_PROVIDER_CREDENTIAL_REQUIRED`

## Initial host and Docker preflight (before remediation)

- Environment: WSL2 Ubuntu 24.04.4
- Docker client/server: 29.1.3 / 29.1.3
- Docker engine and containerd: available
- Windows `Quant-WSL-Keepalive`: running
- No container was restarted, removed, or recreated during this acceptance

The expected containers were running, but they were not built from the Phase 7
checkpoint:

| Container | Actual image | State | Restart count | OOM killed | Memory limit |
|---|---|---:|---:|---:|---:|
| `quant-postgres` | `postgres:16-alpine` | running | 0 | false | 768 MiB |
| `quant-collector` | `quant-phase5:f55a21b-local` | running | 275 at final read | false | 256 MiB |
| `quant-engine` | `quant-phase5:f55a21b-local` | running | 3 | false | 384 MiB |

The repository Dockerfile still starts
`quant_phase1.entrypoints.engine`, and the checked-in local compose file refers
to a Phase 4 image. There is no Phase 7 runtime entrypoint wired into the
currently running containers.

## Initial runtime resource sample (before remediation)

The recorded `docker stats --no-stream` sample was:

| Container | CPU | Memory usage / limit |
|---|---:|---:|
| `quant-postgres` | 1.74% | 542.5 MiB / 768 MiB (70.64%) |
| `quant-collector` | 1.81% | 53.55 MiB / 256 MiB (20.92%) |
| `quant-engine` | 0.00% | 383.6 MiB / 384 MiB (99.89%) |

The engine memory sample is at the configured limit and is an OOM-risk blocker
for runtime acceptance.

## Initial collector and engine observations (before remediation)

The collector log repeatedly ends in the existing Phase 4 failure:

`ValueError: one liquidation rollup cannot mix source semantics`

The failure occurs while hydrating persisted Phase 4 windows and is followed
by unclosed HTTP-session warnings. The collector is therefore in a persistent
restart loop. Phase 4 code was not modified because this acceptance explicitly
forbids unrelated Phase 4 changes.

The engine initially encountered the known Bitget public REST connection reset.
Later logs contained Stage 1 cycles and degraded Phase 2 cycles, but those logs
come from the Phase 5 image and cannot be counted as Phase 7 live-chain or
spot-source acceptance evidence.

## PostgreSQL and migration gate

- PostgreSQL: 16.15
- Database: `quant`
- `current_database()`: `quant`
- Session timezone: `UTC`
- Active database size: approximately 9.2 GB
- Applied migrations in the active database: `001` through `011`
- Migration `012_phase7_onchain_spot_context.sql`: present in the repository,
  not applied to the active runtime database

The active database therefore fails the required `001–012` migration gate.
No migration was applied during this acceptance, and no existing migration
`001–011` was modified.

## Phase 7 source registry

The checked-out Phase 7 registry reports:

| Source | Status | Credential mode |
|---|---|---|
| Bitcoin RPC | `NOT_CONFIGURED` | read-only provider |
| Ethereum RPC | `NOT_CONFIGURED` | read-only provider |
| Binance Spot | `ENABLED` | none |
| Bitget UTA v3 Spot | `PENDING_CONTRACT` | none |

There is no configured Bitcoin or Ethereum source capable of satisfying the
required live chain progression gate. The enabled Binance Spot definition is
not sufficient for acceptance because a chain source and an integrated Phase 7
runtime are also required. No fabricated chain, spot, valuation, finality,
reorg, cursor, or coverage observations were inserted.

## Deterministic test evidence

- Phase 7 focused tests: `154 passed`
- Previously recorded full no-DSN regression: `744 passed, 13 skipped`
- Previously recorded isolated PostgreSQL integration focus: `15 passed`

These are code/test results only. They do not substitute for the failed live
runtime gates above.

## Security and scope checks

- Private exchange API: not used
- API keys/secrets/passphrases: not requested or printed
- Order/position API: not used
- Live trading: not enabled
- Remote ECS or remote PostgreSQL: not accessed
- Phase 8/9: not started
- Phase 4 source adapter: not modified

## Blockers

1. `PHASE7_RUNTIME_NOT_DEPLOYED`: active collector and engine containers run a
   Phase 5 image; the repository runtime wiring still points at a Phase 1/4
   entrypoint rather than an integrated Phase 7 runtime.
2. `MIGRATION_012_NOT_APPLIED`: active `quant` database is at migrations
   `001–011`, not `001–012`.
3. `PHASE4_COLLECTOR_CRASH_LOOP`: existing liquidation aggregation semantic
   exception causes repeated collector restarts; fixing it would violate the
   scope restriction against Phase 4 changes.
4. `ENGINE_MEMORY_AT_LIMIT`: recorded engine usage was 99.89% of its 384 MiB
   limit.
5. `SOURCE_ACCESS_BLOCKER`: Bitcoin and Ethereum RPC sources are
   `NOT_CONFIGURED`; therefore the required live chain gate cannot be proven.

## Initial final state (before remediation)

`PHASE7_LOCAL_RUNTIME_NOT_ACCEPTED`

## RUNTIME_REMEDIATION

### Runtime deployment

- Old runtime image: `quant-phase5:f55a21b-local`
- Old collector image ID: `sha256:9cc095b70acaa24c56fbf8d59551a53cb032d31d0d67873e6a588b07461d9285`
- New runtime image: `quant-phase7:42e2030-local`
- New image ID: `sha256:35b41f2d0ae27c3ac897b94ae70552098fa06a6c953d95bd26402cf1624f8ce6`
- New image Git SHA label: `42e2030eede4b6d54d357c92e6c46bee571b871e`
- New collector ID: `adaaabf087bd5ce09f24e68f6ab0098f65ec379a144dfd8da9b6afbffee92b9b`
- New engine ID: `ce16ec0d31e153771beb4fa7c0cac6b3551112c060e0002bd3d4f1c789dcee00`
- New application `StartedAt`: `2026-09-23T04:44:04Z`
- PostgreSQL container and `quant_local_pgdata` volume: preserved
- Resource limits: unchanged (PostgreSQL 768 MiB, collector 256 MiB, engine 384 MiB)
- `TRADING_MODE`: `paper`

### Migration 012

- Active database: `quant`
- Active database timezone: `UTC`
- Active migration runner: Migration 012 applied successfully
- Active migration rerun: `[]` (0 new migrations)
- Phase 7 core tables: present and empty at remediation time
- Isolated PostgreSQL 16.15 first run: all migrations `001–012` applied
- Isolated PostgreSQL 16.15 second run: `[]` (0 new migrations)
- Isolated temporary container and network: removed

The normal migration table also contains the existing `009_phase4_metrics.repair.v1`
marker in addition to the numbered migrations; this is the pre-existing repair
marker, not a new Phase 7 migration.

### Collector regression remediation

The new image reproduced the exact prior exception during hydration:

`ValueError: one liquidation rollup cannot mix source semantics`

The verified persisted case contained a `NOT_AVAILABLE` gap minute adjacent to
an `AGGREGATED_MAX_PER_SECOND / PARTIAL_AGGREGATED` Bitget minute in the same
rollup bucket. The minimal fix is fail-closed:

- no cross-semantic arithmetic is performed;
- the rollup becomes `ERROR` with reason `MIXED_SOURCE_SEMANTICS`;
- source granularity and coverage become `NOT_AVAILABLE`;
- observed counts and notional remain zero/null;
- Phase 4 health is marked degraded/error;
- the warning contains only bounded identity fields, never raw payload data.

Approved homogeneous Bitget semantics remain unchanged:
`AGGREGATED_MAX_PER_SECOND` and `PARTIAL_AGGREGATED`.

Results:

- Failing regression reproduced the crash before the fix.
- Focused regression after the fix: `1 passed`.
- Phase 4 suite after the fix: `216 passed, 1 skipped`.
- Collector after redeploy: RestartCount remained `0` throughout the 10-minute observation.
- No subsequent traceback or collector crash was observed.
- Phase 4 health remains explicitly degraded for the existing
  `LIQUIDATION_GAP_NO_BACKFILL` data condition; this is a data-source health
  state, not a process crash.

### Engine resource recheck

Ten one-minute samples were collected from 04:44:17Z through 04:53:36Z.

| Sample window | Collector memory | Engine memory | Collector restarts | Engine restarts |
|---|---:|---:|---:|---:|
| 04:44–04:45Z | 49.5–56.4 MiB | 49.6–57.7 MiB | 0 | 0 |
| 04:46–04:47Z | 125.8–136.1 MiB | 101.3 MiB | 0 | 0 |
| 04:48–04:49Z | 144.6–159.6 MiB | 101.3 MiB | 0 | 0 |
| 04:50Z | 163.4 MiB | 355.6 MiB (92.60%) | 0 | 0 |
| 04:51–04:53Z | 161.6–166.6 MiB | 355.1 MiB (92.47%) | 0 | 0 |

All samples had `OOMKilled=false`. The engine rose during the first Stage 1
cycle, then remained at approximately 355.1 MiB of the 384 MiB cgroup limit
(92.47%) through the final samples; `/proc/1/status` reported 381.6 MB RSS at
the final check. It did not continue linearly during the observation, but the
remaining headroom is a recorded resource warning for the next acceptance.
No limit was increased, no periodic restart was added, and no unbounded-cache
change was made.

### Source configuration contract

The design and current registry were audited without inventing environment
variables:

| Source | Contract | Endpoint/config status | Credentials |
|---|---|---|---|
| Bitcoin | Bitcoin Core-compatible JSON-RPC; `getblockchaininfo`, `getblockhash`, `getblock`, `getrawtransaction`; 1 req/s local cap; 2s connect/8s total timeout | No default URL and no Bitcoin-specific env var is implemented; registry remains `NOT_CONFIGURED` | Endpoint-dependent read-only provider auth only; none supplied |
| Ethereum | Ethereum JSON-RPC; `eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`, `eth_getLogs`, `eth_getTransactionReceipt`; 1 req/s local cap; 1,000-block log range | No default URL and no Ethereum-specific env var is implemented; registry remains `NOT_CONFIGURED` | Provider-dependent read-only auth only; none supplied |
| Binance Spot | `https://api.binance.com/api/v3/aggTrades`, `wss://stream.binance.com:9443/ws` | `ENABLED` | None |
| Bitget UTA v3 Spot | `https://api.bitget.com/api/v3/market/fills`, `wss://ws.bitget.com/v3/ws/public` | `PENDING_CONTRACT`; no v2 fallback | None |

The design explicitly leaves chain endpoint/provider selection to operator
configuration and does not approve a default third-party provider. The next
full re-acceptance therefore still needs an operator-approved Bitcoin and/or
Ethereum endpoint configuration; no key was requested, stored, logged, or
committed.

### Smoke and regression evidence

- Phase 7 deterministic suite: `154 passed`
- Full local regression after memory remediation: `746 passed, 13 skipped`
- Migration runner after redeploy: `0 new migrations`
- Phase 1–6 code paths remained covered by the full regression suite
- No full Phase 7 Runtime Re-Acceptance or live chain acceptance was started;
  the memory-only 30-minute stability window completed.

### Remaining gates

The runtime remediation is complete. The next formal re-acceptance still has
these external/data gates:

1. Operator-approved Bitcoin and Ethereum read-only endpoint configuration.
2. Bitget UTA v3 Spot contract verification remains pending.
3. Existing Phase 4 Bybit external-source gate remains pending.
4. Existing Phase 6 AI provider credential state remains required, but AI is
   not a Phase 7 runtime dependency.

## PRIOR FINAL LOCAL RUNTIME RE-ACCEPTANCE (BEFORE MEMORY REMEDIATION)

### Image identity

- `RUNTIME_CODE_SHA=42e2030eede4b6d54d357c92e6c46bee571b871e`
- `REPORT_HEAD=d72c7f313c4989d878f9938bfb9d227821d148ea`
- `git diff 42e2030..d72c7f3`: report Markdown only
- Runtime image `quant-phase7:42e2030-local` therefore remained valid for the
  runtime code SHA; the HEAD difference is documentation only.
- Branch: `phase7`; working tree was clean before this report update.

### Preflight and migration gates

- Active containers used the expected Phase 7 image; PostgreSQL remained
  `postgres:16-alpine` and its existing volume was preserved.
- `quant-postgres`, `quant-collector`, and `quant-engine` were running with
  `RestartCount=0` and `OOMKilled=false` at preflight.
- PostgreSQL `16.15`, database `quant`, timezone `UTC`.
- Active `schema_migrations` contains `001–012`; Migration 012 runner result
  was `0 new migrations` on the repeated check.
- Isolated PostgreSQL 16.15 migration test applied `001–012` on the first run
  and returned `0 new migrations` on the second run; the temporary database
  and network were removed.

### Collector gate

The mixed-liquidation-semantics fix remained deployed and the collector had no
same exception or process exit during the observed runtime. Existing approved
homogeneous semantics were not changed. The earlier 10-minute remediation
observation had `RestartCount=0` and `OOMKilled=false`.

### Engine memory strong gate — BLOCKER

The formal re-acceptance preflight observed the engine at `372.5 MiB / 384 MiB`
(96.99%) with RSS `400.7 MB`. Consecutive samples were:

| UTC sample | Engine cgroup memory | Engine RSS |
|---|---:|---:|
| 05:35:02 | 372.5 MiB (96.99%) | 400.7 MB |
| 05:36:04 | 372.8 MiB (97.08%) | 400.7 MB |
| 05:37:07 | 372.7 MiB (97.06%) | 400.7 MB |

This is a sustained `>=95%` memory condition and is classified as
`ENGINE_RESOURCE_BLOCKER`. No memory limit was raised, no periodic restart was
added, and no restart was used to mask the condition. The formal 30–60 minute
stability window was not started.

### Live source gates

- Bitcoin: `NOT_CONFIGURED`; no approved endpoint or Bitcoin-specific
  environment variable exists in the current contract.
- Ethereum: `NOT_CONFIGURED`; no approved endpoint or Ethereum-specific
  environment variable exists in the current contract.
- Binance Spot is defined as a no-credential public source, but no formal live
  progression gate was counted after the engine strong gate failed.
- Bitget UTA v3 Spot remains `PENDING_CONTRACT`.
- Consequently there is no valid live chain block/cursor progression or live
  Spot progression to report, and no fixture was promoted to live evidence.

### Unstarted gates

Because the engine strong gate failed, the following formal gates were not
started: live chain/spot progression, asset identity, valuation skew, finality
and reorg runtime checks, restart recovery, PostgreSQL outage recovery, source
failure recovery, retention runtime checks, 30-minute stability, and final
full acceptance regression. Existing deterministic test evidence remains
unchanged: Phase 4 `216 passed, 1 skipped`; Phase 7 `154 passed`; full local
regression `745 passed, 13 skipped`.

### Final local runtime state

`PHASE7_LOCAL_RUNTIME_NOT_ACCEPTED`

Blocker: `ENGINE_RESOURCE_BLOCKER` — engine remained above the permitted 95%
memory threshold under the unchanged 384 MiB limit. Secondary outstanding
gate: no operator-approved Bitcoin/Ethereum endpoint is configured, so live
chain acceptance cannot be performed without an approved data-provider
configuration.

## ENGINE_MEMORY_REMEDIATION

### Identity and scope

- `RUNTIME_CODE_SHA=03dcfc17c952c1fb208add8cd29fe4b75e7d7812`
- New image: `quant-phase7:03dcfc1-local`
- Image ID: `sha256:b330abab6477f2a8757c0db3e2c54b8add31b50317c3f858b67f9149731f94c4`
- Image revision label matches the full runtime code SHA.
- The change is limited to bounded Kline hydration and its deterministic test;
  no memory limit, safety check, trading path, canonical semantic, or retention
  policy was removed or weakened.
- Detailed inventory: [PHASE_7_ENGINE_MEMORY_AUDIT.md](/home/lucas045057/projects/quant/PHASE_7_ENGINE_MEMORY_AUDIT.md)

### Root cause and fix

The old engine query materialized all retained Klines: `169,080` rows at the
time of the audit, with up to `398` rows per symbol/timeframe group. The local
runtime was configured with `KLINE_FETCH_LIMIT=10`. The repository now uses a
window function partitioned by `symbol, interval` and loads only the newest 10
available candles per group; the engine passes the configured limit explicitly.
The real-database verification returned `5,160` candles for 200 symbols and
516 groups, with a maximum of 10 per group.

### Before/after memory evidence

Before remediation, one Python PID with two threads and no child process reached
`382.0–383.8 MiB / 384 MiB`; `VmRSS` reached `410.3 MB` and anonymous memory
`387.9 MB`. After the new image started at `05:51:48Z`, the 30 one-minute
sample set was:

| Sample | Engine cgroup memory | Engine RSS |
|---|---:|---:|
| 0 min | 48.46 MiB (12.6%) | 67.8 MB |
| 1 min | 54.03 MiB (14.1%) | 74.1 MB |
| 3 min | 101.0 MiB (26.3%) | 124.5 MB |
| 5 min | 101.0 MiB (26.3%) | 124.5 MB |
| 10 min | 106.1 MiB (27.6%) | 129.4 MB |
| 15 min | 110.0 MiB (28.6%) | 133.4 MB |
| 30 min | 112.6 MiB (29.3%) | 136.4 MB |
| Final 06:23:51Z | 112.9 MiB (29.4%) | 136.6 MB |

Across all 30 one-minute samples: minimum `48.46 MiB`, average `104.24 MiB`,
peak `113.4 MiB`, final window sample `112.6 MiB`, and final post-window check
`112.9 MiB`. The remaining engine headroom at the final check was about
`271.1 MiB` (`70.6%` of the configured limit). `RestartCount=0` and
`OOMKilled=false` throughout; no periodic restart or fault injection was used.

### Verification results

- Phase 4/Phase 7/resource focused subset: `383 passed, 1 skipped`.
- Full local regression: `746 passed, 13 skipped`.
- PostgreSQL: 16.15, `quant`, UTC, migrations `001–012`; active migration
  rerun remained `0 new migrations`.
- Collector remained running with its existing approved remediation image;
  engine ran the new image and emitted continuing Stage 1/Phase 2 cycle logs
  without traceback or process exit.
- BTC/Ethereum remain `NOT_CONFIGURED`; no live chain/spot gate, provider
  credential, ECS action, or Phase 8 work was performed.

### Remediation result

`PHASE7_ENGINE_MEMORY_REMEDIATED`

## SOURCE_CONFIGURATION_IMPLEMENTATION

### Scope and safety boundary

This change implements the Phase 7 source configuration contract only. It does
not connect to Bitcoin, Ethereum, any exchange, ECS, Phase 8, or Phase 6 AI;
it does not change Migration 012, canonical asset identity, finality, reorg,
whale, or Spot semantics. All tests use fake/local configuration and no live
endpoint.

### Formal environment variables

All values are optional and default to disabled/unconfigured. Real endpoint
values must be supplied by the operator through the existing ignored secret
mechanism or process environment; no values are committed here.

Bitcoin Core-compatible RPC:

- `PHASE7_BITCOIN_RPC_ENABLED`
- `PHASE7_BITCOIN_RPC_URL`
- `PHASE7_BITCOIN_RPC_AUTH_MODE` (`none`, `url`, or `basic`)
- `PHASE7_BITCOIN_RPC_USERNAME` (only for `basic`)
- `PHASE7_BITCOIN_RPC_PASSWORD` (only for `basic`)
- `PHASE7_BITCOIN_RPC_TIMEOUT_SECONDS`
- `PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND`
- `PHASE7_BITCOIN_RPC_MAX_CONCURRENCY`

Ethereum JSON-RPC:

- `PHASE7_ETHEREUM_RPC_ENABLED`
- `PHASE7_ETHEREUM_RPC_URL`
- `PHASE7_ETHEREUM_RPC_AUTH_MODE` (`none` or `url`)
- `PHASE7_ETHEREUM_RPC_TIMEOUT_SECONDS`
- `PHASE7_ETHEREUM_RPC_REQUESTS_PER_SECOND`
- `PHASE7_ETHEREUM_RPC_MAX_CONCURRENCY`

`basic` is restricted to Bitcoin self-hosted RPC configuration. Managed
provider credentials may be embedded in the endpoint URL and are treated as
secret-bearing configuration; no provider-specific SDK or provider logic was
added.

### Validation and status semantics

- `enabled=false` is reported as `SOURCE_DISABLED`.
- `enabled=true` without an endpoint is rejected during settings/startup
  validation as `SOURCE_NOT_CONFIGURED`.
- Malformed HTTP(S) URLs, invalid ports, unsupported auth modes, incomplete
  basic credentials, timeout values outside `0.1–60` seconds, rates outside
  `0.1–100` requests/second, or concurrency above `8` fail closed.
- No default public RPC endpoint is present.
- A configured endpoint is not reported as live data availability until a
  future adapter reports a successful read; Health Registry status remains
  `NOT_AVAILABLE` with safe configuration metadata.

### Secret handling and health integration

`Phase7RpcSourceSettings` excludes endpoint, username, and password from its
representation. Endpoint diagnostics retain only the host and optional port;
scheme, path, query, userinfo, tokens, and authorization values are removed.
The existing structured logger now applies the same URL and Authorization
redaction before writing messages. Phase 7 source configuration is exposed
through the existing Health Registry as `bitcoin_rpc` and `ethereum_rpc` with
bounded scalar details only; raw payloads and full URLs are not accepted.

### Implementation and verification

- Configuration model and environment parsing:
  `src/quant_phase1/config.py`
- URL/Authorization sanitization:
  `src/quant_phase1/logging.py`
- Source status, startup validation, diagnostics, and Health Registry bridge:
  `src/quant_phase7/source_config.py`
- Example variable names with blank values:
  `.env.example`
- Fake/local contract, malformed-input, reload, redaction, and health tests:
  `tests/test_phase7_source_config.py`
- Phase 7/config/security regression: `222 passed`.
- Source configuration implementation commit: `1f483030120019f1428f28caff377809270b76b2`

### Current implementation result

`PHASE7_SOURCE_CONFIG_READY`

The next operator action, when live acceptance is intentionally resumed, is to
set the approved read-only provider variables listed above. No real values are
included in this report.

## FINAL LOCAL RUNTIME RE-ACCEPTANCE POST MEMORY REMEDIATION — HISTORICAL RESULT

This section records the result before the source configuration contract was
implemented. The current task result is recorded in
`SOURCE_CONFIGURATION_IMPLEMENTATION` above.

### Re-acceptance scope and identity

This section is the current result after the engine memory remediation. The
pre-remediation rejection above is retained as historical evidence and is not
the current runtime result.

- `RUNTIME_CODE_SHA=03dcfc17c952c1fb208add8cd29fe4b75e7d7812`
- Runtime image: `quant-phase7:03dcfc1-local`
- Runtime image ID: `sha256:b330abab6477f2a8757c0db3e2c54b8add31b50317c3f858b67f9149731f94c4`
- Image revision label matches the runtime code SHA.
- Git audit baseline: `e5ba700b04dfa3521defaad09f483cb955f01b8a`
- Diff from the runtime-code baseline contains documentation only; no runtime
  code, configuration, migration, or dependency change was introduced for
  this re-acceptance.

### Approved data-source configuration audit

The Phase 7 contract requires a real, operator-approved Chain source before
the final local acceptance can proceed. The repository currently defines the
following normative read-only interfaces:

| Domain | Approved source contract | Current status | Endpoint / credential configuration |
|---|---|---|---|
| Bitcoin | Bitcoin Core-compatible JSON-RPC (`getblockchaininfo`, `getblockhash`, `getblock`, `getrawtransaction`) | `NOT_CONFIGURED` | No endpoint is configured; no BTC provider environment variable is implemented |
| Ethereum | Read-only Ethereum JSON-RPC (`eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`, `eth_getLogs`, `eth_getTransactionReceipt`) | `NOT_CONFIGURED` | No endpoint is configured; no ETH/EVM provider environment variable is implemented |
| Binance Spot | Approved public `aggTrade` source | `ENABLED` | Public no-credential contract exists, but it cannot substitute for the required Chain gate |
| Bitget Spot | UTA v3 `publicTrade` source | `PENDING_CONTRACT` | No live acceptance source is promoted |

There are no exact BTC/ETH credential environment names to provide: the
current config model and environment loader implement none. Do not invent
`BTC_*`, `ETH_*`, `EVM_*`, or generic `RPC_*` variables. An operator must first
configure an approved read-only provider endpoint and its credential through
the project's approved secret/configuration mechanism, then rerun the source
contract tests.

No provider credential, token, endpoint secret, proxy, relay, or unofficial
source was requested, printed, or added during this audit.

### Gates passed before the hard stop

- Runtime image identity and revision-label verification: `PASS`.
- Git working tree and metadata audit: `PASS` before report-only changes.
- Migration 012 and UTC database checks: `PASS`.
- Engine memory remediation: `PASS`; 30-minute post-fix window peaked at
  `113.4 MiB / 384 MiB`, with `RestartCount=0` and `OOMKilled=false`.
- Bounded Kline hydration: `PASS`; `5,160` candles for `516` groups, maximum
  `10` rows per group.
- Existing collector observation: `PASS` for the observed remediation window.
- Full local regression already executed against this runtime code:
  `746 passed, 13 skipped`.

### Gates not executed because Chain configuration is missing

The following gates were deliberately not started and have no live evidence:

- Bitcoin and Ethereum real block/cursor progression.
- Chain semantic validation, finality/reorg handling, and source recovery.
- Real Spot progression and cross-source semantic validation.
- Live asset identity and USD valuation checks.
- Restart recovery with live Chain/Spot sources.
- PostgreSQL outage and source-failure recovery with live providers.
- Final 30+ minute all-source stability window.
- Final Phase 7 acceptance regression that depends on those live gates.

### Historical result

`PHASE7_DATA_PROVIDER_CREDENTIAL_REQUIRED`

Blocker: no approved read-only Bitcoin Core-compatible or Ethereum JSON-RPC
provider endpoint is configured, and no corresponding provider credential
environment contract is implemented. Phase 7 final local runtime acceptance
cannot be claimed without that operator-approved configuration.

No ECS was contacted, no Phase 8 work was performed, no Phase 6 AI credential
was used, and no live trading capability was enabled.

## PHASE 7 FINAL LOCAL RUNTIME ACCEPTANCE — CURRENT ATTEMPT

### Checkpoint and image identity

- Branch: `phase7`
- Final Git HEAD: `b993dd9ebaba1277379d93d7fd0101a6e218c0f3`
- Working tree before this report update: clean
- Candidate image rebuilt from the current HEAD:
  `quant-phase7:b993dd9-local`
- Candidate image ID: `sha256:357964bbd7c5657f86fec55223dea742589fdbd4cda1a7ad8692791586d51a3d`
- Candidate image revision label matches the current HEAD.

The running containers were not current-HEAD containers at the preflight:

| Container | Running image | Runtime code revision |
|---|---|---|
| `quant-engine` | `quant-phase7:03dcfc1-local` | `03dcfc17c952c1fb208add8cd29fe4b75e7d7812` |
| `quant-collector` | `quant-phase7:42e2030-local` | `42e2030eede4b6d54d357c92e6c46bee571b871e` |

The current HEAD differs from the running runtime code in configuration,
logging, Phase 7 source-configuration code, and report/test files. The
candidate image was built locally, but the containers were not replaced because
the repository has no Phase 7 Chain/Spot runtime orchestration to execute.

### Runtime wiring blocker

The existing collector and engine entrypoints contain no import or invocation
of `quant_phase7` Chain/Spot ingestion. They continue to run the existing
Phase 1–5/derivative paths. The Phase 7 package contains contracts, parsers,
adapters, persistence and deterministic helpers, but no live collector task,
cursor loop, bounded backfill loop, Spot task, or Phase 7 runtime entrypoint.

Therefore the following gates were not claimed or started:

- live Bitcoin/Ethereum Chain ingestion and cursor persistence;
- real Spot trade ingestion and Spot window progression;
- BTC/ETH semantic runtime samples;
- USD valuation runtime samples;
- finality, cursor restart, bounded backfill and live gap runtime gates;
- Spot/perpetual isolation in the live runtime;
- source outage, PostgreSQL outage and controlled restart recovery;
- final 30+ minute stability window with Phase 7 sources.

The previously completed minimal RPC evidence remains valid: Bitcoin mainnet
read-only JSON-RPC and Ethereum `chainId=0x1` passed, and Ethereum advanced
from block `26038526` to `26038527` during the short probe. It is not a
substitute for integrated Phase 7 runtime evidence. The previous secret audit
reported `SECRET_LEAK_FOUND=false`.

### Database gate observed in this attempt

- Database: `quant`
- PostgreSQL: `16.15`
- Timezone: `UTC`
- Migration `012_phase7_onchain_spot_context.sql`: present
- No migration files were modified.
- Fresh isolated database, second migration run, and final runtime
  idempotency were not claimed because the Phase 7 runtime wiring blocker was
  found before starting the acceptance window.

### Current result

`PHASE7_LOCAL_RUNTIME_NOT_ACCEPTED`

Blockers:

1. `PHASE7_RUNTIME_NOT_INTEGRATED`: no live Phase 7 Chain/Spot ingestion
   orchestration is wired into collector/engine.
2. `RUNTIME_IMAGE_NOT_DEPLOYED`: the running containers predate the current
   configuration/runtime code; a current-HEAD candidate image exists but was
   not deployed into an acceptance window.

No code was changed during this acceptance attempt, no live provider was
reconfigured, no database data was modified, no ECS was contacted, and no
Phase 8 work was started.

## PHASE 7 RUNTIME INTEGRATION + LOCAL ACCEPTANCE — 2026-09-24 RETEST

### Baseline

- Project: `/home/lucas045057/projects/quant`
- Branch at start: `phase7`
- Starting HEAD: `7d759ad2fa7f8ea69f46c19eab935351bf6bb2b2`
- Working tree at start: clean.
- `TRADING_MODE=paper`; Phase 7 runtime enabled only in the isolated test
  Compose project. No production PostgreSQL volume or ECS was accessed.
- Formal caps were unchanged: PostgreSQL 768 MiB, Collector 256 MiB, Engine
  384 MiB.

### Runtime Wiring Map

| Path | State | Evidence / remaining gap |
|---|---|---|
| Collector entrypoint → `Phase7CollectorRuntime` | WIRED | Owned startup task, per-source supervisors, failure isolation, health updates, cancellation and shutdown are present. |
| Bitcoin/Ethereum RPC → parser → transfer/cursor repository | PARTIALLY_WIRED | Bounded finalized-block processing and atomic event/checkpoint persistence are implemented, but the configured endpoints fail on required block methods before runtime cursor/event persistence. |
| Binance REST / public WebSocket → spot parser → spot windows | PARTIALLY_WIRED | Runtime tasks and reconnect/resubscribe path exist; integrated source requests/errors prevented persisted spot windows during this run. |
| Engine entrypoint → Phase 7 engine lifecycle | WIRED | Context-only heartbeat, retention lifecycle, and additive Stage1 context savepoint hook; Stage1 eligibility and trading behavior are unchanged. |
| Address-label snapshot ingestion | NOT_WIRED | No operator-reviewed versioned snapshot was configured or loaded. No labels were invented. |
| On-chain generic flow / exchange-flow windows | NOT_WIRED | Runtime does not yet invoke the designed label-classification and generic-flow aggregation/persistence path. |
| Whale windows | NOT_WIRED | Runtime does not yet invoke `evaluate_whale_transfer` / `aggregate_whale_window` or persist their results. |
| Stablecoin windows | NOT_WIRED | Runtime does not yet invoke the stablecoin classification/aggregation persistence path. Bridge correlation remains intentionally unavailable under the approved V1 design. |
| Health / source lifecycle | PARTIALLY_WIRED | RPC and Spot health are updated from real source-cycle results; no cursor progression was available, and semantic pipeline health is not independently tracked. |

The existing helper contracts and deterministic tests are not themselves
evidence of production runtime integration. In particular, the current
Collector worker persists canonical transfer batches/checkpoints and Spot
windows, but it does not yet invoke the existing address-label, generic flow,
whale, and stablecoin semantic writers. This remains a runtime integration
blocker even though the entrypoints now own Phase 7 lifecycle tasks.

### Source Registry

- `BITCOIN_RPC_CONFIGURED=true`; `ETHEREUM_RPC_CONFIGURED=true` (values and
  endpoint URLs intentionally omitted).
- Bitcoin, Ethereum, Binance Spot REST, and Binance Spot WebSocket source
  workers are registered by the Collector. Disabled chain sources are kept
  distinct from configured sources that return errors.
- No reviewed address-label snapshot or separate approved label source was
  configured. Unknown addresses therefore remain unknown; no exchange or
  whale labels were inferred.
- AI provider remained `NOT_CONFIGURED`; this did not prevent Phase 1–6
  runtime startup. No real AI provider was contacted.

### Bitcoin Runtime

- Direct read-only probes identified Bitcoin mainnet (`chain=main`) and
  `getblockchaininfo` / `getblockhash` worked.
- Runtime block retrieval through `getblock` failed with a sanitized
  `Phase7RpcError: invalid JSON-RPC response`.
- Result: no runtime block/event persistence, no persistent cursor advance,
  and no runtime finality evidence. No wallet or transaction-broadcast RPC was
  called.

### Ethereum Runtime

- Direct read-only probe returned `eth_chainId=0x1` (Ethereum mainnet).
- Read-only head samples advanced from block `0x18d762b` to `0x18d762e` in
  32 seconds (`+3` blocks); this establishes provider head progression only,
  not Collector cursor progression.
- Runtime `eth_getBlockByNumber` for latest/finalized returned invalid
  non-JSON content despite HTTP 200 in one probe and timed out in another.
  Runtime did not persist Ethereum blocks, native ETH transfers, ERC-20
  Transfer logs, receipts, or cursor state.
- No transaction signing or send/broadcast RPC was called.

### Cursor / Backfill

- Runtime code uses persistent source/chain checkpoints, bounded block ranges,
  bounded batch sizes, and checkpoint/event persistence in one repository
  transaction. Restart loads the checkpoint rather than starting from genesis.
- Live acceptance evidence: **not established**. The RPC block-method failures
  occurred before any chain checkpoint could be committed; all Phase 7
  ingestion checkpoint rows remained `0` in the measured run.
- No repeated full-history backfill was observed or claimed.

### Finality / Reorg

- Deterministic parser/recovery tests remain separate from live acceptance.
- The runtime code selects finalized heights and checks a persisted block hash
  before reprocessing a changed checkpoint block. No live block was available
  to validate this path or its persistence semantics in this run.
- Reorg recovery, invalidation/replacement, and recomputation were not claimed
  as runtime-verified.

### Event-time Valuation

- Runtime event pricing is joined by event time through the configured local
  market data repository; no latest/current price fallback is used.
- No real on-chain event was persisted, so event-time valuation and
  `price_timestamp <= event_timestamp` were not validated against live rows.
- Missing prices were not replaced with zero or future prices.

### Labels / Whale

- No operator-reviewed address-label snapshot was available; address-label
  rows remained `0` and coverage is unavailable, not assumed complete.
- Whale threshold helper contracts are versioned/config-driven, but the live
  Collector does not yet call the whale evaluator/window aggregator or persist
  whale windows. Whale-window rows remained `0`.
- No live whale event was claimed or synthesized.

### Exchange Flow

- No reviewed label snapshot means exchange attribution is unavailable.
- No addresses were classified as exchange, external, protocol, treasury, or
  bridge from size or heuristics.
- The runtime does not yet call the exchange-flow classifier or persist
  `phase7_onchain_flow_windows`; those rows remained `0`.

### Stablecoin / Bridge

- Runtime queried allowlisted Ethereum token logs as part of the intended
  chain path, but Ethereum block/log retrieval failed before event processing.
- Stablecoin classification/window persistence is not yet wired into the
  Collector. Stablecoin context rows remained `0`.
- Bridge correlation is not proven by the approved V1 contract; bridge legs
  were not fabricated or netted, and no bridge context was claimed.

### Spot Flow

- The isolated runtime attempted Binance Spot REST and public WebSocket
  ingestion. Integrated calls produced timeout, sanitized RPC/source errors,
  and JSON decode failures; the direct one-off read-only REST and WebSocket
  probes did return valid public data, but do not prove integrated persistence.
- No Spot flow windows or Spot cursor rows were persisted (`0` each). Spot and
  perpetual tables remain separate; no Spot data was added to Phase 3/4
  perpetual flow tables.
- Directional CVD/window progression was therefore not accepted as live
  evidence.

### Persistence

- A fresh, project-scoped disposable PostgreSQL 16 Alpine database was used;
  it had no host port mapping. Migrations applied from empty state, then the
  migration runner returned `0 new migrations` on the repeat run.
- Database name was `quant`, timezone was UTC, and the test used a dedicated
  disposable volume. The actual Phase 1–6 production volume was not mounted.
- At the measured sample (`2026-09-24T14:56:54Z`), database size was
  `90,029,079` bytes. Counts: migrations `14`, symbols `805`, market snapshots
  `2,610`, klines `79,144`, screening results `200`, Phase 7 Stage1 context
  enrichments `200`, runtime health events `10`.
- Phase 7 counts at that sample: asset registry `0`, address labels `0`,
  on-chain transfers `0`, on-chain flow windows `0`, whale windows `0`, Spot
  windows `0`, stablecoin context `0`, ingestion checkpoints `0`.
- All `200` Phase 7 Stage1 context enrichments were
  `NOT_AVAILABLE / PHASE7_CONTEXT_NOT_AVAILABLE`; Phase 7 remained
  context-only and did not alter eligibility.

### Health Semantics

- Real runtime source failures were represented as degraded/error states:
  Bitcoin RPC `ERROR`, Ethereum RPC `ERROR`, Binance Spot REST `ERROR`, and
  Binance Spot WebSocket `ERROR` in the measured sample.
- Phase 6 AI/provider absence remained `NOT_CONFIGURED` / unavailable and did
  not stop the other lifecycle from starting.
- Health rows/events were produced, but there was no cursor advancement to
  support an `AVAILABLE` chain-ingestion claim.

### Failure Injection

- Existing deterministic failure/recovery tests cover bounded retries,
  sanitized invalid responses, source supervision, Spot WebSocket reconnect,
  and lifecycle cancellation. Full disposable-PostgreSQL outage/recovery
  injection with live source progression was **not run** because providers
  never reached the persistence path.
- No failure was injected against a real provider.

### Real Read-only RPC Evidence

- Bitcoin: mainnet identity and height/hash probes passed; required `getblock`
  response failed JSON-RPC parsing. Runtime progression `NOT_AVAILABLE`.
- Ethereum: mainnet identity passed and head advanced `+3` in 32 seconds;
  required latest/finalized block data failed parse or timed out. Runtime
  progression `NOT_AVAILABLE`.
- Binance: isolated direct public REST and WebSocket probes each succeeded;
  the integrated Collector path remained unstable and wrote no Spot windows.
- Proxy environment in the app containers: `PROXY_ENV_SET=false`.
- No credential-bearing endpoint, RPC token, response body, or auth header is
  included in this report.

### Deterministic Replay

- Full regression includes deterministic Phase 7 contracts and replay-related
  tests. The Phase 6 Deterministic Replay V2 runner was **not run** for this
  attempt: the required live source/cursor progression and semantic runtime
  output were absent, so a replay could not demonstrate the requested same
  candidate-image/live-ingestion non-regression proof.
- No Phase 6 contract, frozen Stage1 eligibility rule, or historical migration
  `001–012` was changed.

### Startup / Shutdown

- Candidate v2 runtime was started with `paper` mode, isolated Compose project,
  no host ports, and the formal memory caps. Its prior stop ended Collector and
  Engine with exit `137`, `OOMKilled=false`, under the existing 10-second
  grace. A regression test reproduced slow pending WebSocket startup and was
  RED before the cancellation fix.
- The Collector now races long startup awaits against the stop event and
  cancels pending startup work. The focused test is GREEN.
- A v3 diagnostic image (`sha256:41d22ee16af5447729709d8ba9f75009e4a2bf7d6b46cd27a3441cbcf9b60b71`)
  was built solely to verify this shutdown fix. Collector and Engine each
  exited `0`, `OOMKilled=false`, in about `11.4` seconds under the same
  10-second stop grace; PostgreSQL stopped cleanly. This is a one-off shutdown
  diagnostic, not a full acceptance candidate.
- The local Compose v1.29.2 tool encountered a `ContainerConfig` recreation
  incompatibility when the base local Compose file was mistakenly combined
  with the acceptance overlay. Existing production containers were not
  changed. A newly created, unattached empty volume from that failed attempt
  was verified unused and removed; the actual acceptance database volume was
  retained.

### Restart Cycles

- Required three progression/recovery restart cycles: **NOT RUN**. No source
  cursor or Phase 7 event existed to test recovery/dedup after restart.
- One brief post-fix v3 startup/stop diagnostic passed graceful shutdown but
  does not count as a restart-cycle acceptance.

### 60 Minute Runtime

- Required `>=60 minute` integrated runtime: **NOT RUN**. The v2 diagnostic
  ran for approximately 9 minutes and was stopped after repeated source errors
  and zero Phase 7 cursor/event progression. No 60-minute acceptance is
  claimed.
- Per-5-minute cursor lag, pending-task, queue-depth, and source-health
  acceptance series were not established.

### Resource Usage

Measured short v2 sample at `2026-09-24T14:56:54Z` (not a sustained-capacity
or long-run leak test):

| Container | Current | Peak | Limit | OOM / max events | PSI |
|---|---:|---:|---:|---|---|
| Collector | 231,698,432 B | 266,641,408 B | 268,435,456 B (256 MiB) | none | 0 |
| Engine | 122,974,208 B | 180,752,384 B | 402,653,184 B (384 MiB) | none | 0 |
| PostgreSQL | 348,512,256 B | 351,825,920 B | 805,306,368 B (768 MiB) | none | 0 |

- Collector peak was about `99.3%` of its cap once; no `memory.events` max,
  OOM/OOM-kill, or PSI pressure was observed in the sample. Sustained
  `>=95%` use was not measured and resource acceptance is therefore pending.
- Container CPU sample: PostgreSQL `0.10%`, Collector `0.05%`, Engine
  `0.05%`; aggregate host CPU was not captured. The WSL host has about 31 GiB
  available and cannot qualify the target low-memory ECS resource profile.
- Log sizes at the sample: Collector `2,989` B, Engine `263` B, PostgreSQL
  `4,079` B. No long-horizon log or database growth estimate is inferred.

### Database Validation

- Empty-database migrations and the second idempotency run passed on the
  disposable database. No migration file was modified.
- Canonical chain/Spot event tables remained empty because upstream parsing or
  integrated source calls failed. Runtime row counts above are measured, not
  inferred from fixtures.
- No future-price leakage was found in live data because no live on-chain
  valuation row existed; this gate remains unverified, not passed.
- Restart deduplication against real persisted chain/Spot events remains
  unverified.

### Regression

- Latest full local suite after the shutdown cancellation fix:
  `852 passed, 14 skipped, 0 failed`.
- Skips: 8 opt-in live exchange contract probes and 6 PostgreSQL-DSN-gated
  pytest tests. The dedicated fresh disposable-PostgreSQL migration/runtime
  trial was performed separately and did not silently skip.
- Focused Collector shutdown + Phase 7 runtime integration suite:
  `20 passed`.
- `git diff --check` and Python compile check passed after the runtime changes.

### Secret Audit

- Added diff lines, new artifacts, the report, Collector/Engine logs, and
  `system_health.details` were checked against configured RPC URLs/tokens and
  the full local PostgreSQL DSN. No credential-bearing URL or RPC/token value
  was found in those locations; auth-pattern scan was also clear:
  `SECRET_LEAK_FOUND=false` for this change set and its runtime outputs.
- `.env.local` remains ignored and untracked; its contents were not printed or
  added. Container environment remained paper-only and contained no trading
  API credentials. Proxy variables were absent.
- A broad URL-pattern scan had a false positive from public non-credential
  exchange endpoint constants; a follow-up value-aware scan distinguished
  those public constants from credential-bearing RPC configuration. A
  whole-file scan also matched the pre-existing local PostgreSQL DSN default
  in tracked baseline configuration; that default was outside this turn's
  added lines and is not copied into this report. It remains a separate
  credential-hygiene item for a future scoped change.

### Remaining Risks

1. `getblock` and Ethereum latest/finalized block responses from the currently
   configured provider(s) do not satisfy the runtime's expected JSON-RPC
   response contract. No endpoint value is disclosed. Resolve/validate the
   approved provider capability before retrying live chain progression.
2. Address-label snapshot ingestion and the generic on-chain flow, exchange
   flow, whale, and stablecoin aggregation/persistence loops are not wired
   into the formal runtime. This is an implementation gap, not merely missing
   live evidence.
3. Integrated Binance REST/WS ingestion is unstable despite one-off public
   probes passing; no cursor/window persisted.
4. Three restart cycles, PostgreSQL outage/recovery injection, event-time
   valuation sampling, 60-minute runtime, sustained resource acceptance, and
   Phase 6 Replay V2 non-regression remain unverified.
5. Phase 7 local runtime acceptance must not be inferred from passing unit
   tests, source identity probes, or empty-table migrations.

### Current Result

`PHASE7_RUNTIME_NOT_INTEGRATED`

Independent blocker:

`PHASE7_RPC_PROVIDER_RESPONSE_BLOCKED`

The Collector/Engine now own Phase 7 lifecycle tasks, but the required
semantic aggregation/persistence paths are not all connected, and configured
live RPC block methods do not return usable payloads. Acceptance gates 1, 3,
4, 6–18, 20, 23, and 24 are not all evidenced. Do not report
`PHASE7_LOCAL_RUNTIME_ACCEPTED`.

No Phase 8/9, Jev, live AI, ECS, production PostgreSQL, blockchain transaction,
or live trading operation was performed. Stop here pending explicit follow-up.

### Git

- Branch: `phase7`
- HEAD: `7d759ad2fa7f8ea69f46c19eab935351bf6bb2b2`
- Working tree: **not clean**; runtime integration, tests, Compose acceptance
  configuration, and this report remain uncommitted.
- No completion commit was created because the runtime and acceptance gates
  above remain blocked. The changes are preserved for the next scoped step.

## PHASE 7 SOURCE RESPONSE CONTRACT REMEDIATION + FINAL RUNTIME WIRING — 2026-09-25

### Source Response Contract Audit

The diagnostic probe is read-only and reports status/shape metadata only. It
does not emit configured RPC endpoints, response bodies, authentication data,
or provider error text.

#### Bitcoin RPC

- Adapter contract: Bitcoin Core JSON-RPC 1.0 request/response envelope.
- Request path: configured HTTP(S) RPC endpoint; no endpoint value recorded.
- Authentication: configured mode `NONE`; rate limit 1 request/second,
  concurrency 1, timeout 8 seconds.
- `getblockchaininfo`: HTTP 200, `application/json`, parsed object; chain
  `main`, latest height 968421.
- `getblockcount`: HTTP 200, `application/json`, parsed integer 968421.
- `getblockhash`: HTTP 200, parsed 64-character hash string.
- Required `getblock` payload: three bounded attempts across verbosity 0, 1,
  and 2 each returned HTTP 200 with `application/json` metadata but a body
  classified by the JSON-RPC parser as `PLAIN_TEXT_ERROR`. No response body
  was retained in this report. The required block/transaction contract is
  therefore **incompatible with the configured provider**; the parser was
  not weakened and no alternate endpoint/provider was selected.

#### Ethereum RPC

- Adapter contract: Ethereum JSON-RPC 2.0 request/response envelope.
- Request path: configured HTTP(S) RPC endpoint; no endpoint value recorded.
- Authentication: configured mode `NONE`; rate limit 1 request/second,
  concurrency 1, timeout 8 seconds.
- `eth_chainId`: HTTP 200, parsed `0x1` (Ethereum mainnet).
- `eth_blockNumber`: HTTP 200, parsed height 26048482.
- Latest `eth_getBlockByNumber`: HTTP 200, parsed block object with number,
  hash and 123 transaction entries; fixed historical block `0x1` also parsed.
  Latest-block parsing succeeded in this retest; an earlier probe in this
  audit sequence had an unparseable latest-block response, so provider
  stability has not been certified.
- `eth_getLogs` and `eth_getTransactionReceipt` remain adapter/runtime
  requests with unit contract coverage; no sustained live ingestion was run.

#### Spot Source

- Binance public REST `GET /api/v3/aggTrades` for the fixed BTCUSDT/ETHUSDT
  allowlist: HTTP 200, `application/json`, one current response parsed into a
  canonical trade. No key or other authentication was used.
- Public WebSocket `aggTrade` subscription: HTTP Upgrade, subscription ACK,
  and a real trade message all passed. Parser fields include symbol, aggregate
  trade id, price, quantity, first/last trade ids, event time, and maker-side
  flag; side is derived from the maker-side flag.
- This validates the Spot wire contract only; it is not evidence of a
  continuously running Collector or database ingestion.

### Missing Runtime Wiring Remediation

- **Collector:** the existing Phase 7 Collector lifecycle owns the bounded
  Bitcoin, Ethereum and Binance Spot supervisors; Spot REST backfill and
  WebSocket recovery feed the canonical trade/window path. Runtime tests cover
  startup, persistence handoff, exception isolation and shutdown.
- **Engine:** `Phase7EngineRuntime` owns the periodic context cycle, health
  updates, migration entry, bounded retention and graceful cancellation.
- **Address labels:** optional reviewed snapshots are hash-validated. Blank
  configuration leaves labels `NOT_AVAILABLE`; no address category is
  inferred. Snapshot rows are upserted once per content hash per process.
- **Generic chain flow:** recomputes closed event-time windows from persisted
  canonical transfer events and reviewed labels. Unknown endpoints remain
  unknown and do not become exchange flow.
- **Whale context:** requires an explicit versioned threshold configuration
  and event-time USD valuation. Missing thresholds/valuation remain unavailable
  or unevaluable; no threshold is guessed.
- **Exchange flow:** derived only from reviewed address labels and persisted
  transfers; it is not sourced from an invented venue classification.
- **Stablecoin context:** limited to the configured USDT/USDC asset registry.
  Zero-value token transfers are preserved. Without reviewed issuer addresses,
  mint/burn attribution remains unknown.
- **Bridge:** a reviewed bridge endpoint is excluded from ordinary flow and
  represented as `BRIDGE_TRANSFER`; bridge-leg correlation/volume is not
  implemented and is not fabricated.
- **Configuration delivery:** bounded interval/lookback/batch settings are
  passed to the correct local containers. The optional reviewed JSON directory
  is mounted read-only into Collector and Engine; absent config remains
  `NOT_AVAILABLE`. No additional service was added.

### Persistence

- **Cursors:** no live Phase 7 checkpoint/cursor was written in this attempt.
  Existing source cursor persistence remains in the Collector path, and its
  failure/replay behavior has unit coverage.
- **Chain events:** Migration 012 tables/repository contracts are used by the
  implementation. No migration was applied to a database and no real chain
  event row count is claimed here.
- **Context windows:** the Engine now scans the bounded four-hour event-time
  lookback using the `(asset_id, event_time)` index, selects only fully closed
  windows for 1m/5m/15m/1H/4H, and uses the newest source `processed_at` as a
  watermark. Late-arriving or changed events cause an idempotent window
  recomputation. Work is capped at 16 windows per cycle and 10,000 events per
  window; config changes invalidate the in-memory replay watermark.
- **Spot windows:** deterministic tests exercise Collector REST-to-canonical
  trade processing and persistence handoff. No live Spot window or CVD row was
  written to PostgreSQL in this attempt.
- **Idempotency:** database upserts remain keyed by their canonical window
  identity; runtime restart replay is bounded by the configured lookback.

### Short Real Progression

- Ethereum observations: prior sampled height 26048421; current sampled height
  26048482 (`+61`). The earlier sample's exact request-start timestamp was not
  retained, so no precise elapsed duration is claimed. Current response shape
  passes, but the progression timing gate is not recorded as a formal pass.
- Bitcoin height advanced from the prior sample, but required `getblock`
  responses remain unparseable. A height alone is not sufficient to pass the
  Bitcoin source gate.
- **No integrated Collector/Engine progression was started.** The Bitcoin
  block payload contract is a required input and currently fails. No 10/15/60
  minute acceptance run was attempted.

### Resource Observation

- Docker containers were not started; no PostgreSQL connection was opened.
- RAM/CPU, database size/row growth, log growth and restart behavior therefore
  have no runtime measurements for this attempt. No estimate is presented as
  a measurement.
- Local Compose YAML parsed successfully and standalone `docker-compose ...
  config -q` passed. The newer `docker compose` plugin, `ruff`, and a live
  Docker Runtime Acceptance environment are unavailable in this WSL session.

### Shutdown

- Unit tests verify owned Engine heartbeat/context task cancellation and
  `STOPPED` health state. Since no live runtime was started, no process-level
  restart/recovery result is claimed.

### Regression

- Focused Phase 7/config/safety tests: **111 passed**.
- Full repository regression: **877 passed, 14 skipped, 0 failed**.
- The 14 skips are the existing opt-in Phase 2/3/4 live contract probes and
  PostgreSQL integration tests requiring `TEST_POSTGRES_DSN` or an explicit
  runtime-acceptance gate.
- `compileall`, YAML syntax parsing and `git diff --check` passed.

### Secret Audit

- RPC diagnostics expose only method identifiers, HTTP/content-type/size,
  parser classification and selected non-secret result metadata. RPC endpoints,
  response bodies, tokens, credentials and authorization headers were not
  emitted or recorded in this section.
- No Collector/Engine runtime logs or health rows were generated because the
  runtime was not started. `.env.local` is absent from Git status and remains
  ignored.
- `SECRET_LEAK_FOUND=false` for probe/test/report output produced in this
  attempt.

### Current Result

- Runtime wiring and deterministic aggregation/persistence paths are present
  and regression-tested, but live acceptance is blocked before startup by the
  configured Bitcoin provider's required `getblock` response contract.
- No provider replacement, automatic fallback, Phase 8/9 work, real AI,
  production database, ECS, or trading operation was performed.
- Required next action: manually provide/approve a Bitcoin RPC provider that
  returns the documented Bitcoin Core 1.0 JSON response for `getblock`, then
  rerun the bounded source contract and progression gate. Do not weaken the
  parser or add a hidden fallback.

**PHASE7_RPC_PROVIDER_REPLACEMENT_REQUIRED**

## PHASE 7 REAL SOURCE + SHORT RUNTIME INTEGRATION ACCEPTANCE ATTEMPT

### Checkpoint and scope

- Branch: `phase7`
- HEAD: `764e973882790385451c72cbeeeb27b15207b0c6` (unchanged)
- Worktree was clean at the start of this attempt.
- `.env.local` is ignored by Git. Its contents, credentials, authenticated
  headers, and private RPC address are intentionally omitted.
- No source code, runtime configuration, resource limit, or secret value was
  changed. No ECS, AI provider, trading endpoint, or wallet operation was used.

### Bitcoin official adapter and parser

- The formal Phase 7 transport used the configured API-key-header auth mode;
  the actual key and endpoint are not recorded.
- Production transport defaults to an 8 MiB response cap, 8 second timeout,
  1 request/second, and concurrency 1. Retry count is bounded at 3.
- `getblockchaininfo`, `getblockcount`, `getblockhash(latest)`, and `getblock`
  verbosity 0 and 1 returned valid JSON-RPC responses in the default-cap
  probe. Mainnet was identified and the latest height/hash were valid.
- `getblock` verbosity 2 did not pass through the production default
  transport. A repeated attempt failed as a bounded response/retry failure;
  the probe recorded no successful JSON-RPC envelope for that request.
- A separate diagnostic invocation with a one-off 32 MiB cap received a
  9,823,174-byte verbosity-2 response and the production Bitcoin parser's
  required checks all passed: hash, height, transaction identity, exact
  satoshi conversion, and provenance. This override is diagnostic only and
  is not the production runtime configuration.
- Result: `BITCOIN_OFFICIAL_ADAPTER_CONTRACT_PASS=false`;
  parser sample under the temporary diagnostic cap passed, but the required
  end-to-end production contract remains blocked by the default transport.

### Bitcoin progression

- T0: `2026-09-24T18:15:39.671267Z`, monotonic 149902.481 s,
  height 968433.
- T1: `2026-09-24T18:25:41.073561Z`, monotonic 150503.883 s,
  height 968433; duration 601.4 s.
- No new block was observed during the bounded 10-minute window; height did
  not decrease. A later independent probe observed height 968434.
- No backfill was invoked. Code-level bounds remain: 8 second request timeout,
  1 request/second, concurrency 1, at most 3 transport attempts, and at most
  12 blocks in one Bitcoin recovery cycle.

### Ethereum mainnet and parser sample

- A low-rate formal-client progression sampled `eth_chainId=0x1` at both ends.
- T0: `2026-09-24T18:30:02.673405Z`, height 26049109.
- T1: `2026-09-24T18:31:04.065447Z`, height 26049114; elapsed 61.4 s;
  height was nondecreasing and advanced by 5.
- Latest/finalized block metadata had valid response shapes in the bounded
  source probe. A formal runtime-helper parser sample was attempted, but the
  provider exhausted its bounded retry budget after HTTP 429. No parser PASS
  is claimed for this sample, and no further high-rate retries were made.

### Binance Spot real-data probe

- Public REST: HTTP 200 and official adapter parsed real BTCUSDT aggregate
  trades. Public WebSocket: upgrade, subscription acknowledgement, and a real
  trade message passed the formal adapter/parser.
- A separate event-time aggregation probe requested the prior closed 1-minute
  interval and processed 391 real REST trades through
  `BinanceSpotAdapter` and `aggregate_spot_window`: 391 trades in-window,
  `AVAILABLE`, coverage 1.0000. No fixture data or persistence write was used.

### Docker, database, and integrated runtime

- Docker Engine client/server: 29.1.3 / 29.1.3. The Docker Compose v2 plugin
  was unavailable; legacy `docker-compose` 1.29.2 is installed.
- The acceptance Compose file retains its configured caps: PostgreSQL 768 MiB,
  Collector 256 MiB, Engine 384 MiB. No cap was raised.
- Read-only Compose validation could not resolve the required
  `PHASE7_ACCEPTANCE_IMAGE` setting. More importantly, the mandatory Bitcoin
  production transport preflight failed, so no acceptance PostgreSQL,
  Collector, or Engine was started. Existing containers and volumes were left
  untouched.
- Fresh/repeated migrations, runtime persistence/cursors, health rows,
  memory/queue measurements, clean stop, restart/cursor recovery, dedup, and
  post-restart runtime were therefore **NOT RUN**. No database or runtime
  measurements are claimed.

### Regression and security

- Bitcoin/parser and related source-focused tests: **96 passed**.
- Phase 7 focused suite: **217 passed**.
- Phase 1 lifecycle plus Phase 6 runtime non-regression: **27 passed, 1
  skipped** (`TEST_POSTGRES_DSN` not configured).
- Full regression: **890 passed, 14 skipped, 0 failed**. Skips: 3 Phase 2,
  2 Phase 3, and 3 Phase 4 opt-in public live probes; 1 each for Phase 3
  persistence, Phase 4 persistence, Phase 6 persistence, Phase 6 runtime
  integration, and repository integration (all PostgreSQL cases require
  `TEST_POSTGRES_DSN`).
- `.env.local` remains ignored. No credential, authenticated header, private
  endpoint, or resolved Compose configuration was emitted into this report or
  command output. No runtime logs were created by this attempt.
- `SECRET_LEAK_FOUND=false` for artifacts generated in this attempt. A
  dedicated gitleaks/trufflehog executable was not installed; no claim is made
  that pre-existing unrelated container logs were scanned.

### Result and blockers

`PHASE7_RUNTIME_INTEGRATION_NOT_ACCEPTED`

Blockers before runtime acceptance can proceed:

1. `BITCOIN_VERBOSITY_2_EXCEEDS_PRODUCTION_RESPONSE_CAP`: the required real
   block response cannot be parsed through the unchanged 8 MiB production
   transport. The temporary 32 MiB probe override is not valid runtime
   acceptance evidence.
2. `ETHEREUM_RUNTIME_PARSER_SAMPLE_RATE_LIMITED`: the formal parser-path probe
   encountered HTTP 429 after bounded retries; parser-path evidence is absent.
3. `PHASE7_ACCEPTANCE_IMAGE_UNSET`: Compose validation requires an explicit
   local acceptance image built from the immutable checkpoint.

No Phase 7 local runtime acceptance is claimed. Required next step is an
explicit design/implementation decision for the Bitcoin response contract;
this attempt did not change code or resource caps.

## Bitcoin Provider Replacement — Corrected Bounded-Stream Preflight (2026-09-25)

### Preflight Context

- Checkpoint commit preserving the previously reviewed runtime wiring and
  bounded backfill: `8995dd37190476d11699699d1f41ac4223c9b1b9`.
- Secret-safe configuration check: `BITCOIN_RPC_CONFIGURED=true`. The existing
  local configuration was already present before the earlier provider probe;
  this audit cannot independently confirm that its provider identity changed.
  The endpoint and any credential-bearing components are intentionally omitted.
- The production transport and the Spot REST path previously used a single
  bounded `read()` call. With compressed responses, that may return fewer
  decompressed bytes than the complete body. A bounded streaming reader now
  consumes the full decompressed stream up to the configured cap. A regression
  test covers complete multi-chunk reads and cap enforcement. This corrects the
  diagnostic method; it does not relax the provider response contract.
- Consequently, the earlier classification of all `getblock` verbosity
  responses as malformed was incomplete: verbosity 1 now validates, while 0
  and 2 still fail after complete-body reads.

### JSON-RPC Contract Matrix

The following is the latest bounded live probe through the formal transport.
Provider response bodies, request URLs, hashes and credentials are not
recorded.

| Method | HTTP / media type | JSON parse / envelope | Result shape | Latency / outcome |
|---|---|---|---|---|
| `getblockchaininfo` | 200 / `application/json` | valid / valid | object; mainnet; height 968430 | 1220.7 ms; PASS |
| `getblockcount` | 200 / `application/json` | valid / valid | integer; height 968430 | 721.1 ms; PASS |
| `getblockhash(latest)` | 200 / `application/json` | valid / valid | 64-character hash string | 697.3 ms total; PASS |
| `getblock(hash, 0)` | 3 HTTP 200 / `application/json` | invalid JSON / invalid envelope | unavailable | 8090.3, 8495.9, 8501.4 ms; all FAIL; bodies 2,846,971 / 1,037,982 / 2,800,224 bytes |
| `getblock(hash, 1)` | 200 / `application/json` | valid / valid | object; 6,204 transactions | 2062.3 ms; PASS |
| `getblock(hash, 2)` | 2 HTTP 200 / `application/json`; 1 attempt had no HTTP response metadata | invalid JSON for both HTTP responses / invalid envelope | unavailable | 8948.1, no response metadata, 8989.2 ms; FAIL; HTTP bodies 3,687,873 / 2,352,517 bytes |

For failed `getblock` calls the sanitized adapter reports `Phase7RpcError`;
provider error code/message are unavailable because no valid JSON-RPC error
envelope was parsed. No raw body was emitted. The transport accepted only
bounded response sizes and did not add a parser workaround.

### getblock Verbosity 0

- Required contract: valid JSON-RPC envelope whose result is a block hex
  string.
- Observed: all three HTTP responses were labeled JSON by content type, but the
  complete bodies did not parse as JSON. No envelope or result type was
  available. Contract: FAIL.

### getblock Verbosity 1

- Required contract: valid JSON-RPC envelope with a block object.
- Observed: valid JSON and envelope; object result with 6,204 transactions.
  Contract: PASS for this sample.

### getblock Verbosity 2

- Required contract: valid JSON-RPC envelope with a block object and
  transaction structures supported by the current parser.
- Observed: two complete HTTP response bodies failed JSON parsing; a third
  attempt yielded no HTTP metadata. No envelope or result type was available.
  Contract: FAIL.

### Parser Compatibility

- Parser-through-adapter validation was not run because the required provider
  contract gate (verbosity 0/1/2) did not pass. The parser was not bypassed or
  changed to accept non-contract responses. Bitcoin progression, cursor checks,
  and Bitcoin persistence/restart probes remain gated and NOT RUN.

### Provider Classification

- `BITCOIN_RPC_PROVIDER_STATUS=PROVIDER_INCOMPATIBLE` for the currently
  configured provider: required verbosity 0 and 2 contracts fail despite the
  corrected complete-body reader; verbosity 1 alone is insufficient.
- No automatic fallback, alternate endpoint, proxy, or parser relaxation was
  introduced.

## Formal Source Progression

### Bitcoin

- The finite source-contract probe observed mainnet height 968430 and the
  latest block hash request succeeded. This is not a progression gate.
- The required `getblock` contract failed, so the conditional 10–15 minute
  Bitcoin height/hash/cursor progression test was correctly NOT RUN. No
  progression or cursor result is claimed.

### Ethereum

- Formal T0: UTC `2026-09-24T17:05:47.124095Z`, monotonic 145709.934 s,
  `eth_chainId=0x1`, block 26048691.
- Formal T1: UTC `2026-09-24T17:06:38.444423Z`, monotonic 145761.254 s,
  `eth_chainId=0x1`, block 26048695.
- Elapsed 51.32 s; block height advanced by 4. `BLOCK_PROGRESSION=true`.
- The later bounded preflight also parsed `eth_chainId=0x1`, a latest block
  object, and a historical block object. This remains source-probe evidence,
  not database/runtime evidence.

### Binance Spot

- A prior short direct-only sample through the formal Spot adapter validated
  REST discovery, WebSocket upgrade/ack, real aggregate-trade events, REST
  parsing, aggregation and a closed Spot window: 255 WebSocket trades were
  observed in about 25 seconds; a later REST sample parsed 931 trades into an
  `AVAILABLE` window. No database write was made.
- In the latest repeat probe, WebSocket upgrade, subscription acknowledgement
  and one real trade passed; the REST request timed out before a response.
  Thus Spot parsing/aggregation has a passing sample but REST availability was
  intermittent across these probes; continuous source progression is not
  certified.
- Spot remains strictly distinct from perpetual derivative flow. No CVD or
  runtime persistence row was written in this preflight.

## Short Integrated Runtime

### Cursor

- NOT RUN: blocked by the required Bitcoin provider contract gate.

### Persistence

- NOT RUN: no disposable PostgreSQL, Collector or Engine was started; no cursor,
  canonical event, Spot window, provenance, or health rows were written.

### Restart Probe

- NOT RUN: no runtime process was started, so no cursor restart/recovery or
  duplicate-event result is claimed.

### Resource Observation

- NOT RUN: no containers were started; there are no new memory, queue-depth,
  CPU, database-growth or log-growth measurements.

### Shutdown

- NOT RUN at process level. No disposable runtime was started or left running.

### Security and Scope

- Probe output and this report contain no RPC URL, token, authorization header
  or provider response body. `.env.local` remains gitignored and is not staged.
- No Phase 8/9, AI provider, ECS, live trading, production database, or
  persistent runtime operation was performed.

### Regression

- Phase 7 focused suite: **205 passed**.
- Phase 1 Collector/Engine shutdown lifecycle plus Phase 6 runtime integration:
  **21 passed, 1 skipped**. The skip requires `TEST_POSTGRES_DSN`.
- Full repository suite: **878 passed, 14 skipped, 0 failed**. Skips are
  opt-in Phase 2/3/4 public live probes and PostgreSQL/runtime integration tests
  requiring `TEST_POSTGRES_DSN` or an explicit runtime-acceptance gate.
- `compileall`, local Compose config validation, `.env.local` ignore check and
  `git diff --check`: PASS.
- `SECRET_LEAK_FOUND=false` for the sanitized probe/report and repository
  changes after the final secret scan.

### Current Result

- The complete-body transport fix corrects a response-truncation diagnostic
  issue. Current Bitcoin verbosity 0 and 2 still fail the required contract,
  so no parser or runtime gate can be passed on this evidence.
- Required next action: configure/approve a Bitcoin RPC provider that returns
  the required Bitcoin Core JSON-RPC contract, then repeat the bounded matrix.

**PHASE7_RPC_PROVIDER_REPLACEMENT_REQUIRED**

## Latest Acceptance Disposition (2026-09-25 Asia/Shanghai)

This disposition reflects the Phase 7 Real Source + Short Runtime Integration
Acceptance attempt documented earlier in this report.

- Immutable checkpoint remains `764e973882790385451c72cbeeeb27b15207b0c6`;
  branch remains `phase7`.
- Bitcoin verbosity 2 failed the production 8 MiB response contract. The
  temporary 32 MiB diagnostic parser pass is not production acceptance.
- Bitcoin progression ran for 601.4 seconds with no height regression; no new
  block arrived during that window. A later independent probe saw height
  advance.
- Ethereum mainnet height advanced from 26049109 to 26049114 over 61.4 seconds.
  Its formal runtime parser sample was rate-limited (HTTP 429) and is unproven.
- Binance public REST, WebSocket, real trade parsing, and a real 1-minute
  event-time aggregation passed; the sample was not persisted.
- No disposable database or runtime containers were started. Compose v2 is
  unavailable, legacy Compose validation also requires the unset
  `PHASE7_ACCEPTANCE_IMAGE`; migrations, DB evidence, resource sampling,
  shutdown, restart/recovery, and dedup remain NOT RUN.
- Regression: **890 passed, 14 skipped, 0 failed**. Phase 7: **217 passed**.
  Lifecycle/Phase 6 focus: **27 passed, 1 skipped**.
- `.env.local` remains ignored. No secret was emitted or added to this report.

Final disposition: `PHASE7_RUNTIME_INTEGRATION_NOT_ACCEPTED`.

## Pre-Runtime Blocker Remediation (2026-09-25)

### Baseline and Scope

- Branch: `phase7`; HEAD: `764e973882790385451c72cbeeeb27b15207b0c6`.
- Only bounded configuration/transport retry behavior, the probe metadata,
  acceptance preflight, related tests, and this report were changed.
- No PostgreSQL, Collector, or Engine was started. No database connection,
  persistence, ECS, live trading, Phase 8/9, Jev, or AI operation was made.
- `.env.local` was not modified; it remains ignored and absent from Git status.

### Bitcoin Response Budget and Live Evidence

- Added `PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES`, default **33,554,432 bytes
  (32 MiB)**, validated to `1..67,108,864` bytes (64 MiB hard maximum).
- Ethereum and other RPC response behavior remains at the existing 8 MiB
  default. The bounded reader still streams decompressed bytes and fails closed
  above the configured cap. Bitcoin JSON floats continue to decode as
  `Decimal`; the exact-satoshi parser test passes.
- Earlier three-block diagnostic measurements used the same formal transport
  with a one-off 32 MiB override, so they are recorded as measurements, not
  production acceptance: 9,823,257 bytes / parser PASS (6,788 events),
  11,001,718 bytes / parser FAIL at the existing bounded event-batch limit,
  and 10,470,971 bytes / parser PASS (9,221 events). HTTP `Content-Length` was
  absent for the large responses. Measured request latencies were approximately
  2.2–3.3 seconds.
- With the new production default loaded from configuration (no response-cap
  override), a real `verbosity=2` response of **10,588,603 bytes** was fully
  read and JSON-decoded under the 32 MiB cap; `Content-Length` was absent. Its
  production parser report was FAIL. The failure category was not retained in
  this sanitized capture, so it is not attributed to a specific parser rule.
  A subsequent bounded request for an adjacent block exhausted the transport
  timeout retry policy. Thus the response-size blocker is remediated, but the
  required live `verbosity=2 + parser` gate is not yet proven.
- Prior in-process measurements across the three diagnostic blocks observed a
  maximum RSS high-water delta of about **40,688 KiB** and peak RSS about
  **117,908 KiB**. This is a single-process diagnostic, not a Collector
  container measurement; no OOM-risk conclusion for a running 256 MiB container
  is claimed.
- The unit matrix covers configured default and override validation,
  below/above byte cap, Decimal-to-exact-satoshi parsing, parser checks, and
  fail-closed bounded-body behavior.

### Ethereum 429 Handling and Provider Decision

- Audited source settings: timeout 8 seconds, configured rate 1 request/second,
  concurrency 1. The live diagnostic lowered the rate to **0.1 requests/second**
  and kept concurrency 1; this was an in-memory diagnostic override only.
- Retry behavior is bounded to 3 attempts and at most 8 seconds of explicit
  retry waiting per call. Numeric or HTTP-date `Retry-After` is parsed and
  honored when it fits the budget; otherwise the request fails closed without
  an early retry. Missing/invalid `Retry-After` uses bounded exponential
  backoff. There is no unbounded retry or concurrency fan-out.
- If the bounded 429 retry budget is exhausted, the source cycle continues to
  retry on its normal lifecycle interval. The persisted base health status is
  `NOT_AVAILABLE` (the shared health enum has no `PARTIAL`); details explicitly
  carry `phase7_status=PARTIAL`, `reason=RATE_LIMITED`, `failure_stage=HTTP_429`,
  and `runtime_state=DEGRADED`. This is tested and does not mark the source
  permanently failed or falsely available.
- The limited live run reached the formal block/log/receipt parser stage but
  exhausted HTTP 429 handling there after **70.5 seconds**. Prior read-only
  preflight established Ethereum mainnet chain ID `0x1` and block progression;
  the formal parser path remains unverified. No provider was switched.
- Decision: `ETHEREUM_RPC_PROVIDER_REPLACEMENT_REQUIRED=true` before Runtime
  Acceptance can proceed.

### Compose Compatibility and Acceptance Image

- `docker compose` v2 was unavailable; the runner selected **docker-compose
  1.29.2**.
- `docker-compose.phase7-acceptance.yml config --quiet` passed and resolved
  exactly `postgres`, `quant-collector`, and `quant-engine`. The runner also
  checks `.env.local` is present, ignored, and absent from Git status; it does
  not print rendered Compose configuration.
- The Bitcoin response-cap environment variable is wired to the Collector
  with the same 32 MiB default. Resource limits remain PostgreSQL **768 MiB**,
  Collector **256 MiB**, and Engine **384 MiB**; no ports are published.
- Built with the existing project `Dockerfile` from the current worktree and
  tagged with both the current Git short SHA and the copied build-input
  fingerprint:
  - Tag: `quant-phase7:phase7-764e97388-dirty-4ec98132bfec`
  - Image ID: `sha256:ebd7ccda3e6ff1f441f3099f0efbbaacdb334e6b43e9b5111c2bf7eb2cbc22e3`
  - Build-input SHA-256: `4ec98132bfec2810ca93cc4601758b95f2233928e569546e5147e0036ad0665b`
- Runtime containers started: **NO**.

### Regression and Secret Safety

- Bitcoin/parser/config focused tests: **71 passed**.
- Phase 7 suite: **230 passed**.
- Collector shutdown + Phase 6 runtime non-regression: **21 passed, 1 skipped**
  (`TEST_POSTGRES_DSN` not configured).
- Full regression: **903 passed, 14 skipped, 0 failed**. The 14 skips remain
  opt-in Phase 2/3/4 live contracts and PostgreSQL/runtime integration tests
  requiring `TEST_POSTGRES_DSN` or an explicit runtime gate.
- One existing deprecation warning remains for `aiohttp.BasicAuth`; it is
  unrelated to these changes.
- `compileall` and `git diff --check`: PASS. Runtime code, probe output, image
  labels, and report contain no credential-bearing URL or API key.
- Secret scan: `SECRET_LEAK_FOUND=false`; `.env.local` ignored: **YES**; listed
  in Git status: **NO**.

### Final Pre-Runtime Disposition

- Bitcoin 32 MiB production response budget is implemented and live transport
  plus JSON parsing passed for a response larger than the old 8 MiB cap.
  However, the current production `verbosity=2` sample did not pass the parser,
  so the strict Bitcoin end-to-end gate remains blocked pending a bounded,
  reproducible parser-pass sample.
- Ethereum formal block/log/receipt parsing continues to encounter HTTP 429
  at a conservative rate.
- Acceptance Compose and image are ready, but neither resolves the source-data
  blockers above. No Runtime Acceptance was started.

`PHASE7_BITCOIN_TRANSPORT_BLOCKED`

`PHASE7_ETHEREUM_RPC_PROVIDER_REPLACEMENT_REQUIRED`

Overall: `PHASE7_PRE_RUNTIME_BLOCKERS_CLEARED=false`.

## Final Source Blocker Isolation (2026-09-25 Asia/Shanghai)

This follow-up supersedes the preceding source-provider disposition only where
new measurements are recorded below. No PostgreSQL, Collector, or Engine was
started; no runtime acceptance, Phase 8/9, ECS, or database operation occurred.

### Bitcoin: Production Parser Failure Isolated

- Re-ran the real configured NOWNodes → formal `Phase7JsonRpcClient` → bounded
  response reader → JSON `Decimal` decode → production `BitcoinBlockParser`
  path. The latest response was **11,151,344 bytes**, below the configured
  **33,554,432-byte** budget; HTTP `Content-Length` was absent.
- Safe structural summary for block height **968443**: **5,929** transactions,
  **7,625** inputs, and **12,665** output-derived events. Every transaction
  parsed independently; exact Decimal-to-satoshi conversion passed for all
  **12,665** outputs.
- Required checks: `block_hash_valid=true`; `height_valid=true`;
  `previous_block_hash_valid=true`; `timestamp_valid=true`;
  `transaction_identity_valid=true`; `coinbase_valid=true`;
  `normal_transaction_valid=true`; `vin_valid=true`; `vout_valid=true`;
  `satoshi_valid=true`; `provenance_valid=true`;
  `segwit_witness_shape_valid=true`; `confirmations_valid=true`;
  `transaction_count_within_cap=true`; `decimal_json_values=true`;
  `event_count_within_source_batch=false`.
- Exact result: `BITCOIN_PARSER_FAILED_CHECKS=["event_count_within_source_batch"]`.
  The production parser raises `BitcoinPayloadError` at its existing 10,000
  output-event batch bound. The first transaction at which the aggregate
  crosses that bound is index **4596**; it is a normal transaction with one
  input and two outputs. Its output value is a `Decimal`; `scriptPubKey` is a
  mapping with the expected Core fields. No transaction identifier, address,
  script contents, raw block, URL, or authentication material is recorded.
- Of the outputs, **4,777** are legitimate zero-value `OP_RETURN` outputs and
  are included in the source event count. The remaining ordinary and multi-
  input/output transactions also passed individual production parsing. This
  is classification **C — legitimate Bitcoin Core block structure exceeding
  the current bounded source-batch contract**, not a parser field-semantics
  defect and not a provider-contract violation. The cap is intentionally not
  raised and no batching/persistence semantics are changed without a separate
  memory and atomicity design review.
- The current probe helper reports generic parser exception type rather than
  retaining the exact failed check list when whole-block parsing raises. It
  did not produce a false PASS; the exact checks above came from the separate
  bounded in-memory diagnostic. This remains a probe-diagnostic limitation,
  not the cause of the production gate failure.
- `BITCOIN_LIVE_PARSER_PASS=false`.

### Ethereum: PublicNode Provider Replaced and Formally Parsed

- Changed only Ethereum keys in ignored `.env.local`; Bitcoin configuration
  was left unchanged. Effective Ethereum settings: enabled, authentication
  `NONE`, **8-second** timeout, **0.1 requests/second**, concurrency **1**.
  `.env.local` remains ignored and absent from Git status.
- Using the formal Phase 7 JSON-RPC transport against PublicNode Ethereum
  Mainnet: `eth_chainId=0x1`; start height **26049297** at
  `2026-09-24T19:07:33.284Z` (monotonic **153016.094s**); finalized block
  **26049209**. Production `_ethereum_block` called `eth_getBlockByNumber`,
  `eth_getLogs`, and a bounded batch of **159** `eth_getTransactionReceipt`
  requests. Formal block, log, receipt, provenance, and transfer parsing all
  passed, yielding **266** parsed events.
- End height **26049301** at `2026-09-24T19:08:33.453Z` (monotonic
  **153076.263s**), elapsed **60.169s**. Height was non-decreasing and advanced:
  `ETHEREUM_BLOCK_PROGRESSION=true`.
- `ETHEREUM_PROVIDER_RETAINED=false`; `ETHEREUM_PROVIDER_REPLACED=true`;
  `ETHEREUM_LIVE_PARSER_PASS=true`.

### Regression and Remaining Gate

- Bitcoin parser focused: **16 passed**.
- Ethereum adapter/parser + runtime transport focused: **57 passed**, one
  existing `aiohttp.BasicAuth` deprecation warning.
- Phase 7 focused: **230 passed**.
- Phase 1 lifecycle: **8 passed**.
- Phase 6 runtime: **13 passed, 1 skipped** because `TEST_POSTGRES_DSN` is
  unset.
- Full regression: **903 passed, 14 skipped, 0 failed**, with the same
  existing BasicAuth deprecation warning. The 14 skips are opt-in public live
  contracts and PostgreSQL integration tests requiring the documented gates
  or `TEST_POSTGRES_DSN`.
- Previously verified Binance Spot evidence remains valid. Compose validation
  and the acceptance image remain ready under the already documented memory
  caps. No runtime container was started in this follow-up.
- Re-ran the acceptance preflight after the local Ethereum configuration
  change: `docker-compose` config **PASS**; services are exactly
  `postgres,quant-collector,quant-engine`; memory limits remain **768/256/384
  MiB**; image is ready as
  `quant-phase7:phase7-764e97388-dirty-4ec98132bfec` with ID
  `sha256:ebd7ccda3e6ff1f441f3099f0efbbaacdb334e6b43e9b5111c2bf7eb2cbc22e3`.
  `RUNTIME_CONTAINERS_STARTED=NO`.
- Final secret check: `SECRET_LEAK_FOUND=false`. `.env.local` is ignored and
  absent from Git status; `.env.example` keeps the Bitcoin API-key value empty.
  Secret-pattern hits were limited to intentional test fixtures using fake
  credentials and reserved example endpoints.
- `git diff --check`: **PASS**. No commit was created; pre-existing
  uncommitted remediation changes remain intact.
- The sole source blocker is Bitcoin's legitimate **12,665-event** block
  exceeding the current **10,000-event** production batch limit. Therefore
  `PHASE7_SOURCE_BLOCKERS_CLEARED=false`; runtime acceptance must not start.

## Bitcoin Block Event Chunking Remediation (2026-09-25 Asia/Shanghai)

This section supersedes the preceding Bitcoin batch-limit blocker. It does not
claim a PostgreSQL-backed or long-running runtime acceptance. No PostgreSQL,
Collector, or Engine was started, and no Phase 8/9 work was performed.

### Implementation and atomicity

- The Bitcoin parser no longer treats 10,000 as a legal whole-block event
  ceiling. Existing structural bounds on transactions, inputs, and outputs
  remain unchanged; Bitcoin Core parsing, event identity, Decimal/satoshi,
  provenance, and UTXO semantics are unchanged.
- The existing 10,000 limit remains enforced by each production
  `upsert_transfer_events` call. Production runtime passes event-time pricing
  through a lazy iterator, and persistence consumes one bounded chunk at a
  time rather than creating a second full event-list copy or `chunks` list.
- A Bitcoin block now uses one top-level database transaction: all bounded
  event batches and its checkpoint update commit together. Repository scope
  commits migrations before enabling autocommit so each explicit repository
  transaction is a true top-level transaction, not a savepoint inside an
  uncommitted full source-cycle transaction. A failed chunk or checkpoint
  advancement rolls back the entire block and leaves its cursor unchanged.
- Reorg replacement events also use bounded insert chunks in their existing
  atomic recovery transaction. The existing bounded old-event-ID reorg window
  remains fail-closed; it was not broadened in this remediation.

### Deterministic and real-source validation

- The new parser/chunking tests were run red before implementation: the old
  parser rejected the 10,001- and 12,665-event cases, and the repository had
  no chunk-writing entry point. The same tests now pass with the bounded
  implementation.
- Chunk boundaries: **9,999 → [9,999]**, **10,000 → [10,000]**,
  **10,001 → [10,000, 1]**, and **12,665 → [10,000, 2,665]**. All generated
  canonical events were persisted in the transaction model.
- Mid-second-chunk failure and checkpoint-not-advanced failure both rolled
  back all event rows and preserved the old cursor. Replaying the same block
  preserved canonical IDs without duplicate rows. Runtime wiring and large
  source-probe contract tests also passed.
- Read-only live sample used the configured NOWNodes source through the formal
  Phase 7 transport and production Decimal decoder/parser. Bitcoin mainnet
  block **968443** produced **12,665** events from **12,665** Decimal-decoded
  output values. Production chunk preparation yielded **[10,000, 2,665]**;
  all **12,665** events were accepted, the cursor was prepared at **968443**,
  and the deterministic transaction model recorded exactly one commit.
  No database connection or persistent write was used.
- Existing `ETHEREUM_LIVE_PARSER_PASS=true` and Binance Spot REST/WebSocket
  real-data evidence from the preceding section remain unchanged and were not
  re-run as part of this Bitcoin-only remediation.

### Regression, image, and security

- Bitcoin parser, persistence, and runtime focused suite: **87 passed**.
- Phase 7 suite: **246 passed**. Phase 1 lifecycle plus Phase 6 runtime:
  **21 passed, 1 skipped** (`TEST_POSTGRES_DSN` unset).
- Full regression: **919 passed, 14 skipped, 0 failed**. Skips remain the
  documented opt-in public contracts and PostgreSQL integration tests.
- Acceptance preflight: Compose config **PASS**; services are exactly
  `postgres,quant-collector,quant-engine`; memory caps remain PostgreSQL
  **768 MiB**, Collector **256 MiB**, Engine **384 MiB**. Acceptance image
  built successfully (`quant-phase7:phase7-764e97388-dirty-6994a782d197`,
  image ID `sha256:d1b5468e3d794c1ae02ec881c9fe07e7aca8d0d4d75e6c1a8dd466c0d482d121`).
  `RUNTIME_CONTAINERS_STARTED=NO`.
- `.env.local` remains ignored and absent from Git status. No RPC URL,
  authenticated header, or API key was added to code, tests, Compose, or this
  report. `SECRET_LEAK_FOUND=false`.
- Work remains uncommitted on branch `phase7`, HEAD
  `764e973882790385451c72cbeeeb27b15207b0c6`; pre-existing remediation changes
  were preserved.

`BITCOIN_BLOCK_CHUNKING_PASS=true`

Together with the already verified Ethereum parser, Binance Spot, Compose, and
acceptance-image evidence above: `PHASE7_SOURCE_BLOCKERS_CLEARED=true`. This
does not start the 10–15 minute runtime acceptance; that remains a separate
explicit next step.

## PHASE 7 COMPLETE REMAINING ACCEPTANCE — 2026-09-25 Asia/Shanghai

This is the current acceptance disposition. It supersedes earlier pending
status text above. No remote ECS, production PostgreSQL, Phase 8/9, Jev, AI
provider, private trading API, order route, wallet signing, or live trading was
used. `TRADING_MODE=paper` was retained throughout.

### Final result

`PHASE7_RESOURCE_ACCEPTANCE_BLOCKED`

The Collector crossed the explicitly defined resource-risk gate during the
real short-runtime stage. The runtime was stopped cleanly; Replay V3, restart,
finality/reorg acceptance, three restart cycles, and the 60-minute soak were
not started. Those stages must not be inferred as passing from the data and
unit tests recorded here.

### Immutable runtime identity and isolated environment

- Runtime code branch: `phase7`
- Runtime code SHA: `fbdda1ed3d16a479ad10a895c62d5bc7aac46383`
- Acceptance image tag:
  `quant-phase7:phase7-fbdda1ed3-clean-1bde41b028e5`
- Acceptance image ID:
  `sha256:137c45cc91cbb0ff1e8fc19f38a894474d07ed306203d763b4535343abb14007`
- Acceptance image revision label: matches runtime code SHA above
- Acceptance image source fingerprint:
  `1bde41b028e5561f6b0bb046697776ca839e7b9a255e1711a23218824426ab7c`
- PostgreSQL image ID:
  `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`
- Compose: `docker-compose 1.29.2`; configuration validation passed
- Isolated project: `phase7-acc-fbdda1e-20260925`
- Fresh disposable volume: `phase7-acc-fbdda1e-20260925_phase7_acceptance_pgdata`
- The volume was created for this run, not reused from development or a prior
  project. It is retained, with all three acceptance containers stopped, for
  evidence review. Existing unrelated containers and volumes were untouched.
- Only services: PostgreSQL, `quant-collector`, `quant-engine`; no executor or
  other trading service. No host port was published.
- Actual Docker memory caps: PostgreSQL **768 MiB**, Collector **256 MiB**,
  Engine **384 MiB**. No cap was raised.

### PostgreSQL migration and pre-runtime state

- Fresh database: `quant`; session timezone: `UTC`.
- Fresh run applied **14 SQL migrations**, ending at
  `014_phase7_exact_amount_constraint.sql`; the migration table has **15**
  rows including the existing `009_phase4_metrics.repair.v1` marker.
- Repeated migration run applied **0** migrations.
- Before starting Collector/Engine, all nine Phase 7 tables, `system_health`,
  and `runtime_health_events` had zero rows; no fixture, cursor, synthetic
  event, or acceptance marker was inserted.
- During the preceding short DB-fix validation, Migration 014 was verified
  against PostgreSQL and the exact-amount integration test changed from the
  reproduced CHECK-constraint failure to passing. The production parser and
  repository then persisted real Ethereum events in this fresh runtime.

### Real short runtime and source evidence

- Collector/Engine start: `2026-09-24T20:01:22.695Z` /
  `2026-09-24T20:01:22.707Z`
- Collector clean stop: `2026-09-24T20:06:49.574Z`; measured Collector runtime
  **326.878 seconds (5m 26.878s)**, below the required 10-minute minimum and
  15-minute target.
- Before stop, the disposable DB contained **299,006** canonical on-chain
  transfer events: **295,743 Bitcoin** and **3,263 Ethereum**. Checkpoints
  advanced to Bitcoin block `968324` and Ethereum block `26049387`.
- Binance Spot persisted **19** one-minute windows across BTCUSDT and ETHUSDT;
  trade cursors advanced to `4073218594` and `2089273834`. Latest stored Spot
  windows were AVAILABLE at the time of the pre-stop snapshot.
- Other Phase 7 pre-stop counts: asset registry 4; address labels 0; on-chain
  flow windows 8; whale windows 8; stablecoin context 24; Stage1 Phase 7
  context enrichment 200; ingestion checkpoints 4; runtime health events 6.
- The real runtime therefore exercised Bitcoin/Ethereum persistence and Spot
  window writes, but does **not** pass the minimum-duration acceptance gate.
  Per-block live chunk/one-transaction evidence was not collected in this run.
  Existing deterministic chunking evidence remains limited to the prior
  production-path block sample (12,665 events → `[10000, 2665]`) and
  transaction-model tests; it is not represented as a live DB atomicity result
  for this runtime.
- Source health was not uniformly healthy. Bitcoin and Binance Spot reported
  AVAILABLE near the stop. Ethereum checkpoint/events progressed, but its
  `system_health` row remained NOT_AVAILABLE with an old check time. Phase 4
  liquidation and long/short were ERROR; Phase 7 context-engine was
  NOT_AVAILABLE. Phase 6 AI provider/worker and news/macro/unlock paths were
  NOT_AVAILABLE; no AI provider was configured or called. These health
  discrepancies are additional acceptance warnings/blockers and were not
  investigated after the resource gate required stopping.

### Resource evidence and interpretation

Five scheduled samples at 30-second intervals were captured between
`2026-09-24T20:03:53Z` and `20:05:53Z`; one earlier smoke sample is separate.
The sanitized sample file is `/tmp/phase7-short-runtime-samples.jsonl`.

| Service | Limit | Sampled current range | Sampled peak | Memory events | Interpretation |
|---|---:|---:|---:|---|---|
| Collector | 256 MiB | 233.1–255.4 MiB | 256.1 MiB | `max` 0→100; `oom=0`, `oom_kill=0` | 4/5 formal samples exceeded 95% of cap; risk gate triggered |
| Engine | 384 MiB | 126.1–128.5 MiB | 177.6 MiB | `oom=0`, `oom_kill=0` | Below cap in observed samples |
| PostgreSQL | 768 MiB | about 763–767 MiB during app runtime; 734.4 MiB after app stop | 768.1 MiB | `max` 6031; `oom=0`, `oom_kill=0` | `memory.stat.file` was 711.4 MiB and anon 4.0 MiB after app stop; file/page cache dominated. Docker stats showed 418.9 MiB then. Reclaimability was not independently proven, so this is not called a leak or a resource PASS. |

The Collector met the task's explicit failure condition through repeated
near-cap samples and an increasing `memory.events:max` counter. It was stopped
immediately; no memory cap change, retry loop, or 60-minute soak was attempted.
The one Docker CPU sample near stop was PostgreSQL 0.04%, Collector 14.05%, and
Engine 0.00%; it is a point sample, not an aggregate CPU acceptance result.

### Shutdown and persisted database size

- Collector: exit 0, `OOMKilled=false`.
- Engine: exit 0, `OOMKilled=false`.
- PostgreSQL: exit 0, `OOMKilled=false`; its logs contained the clean
  `database system is shut down` marker.
- No acceptance container remained running after stop; the three stopped
  containers and disposable volume were retained. No orphan acceptance
  process was observed.
- Pre-stop database size: **544,578,583 bytes** (~519.3 MiB); on-chain transfer
  relation: **430,260,224 bytes**; Spot relation: **98,304 bytes**.

### Remaining gates (not run)

| Gate | Result |
|---|---|
| Minimum 10-minute real short runtime | **FAIL** — stopped at 5m 26.878s on resource risk |
| Cursor restart acceptance | NOT RUN |
| Restart dedup acceptance | NOT RUN |
| Deterministic Replay V3 / three fresh DB runs | NOT RUN |
| Final dedup scenarios / partial-chunk failure DB injection | NOT RUN in this acceptance; prior deterministic coverage is not a substitute |
| Dedicated Bitcoin/Ethereum finality acceptance | NOT RUN |
| In-window and beyond-window reorg fault injection | NOT RUN |
| Three restart cycles | NOT RUN |
| 60-minute real runtime soak | NOT RUN — prerequisites failed |
| Formal resource acceptance | **BLOCKED** — Collector threshold reached |

The pre-existing bounded old-event-ID reorg lookup remains a fail-closed
design limitation; the required within/beyond-window acceptance was not run.

### Regression, security, and final checkpoint

- Red reproduction: the large valid Ethereum amount failed Migration 012's
  division-based check. Forward-only Migration 014 replaced that check with
  exact multiplicative scaling; no historical migration was modified.
- Exact PostgreSQL persistence integration after Migration 014: **1 passed**.
- Phase 7 + Collector clean-shutdown + Phase 6 runtime focused regression:
  **271 passed, 2 skipped** (those two tests require `TEST_POSTGRES_DSN`; the
  targeted new PostgreSQL test was separately run and passed with the isolated
  disposable DB).
- Full repository regression at runtime code SHA:
  **923 passed, 15 skipped, 0 failed**, one existing aiohttp BasicAuth
  deprecation warning. Skips: Phase 2 public live contracts (3), Phase 3
  public live contracts (2), Phase 4 public live contracts (3), and seven
  PostgreSQL integration tests requiring `TEST_POSTGRES_DSN` in the full-suite
  invocation. No unexplained skip was added.
- `.env.local`: ignored and untracked. Exact configured secret values had
  **zero** matches in tracked/untracked artifacts, new commit diff, runtime
  logs, report, or captured test artifacts. Pattern-only hits were limited to
  synthetic fake-credential/reserved example fixtures in two test files.
  Rendered Compose was validated in memory and never printed or saved.
  `SECRET_LEAK_FOUND=false`.
- Runtime code commit: `fbdda1ed3d16a479ad10a895c62d5bc7aac46383`.
  This report and its local acceptance helper scripts are recorded in the
  subsequent final report checkpoint; the runtime image remains traceable to
  the code SHA above.
- Final acceptance status: `PHASE7_RESOURCE_ACCEPTANCE_BLOCKED`.
  Re-run acceptance only after a Phase 7-scoped memory remediation has its own
  RED/GREEN tests, regression, commit, and rebuilt image. Keep all resource
  caps unchanged. Do not begin Phase 8/9.

## 2026-09-25 Collector memory root-cause and bounded-remediation follow-up

### Immutable revisions and scope

- Branch: `phase7`.
- Baseline before this investigation: `59bf43653adb800cc62e61623450c829038a01a4`.
- Minimal production fix: `10421b77833484ad763d8c49b27be2b027b44c09` (`fix(phase7): release bitcoin block events between cycles`).
- Acceptance monitor/Compose observability checkpoints: `a091cdd9be0336c049516d67ece6c3684ad9225b` and `c156ae3f48083a51eba9dc37721feb3de250082d`.
- The D image was built at `a091cdd9`; application code in that image includes the production fix at `10421b7`. `c156ae3` changes only host-side acceptance monitoring, not runtime application code.
- Collector/PostgreSQL/Engine limits remained 256/768/384 MiB. All acceptance runs used paper mode, public/read-only sources, isolated Compose projects and fresh project-scoped PostgreSQL volumes. No private trading API or order path was used.

### Root-cause classification: MIXED

The deterministic lifetime test reproduced a short-lived inter-block retention defect: after a Bitcoin block was persisted, local references to the full canonical `events` tuple and lazy `priced_events` iterator survived while the next block response was fetched and decoded. The reorg replacement path similarly retained its replacement tuple and old IDs. The minimal fix releases these references immediately after successful persistence; it does not change event identity/count, valuation, chunk boundaries, transaction atomicity, cursor ordering, or rollback semantics. The weak-reference test was RED before the fix and GREEN after it.

Repeated fixed-block cycles did not show a stair-step retained-object baseline after the fix, so the evidence does not support an unbounded Python-object leak. The real D runtime nevertheless approached the cap with predominantly anonymous memory, indicating that the valid Phase 1–7 active working set plus runtime overhead remains too large for a safe 256 MiB operating margin. File/page cache was negligible in the Collector samples. Therefore this is a mixed result: a confirmed transient-retention bug was fixed, but a high legitimate/active working set remains. Available queue telemetry is incomplete, so backlog cannot be ruled out categorically.

### Fixed-block A/B profile

The deterministic Core-shaped fixture contains 12,665 events and an 11,189,431-byte JSON response. It was processed for five repeated cycles, two blocks per cycle, under the unchanged 256 MiB cgroup cap. It is a deterministic fixture, not a saved live block. Largest comparable block-2 parse/canonical-event sample fell from 193.6 MiB before the fix to 137.9 MiB after it (55.7 MiB lower). Other sampled stages also fell: response stage 177.8→154.1 MiB; decoded-JSON stage 180.2→165.3 MiB. Post-fix persistence was about 138.2 MiB and cycle-end cgroup use about 133.2 MiB; traced allocations returned to about 0.5 MiB at cycle end. Five cycles showed a stable baseline, with no cgroup max/OOM events. This validates the object-lifetime fix, but does not establish that the full live Collector fits within the cap.

### Controlled runtime A/B observations

The isolated A/B runs used the same post-fix application image and unchanged service caps, but live exchange windows are not an identical deterministic workload; treat these as characterization, not precise causal attribution. A (Phase 7 disabled) warm sample ranged about 188.8–199.2 MiB cgroup current, mostly anonymous pages. B (BTC+ETH+Spot enabled) ranged about 76.2–179.6 MiB during its one-minute window; C (BTC disabled, ETH+Spot enabled) reached about 141.7 MiB. Bitcoin and Spot cursors advanced in B; Ethereum health reporting was inconsistent with checkpoint state. These short runs did not cross the stop threshold, but are not substitutes for D.

### Full D runtime and stop gate

D used image `quant-phase7:phase7-a091cdd9b-clean-30d189485f40`, isolated project `phase7-runtime-d-20260925-a091cdd`, and the unchanged 256 MiB Collector cap. Sampling was every 10 seconds: 51 samples from `2026-09-25T06:13:00.891520Z` through `2026-09-25T06:21:20.891574Z` (500 seconds / 8m20s). The runtime was stopped immediately when the sample crossed the mandated 95% gate; it therefore did not satisfy the >=10-minute retry requirement.

| D Collector metric | Observed |
|---|---:|
| Limit | 268,435,456 bytes (256 MiB) |
| Peak `memory.current` | 262,103,040 bytes (97.64%) |
| `memory.peak` | 264,798,208 bytes (98.64% of limit) |
| Peak anon / file | 254,435,328 / 20,480 bytes |
| Peak kernel / sock | 3,346,432 / 4,521,984 bytes |
| Peak shmem / slab / pagetables | 0 / 1,739,104 / 839,680 bytes |
| Process RSS / PSS at peak sample | 276,725,760 / 262,478,848 bytes |
| `memory.events:max` / OOM / OOM-kill | 0 / 0 / 0 |

No OOM occurred and the kernel max-event counter remained zero, but the explicit 95% safety gate was crossed; this is a resource acceptance failure, not a pass. The evidence file records Bitcoin cursor 968367→968417 and Binance BTC/ETH Spot trade cursors advancing. At the final sample, the Ethereum checkpoint was present with status AVAILABLE while its health status was ERROR; Phase 4 liquidation and long/short also reported intermittent ERROR, and Phase 7 context-engine was NOT_AVAILABLE. These are separate health/source issues and were not changed in this memory remediation.

Only `phase6-ai-worker.queue_depth=0` was exposed. Its backfill/backlog fields and other recovery queues, pending task counts, and active RPC counts were not exposed by the health contract; those values are NOT_EXPOSED, not inferred as zero. Cgroup task count was 9 and process thread count 7 at the peak sample.

The required post-workload 60–120 second real-runtime quiescence was not run because D crossed the hard stop gate. The repeated deterministic cycles provide cleanup/baseline evidence, but do not replace that quiescence test. D containers and its Compose network were stopped; its project-scoped PostgreSQL volume was preserved. No unrelated containers or volumes were removed.

### Regression, security, and disposition

- Latest full regression: **924 passed, 15 skipped, 0 failed** in 28.16 seconds; one existing `aiohttp.BasicAuth` deprecation warning. Skips were the documented Phase 2/3/4 public probes and PostgreSQL integration tests without `TEST_POSTGRES_DSN`.
- Resource-focused suite: **29 passed**; Phase 7 plus Phase 3/Bitcoin targeted set: **44 passed, 1 skipped**. The deterministic profile and A/B runs used no secret-bearing output.
- `.env.local` remained ignored/untracked. Secret audit result: `SECRET_LEAK_FOUND=false`; no configured RPC credential, authenticated URL, or header was written to Git/report/runtime output.
- Final status: `PHASE7_RESOURCE_ACCEPTANCE_BLOCKED` because D crossed 95% before 10 minutes. The evidence does **not** prove architectural impossibility at 256 MiB, so `COLLECTOR_256M_CAPACITY_INSUFFICIENT=true` is not asserted. Do not increase the cap or proceed to Replay V3, reorg, restart cycles, 60-minute soak, Phase 8, or Phase 9 in this task.

## 2026-09-25 Real-D memory observability and 256 MiB re-acceptance

### Baseline, implementation, and immutable runtime

- Task intake baseline: branch `phase7`, HEAD `b6283469221ffc8009e481fb3d0c6cd4bcad434b`. The diff from `c156ae3` to that baseline contained only the local report, acceptance scripts, and tests; no application-runtime source file was changed in that interval.
- The minimal observability change was separately tested and committed as `bd4b157eed3706821261be88cf2d308dc2f6b029` (`chore(phase7): add collector memory diagnostics`). It adds read-only scalar snapshots and a bounded acceptance-only diagnostic file. Queue capacities, concurrency, retries, business processing, persistence, and cursor semantics were not changed.
- Focused observability/source/lifecycle checks: **166 passed, 2 skipped** (PostgreSQL cases requiring `TEST_POSTGRES_DSN`). Full regression: **936 passed, 15 skipped, 0 failed**. The skips are existing public live probes and PostgreSQL integration cases without the test DSN. `compileall` passed; Ruff was unavailable in the local virtual environment.
- Runtime image: `quant-phase7:phase7-bd4b157ee-clean-16f9592f8991`, image ID `sha256:052307ab66dc95cf6099b70213f48309a5e76100b67f3c4c6d41217c878577ec`, built from the immutable commit above. All acceptance services used that image where applicable.
- Isolated Compose project: `phase7-real-d-20260925-bd4b157`; services were only PostgreSQL, Collector, and Engine, with no published host ports. Mode was `paper`. Limits remained Collector **268,435,456 B (256 MiB)**, Engine **402,653,184 B (384 MiB)**, PostgreSQL **805,306,368 B (768 MiB)**. No unrelated container or volume was touched.

### Real-D observation window and disposition

The Collector/Engine/PostgreSQL runtime remained active for the full observation and ended with an additional sample to cover a complete 15-minute window: **92 samples, 10 seconds apart, 910 seconds (15m10s)** from `2026-09-25T09:17:37.948687Z` through `2026-09-25T09:32:50.964870Z`. The two-sample supplement followed the 90-sample primary monitor without restarting the runtime. The containers themselves were started at `09:15:11Z` (PostgreSQL) / `09:15:16Z` (Collector and Engine) and stopped at `09:35:12–09:35:15Z`, for about 20 minutes total runtime.

Sanitized samples remain at `/tmp/phase7-real-d-20260925-bd4b157-samples.jsonl` and `/tmp/phase7-real-d-20260925-bd4b157-supplement.jsonl`; monitor stdout was kept separately in matching `*-monitor.log` files.

| Collector metric | Measured |
|---|---:|
| Limit | 256 MiB |
| Sampled `memory.current` min / mean / p50 / p95 / max | 211.4 / 231.5 / 233.0 / 238.0 / 241.1 MiB |
| cgroup `memory.peak` maximum | 247.6 MiB (96.7% of limit; brief high-water mark) |
| 10-second samples at or above 90% / 95% | 67 / 0 of 92 |
| `anon` mean / p95 / max | 227.5 / 233.3 / 233.9 MiB |
| Maximum `file` / `kernel` / `slab` / `sock` / `pagetables` | 0.1 / 3.3 / 1.74 / 5.98 / 0.82 MiB |
| `memory.events:max` / `oom` / `oom_kill` | 0 / 0 / 0 throughout |
| Process RSS / PSS / VmSize maximum | 254.7 / 241.1 / 724.1 MiB |
| Threads / cgroup tasks maximum | 7 / 9 |
| Asyncio task count maximum | 49 |

The brief cgroup high-water mark exceeded 95%, but no 10-second `memory.current` sample reached 95%; it quickly returned below the threshold and did not form a sustained high-water interval. This follows the current task’s sustained-risk rule and is not treated as a failure by a single transient `memory.peak`. The live mean was about 13 MiB above the earlier deterministic replay’s last-five-minute p95 of 218.5 MiB; these are different workloads, so this is descriptive rather than a controlled A/B comparison. Live multi-source work plus the steady Collector working set explains the higher baseline more plausibly than an unbounded Python-object leak.

The largest recorded `memory.peak` sample was at `09:18:07.948715Z`: cgroup high-water 247.6 MiB, sampled current 236.5 MiB; Bitcoin was in `BLOCK_RPC` around height 968396 with 8,696 block events in its diagnostic state and 31,596,139 cumulative response bytes for that source cycle; Ethereum had a bounded 119-block backfill and was in cycle backoff; Spot had 244 pending trades. This is time-correlated evidence for a **multi-source overlap / Bitcoin block-workload peak**, not proof that one event alone caused the peak. The later current-memory series stayed below 241.1 MiB and its anonymous-memory series plateaued rather than climbing without bound.

### Queue, buffer, source, and persistence audit

| Path | Bound / visibility | Real-D observed high-water | Result |
|---|---|---:|---|
| Phase 1 event buffer | Capacity 2,000; non-consuming depth snapshot | Depth 0; dropped 0 | Bounded; no accumulation observed |
| Phase 1 recovery admission | Queue capacity 800; 8 workers plus scheduler | Queue, in-flight, active, retry, rerun, and failed counts all 0 | Bounded; no backlog observed |
| Phase 1 WebSocket stores | Latest-value ticker map; 4 closed candles per symbol/interval; 5 connections / 10 tasks | 200 tickers; 3,200 closed-candle slots | Bounded. The WebSocket library’s internal transport queue is not exposed by its public API and is reported as NOT_EXPOSED, not zero. |
| Phase 3 trade aggregation | Queue capacity 42,000 | Depth 131; 23 pending windows | Bounded and far below capacity |
| Bitcoin RPC / block / persistence | Sequential RPC (max active 1); backfill at most 12 blocks; response body bounded by 32 MiB default / 64 MiB hard limit; atomic per-block persistence with chunks at most 10,000 events | Backfill 12; active block/RPC max 1; block-event diagnostic max 14,734; response-size metric max 11,701,691 B; planned pending chunk metric max 2 | Bounded. Cycle byte totals are cumulative per-cycle, not a single response size. No cursor/persistence semantic change was made. |
| Ethereum RPC / block / logs / receipts | Sequential RPC (max active 1); backfill at most 120 blocks; logs cap 2,000; receipt candidates cap 250; response body 8 MiB | Backfill 120; pending logs 169; pending receipts 0 | Bounded, but the source cycle repeatedly fails before checkpoint advancement; see health audit below. |
| Binance Spot REST / WebSocket | REST request active count; bounded per-symbol trade buffers (aggregate diagnostic capacity 6,000); inline WS parse; aggregation windows tracked | Pending trades max 4,321/6,000; windows max 4; REST active max 1 | Bounded. Trade pending rose and drained in batches (e.g. 4,124→749), not monotonically. Aiohttp’s internal WebSocket queue is not exposed by the public API and is reported as NOT_EXPOSED. |
| Database writer | Synchronous writer; no asynchronous batch queue | Pending batches 0; active transactions max 2; idle-in-transaction max 1 | No persistent writer backlog observed |
| Retry/task lifecycle | Bounded per-source retry loops and fixed service task ownership | Collector asyncio tasks 41–49; no monotonic task growth | Bounded during this window |

The measured source cursors were:

| Source | First sample | Last sample | Result |
|---|---:|---:|---|
| Bitcoin blocks | 968,393 | 968,472 | Advanced 79 blocks |
| Ethereum blocks | 26,053,316 | 26,053,316 | No progress |
| Binance BTCUSDT trade ID | 4,073,558,164 | 4,073,568,586 | Advanced |
| Binance ETHUSDT trade ID | 2,089,515,279 | 2,089,529,689 | Advanced |

### Ethereum health/checkpoint root-cause audit

This run does **not** show a health-state synchronization-only defect. The durable Ethereum checkpoint was last updated at `2026-09-25T09:16:08.6777Z` and remained `AVAILABLE` at cursor `26053316`; the Collector health row was refreshed during the run and ended `ERROR`, with safe reason category `Phase7RpcError` (latest health write `09:34:12.809014Z`). Runtime diagnostics repeatedly reached `LOGS_REQUEST` and then `CYCLE_BACKOFF`; the source health writer records the sanitized exception class while omitting provider error text/URL. Thus the observed health `ERROR` matches repeated real cycle failure, rather than a stale aggregation timestamp, and no Ethereum checkpoint/data progress occurred during D. The exact provider-side cause is intentionally not surfaced in logs and remains unresolved; treat Ethereum live ingestion as a separate Phase 7 source blocker before any full source/runtime acceptance. No provider, health semantics, or Ethereum code was changed.

### PostgreSQL, runtime health, and shutdown

- Migrations `001` through `014` were applied in the isolated database; `SHOW timezone` returned `UTC`. Final database size was **1,643,314,199 bytes**; PostgreSQL data directory was **2.5 GiB**. These are final inventory figures, not a long-term growth projection.
- Final row counts at shutdown (not asserted as deltas): `symbols` 805; `market_snapshots` 34,610; `market_observations` 103,830; `klines` 79,205; `phase7_ingestion_checkpoints` 4; `phase7_onchain_transfer_events` 994,172; `phase7_spot_flow_windows` 46; `phase7_onchain_flow_windows` 4; `trade_flow_windows` 742; `system_health` 22; `runtime_health_events` 12.
- Docker stats near the end: Collector 236 MiB/256 MiB (92.17%), Engine 153.4 MiB/384 MiB, PostgreSQL 439.9 MiB/768 MiB. Instantaneous CPU was Collector 13.32%, Engine 0%, PostgreSQL 3.05%; cgroup CPU averaged over the sample window was 23.04%, 0.91%, and 31.41% of one core respectively.
- Last sampled health: Bitcoin, Binance Spot REST/WS, `quant-collector`, and `quant-engine` were `AVAILABLE`; Ethereum was `ERROR`; Phase 4 liquidation/long-short were `ERROR`, Phase 5 context was `STALE`, and Phase 7 context engine was `NOT_AVAILABLE`. These source/phase health states are not presented as a full Phase 7 acceptance pass.
- PostgreSQL raw cgroup `memory.current` peaked at 766.9 MiB and its `memory.events:max` rose from 853 to 35,177, with `oom=0` and `oom_kill=0`. Its `memory.stat.file` peaked at 745.4 MiB while `anon` peaked at 35.4 MiB; this is a PostgreSQL/page-cache resource warning, not a Collector event and not proof of reclaimability. Docker’s cache-adjusted stats showed 439.9 MiB at the near-end sample. Keep this separate for PostgreSQL resource review; the Collector’s own `max/oom/oom_kill` counters remained 0.
- Safe source quiescence while keeping the Collector process alive was not feasible: the runtime has no independent pause-input control, and stopping the source lifecycle stops the Collector. No artificial GC was used. After the observation window, the isolated Collector, Engine, and PostgreSQL were stopped in order; each exited code 0 with `OOMKilled=false`. The project volume was preserved. No other containers were stopped.

### Security and disposition

- The configured secrets remained in ignored, untracked `.env.local`; no endpoint, key, authorization header, or payload was included in diagnostics or this report. Secret audit: `SECRET_LEAK_FOUND=false`.
- 320/384 MiB diagnostic runs remain `NON_ACCEPTANCE_DIAGNOSTIC` only and were not used in this conclusion.
- Memory classification: `LIVE_D_ROOT_CAUSE=MIXED(MULTI_SOURCE_OVERLAP, STEADY_WORKING_SET)`. No unbounded memory leak or monotonic Collector queue/task growth was observed; the transient high-water correlated with concurrent BTC block work and other source load.
- **`PHASE7_RESOURCE_BLOCKER_CLEARED=true` for the Collector 256 MiB memory gate only.** This is not a full Phase 7 runtime/source acceptance: Ethereum’s enabled source did not advance and remains a separate blocker. Do not infer Phase 7 overall acceptance, and do not start Replay V3/finality/reorg/restarts/60-minute soak, Phase 8, Phase 9, or any out-of-scope work in this task.

## 2026-09-25 Ethereum collection and PostgreSQL 768 MiB blocker follow-up

### Scope and baseline

- Branch `phase7`, baseline HEAD `bd4b157eed3706821261be88cf2d308dc2f6b029`. The existing, reviewed-but-uncommitted runtime changes in `src/quant_phase7/runtime.py` and `tests/test_phase7_runtime_integration.py` were preserved; no additional production-code edits were made in this follow-up. This section supersedes the earlier Ethereum “no progress” status only for the later runtime windows below; it does not erase that historical failure.
- Only the isolated Compose project `phase7-eth-pg-audit-20260925` was used. Its services were PostgreSQL, Collector, and Engine, with caps unchanged at 768/256/384 MiB. Runtime mode was `paper`. Production `quant-postgres`, other application containers, and external hosts were not modified.
- The earlier `PHASE7_RESOURCE_BLOCKER_CLEARED=true` remains limited to the Collector 256 MiB gate; it was not reopened by this PostgreSQL review.

### Ethereum runtime isolation and result

The earlier Real-D record remains a genuine historical failure: its Ethereum checkpoint stayed at `26053316`, health ended `ERROR`, and diagnostics repeatedly moved from `LOGS_REQUEST` to `CYCLE_BACKOFF`. The stage maps to the single-block `eth_getLogs` request (`fromBlock == toBlock`; requested span = 1 block). The failure logger intentionally retained only the exception class and stage, not HTTP status, JSON-RPC code/message category, attempt, Retry-After, or per-request latency. Therefore the historical provider-side cause cannot be distinguished as rate limiting, timeout, invalid parameters, or another provider error without inventing evidence: `ETH_RUNTIME_ROOT_CAUSE=UNKNOWN` (last observed stage: `eth_getLogs`). No historical request body, URL, credential, or header was recovered or emitted.

The existing runtime remediation had two relevant bounded behaviors: receipt candidates are admitted up to the explicit 2,000-per-block fail-closed cap and fetched in sequential batches of at most 250; after each committed block/checkpoint, Ethereum health is refreshed with `progress_source=BLOCK_CHECKPOINT_COMMITTED`. No receipt candidate, log, or event is silently skipped. Focused tests cover both chunking/cap behavior and health synchronization after persistence.

The failure did not reproduce in the subsequent real Collector runtime. In the 15-minute 10-second-sampled Real-D window (`2026-09-25T10:50:02Z`–`11:05:02Z`), the Ethereum checkpoint advanced `26053509`→`26053538` (+29 blocks), with health available after warm-up. The same runtime continued to checkpoint `26053556` by approximately `11:14Z`; the persisted Ethereum event count was 28,689 at that point. No Ethereum `phase7_source_cycle_failed` or rate-limited log was observed in the later run. A further integrated retest advanced `26053556`→`26053565` and persisted Ethereum events `28,689`→`32,147`; all four source health rows (Bitcoin, Ethereum, Spot REST, Spot WebSocket) were `AVAILABLE` at its last sample. Thus `ETHEREUM_RUNTIME_COLLECTION_PASS=true` for the observed current runtime, while the exact cause of the older provider error remains unclassified and should be captured if it recurs.

### PostgreSQL resource characterization

All PostgreSQL evidence below is from the dedicated acceptance volume/container, not the production database. Its database was about 2.7 GiB; the resource cap remained 768 MiB. A scratch table with event-like payload and indexes was created only after verifying the name did not exist. Three deterministic cycles inserted 10,000 rows each (500-row committed batches; 30,000 rows total), validated the per-cycle counts and IDs, then dropped the scratch table. No Quant business table was changed by this workload.

| Cycle | Rows | `memory.events:max` before→after | cgroup current at cycle end | `anon` at cycle end |
|---|---:|---:|---:|---:|
| 1 | 10,000 | 417,271→417,294 (+23) | 804,073,472 B | ~3.0 MiB |
| 2 | 10,000 | 417,295→417,382 (+87) | 803,774,464 B | ~3.0 MiB |
| 3 | 10,000 | 417,385→417,467 (+82) | 804,003,840 B | ~3.0 MiB |

Across the repeated workload, cycle-end memory did not rise monotonically; anonymous memory remained bounded, all 30,000 rows validated, and OOM/OOM-kill stayed zero. After application writes stopped, PostgreSQL remained running for 120 seconds. In that interval cgroup current fell from about 738.8 MiB to 148.0 MiB; `file` fell from about 719.0 MiB to 139.5 MiB; `inactive_file` fell from about 366.7 MiB to 1.6 MiB; `anon` remained about 1.2 MiB; and `memory.events:max` stayed at 417,472. This proves that the bulk of the idle footprint was reclaimable file cache. PostgreSQL was then stopped normally (exit 0, `OOMKilled=false`). The temporary resource-audit table and the separate 11 MiB PostgreSQL integration-test database created for this task were removed; neither contained user/production data.

However, the subsequent live multi-source runtime produced new, sustained pressure under the exact 768 MiB cap. The cgroup counter reset on container restart; over the 5-minute-10-second partial integrated retest it rose from 0 to 161,688. Sampled `memory.current` reached 767.97 MiB and cgroup `memory.peak` reached 768.18 MiB; peak `memory.stat.file` was 747.2 MiB and peak `anon` was 28.8 MiB. At the last sample, `file=736.23 MiB`, `anon=7.90 MiB`, and `inactive_file=375.87 MiB`. OOM and OOM-kill remained zero, and database checkpoints and source data continued to advance. The PostgreSQL log also recorded a checkpoint writing 3,604 buffers (~22 MiB) with 39.197 seconds of write time; this is an I/O-pressure warning, though no application write failure was observed. The ongoing `memory.events:max` growth during real runtime violates this task’s explicit resource gate even though the cache is reclaimable after quiescence: `POSTGRESQL_768M_RESOURCE_BLOCKED=true`. Keep the cap at 768 MiB; do not infer that reclaimability alone makes the active-workload pressure acceptable.

### Partial integrated retest, tests, and disposition

The integrated run was intentionally stopped after the PostgreSQL counter showed sustained growth, before the required 10-minute minimum; it is not a full integrated acceptance. From `11:25:42Z` to `11:30:52Z` (32 samples, 10 seconds apart):

- Bitcoin block checkpoint: `968536`→`968537`; canonical event rows: `1,615,561`→`1,629,974`.
- Ethereum checkpoint: `26053556`→`26053565`; canonical event rows: `28,689`→`32,147`.
- Binance Spot BTC/ETH trade IDs advanced `4073613481`→`4073638944` and `2089570531`→`2089597221` respectively. All four source health statuses were `AVAILABLE` at the last sample (Ethereum was `NOT_AVAILABLE` during initial warm-up for 5 samples, then `AVAILABLE`).
- Collector cap remained 256 MiB; sampled `memory.current` peaked at 209.6 MiB, cgroup `memory.peak` at 216.8 MiB, and `memory.events:max/oom/oom_kill` remained `0/0/0`. Engine cap remained 384 MiB; sampled current peaked at 176.8 MiB. The PostgreSQL result above blocks acceptance, so this 5-minute-10-second sample is not presented as meeting the integrated runtime duration requirement.
- Collector and Engine exited 0 with `OOMKilled=false`; PostgreSQL performed a clean shutdown and exited 0 with `OOMKilled=false`. All three isolated containers are stopped; their project-scoped volume is preserved.

Validation: Ethereum/runtime/Phase 1 shutdown/Phase 6 runtime focused suite **114 passed, 3 skipped**; Phase 7 persistence tests against a fresh isolated test database **22 passed**; full default regression **939 passed, 15 skipped, 0 failed** (existing public live probes and DB-gated tests skipped without `TEST_POSTGRES_DSN`; the Phase 7 persistence gate was separately run and passed). One existing `aiohttp.BasicAuth` deprecation warning remains. No application files changed in this follow-up; only this report was appended.

Secret audit: `SECRET_LEAK_FOUND=false`; `.env.local` remained ignored and was not printed or added to Git. No RPC URL, API key, authorization header, or response payload was placed in the report or acceptance diagnostics.

**Disposition:** `ETHEREUM_RUNTIME_COLLECTION_PASS=true`, historical Ethereum RPC root cause still `UNKNOWN`, `POSTGRESQL_768M_RESOURCE_BLOCKED=true`, and overall `PHASE7_RUNTIME_BLOCKERS_CLEARED=false`. Do not start Phase 8/9, Replay V3, finality/reorg work, ECS, AI, or live trading. The next action should be a focused PostgreSQL active-workload investigation (checkpoint/write latency and cgroup reclaim/refault behavior) without raising the cap; preserve the successful idle reclaim evidence and the counter-growth failure evidence together.

## Phase 7 Freeze Decision — 2026-09-25

```text
PHASE7_FEATURE_COMPLETE=true
PHASE7_FINAL_RUNTIME_ACCEPTANCE_DEFERRED=true
PHASE7_DATA_LAYER_HARDENING_PENDING=true
```

Phase 7 feature development is frozen. Its source-level features, persistence
contracts, and the latest bounded source observations are retained as the
Phase 7 feature baseline. This is **not** a declaration that Phase 7 final
integrated runtime acceptance passed: the PostgreSQL 768 MiB active-workload
pressure remains unresolved. Final Phase 1–8 integrated runtime acceptance is
deliberately deferred to Data Layer V1 Hardening after Phase 8 feature work.
“Deferred” does not mean the feature implementation failed, and it does not
mean runtime acceptance passed.

Latest Phase 7 evidence remains scoped to the individual gates that were
actually observed:

- Collector 256 MiB resource gate: the later 15m10 Real-D observation remained
  below the sustained stop threshold; sampled `memory.current` max was 241.1
  MiB, `memory.peak` was 247.6 MiB, and `memory.events:max/oom/oom_kill` stayed
  `0/0/0`.
- Ethereum public collection: the follow-up real windows showed cursor and
  persisted-event progression, with source health `AVAILABLE` at their final
  samples. The earlier failed window and its unclassified provider-side cause
  remain historical evidence.
- PostgreSQL 768 MiB active integrated workload: still blocked. In the
  5m10 partial retest, `memory.current` reached 767.97 MiB, cgroup
  `memory.peak` reached 768.18 MiB, `memory.events:max` rose by 161,688, and a
  checkpoint write of about 22 MiB took 39.197 seconds. OOM remained zero.
  Separate idle-quiescence tests demonstrated substantial file-cache
  reclamation, so these observations do not prove a memory leak and do not
  clear the active-workload blocker.
- Latest complete default regression in this worktree: **939 passed, 15
  skipped, 0 failed**. Skips are the documented opt-in public contract probes
  and database integration tests that require `TEST_POSTGRES_DSN`.

The Ethereum receipt fanout/progress fix and its tests were committed
separately before this documentation freeze as
`d5fdfc7` (`fix(phase7): bound Ethereum receipt fanout and report progress`);
they are not mixed into the freeze documentation commit.

The remaining cross-phase resource, scheduling, health, replay, recovery, and
soak work is tracked in `DATA_LAYER_HARDENING_BACKLOG.md`. No Phase 8–related
runtime, server, trading, AI, or production database operation is implied by
this freeze decision.
