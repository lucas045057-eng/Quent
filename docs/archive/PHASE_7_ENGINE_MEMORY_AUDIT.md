# Phase 7 Engine Memory Audit

## Scope and decision

This audit is limited to the local WSL2 runtime at
`/home/lucas045057/projects/quant`. It does not configure Bitcoin or Ethereum,
connect to ECS, start Phase 8, add credentials, or change the 384 MiB engine
limit.

The observed blocker was a high steady-state engine footprint, not a second
worker or a page-cache-only condition. The primary confirmed cause was the
engine's database hydration path materializing the complete retained Kline
history even though the runtime configuration requested only
`KLINE_FETCH_LIMIT=10` closed candles per symbol and timeframe.

## Runtime baseline before remediation

The existing `quant-engine` container used one Python PID (`PID 1`) with two
threads, six open file descriptors, and no child processes. It was stopped
gracefully after the baseline approached the cgroup ceiling; PostgreSQL and
the collector were left running.

| UTC | Container memory | VmRSS | RssAnon | RssFile | RssShmem | Threads | Tasks |
|---|---:|---:|---:|---:|---:|---:|---:|
| 05:44:37 | 382.0 MiB / 384 MiB | 406.5 MB | 384.1 MB | 22.4 MB | 0 | 2 | 2 |
| 05:45:38 | 381.0 MiB / 384 MiB | 405.6 MB | 383.2 MB | 22.4 MB | 0 | 2 | 2 |
| 05:46:40 | 383.8 MiB / 384 MiB | 410.3 MB | 387.9 MB | 22.4 MB | 0 | 2 | 2 |

`smaps_rollup` confirmed approximately 380–386 MiB of private dirty/anonymous
memory and only about 9 MiB PSS file-backed memory. Swap was approximately
24–28 MiB. This rules out multiple workers and makes a Python/object-retention
or allocator high-water footprint the relevant investigation path.

The earlier remediation observation had already shown the same process rising
from about 101 MiB to 355 MiB during the first Stage 1 cycle. The new baseline
continued to exceed 95% and approached 100%; it was not accepted as bounded
runtime capacity.

## Database hydration evidence

Read-only counts from the local `quant` database at audit time:

| Dataset | Rows |
|---|---:|
| `klines` | 169,080 |
| `market_snapshots` | 2,848,763 |
| `open_interest` | 88,162 |
| `funding_rates` | 54,346 |
| `trade_flow_windows` | 21,804 |
| `liquidation_events` | 2,766 |
| `liquidation_windows` | 1,803 |
| `phase5_market_leader_context` | 825 |

Kline distribution:

| Interval | Rows |
|---|---:|
| `5m` | 75,709 |
| `15m` | 43,785 |
| `1H` | 27,434 |
| `4H` | 22,152 |

Across `(symbol, interval)` groups the maximum retained count was 398 and the
average was about 170.4. The old `Phase1Repository.load_latest_market_batch`
query filtered by selected symbols and status but had no per-symbol/timeframe
limit, so it returned all 169,080 rows and constructed a `Candle` object for
each. The engine configuration was `KLINE_FETCH_LIMIT=10`, making this a
direct mismatch between the configured batch contract and the database read.

## Memory inventory

| Component | Long-lived structure | Bound / observed cardinality | Cleanup / lifecycle | Risk assessment |
|---|---|---:|---|---|
| Phase 1 engine | `MarketDataBatch.candles_by_symbol` | Previously all retained Klines; now latest configured rows per `(symbol, interval)` | Per cycle; batch becomes collectible after callback | **Confirmed primary cause before fix** |
| Phase 1 runtime | ticker map and closed-bar deques | `WebSocketCanonicalStore.capacity <= KLINE_FETCH_LIMIT` | Per collector process; bounded deques | Bounded |
| Phase 2 engine | `history[(canonical, exchange)]` deques | max 400 observations/key; hydration query max 4,000 rows | Fixed deque; one runtime instance | Bounded, secondary allocation |
| Phase 2 engine | cycle-local OI/funding/snapshot lists/maps | One cycle and current configured universe | Released after cycle | Bounded by cycle input |
| Phase 3 collector | trade queues, dedup maps, CVD history | Configured queue/dedup caps; CVD 1,440 minutes | TTL/maxlen and universe lifecycle | Collector-owned and bounded |
| Phase 3 engine hook | latest flow/CVD/cross-exchange context | Latest distinct rows per symbol/timeframe | Cycle-local | Bounded |
| Phase 4 collector | queues/builders/finalized/rollup state | Global event/window/bytes budgets | Queue caps, finalization, retention | Collector-owned and bounded |
| Phase 4 engine hook | latest 64 rows per metric plus latest 64 one-minute windows | At most 4 bounded query result sets per symbol | Cycle-local | Bounded |
| Phase 5 engine | `BoundedContextCache` | capacity 512 | Rebuilt per cycle | Bounded |
| Phase 5 cycle | context maps and enrichment rows | Universe × four timeframes; cycle-local | Released after persistence | Bounded, but materialized temporarily |
| Phase 6 engine | `Phase6ContextRuntime` | Created per cycle; no provider configured | No persistent runtime cache in engine | No observed retained provider state |
| Phase 6 definitions | AI TTL cache / queues | Configured only inside provider runtime; provider absent | TTL and byte/entry caps | Not active in current runtime |
| Phase 7 asset registry | canonical asset definitions | Small static registry | Immutable module data | Negligible |
| Phase 7 chain state | cursors, reorg history, backfill buffers | Adapter caps; no source configured | Bounded persistence/adapter state | Not active |
| Phase 7 spot state | spot buffers/windows | adapter and aggregation caps | Bounded | Not active |
| Health / DB writes | health payloads and pending writes | DB writes are per cycle; no unbounded engine event list found | Commit/connection scope | No evidence of retained history |

## Static audit conclusions

- The engine command is one `python -m quant_phase1.entrypoints.engine` process;
  no Uvicorn/Gunicorn worker, duplicate engine, or child worker was present.
- `PeriodicScheduler` does not overlap callbacks.
- Phase 2 history, Phase 3/4 context reads, Phase 5 cache, Phase 6 provider
  cache, and Phase 7 source buffers all have explicit bounds or are cycle-local.
- The engine did not run Phase 7 chain ingestion, so no live chain buffer can
  explain the baseline.
- The engine memory was predominantly anonymous/private memory, not a shared
  page-cache artifact.
- The direct unbounded read of retained Klines is therefore the minimal
  evidence-backed remediation target. No broad framework rewrite or semantic
  data drop is justified.

## Remediation

`load_latest_market_batch()` now accepts `candle_limit` and uses
`ROW_NUMBER() OVER (PARTITION BY symbol, interval ORDER BY bar_open_timestamp
DESC)` to load only the newest configured number of available closed candles
for each selected symbol/timeframe. The engine passes
`settings.kline_fetch_limit` explicitly. This does not alter canonical data,
retention, Stage 1 rules, or the collector's persisted history; it only bounds
the in-memory read set.

The deterministic regression test verifies the per-symbol/timeframe window
contract. A follow-up runtime image and cold-start memory observation are
required before declaring the engine remediated.
## Remediation verification

- Runtime code commit: `03dcfc17c952c1fb208add8cd29fe4b75e7d7812`.
- Image: `quant-phase7:03dcfc1-local`.
- Image ID: `sha256:b330abab6477f2a8757c0db3e2c54b8add31b50317c3f858b67f9149731f94c4`.
- Image revision label matches the full runtime code commit.
- Against the real local database, the bounded loader returned 5,160 candles
  for 200 symbols across 516 symbol/timeframe groups: maximum 10 candles per
  group with `KLINE_FETCH_LIMIT=10`.
- Cold-start / stability samples from `05:51:48Z` through `06:23:51Z` stayed
  well below the 384 MiB limit. The 30 one-minute sample set had minimum
  `48.46 MiB`, average `104.24 MiB`, peak `113.4 MiB`, and the final sampled
  value was `112.9 MiB` (`29.4%` of the limit).
- Selected samples: 0m `48.46 MiB`; 1m `54.03 MiB`; 3m `101.0 MiB`; 5m
  `101.0 MiB`; 10m `106.1 MiB`; 15m `110.0 MiB`; 30m `112.6 MiB`; final
  post-window check `112.9 MiB`.
- Final engine `RestartCount=0`, `OOMKilled=false`, two threads, no child
  process, and no observed queue/cache runaway. The 30-minute window contained
  no restart or fault injection.
- Focused repository/runtime tests passed; Phase 4/Phase 7/resource subset
  passed `383` with one PostgreSQL-dependent skip; full local regression passed
  `746` with `13` expected skips.

The engine memory blocker is remediated under the unchanged 384 MiB limit. This
does not start Phase 7 live chain/spot gates, configure providers, or constitute
full Phase 7 runtime acceptance.

