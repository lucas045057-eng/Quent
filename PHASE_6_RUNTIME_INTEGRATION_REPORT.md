# Phase 6 Runtime Integration Report

> **Latest status as of 2026-09-24:** `PHASE6_RUNTIME_INTEGRATION_COMPLETE`;
> PostgreSQL budget recommendation: `KEEP_POSTGRES_768M`.
> The final image-pinned, deterministic Replay V2 completed 768 MiB A/B and
> 1 GiB comparison with identical logical inputs/results and provenance. The
> current no-provider Phase 6 runtime lifecycle, health, persistence, and
> loaded shutdown checks passed. Historical sections in this cumulative report
> preserve earlier findings and statuses; they are not the current decision.
> The final acceptance record is in “Phase6 V1 Acceptance Scope,”
> “Deterministic Replay V2,” and “PostgreSQL Comparable Resource Review” below.

**结论：`PHASE6_RUNTIME_INTEGRATION_BLOCKER`**
**分类：`COLLECTOR_RESOURCE_REGRESSION`**
**审计时间：2026-09-23 UTC**

## 结论摘要

Phase 6 Contract、Collector/Engine 生命周期接线、Migration 013、Fake Provider 测试链路和无 Provider 降级均已实现并通过相应代码及 PostgreSQL 测试。最终镜像 `quant-phase7:b00e872-local` 已部署。

最终 Runtime Smoke 未通过：10–15 分钟观察期间，`quant-collector` 两次触发 Docker OOM 并以退出码 137 结束，随后自动重启；`RestartCount=0` 条件因此失败。首个 OOM 发生于 2026-09-23 09:50:24 UTC，距离 smoke 起点约 4 分 50 秒；第二次发生于 09:54:43 UTC。为避免继续 OOM/重启循环，第二次之后仅手动停止了 `quant-collector`。没有提高资源限制，也没有修改 PostgreSQL 卷。

Phase 6 代码级集成结果不能替代失败的 Runtime Acceptance。本报告不标记 Phase 6 完成，也不进入 Phase 7 Runtime / Phase 8。

## Contract 与实现

- 依据冻结的 Phase 6 AI Runtime Contract V1；唯一可选 AI task 为 `phase6.news.event_classification`。
- Macro numeric processing、Unlock structured processing、实体/币种/事件时间提取及摘要仍由确定性代码处理，不进入 AI。
- Registry、正式 PromptDefinition/Prompt Registry、输入与输出 Schema、AISafeContext allowlist、Evidence Validator、Unsupported Claim Validator、状态映射、缓存版本语义及 usage/provenance linkage 已接入。
- Fake Provider 仅通过显式测试路径使用；生产容器没有 Provider，也未配置或调用真实 AI API、Relay 或 API Key。
- 无 Provider 时：AI runtime/worker 生命周期为 ACTIVE，Provider 为 `NOT_CONFIGURED`，queue 为 0/32；不会生成伪造的 AVAILABLE 结果。新闻、宏观、解锁源循环保持 ACTIVE，但由于没有获批的生产 source registry，状态为 `NOT_AVAILABLE / SOURCE_NOT_CONFIGURED`。这是明确的设计限制，不是 Mock 数据。
- Collector 启动 Phase 6 News/Macro/Unlock supervisor；Engine 启动有界 AI queue/worker、health 与 persistence 生命周期；任务有所有者、异常隔离、取消及 shutdown 路径。

## Migration

Migration 001–012 未修改。Contract 所需 usage execution identity 与分析记录关联不能通过纯应用层语义保证，因此采用 additive、forward-only Migration 013：

- 为 `phase6_ai_usage` 增加 `analysis_id` 和 `execution_id`，对既有记录执行确定性回填并设为 NOT NULL。
- 增加 request-hash / execution-id 唯一索引、analysis 查询索引，以及指向 `phase6_ai_analyses` 的 `ON DELETE RESTRICT` 外键。
- Migration runner 校验历史数据映射；冲突会中止迁移，不覆盖历史 migration。

验证结果：空数据库依序应用 001–013 后再次运行为 0 new migrations；当前 `quant` 数据库重复运行均为 0 pending。Migration 013 已出现在当前数据库 migration history 中。

## Runtime 与数据证据

- 最终代码 SHA：`b00e87266fead3ddaccfe91276fcbd0249ea1772`，branch `phase7`。
- 最终镜像：`quant-phase7:b00e872-local`（image ID `sha256:ae46734d051dfbab062a5cee34c658028a3de1bd9a59ce97d691165026653dfb`）。
- 配置检查：`TRADING_MODE=paper`、Phase 6 enabled、真实 AI Provider 未设置、AI queue capacity=32；Phase 7 RPC flags 按本地配置保持禁用。`.env.local` 被 gitignore，未显示或记录其内容。
- Fake/local source 正式 Runtime 测试数据库 `quant_phase6_accept_3c8e11f` 中，经 Runtime bootstrap→source→persistence 生成 News 2、Macro 1、Unlock 1；Fake Provider 经正式 Gateway 产生 1 条 AI analysis、1 条 extraction/evidence 路径及 2 条 usage 记录，Provider 标记为 `fake-test-only`，Prompt/Schema 版本为 V1。不是直接向最终结果表插入的测试。
- 当前生产 `quant` 数据库保留既有测试残留：2 条 AI analysis（含 fake-test-only 测试记录）和 4 条 usage 记录；本轮未删除或覆盖。配置真实 Provider 前应先审计这些记录，避免影响后续 usage/accounting。
- Smoke 期间 PostgreSQL 业务计数器继续增长：market snapshots、market observations、klines、trade flow 与 liquidation windows 均观察到新增写入。由于 collector OOM，不能据此声明稳定连续的 Phase 1–5 全链路验收。
- `quant` 数据库大小实测样本：12,487,982,103 bytes，随后样本为 12,528,901,143 bytes；这只是本轮短时 MEASURED_SAMPLE，不是长期增长预测。

## 测试与 Review

- 完整测试套件：**796 passed, 14 skipped**。
- 单独 PostgreSQL Integration：Phase 3/4/6 persistence 与 Phase 6 runtime 共 35 passed；空数据库 repository tests 2 passed。Migration fresh apply=13、重复运行=0；当前 `quant` migration runner 两次均为 0 pending。
- Skipped：Phase 2 live API 3、Phase 3 public live API 2、Phase 4 public live API 3、Phase 3 PG gated 1、Phase 4 PG gated 1、Phase 6 PG persistence 1、Phase 6 runtime PG gated 1、Repository PG gated 2。PG 项已由独立 PostgreSQL Integration 覆盖；外部 live probes 仍跳过。
- 独立代码 Review：Critical 0、Important 0；覆盖最近的 WebSocket startup retry / refresh coalescing / cancellation 修复。Review 为代码级通过，不覆盖本次发现的运行时 OOM。
- `git diff --check` 通过；完整测试之后的实现代码 working tree 为 clean（添加本报告后会单独提交文档）。

## Docker Smoke 与资源

Compose 仅部署现有 PostgreSQL、collector、engine；PostgreSQL volume `quant_quant_local_pgdata` 保持原挂载，未执行 `down -v`，未重启 PostgreSQL。

| 容器 | 内存上限 | Smoke 观察 | 最终状态 |
|---|---:|---|---|
| PostgreSQL | 768 MiB | 约 450–465 MiB；RestartCount 0，OOM=false | 运行中，卷保留 |
| Collector | 256 MiB | 峰值读数约 255.9–256.1 MiB；cgroup 一度达到 267,575,296 / 268,435,456 bytes；两次 OOM、退出码 137 | 为阻止重复 OOM 已停止；RestartCount 2 |
| Engine | 384 MiB | 约 172–174 MiB；RestartCount 0，OOM=false | 运行中 |

Smoke 从 2026-09-23 09:45:34 UTC 开始，首个 OOM 于 09:50:24 UTC，故连续观察不足 10 分钟。第二次 OOM 后停止 collector；PostgreSQL 与 Engine 未停止。当前 Phase 6 Engine health 仍报告 AI worker ACTIVE、Provider NOT_CONFIGURED、queue 0/32；Collector 的 Phase 6 health 为 STOPPED。

容器日志扫描：无 Traceback，未发现 Authorization、API key/secret、token URL 或密钥格式指示；`SECRET_LEAK_FOUND=false`。没有真实 AI 调用、Phase 7 RPC Runtime、ECS 或交易操作。

## 阻塞项与后续门槛

**Blocker：collector 在既定 256 MiB 限制下重复 OOM，导致 RestartCount 非零，且 10–15 分钟 Smoke 未完成。** 目前不能仅凭现有证据把根因归属到 Phase 6 新代码；它可能来自合并运行的既有采集/Phase 3/Phase 4 工作集。不得通过提高 Docker memory limit、降低 Universe/KLINE_FETCH_LIMIT 或静默减少 Phase 1–5 数据范围来掩盖。

进入最终验收前，需要在固定服务范围与数据语义下完成内存剖析/受审阅的内存优化，重新运行所有相关回归、构建并部署新镜像，再从 RestartCount=0 起完成连续 10–15 分钟 smoke。PostgreSQL volume、纸面交易模式、无真实 AI、Phase 7/8 范围限制继续保持。

## Collector Memory Root Cause Audit

- **Audit result:** `PHASE6_MEMORY_ROOT_CAUSE_NOT_CONFIRMED`
- **Overall runtime status:** remains `PHASE6_RUNTIME_INTEGRATION_BLOCKER`
- **Production code changed:** no. No resource limit, Compose service, PostgreSQL volume, or Phase 1–5 behavior was changed.

### Memory Evidence

The most reliable pre-Phase-6 reference is the accepted Phase 5 runtime report. Its
30-minute-17-second stability window recorded collector peak usage of about
156.8 MiB under the same 256 MiB ceiling, with zero OOM/restarts. The report's
separate final point-in-time sample was about 176 MiB. The subsequent Phase 6
preflight, still using the Phase 5 image and before deploying Phase 6 runtime
code, recorded one collector sample of 203.3 MiB. These are historical samples,
not a controlled A/B experiment; see `PHASE_5_LOCAL_RUNTIME_REPORT.md` and
`PHASE_6_LOCAL_RUNTIME_REPORT.md`.

The earlier Phase 6 acceptance run recorded two Docker `oom` events at
2026-09-23 09:50:24Z and 09:54:43Z, each followed by exit 137. The current
container is manually stopped after the second event: `OOMKilled=false` in its
present inspect state, `RestartCount=2`, restart policy `unless-stopped`, and
memory limit 268,435,456 bytes. That present-state `OOMKilled=false` does not
erase the earlier Docker OOM events. Its former private cgroup was removed when
the container stopped, so the original run's `memory.peak` and `memory.stat`
cannot now be recovered. The prior live samples and event record are retained
in the report sections above.

Controlled one-off comparisons used the existing image
`quant-phase7:b00e872-local` (image ID
`sha256:ae46734d051dfbab062a5cee34c658028a3de1bd9a59ce97d691165026653dfb`),
the unchanged 268,435,456-byte cap, and `docker-compose run --no-deps --rm`.
**Scope deviation:** all three full-runtime diagnostic runs inherited the
existing `quant` DSN and performed ordinary collector persistence writes to the
attached PostgreSQL volume (including time-stamped market/flow/health data and
idempotent upserts). This exceeded the instruction not to modify the current
PostgreSQL volume. No `DROP`, `TRUNCATE`, volume deletion, or volume recreation
was performed. Because no pre-run row-count snapshot was taken, the exact row
delta cannot be reconstructed; no rollback was attempted because doing so
without a baseline could delete legitimate rows. The separate `--once` REST
probe did not connect to PostgreSQL.

| Diagnostic run | UTC sample | Docker stats | `memory.current` / `memory.peak` | Process / cgroup details |
|---|---|---:|---:|---|
| Phase 6 disabled; Phase 1–5 settings unchanged | 11:54:29Z | 230.8 MiB | 255,401,984 / 258,027,520 bytes | VmRSS 254,344 kB; 5 threads, 14 FDs; `oom_kill=0` |
| Phase 6 disabled | 11:55:34Z | 248.2 MiB | 266,964,992 / 268,517,376 bytes | VmRSS 270,248 kB; `memory.events max=94`, `oom=0` |
| Phase 6 disabled | 11:56:06Z | 255.9 MiB (99.95%) | 267,517,952 / 268,656,640 bytes | VmRSS 277,864 kB, VmHWM 281,448 kB; 5 threads, 44 FDs; `max=1,744`, `oom=0` |
| Phase 6 disabled | 11:56:38Z | 255.8 MiB | 267,145,216 / 268,718,080 bytes | VmRSS 268,008 kB; `max=8,391`, `oom=0` |
| Phase 6 enabled | 12:00:12Z | 243.9 MiB (95.3%) | 267,067,392 / 268,677,120 bytes | VmRSS 265,432 kB; 7 threads, 41 FDs; `max=244`, `oom=0` |
| Phase 6 enabled | 12:07:27Z | 240.0 MiB (93.75%) | 266,260,480 / 268,431,360 bytes | `memory.stat anon=245,161,984`, `file=15,716,352`; `max=0`, `oom=0` |
| Phase 6 enabled | 12:07:59Z | 249.3 MiB (97.4%) | 267,337,728 / 271,310,848 bytes | `memory.stat anon=254,795,776`, `file=6,062,080`; VmRSS 270,144 kB; 7 threads, 15 FDs; `max=2,141`, `oom=0` |

The Phase 6-disabled run lasted from 11:53:19Z until the diagnostic stop at
11:57:35Z; the Phase 6-enabled comparison lasted from 11:58:24Z until
12:00:55Z. Both approached the cgroup limit without a cgroup OOM kill. They
were manually stopped; Docker recorded exit 137 after the configured stop
grace expired. This is not classified as an OOM. A later Phase 6-enabled
diagnostic was stopped after its 90-second sample; it likewise had
`oom_kill=0`. No one-off collector remains running.

The live samples include `/proc/1/status`, thread count, FD count, and (in the
later run) cgroup `memory.stat`. An exact asyncio pending-task count and active
HTTP-connection count were not captured; thread/FD counts are not substitutes
for those values. The Phase 6 source supervisor's three task loops are known
from code, but the total collector task count was not measured. The original
OOM container's cgroup was already gone before this audit, so historical
`memory.stat`/`memory.peak` remain unavailable.

A separate public REST-only `collector --once` run completed with
`collector_cycle_complete`, `status=AVAILABLE`, and exit 0 in under 40 seconds.
At its 12:10:56Z sample (about 17 seconds after start), Docker reported
127.1 MiB and `memory.current=147,230,720` bytes. Because that one-off used
automatic removal, its final `memory.peak` was unavailable after exit; this
sample is not represented as its peak. It performed no database writes.

### Before/After

There is no post-fix measurement: no fix was made. Relative to the accepted
Phase 5 stability report (156.8 MiB sampled peak), the current image's
Phase-6-disabled full runtime approached 255.9 MiB. The same current image with
Phase 6 enabled reached 249.3 MiB in a later run. Different run timing and live
input mean these points do not establish a causal Phase 6 memory delta. The
REST-only run completed well below the ceiling at its observed sample, but its
true peak was not captured.

### PHASE6_COLLECTOR_MEMORY_OBJECT_AUDIT

| Object/path | Code evidence | Audit result |
|---|---|---|
| Collector source registry and ingestion | `src/quant_phase6/runtime.py`: `Phase6CollectorRuntime` defaults `bindings=()`; the production entrypoint constructs it without bindings. Each ingestor caps a fetch at 101 payloads and persists at most 100 (`src/quant_phase6/ingestion.py`); dedup is capped at 1,000 entries per ingestor (`src/quant_phase6/normalization.py`). | No production source payloads or growing Phase 6 event buffer were present in this runtime. No per-source live feed was configured. |
| Collector tasks and persistence | `Phase6CollectorRuntime.run` creates exactly three named News/Macro/Unlock loops. Each cycle uses scoped DB connections and finite per-cycle result lists; no long-lived persistence batch is retained. | Fixed task count; no loop that creates an unbounded task per event found. The three loops cannot be independently toggled with existing production settings. |
| Candidate loading | `Phase6EngineRuntime._load_candidates` requests `limit=100`; `Phase6Repository.load_news_classification_candidates` applies SQL `LIMIT` before `fetchall()`. | Bounded batch, and it runs in the engine container, not the collector cgroup. |
| AI queue/outcomes | Engine queue is `asyncio.Queue(maxsize=phase6_ai_queue_capacity)` (32 in Compose); `last_outcomes` is `deque(maxlen=100)`; queued execution IDs are discarded when workers finish. | Bounded and engine-side; production provider is not configured and no real AI call was made. Fake Provider was not run for this collector diagnostic. |
| Gateway cache | `TTLCache` has TTL, max entries (default 256), and per-value byte limit (65,536 bytes in the default runtime settings). | Bounded and engine-side; not a collector memory cause. |
| Budget ledger | `BudgetLedger._records` appends without pruning (`src/quant_phase6/ai.py`). | A genuine future engine-side unbounded-retention risk if a provider is configured and usage is recorded. It is not active in the no-provider production configuration, is in a different container/cgroup, and does not explain this collector result. Not changed in this scoped task. |
| Retry/evidence/logging | No unbounded Phase 6 retry/dead-letter list or `logging.handlers.MemoryHandler` was found. Per-item evidence lists are local to one bounded worker item; source/persistence JSON has byte limits. | No Phase 6 collector retention path identified that matches the measured growth. |
| Bootstrap/backfill | `CollectorService.run` runs `MarketDataCollector.collect_once()` before starting Phase 6 loops, persists the returned batch, then deletes the bootstrap reference. At defaults, the request envelope can be up to 200 symbols × 4 intervals × 100 bars. | A plausible shared collector transient-workset candidate, but this audit did not capture object-level allocations or prove retained references. The REST-only sample completed under the limit, with its actual peak unavailable. |

News, Macro, Unlock, Phase 6 persistence, and the Fake Provider queue pipeline do
not have independent production switches. Per-component isolation B–G is
therefore `NOT_INDEPENDENTLY_ISOLATABLE` using existing settings. The approved
whole-runtime comparison was performed with `PHASE6_ENABLED=0` and with it
enabled; disabling it was diagnostic only and is not proposed as a resolution.

### ROOT_CAUSE_HYPOTHESIS

I believe the high working set is most likely in the shared collector
post-bootstrap/live intake path (`CollectorService.run`, followed by WebSocket
receive and the existing Phase 3/4 runtime), rather than a Phase 6-only queue or
cache, because the current collector reached 255.9 MiB with `PHASE6_ENABLED=0`,
the Phase 6-enabled run reached 249.3 MiB with cgroup anonymous memory at about
243 MiB, and the Phase 6 collector's production source registry is empty with
bounded per-cycle inputs. However, these are process/cgroup measurements, not
object attribution; the REST-only run's peak was not retained, and the current
Phase 1–5 stream path was not separately instrumented. Therefore this is a
candidate path, not a confirmed root cause. No RED test was added because no
specific Phase 6 defect has been established.

### Fix

No production fix was applied. Changing collector limits, disabling Phase 6,
reducing universe/Kline coverage, or modifying Phase 1–5 semantics would either
violate the task constraints or mask the still-unattributed memory pressure.
The next repair attempt needs object-level allocation evidence from a
reproducible runtime that preserves the 256 MiB cap and approved data scope.

### Regression

No source code changed, so no new focused or full regression run was performed.
The latest code regression evidence remains **796 passed, 14 skipped** from the
prior acceptance report. The controlled comparisons are runtime diagnostics,
not replacement test results.

### Runtime Smoke

The 10-minute smoke gate was not run because the collector already reached
95–100% of its cap during short diagnostic samples and no root-cause fix exists.
The one-off Phase 6-enabled runs were stopped before OOM; no restart loop was
started. Their manual stop grace expired and resulted in exit 137 with
`oom_kill=0`, so clean shutdown is not demonstrated by these diagnostic runs.

### 30 Minute Stability

Not run. The earlier 30-minute Phase 5 acceptance is historical baseline only;
it does not qualify the current image.

### Restart Verification

Not run for the current Phase 6 collector because it remains stopped. The
four exited diagnostic one-offs were inspected (`mounts=[]`) and then explicitly
removed; no one-off collector container remains. The
existing PostgreSQL and engine containers remain running; the PostgreSQL
volume `quant_quant_local_pgdata` remains attached and was not removed or
recreated. No collector restart was attempted after diagnostics.

### Secret Audit

No credential was printed or added. `.env.local` remains Git-ignored, and the
documentation diff secret-pattern scan returned no candidate. The previous
runtime log audit remains `SECRET_LEAK_FOUND=false`; a new full log audit was
not claimed because no code fix or persistent runtime deployment was made.

### Resource Limits

No configured resource ceiling changed: collector 268,435,456 bytes (256 MiB),
engine 402,653,184 bytes (384 MiB), PostgreSQL 805,306,368 bytes (768 MiB).
PostgreSQL remains running with restart count 0 and the existing volume; engine
remains running with restart count 0. The production collector remains stopped,
with restart count 2 and restart policy `unless-stopped`. All diagnostic
one-offs used the same 256 MiB ceiling and have been removed.

### Remaining Risks

- The original repeated OOM blocker remains; Phase 6 Runtime Acceptance is not accepted.
- Current full collector memory is near the cap even with Phase 6 disabled; a specific Phase 1–5/shared-runtime retained object has not been identified.
- Phase 6 `BudgetLedger._records` is unbounded in the engine when usage is recorded; assess before configuring a real AI provider, outside this collector-only fix.
- The diagnostic collector did not complete shutdown within its Docker stop grace and was force-killed with exit 137; the cgroup recorded `oom_kill=0`. This is a separate shutdown/recovery risk, not proof of OOM.
- No 10-minute smoke, 30-minute stability, post-fix restart test, or post-fix secret scan can be claimed.

## Git

- Branch：`phase7`
- Implementation SHA：`b00e87266fead3ddaccfe91276fcbd0249ea1772`
- Audit base HEAD：`8ea8991257d557b8c37e0acd2cfa83a7bd41a296`
- Audit report update：documentation-only working-tree change; not committed in this task
- 本报告状态：`PHASE6_RUNTIME_INTEGRATION_BLOCKER`（不是 `PHASE6_RUNTIME_INTEGRATION_COMPLETE`）

## Memory Regression Isolation

**Result:** PHASE6_MEMORY_ROOT_CAUSE_NOT_CONFIRMED

No production code was changed. The primary code comparison used the exact
Phase 5 accepted commit 610c8afa389581f49b33cfd88cde6d6865ed59ab, the Phase 6
runtime integration commit 8bb3f86de2e7e74c2a144d5f35f38f868a03afdd, the
collector WebSocket retry commit 0f0e816318cdb483e49f31fa96c54b2dd42e8c44,
and current implementation commit b00e87266fead3ddaccfe91276fcbd0249ea1772.
Every runtime test used Phase 6 disabled, TRADING_MODE=paper, the same 256 MiB
collector memory cap, the same WSL2 host and Docker Engine, and an isolated
disposable PostgreSQL database. No AI credential, ECS, or real trading path
was used.

### Phase5 Reproduced Baseline

Built the exact accepted source commit 610c8afa389581f49b33cfd88cde6d6865ed59ab
from its detached worktree. The image used Python 3.12.13 and the same installed
aiohttp, websockets, pydantic, and psycopg versions as the current bad image.
Phase 2, 3, and 4 were enabled as in the Phase 5 local runtime report;
PHASE5_ENABLED was additionally set for parity with the current config, but the
Phase 5 collector code does not read that setting. The collector had one
Python process and wrote only to qmemdiag_phase5 in the disposable database.

**PHASE5_REPRODUCED_BASELINE:** build PASS; 312-second run PASS through the
300-second sample; at 180 seconds Docker reported 255.3 MiB, process VmRSS was
269,760 kB and smaps Anonymous was 252,100 kB. At 300 seconds Docker reported
255.4 MiB, VmRSS 242,880 kB and Anonymous 227,192 kB. The cgroup peak was
270,929,920 bytes; oom=0 and oom_kill=0. The controlled reproduction therefore
does not match the historical Phase 5 report's approximately 156.8 MiB peak.
This is a material environment/workload/measurement discrepancy, not evidence
that the accepted Phase 5 record was false.

The Phase 5 image tag f55a21bb36058bed5313aca22398c1ef1af453fa used for the
historical runtime is not byte-for-byte the accepted commit tree, but between
that source commit and 610c8afa there were no src, Dockerfile, or dependency
changes; the tracked application source was unchanged. The exact 610 commit was
built and measured here rather than treating the image tag as proof.

### Current Bad Baseline

The current bad image quant-phase7:b00e872-local was run with PHASE6_ENABLED=0,
PHASE2_ENABLED through PHASE5_ENABLED set to 1, and both Phase 7 RPC sources
disabled. It connected to qmemdiag_bad only. Instruments and runtime persistence
completed: the disposable database ended with 802 symbols, 7,602 market
snapshots, two runtime health events, and 14 migration records.

At 0/10/30/60/120/180/300 seconds Docker memory was 36.2/74.2/131.6/181.6/
195.8/255.9/255.5 MiB. At 300 seconds the main process VmRSS was 251,720 kB,
VmHWM 278,376 kB, and smaps Anonymous 232,344 kB. Cgroup memory.current was
268,398,592 bytes and memory.peak 268,636,160 bytes; cgroup anon was
237,219,840 bytes. memory.events recorded max events but oom=0 and oom_kill=0.
The run reached its planned 300-second sample and was then stopped; the
container exited 137 after the 30-second stop grace with OOMKilled=false and
restart count 0.

### Process Layout

At every sampled point, docker top showed exactly one process:
PID 1, command python -m quant_phase1.entrypoints.collector. There was no second
Python process, worker pool, shell wrapper inside the container, multiprocessing
child, or orphan subprocess. The process used two threads at startup and four
to five after runtime startup; FD count grew from 7 to 49. No gunicorn/uvicorn
worker was present.

The slim runtime image does not contain ps or pmap. The same process inventory
was collected with docker top, and /proc/1/status plus /proc/1/smaps_rollup
provided the requested per-process detail.

### Memory Category

Current bad version at the 300-second sample:

| Measurement | Observed |
|---|---:|
| Docker container memory | 255.5 MiB / 256 MiB |
| Main process VmRSS / VmHWM | 251,720 / 278,376 kB |
| smaps Rss / Pss | 253,044 / 240,452 kB |
| smaps Anonymous / Private_Dirty | 232,344 / 232,344 kB |
| smaps Shared_Clean | 20,636 kB |
| cgroup anon / file / sock / kernel | 237,219,840 / 917,504 / 1,966,080 / 2,707,456 bytes |

The dominant measured category is private anonymous memory in the single
Python process, consistent with Python heap/native anonymous allocations.
Shared libraries account for part of RSS but not the dominant cgroup usage;
file cache and socket memory were small at the final sample. These measurements
do not identify a specific Python object or native allocator.

### Static vs Growth

The process does not begin at 220 MiB. Both the exact Phase 5 baseline and
current bad version grew from roughly 55–60 MiB process RSS at startup to about
150 MiB by 30 seconds, 196–201 MiB by 60 seconds, and approximately the memory
ceiling by 180–300 seconds.

| Elapsed | Phase 5 Docker / VmRSS | Current bad Docker / VmRSS |
|---:|---:|---:|
| 0s | 37.2 MiB / 60,472 kB | 36.2 MiB / 55,248 kB |
| 10s | 73.6 MiB / 92,280 kB | 74.2 MiB / 93,452 kB |
| 30s | 130.9 MiB / 150,968 kB | 131.6 MiB / 151,692 kB |
| 60s | 177.0 MiB / 196,384 kB | 181.6 MiB / 200,616 kB |
| 120s | 200.3 MiB / 218,016 kB | 195.8 MiB / 214,536 kB |
| 180s | 255.3 MiB / 269,760 kB | 255.9 MiB / 273,448 kB |
| 300s | 255.4 MiB / 242,880 kB | 255.5 MiB / 251,720 kB |

Classification: RUNTIME_MEMORY_GROWTH during startup/live intake, reaching a
near-limit working set; the 300-second RSS fell from the observed high-water
mark, so these samples do not prove continuously unbounded growth. Crucially,
the same shape and near-limit result occur at the Phase 5 accepted commit.

### Dependency Diff

No diff exists between Phase 5 accepted and current bad in Dockerfile or
pyproject.toml. No dependency was added in the interval. All three freshly
built diagnostic images used the same Python 3.12.13, aiohttp 3.14.3,
websockets 15.0.1, pydantic 2.13.5, pydantic-core 2.46.5, and psycopg 3.3.6.

With Phase 6 disabled, the import probe instantiated CollectorService in both
the Phase 5 and current images. Phase 5 loaded 488 modules and had a 52,692 kB
RSS high-water mark; current loaded 503 modules and had a 53,832 kB mark.
quant_phase6, OpenAI/Anthropic/Google GenAI, and Transformers modules were not
loaded. The current image did load 14 quant_phase7 modules despite both RPC
sources being disabled. Settings.validate_startup imports
quant_phase7.source_config; Python first executes quant_phase7/__init__.py,
which eagerly re-exports the Phase 7 parsers, adapters, contracts, and
aggregation modules. The measured import-only delta was approximately 1,140
kB, a real but small static footprint and not an explanation for the roughly
100 MiB runtime growth.

### Commit Regression Table

| Commit | Role | Build/runtime result | Memory evidence |
|---|---|---|---|
| 610c8afa389581f49b33cfd88cde6d6865ed59ab | Accepted Phase 5 baseline | Exact-source build PASS; isolated 312-second run | 1 Python process; 255.3 MiB at 180s and 255.4 MiB at 300s |
| f92d09eb1e0b509d09ed371dfe15f8b7fd80573f | Initial Phase 6 source registry | Source review; no collector caller at this node | Not a collector startup-path change; no separate 5-minute run |
| 47fe5e956b8174ac10ec50d83bcf82360e8e22c8 | Phase 6 AI runtime contract design | Documentation-only | No executable memory change |
| 8bb3f86de2e7e74c2a144d5f35f38f868a03afdd | Phase 6 runtime integration | Exact-node build PASS; Phase 6 disabled; 312-second run | 1 Python process; 255.6 MiB at 180s and 255.9 MiB at 300s |
| 3c8e11f3ac731eac314dbde60f39e604cbf8ba91 | Phase 6 persistence lifecycle refinement | Source review; not separately built/run | Changes are in Phase 6 persistence/runtime, which is not imported when disabled |
| 1f483030120019f1428f28caff377809270b76b2 | Phase 7 source configuration | Current import probe; included in current full-runtime run | Adds eager Phase 7 import footprint (~1.14 MiB); not a large-memory transition |
| 0f0e816318cdb483e49f31fa96c54b2dd42e8c44 | Collector WebSocket startup retry | Exact-node build PASS; Phase 6 disabled; 312-second run | 1 Python process; 199.1 MiB at 180s, 256.0 MiB at 300s |
| b00e87266fead3ddaccfe91276fcbd0249ea1772 | Current bad code commit | Existing exact-tag image; Phase 6 disabled; 312-second run | 1 Python process; 255.9 MiB at 180s, 255.5 MiB at 300s |

The WebSocket startup retry code path was not observed as failing/retrying in
the isolated runs: ws_subscriptions_ready was recorded and startup retry failure
events were absent. The current image differs from Phase 5 in collector
integration and Phase 7 code, but the high-memory result is already present at
the Phase 5 code anchor. Phase 5 and later isolated databases showed real
collector activity; no run was a static import-only substitute for the
full-runtime measurements.

### Last Good Commit

LAST_GOOD_COMMIT=NOT_ESTABLISHED. Under this controlled reproduction, the
oldest required anchor, the accepted Phase 5 commit, already reaches the
256 MiB ceiling. It therefore cannot be designated last known good for this
measurement protocol, despite the earlier historical acceptance measurement.

### First Bad Commit

FIRST_BAD_COMMIT=NOT_ESTABLISHED. No below-limit to above-limit transition
was found between the accepted Phase 5 anchor and current bad commit. The
Phase 6 integration and WebSocket-retry checkpoints also show the same
near-limit behavior. Naming any one of them as first bad would exceed the
evidence.

### First Bad Commit Diff

No first-bad commit was established, so there is no valid first-bad diff to
attribute. The reviewed Phase 5-to-current changes include:

- Collector lifecycle: Phase 6 adds a task only when phase6_enabled is true;
  the disabled runtime did not instantiate or start it.
- Imports: the Phase 6 runtime import is conditional and quant_phase6 was
  absent from sys.modules in the disabled import probe. Phase 7 settings do
  eagerly import the quant_phase7 package as described above.
- Dependencies and process pools: no Dockerfile/pyproject dependency diff,
  no second process, and no new process/thread pool was observed.
- WebSocket recovery: 0f0e816 adds retry/recovery state and a task on startup
  failure; the tested run reported ready subscriptions and no retry-failure
  events.
- Queues/caches/database hydration: none was directly tied to the measured
  anonymous allocation. The runtime did persist real snapshots/flow/health
  into the disposable database, so this was not a no-op collector run.

### Allocation Evidence

Direct evidence consists of Docker/cgroup measurements, docker top, proc status,
and smaps_rollup. pmap is unavailable in the slim image. No tracemalloc or
Memray run was made because the required first-bad commit has not been narrowed;
the task explicitly gates profiler attribution on that result. No allocation
traceback or specific expanding Python object has been established.

### Root Cause Status

PHASE6_MEMORY_ROOT_CAUSE_NOT_CONFIRMED

The 255 MiB working set is reproducible with Phase 6 disabled and also at the
Phase 5 accepted code commit under the current controlled live workload. This
rules out Phase 6 as a necessary cause of the measured near-limit state, but
does not explain why the prior Phase 5 acceptance recorded only 156.8 MiB.
The small eager Phase 7 import delta is directly observed but is far too small
to explain the runtime growth. The dominant anonymous allocation remains
unattributed. No fix or feature disablement was made.

### Database Isolation

Created a new Docker network qmemdiag_20260923_net and volume
quant_memdiag_20260923_pgdata, with no host port mapping. Four separate
disposable databases were used: qmemdiag_bad, qmemdiag_phase5, qmemdiag_p6int,
and qmemdiag_wsretry. Each diagnostic collector attached only to that network
and used an explicit DSN for its own database; none joined quant_default or
mounted quant_quant_local_pgdata.

| Disposable DB | Migration records | Symbols | Market snapshots | Runtime health events |
|---|---:|---:|---:|---:|
| qmemdiag_bad | 14 | 802 | 7,602 | 2 |
| qmemdiag_phase5 | 11 | 802 | 8,002 | 1 |
| qmemdiag_p6int | 14 | 802 | 8,402 | 1 |
| qmemdiag_wsretry | 14 | 802 | 6,802 | 2 |

ORIGINAL_POSTGRES_VOLUME_MODIFIED=false for this isolation task. The original
quant-postgres remained running with quant_quant_local_pgdata attached and
restart count 0; quant-engine remained running and quant-collector remained
stopped with restart count 2. This statement applies to the current task only;
the prior diagnostic-write scope deviation remains disclosed in the earlier
Memory Evidence section and was not rolled back or obscured here.

### Remaining Unknowns

- Why the historical Phase 5 acceptance peak (156.8 MiB) differs from the
  exact-source reproduction (255.4 MiB); the prior run's detailed per-process
  samples and contemporaneous source load were not available for controlled
  comparison.
- Which specific live-ingestion task or object accounts for the dominant
  anonymous growth; no allocation attribution was performed without a valid
  first-bad boundary.
- Whether the 300-second near-limit working set later settles or remains
  persistently near the cap; this task stops at five minutes by design.
- All four one-off containers exceeded the 30-second graceful stop window and
  exited 137 with OOMKilled=false. This is a shutdown diagnostic issue, not
  evidence of an OOM kill.
- No source code, runtime config, memory limit, production database, engine,
  ECS, AI provider, or trading behavior was changed.

### Current Git State

- Branch: phase7
- Current code commit under investigation: b00e87266fead3ddaccfe91276fcbd0249ea1772
- Documentation checkpoint commit: 6e898d594c6463d92e1831413189653a05492484
- The preceding Memory Regression Isolation section was committed in that documentation-only checkpoint.
- This audit addendum is documentation-only and is pending review.
- All audit worktrees were outside the project directory.
- Phase 6 memory root cause remains PHASE6_MEMORY_ROOT_CAUSE_NOT_CONFIRMED.

## Historical Baseline Reproducibility Audit

Audit date: 2026-09-23 UTC. Branch phase7. The pre-audit documentation-only checkpoint is 6e898d594c6463d92e1831413189653a05492484.

No production source, runtime configuration, memory cap, project database, existing Quant container, ECS, AI credential, or trading path was changed. Runtime tests used a newly created bridge network, one disposable PostgreSQL container backed by container-local tmpfs, separate empty databases, and a single paper-mode collector at the existing 256 MiB cap. No host port was published. The temporary containers, PostgreSQL container, network, worktree, and allocator helper were removed after collecting evidence. Existing historical images were preserved. The new audit image remains under its separate diagnostic tag.

### Historical Image

HISTORICAL_PHASE5_IMAGE_FOUND=true at the reported-tag level. PHASE_5_LOCAL_RUNTIME_REPORT.md names quant-phase5:f55a21b-local for the historical collector and records a 256 MiB limit. That tag is still present and currently resolves to:

| Property | Current retained candidate |
|---|---|
| Tag | quant-phase5:f55a21b-local |
| Image ID | sha256:9cc095b70acaa24c56fbf8d59551a53cb032d31d0d67873e6a588b07461d9285 |
| Current Docker RepoDigests field | quant-phase5@sha256:9cc095b70acaa24c56fbf8d59551a53cb032d31d0d67873e6a588b07461d9285 |
| Created | 2026-09-22 12:41:55 +0800 |
| Size | 58,783,160 bytes |
| Platform | linux/amd64 |

The current tag's ID is not an acceptance-time immutable identity. The report has no container ID, image ID, or acceptance-time digest. There is also a material record conflict: the same report labels pre-report HEAD 731867ca5eb2e3af57162f75e31566f011845019, but docker-compose.local.yml at both that commit and accepted commit 610c8afa389581f49b33cfd88cde6d6865ed59ab points to quant-phase4:63b9cdd-local, not quant-phase5:f55a21b-local. The retained Phase 4 image resolves to sha256:0ff3d81bd604cfab05d3154cb4c25b61585a2af1efe89fae00d0d801b7830ee7 and does not contain quant_phase5. The Phase 5 report's table and tracked Compose history therefore do not establish which image the 30-minute acceptance actually ran.

No matching Docker container events were returned for the 2026-09-22 acceptance window, and the local Bash history had no matching Phase 5 image or memory-measurement entries. The f55 tag's creation time precedes the stated acceptance window, which is consistent with—but does not prove—the report's tag claim. I replayed the present f55 image as the best available candidate, not as a proven immutable historical image.

HISTORICAL_IMAGE_RELIABLY_CONFIRMED=false. No historical image was deleted, retagged, or overwritten.

### Rebuilt Phase5 Image

The exact accepted source commit 610c8afa389581f49b33cfd88cde6d6865ed59ab was checked out into a detached temporary worktree and built under a new tag; the historical tag was not changed.

| Property | Exact-source diagnostic build |
|---|---|
| Tag | quant-phase5-rebuilt:610c8afa-artifact-audit |
| Source commit | 610c8afa389581f49b33cfd88cde6d6865ed59ab |
| Image ID | sha256:5010e4a353d8c8f7e6868efa31c84ebb075f5ebbb06bf8e6af63bdfeb5653ba7 |
| Current Docker RepoDigests field | quant-phase5-rebuilt@sha256:5010e4a353d8c8f7e6868efa31c84ebb075f5ebbb06bf8e6af63bdfeb5653ba7 |
| Created | 2026-09-23 21:45:00 +0800 |
| Size | 58,402,101 bytes |
| Platform | linux/amd64 |

The initial no-cache build attempt could not resolve aiohttp from the package index and failed. A subsequent normal Dockerfile build succeeded under the new tag. No pip upgrade command was run; the normal Dockerfile pip install resolved the declared dependency ranges into the disposable image. The build is source-exact, but not a byte-for-byte reproducible build because the project has no dependency lock and the Dockerfile uses mutable tags.

### Image Digest Comparison

The runtime build context between source commits f55a21bb36058bed5313aca22398c1ef1af453fa and 610c8afa389581f49b33cfd88cde6d6865ed59ab is identical for Dockerfile, .dockerignore, pyproject.toml, README.md, src, migrations, and config. The full Git diff outside those build inputs contains documentation/tests and Compose restart-policy changes, not application source or dependency declarations.

The application source content hash under /app/src, excluding __pycache__, is identical in both images: 8755eb741657e0a6d25ebd8ee4bdcf95dfa51f65c2f51585a017940a01c8d883. Installed site-packages non-bytecode files have the same aggregate hash: 749f93239a1f84f2b5e5b43fa4045d4da0f0139a6f49f0dc2e5985f71c6606a7. Both contain 450 installed .pyc files, and the bytecode payloads after the 16-byte headers have the same aggregate hash: 0d5f2d6025a58fbc7686bd2d6e024c350e8f7bb3285819a3591aaf88225651bb.

The retained f55 image additionally contains 97 .pyc files under /app/src; the rebuilt image contains none there. The collector's Python sys.path does not include /app/src, so these files are not the imported package. Docker history shows the old COPY src layer at about 2.2 MB versus about 1.01 MB in the rebuilt image. This explains a layer/size difference, not the observed process RSS difference. Other post-base layer IDs differ, but source, installed package files, bytecode payload, and runtime module set match. No executable build-artifact difference explaining a 50–100 MiB runtime delta was found.

HISTORICAL_VS_REBUILT_TABLE (300-second Docker sample; VmRSS and smaps Anonymous are process-level values; cgroup peak is included only where captured):

| Replay | Image ID | Created / size | Python | Docker memory sample | VmRSS / HWM | smaps Anonymous | cgroup memory.peak | OOM kill |
|---|---|---|---|---:|---:|---:|---:|---|
| Reported historical-tag candidate, run 1 | sha256:9cc095b70acaa24c56fbf8d59551a53cb032d31d0d67873e6a588b07461d9285 | 2026-09-22 12:41:55 +0800 / 58,783,160 B | 3.12.13 | 199.5 MiB | 217,428 / 217,428 kB | 196,228 kB | 235,372,544 B | No |
| Reported historical-tag candidate, run 2 | same | same | 3.12.13 | 198.5 MiB | 218,768 / 218,768 kB | 197,008 kB | 212,094,976 B | No |
| Exact 610 rebuild, run 1 | sha256:5010e4a353d8c8f7e6868efa31c84ebb075f5ebbb06bf8e6af63bdfeb5653ba7 | 2026-09-23 21:45:00 +0800 / 58,402,101 B | 3.12.13 | 254.6 MiB | 251,996 / 272,156 kB | 232,040 kB | 269,139,968 B | No |
| Exact 610 rebuild, allocator run | same | same | 3.12.13 | 184.2 MiB | 205,348 / 205,348 kB | 183,608 kB | Not captured | No |

The last row used the low-overhead signal/allocator helper with tracing disabled. The 254.6 MiB sample came from the uninstrumented run. Both used the same rebuilt image ID and Phase flags, but are not a perfect environment match because of that helper and because live source activity differed. These are sampled 300-second values; only cgroup memory.peak is a high-water mark.

### Base Image Comparison

Dockerfile FROM is python:3.12-slim, a mutable tag. The currently retained base resolves to image ID/repo digest sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf, created 2026-06-24T02:10:24Z. The base has four filesystem layers; all four are identical in the candidate and rebuilt images.

BASE_IMAGE_SAME=true for the images inspected now. This does not prove the acceptance-time base digest because the Phase 5 report did not record one.

### Python Runtime Comparison

Both images report Python 3.12.13, GCC 14.2.0, Debian 13, glibc 2.41, executable /usr/local/bin/python, and linux/amd64. Python CONFIG_ARGS and Python binary version are identical. Both report the same WSL host platform string with kernel 6.6.87.2-microsoft-standard-WSL2.

Current host reference: WSL 2.6.3.0, Linux kernel 6.6.87.2-microsoft-standard-WSL2, Docker Engine 29.1.3, cgroup v2, overlayfs. The earlier Phase 5 report records Docker 29.1.3 but not an acceptance-time WSL kernel or allocator configuration.

### Dependency Comparison

pip freeze and pip list match between the current f55 candidate and exact-source build. The saved sorted pip list inventory, identical in both images, is:

aiohappyeyeballs=2.7.1; aiohttp=3.14.3; aiosignal=1.4.0; annotated-types=0.8.0; attrs=26.1.0; frozenlist=1.8.0; idna=3.20; multidict=6.9.1; pip=25.0.1; propcache=0.5.4; psycopg=3.3.6; psycopg-binary=3.3.6; pydantic=2.13.5; pydantic-core=2.46.5; quant-phase1=0.1.0; typing-inspection=0.4.4; typing-extensions=4.16.0; websockets=15.0.1; yarl=1.25.1.

A complete sorted freeze diff, pip list diff, and package-file hash diff were empty.

DEPENDENCY_REPRODUCIBILITY_STATUS=UNPINNED. pyproject.toml uses bounded ranges (aiohttp >=3.9,<4; websockets >=15,<16; pydantic >=2.7,<3; psycopg[binary] >=3.2,<4). No requirements/constraints/lock file is tracked, so transitive dependencies are also not locked. Current equality is evidence about these two images, not a guarantee future builds resolve identically.

### OS Package Comparison

The full dpkg-query -W package inventory diff is empty. Both images have matching Debian 13 packages, including libc6 2.41-12+deb13u3, libstdc++6 14.2.0-19, libssl3t64/OpenSSL 3.5.6-1~deb13u2, and ca-certificates 20250419. No OS package, malloc library, or database-client package drift was found between the current images.

### Import Footprint and Runtime Environment

Using the same CollectorService construction probe with TRADING_MODE=paper, Phase 2/3/4/5 enabled, Phase 6 disabled, and a disposable DSN, each image loaded 487 modules, including 42 native-extension modules. The sorted sys.modules hash was ffc7122a687a029a8e22089adb25e201dc7084462bace26a4cb562150e8fbc3b. Phase 6 modules were absent. This audit's count is 487; the earlier isolation note recorded 488 under its probe, so only the matching set/hash within this audit is treated as comparative evidence.

At runtime, docker top showed one Python collector process. Observed thread counts ranged from 2 at startup to 4–8 at later samples, and FDs ranged from 7 to 47. No child process pool was present. The allocator/runtime variables PYTHONMALLOC, PYTHONHASHSEED, MALLOC_ARENA_MAX, GLIBC_TUNABLES, OMP_NUM_THREADS, OPENBLAS_NUM_THREADS, MKL_NUM_THREADS, and NUMEXPR_NUM_THREADS were unset in the current probes. Historical .env.local was not read; the acceptance record does not say whether it set any such variables, so historical values remain unknown.

### Allocator Evidence

Low-overhead signal-triggered mallinfo2 and CPython sys._debugmallocstats probes were run without starting tracemalloc. At 300 seconds, the reported-tag candidate had 134 current CPython arenas, 35,898,656 bytes in allocated blocks, and 54,523,648 bytes in available blocks; mallinfo2 reported arena=46,960,640, uordblks=32,804,144, fordblks=14,156,496, hblkhd=1,363,968 bytes. Its process had 8 threads, RSS 207,888 kB, and smaps Anonymous 185,408 kB.

The exact-source build's low-memory allocator probe had 133 current CPython arenas, 34,885,952 bytes in allocated blocks, and 53,548,208 bytes in available blocks; mallinfo2 reported arena=46,252,032, uordblks=41,138,096, fordblks=5,113,936, hblkhd=1,363,968 bytes. It had 7 threads, RSS 205,348 kB, and smaps Anonymous 183,608 kB. These allocator snapshots are close and do not identify a large allocator-specific difference. CPython arena statistics and mallinfo2 categories are not additive or a complete native-memory attribution.

A separate 30-second PYTHONTRACEMALLOC=1 diagnostic reached 95,130,951 bytes traced, but process RSS was already about 251,644 kB and anonymous memory about 229,048 kB. The untraced candidate at 30 seconds was about 131,916–146,076 kB RSS. Tracing added roughly 100–120 MiB at this limit and materially perturbed the process; therefore the traced total cannot be used to declare UNTRACKED_NATIVE_MEMORY_SIGNIFICANT. The diagnostic was stopped before OOM; OOMKilled remained false. No alternative allocator or LD_PRELOAD experiment was run.

### Docker/Cgroup Evidence

The high 300-second exact-source sample recorded Docker stats 254.6 MiB / 256 MiB, process VmRSS 251,996 kB and VmHWM 272,156 kB, smaps Anonymous 232,040 kB, cgroup memory.current 267,943,936 bytes, and cgroup anon 239,607,808 bytes. Other cgroup categories were file 2,686,976, kernel 2,850,816, and sock 5,099,520 bytes. memory.peak was 269,139,968 bytes; memory.events showed max events but oom=0 and oom_kill=0. Thus this high sample was predominantly process/private anonymous memory, not just a Docker accounting discrepancy, and it did not result in an OOM kill.

For comparison, a low 300-second candidate sample recorded Docker stats 199.5 MiB, VmRSS 217,428 kB, smaps Anonymous 196,228 kB, cgroup memory.current 233,537,536 bytes, anon 201,228,288, file 25,018,368, kernel 3,284,992, and sock 3,313,664 bytes. Docker's reported value is not interchangeable with process RSS or cgroup current; file cache and kernel/socket accounting explain part of the difference. The 50+ MiB high-run increase itself is nevertheless visible in RSS and anonymous memory.

### Historical Measurement Method

The final Phase 5 report states “peak observed container memory” of approximately 156.8 MiB for the collector during a 30m17s window. It does not preserve raw samples, sampling interval, peak timestamps, container ID, process RSS, smaps, or cgroup data. A nearby earlier/pre-hardening section says measurements used docker stats and docker inspect, but it does not unambiguously tie that method to the final 156.8 MiB figure. The acceptance record does not establish whether 156.8 MiB was a sampled docker stats maximum, a single instantaneous value, or another metric.

HISTORICAL_MEASUREMENT_METHOD=UNKNOWN (docker stats is plausible but not proven for that value). MEASUREMENT_METHOD_COMPARABLE=UNKNOWN. The historical report's 30-minute maximum is not a raw time series that can be aligned to the current 0/30/60/120/300-second samples.

### Workload Equivalence

| Dimension | Historical Phase 5 acceptance evidence | Current audit replays |
|---|---|---|
| Duration | 30m17s | 300-second measured window, with separate 30-second graceful-stop runs where noted |
| Services | Collector and engine ran with PostgreSQL | Collector only; isolated PostgreSQL; no engine replay |
| Flags | paper; tracked Compose shows Phase 2/3/4 enabled; Phase 5 flag comes from undocumented .env.local/runtime state | paper; Phase 2/3/4/5 enabled; Phase 6 disabled |
| Universe | 803 symbols in nearest recorded baseline | 805 symbols in each disposable DB |
| Stored data | Nearest recorded baseline at 2026-09-22T04:54Z: 823,702 market_snapshots, 2,444,514 market_observations, 106,994 klines, 3,980 cross_exchange_flow_snapshots; acceptance-window DB size 4.427 GB to 4.613 GB | Each database began empty; at/near five minutes, about 2,605–8,605 market_snapshots, 4,815–22,206 observations, 79,126–79,133 klines, and 60–154 flow snapshots; database size about 57.5–82.4 MB |
| External activity | 80 Kline rows and 330 flow rows processed over the historical 30m17s window; Bybit degraded handling was recorded | Live public-source ingestion; no mock data; runtime health occasionally degraded/recovered |
| Memory setup | Collector 256 MiB, engine 384 MiB; historic image identity is unresolved | Collector 256 MiB, no memory-limit changes; same WSL host/Docker engine |

The nearest historical row-count baseline predates the acceptance window by about 2h27m and is not an exact acceptance-start snapshot. The current empty-database tests therefore do not match historical DB hydration, database size, duration, or service concurrency. Current ingestion itself also varied materially: with the same rebuilt image ID and Phase flags, the 300-second Docker sample was 254.6 MiB in one run and 184.2 MiB in another. The latter used the low-overhead allocator helper with tracing disabled, so the environment was not byte-for-byte identical. The high run had 7,805 snapshots at the 300-second sample (8,605 after stop grace) and 22,206 observations at final query; the lower run had 2,605 snapshots and 4,815 observations at query. This is evidence that live workload intensity can change the measured working set, but it does not prove which factor caused the historical 156.8 MiB value.

### Classification

PHASE6_HISTORICAL_BASELINE_NOT_COMPARABLE

The report identifies a plausible retained historical tag, but its immutable acceptance identity conflicts with tracked Compose history and is not recoverable from Docker events or shell history. The historical memory metric has no raw sample method/cadence or process/cgroup data. Current live workload and database state differ, and same-image current runs varied by about 70 MiB. These facts prevent a reliable like-for-like explanation; they do not confirm build-artifact drift, host-runtime drift, or a complete workload-only root cause.

PHASE6_MEMORY_BUILD_ARTIFACT_DRIFT_CONFIRMED=false. PHASE6_MEMORY_HOST_RUNTIME_DRIFT_CONFIRMED=false. PHASE6_MEMORY_ROOT_CAUSE_NOT_CONFIRMED remains the Phase 6 memory root-cause status. No fix is justified by this audit.

All original/historical Docker images remain untouched. The current quant-postgres and quant-engine were not restarted; quant-collector was already exited with restart count 2 at the pre/post read-only snapshots and was not started. No Phase 7 runtime, Phase 8/9, AI provider, ECS, or trading operation was started.

### Audit Git State

Branch phase7; HEAD 6e898d594c6463d92e1831413189653a05492484. Only PHASE_6_RUNTIME_INTEGRATION_REPORT.md is modified by this audit addendum. git diff --check passes. The exact-source diagnostic image tag is retained; all temporary runtime/database/network resources were removed.

## Current Memory Capacity Characterization

Audit date: 2026-09-23 UTC. This section records a new, isolated capacity characterization of the current Phase 6 candidate collector. It does not replace or reinterpret the historical Phase 5 baseline audit above.

### Frozen Candidate and Safety Boundary

- Project branch: `phase7`; pre-test code HEAD: `8b31c0828e5d36cc746e125edb13efb4de7e5555`.
- Frozen image used for both runs: `sha256:ae46734d051dfbab062a5cee34c658028a3de1bd9a59ce97d691165026653dfb` (`quant-phase7:b00e872-local` at inspection; source code candidate `b00e87266fead3ddaccfe91276fcbd0249ea1772`). Both containers were launched by immutable image ID, not by mutable tag.
- No application code, formal Compose resource limit, existing Quant collector/engine/PostgreSQL, persistent volume, or remote environment was changed. Only a new Docker bridge network, isolated disposable PostgreSQL container with a container-local tmpfs data directory, and two temporary collector containers were used. PostgreSQL had no published host port and no production volume was mounted.
- Collector limits were exactly 268,435,456 bytes (256 MiB) and 536,870,912 bytes (512 MiB); `memory-swap` equaled each memory limit, and each collector had a 1.0 CPU quota. The isolated PostgreSQL container had a 768 MiB memory limit and 0.5 CPU quota.
- Both workloads used `TRADING_MODE=paper`, Phase 2/3/4/5/6 enabled, Phase 7 Bitcoin/Ethereum RPC disabled, the same image, same CPU quota, same public endpoints and default bounded queue/configuration settings. Each run began with a separate empty database, then applied its own migrations. The diagnostic did not start a second engine; it measured the collector process only. The existing `quant-engine` was not part of either diagnostic workload and was not changed.
- No private exchange credential, AI provider credential, Phase 7 RPC endpoint, VPN, proxy, or tunnel was passed to either collector. `PHASE6_SOURCE_REGISTRY_PATH` and AI provider settings were left at their empty defaults. Phase 6 ran with zero configured source bindings and reported `SOURCE_NOT_CONFIGURED`; Phase 6 news, macro, unlock, source-registry, and AI result tables remained empty. No advanced data or AI was mocked.
- The live-data workload was not deterministic: external public feeds and reconnect behavior varied over time. The artifact and workload configuration were fixed, but the actual exchange messages were naturally different between sequential runs.

### Measurement Method and Limitations

- The 256 MiB reference was sampled about every 15 seconds for a 612-second measurement window. The container remained alive during the subsequent stop attempt until 657.05 seconds from container start, exited with code 137 after the stop timeout, and had `OOMKilled=false`; no memory samples were claimed for the final shutdown interval.
- The 512 MiB run was sampled every 30 seconds until kernel OOM. Cgroup `memory.current`, `memory.peak`, `memory.stat` (`anon`, `file`, `kernel`, `sock`), `/proc/1/status` RSS/HWM/threads, process count, Docker stats CPU/memory, PostgreSQL database size/tuple counters, and key-table insert/update counters were sampled. Shell quoting made the initial t=0 process RSS/anon subfields invalid, and no t=30 process sample was retained; the corrected sampler resumed at elapsed 49 seconds without restarting the collector. Cgroup startup samples were available. This instrumentation gap does not affect the OOM event or later trend.
- No `tracemalloc`, allocator injection, or memory-affecting profiler was enabled. RSS, Docker stats, and cgroup values are different accounting views and are not additive or directly interchangeable.
- Queue depth and database/persistence buffer depth are **not exposed by a supported runtime diagnostic endpoint**, so actual depths were not measured. Source-configured capacities are not represented as observed depth. Collector-side writes are performed by the existing loops; database size and PostgreSQL tuple/table write counters were sampled as the available persistence evidence. The engine-owned AI queue was not launched in this collector-only test.
- The optional Phase 6 OFF/ON A/B was not run: Phase 6 had no configured real sources and no AI provider, so an OFF comparison would characterize only empty-source lifecycle overhead, not Phase 6 ingestion load. No causal attribution to Phase 6 is made from this test.

### 256 MiB Reference Run

Container `phase6-capacity-256m`; started `2026-09-23T14:54:51.920336945Z`. Sampling began at `2026-09-23T14:54:52Z` and ended at elapsed 612 seconds (`15:05:05Z`).

| Metric | Measured result |
|---|---:|
| Limit / swap limit | 268,435,456 / 268,435,456 bytes |
| Cgroup `memory.peak` | 234,643,456 bytes (223.77 MiB) |
| Last sampled cgroup `memory.current` | 231,940,096 bytes (221.20 MiB) |
| Last sampled cgroup anonymous memory | 204,247,040 bytes (194.79 MiB) |
| Process `VmRSS` / `VmHWM` at last sample | 220,804 / 220,804 kB (215.63 MiB) |
| Docker stats at last sample | 206.2 MiB / 256 MiB |
| Threads / processes at last sample | 7 / 4 |
| Database size, exact | 71,236,631 bytes (67.94 MiB); initial 11,648,023 bytes |
| OOM | No (`OOMKilled=false`, restart count 0) |

Exact post-run row counts: `symbols=805`, `market_snapshots=6,805`, `market_observations=10,815`, `klines=79,155`, `trade_flow_windows=805`, `cvd_snapshots=760`, `liquidation_events=16`, `liquidation_windows=14`, `runtime_health_events=9`, `system_health=10`. Phase 6 news/macro/unlock/source-registry/AI result tables were all zero.

This run did not OOM within its 612-second sampled window. It is not by itself a long-duration capacity pass.

### 512 MiB Diagnostic Headroom Run

Container `phase6-capacity-512m`; started `2026-09-23T15:07:05.442678222Z`, finished `2026-09-23T15:33:28.207018925Z`, exact observed runtime **1,582.764 seconds (26m22.8s)**. It did not reach the planned 60-minute endpoint because the kernel killed the collector at the 512 MiB cgroup limit. The last valid sample preceded termination by about 23 seconds; samples covered the final two minutes at 30-second cadence.

| Elapsed | Cgroup current | Cgroup peak | Cgroup anon | Process RSS/HWM | Database size |
|---:|---:|---:|---:|---:|---:|
| 0m | 23.56 MiB | 25.50 MiB | startup field unavailable | startup RSS unavailable | 11,648,023 B |
| 1m | 162.60 MiB | 178.32 MiB | 159.56 MiB | 181.54 MiB | 48,577,559 B |
| 2m | 188.21 MiB | 190.77 MiB | 178.08 MiB | 199.88 MiB | 53,451,799 B |
| 5m | 192.92 MiB | 202.96 MiB | 186.28 MiB | 207.75 MiB | 64,224,279 B |
| 10m | 204.09 MiB | 207.10 MiB | 195.06 MiB | 216.72 MiB | 70,892,567 B |
| 15m | 267.85 MiB | 270.56 MiB | 259.73 MiB | 281.25 MiB | 74,480,663 B |
| 20m | 391.77 MiB | 394.23 MiB | 381.93 MiB | 403.31 MiB | 78,052,375 B |
| 25m | 470.11 MiB | 472.38 MiB | 456.70 MiB | 477.69 MiB | 80,755,735 B |
| 26m sample, t=1,560s | 505.16 MiB | 507.73 MiB | 496.36 MiB | 517.28 MiB | 82,222,103 B |
| OOM time, t=1,582.764s | Container exited | — | — | — | 82,230,295 B (78.42 MiB) |

The measured cgroup peak was predominantly anonymous memory. The last sample had `anon=520,474,624` bytes, while `file=425,984`, `kernel=3,727,360`, and `sock=4,599,808` bytes. At exit, `memory.events` reported `max=31`, `oom=1`, and `oom_kill=1`; Docker reported `exit=137`, `OOMKilled=true`, and restart count 0. The OOM was independently confirmed by the container state and cgroup event counters.

Exact post-OOM database row counts: `symbols=805`, `market_snapshots=15,205`, `market_observations=10,215`, `klines=79,131`, `trade_flow_windows=1,631`, `cvd_snapshots=1,684`, `liquidation_events=81`, `liquidation_windows=103`, `runtime_health_events=5`, `system_health=10`. Phase 6 news/macro/unlock/source-registry/AI result tables were all zero. The Phase 6 health rows recorded zero sources and `NOT_AVAILABLE` for news/macro/unlock with reason `SOURCE_NOT_CONFIGURED`; the Phase 6 persistence health row was `AVAILABLE` while the collector was active.

PostgreSQL grew by about 67.31 MiB from the first post-start observed database size to the OOM endpoint, while collector anonymous memory rose to about 496 MiB. Thus database file growth alone does not explain the container's memory high-water mark. Docker CPU samples were frequently near the 1.0 CPU quota. Logs showed public WebSocket subscriptions becoming available and Phase 3 flow persistence continuing, with intermittent public WebSocket opening-handshake timeouts and runtime-degraded events; this temporal association is recorded but is not a proven cause of memory growth.

### Runtime Diagnostics and Evidence Boundaries

- Configured but unobserved application queue capacities include the Phase 1 event buffer (2,000), Phase 3 trade queue (2,000), Phase 4 queue (2,000), and engine-owned Phase 6 AI queue (32). These are code/config limits only, not measured queue depths.
- Phase 6 source registry count was zero in both databases; news, macro, unlock, AI analysis, and AI usage row counts were zero. Therefore the observed growth belongs to the complete Phase 2–6-enabled collector candidate under public live market-data load; this evidence does **not** establish that Phase 6 itself caused the growth. Phase 6 OFF/ON causal isolation remains untested.
- The diagnostic did not run an engine, Stage1 processing loop, private API, order path, Phase 7 RPC, or trading action. Trading mode remained paper.
- The prescribed 30m, 45m, and 60m 512 MiB samples were not reached due to OOM. No 30/45/60-minute growth projection was made.

### Classification

**PHASE6_COLLECTOR_MEMORY_GROWTH_CONFIRMED**

The current Phase 6-enabled collector candidate showed a sustained, bursty increase in cgroup anonymous memory and was OOM-killed under a 512 MiB diagnostic cap after 26m22.8s. “Confirmed” describes observed collector-candidate behavior under this workload; it is not a causal finding against Phase 6 code. No fix was attempted in this characterization.

**COLLECTOR_256M_CAPACITY_INSUFFICIENT**

The direct 256 MiB run completed its 612-second sampled window without OOM, with a 223.77 MiB cgroup peak. However, the same immutable candidate and workload configuration under the 512 MiB diagnostic cap later crossed the 256 MiB level between the 14.5- and 15-minute samples and ultimately OOM-killed at 512 MiB. Therefore 256 MiB is insufficient for sustained operation under the measured high-load behavior, despite surviving the shorter reference window.

`PHASE6_COLLECTOR_BOUNDED_CAPACITY_CONFIRMED=false`. The 512 MiB test did not complete 60 minutes. The root cause and Phase 6-specific contribution remain unconfirmed; a follow-up controlled investigation is required before changing production resource caps or application code.

### Test Resource and Git State

The two diagnostic collector containers, isolated PostgreSQL container, and bridge network were created only for this test and were removed after evidence capture. The PostgreSQL data directory was container-local tmpfs; its rows are no longer recoverable, while the exact counts and measurements above are retained in this report. No production container, data volume, database, code, or runtime configuration was altered. The candidate image was preserved. The only project-tree change from this characterization is this documentation section; no runtime code was edited.

The documentation checkpoint preceding this characterization is `8b31c0828e5d36cc746e125edb13efb4de7e5555`. Final branch is `phase7`, HEAD remains `8b31c0828e5d36cc746e125edb13efb4de7e5555`; `git diff --check` passes, and this report is the sole modified file.

## Collector Common Runtime Root Cause Isolation

Audit date: 2026-09-24 (Asia/Shanghai); runtime samples below are UTC on 2026-09-23. This is a diagnostic-only follow-up to the capacity characterization. It does **not** label the behavior a Phase 6 memory leak, and no application fix was made.

### Fixed Artifact

- Project: `/home/lucas045057/projects/quant`; branch `phase7`; documentation checkpoint before this audit: `c0dd1245c6548ed5534ee1844fb2e68b2e902523`.
- Every isolation run used the same immutable collector image ID: `sha256:ae46734d051dfbab062a5cee34c658028a3de1bd9a59ce97d691165026653dfb`, code candidate `b00e87266fead3ddaccfe91276fcbd0249ea1772`. The mutable image tag was not used to launch tests.
- Image Python: 3.12.13; Docker Engine: 29.1.3; host kernel: `6.6.87.2-microsoft-standard-WSL2`; cgroup v2. Isolated PostgreSQL was 16.15, launched from image ID `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
- Each case used `TRADING_MODE=paper`, explicit existing Phase enable switches, public endpoints only, and the same app CPU/memory settings. No app image rebuild occurred between cases.

### Isolation Environment

- Cases each received a newly created private Docker bridge network and a new PostgreSQL 16.15 container with a container-local tmpfs data directory. PostgreSQL had no published host port. The same disposable database was migrated from empty before each collector start; all 13 migrations applied successfully, for 14 recorded migration rows after runtime migration idempotency checks.
- Collector limit: 768 MiB memory, swap limit equal to memory, 1.0 CPU. Disposable PostgreSQL limit: 768 MiB, swap limit equal to memory, 0.5 CPU. There was only one test collector and one test database at a time.
- No production Quant volume, database, collector, engine, Compose service, or remote ECS was mounted or restarted. Existing `quant-engine` and `quant-postgres` remained running. Test containers and networks were removed at the end of each case; the frozen image was preserved.
- The diagnostic-only probe sampled cgroup memory and `/proc` every 30 seconds, and sampled `asyncio.all_tasks()`/GC summaries once per minute (GC object-type census at startup, 5 minutes, and 10 minutes). It captured task coroutine and top-frame summaries, not payloads. No `tracemalloc` or proxy was used. The temporary probe did not modify application source or semantics.
- Public Bitget UTA REST/WebSocket data was real. Phase 3 and Phase 4 public feeds were enabled only in their designated cases. Phase 6 was enabled with an empty/unconfigured source registry. No advanced data or AI was mocked; no private API, API key, order route, engine, Stage1, or trade action was started.

### Sample Windows (UTC)

Each window lasted about ten minutes; the fixed sampling cadence was 30 seconds.

| Case | Start | End |
|---|---|---|
| Phase 1-only reference | 2026-09-23 16:03:27Z | 2026-09-23 16:13:29Z |
| Phase 3-only run 1 | 2026-09-23 16:16:21Z | 2026-09-23 16:26:23Z |
| Phase 3-only repeat | 2026-09-23 16:29:19Z | 2026-09-23 16:39:21Z |
| Phase 1-only repeat | 2026-09-23 16:41:28Z | 2026-09-23 16:51:30Z |
| Phase 4-only | 2026-09-23 16:53:17Z | 2026-09-23 17:03:18Z |
| Phase 6 enabled, unconfigured | 2026-09-23 17:05:05Z | 2026-09-23 17:15:07Z |

### Core Baseline

The formal collector has no setting that disables its Phase 1 instruments/ticker bootstrap, WebSocket subscriptions, Kline/Universe pipeline, or health/persistence loops. A source-free “core-only” collector case therefore cannot be constructed using current production semantics. No synthetic adapter or invented flag was introduced. The valid baseline here is **Phase 1-only** (`PHASE2/3/4/5/6_ENABLED=0`), measured twice. The first Phase 1-only run is a capacity reference; the second includes task-stack diagnostics.

### Source Isolation Matrix

| Group | Collector ownership / current control | Test result |
|---|---|---|
| A — Phase 1 instruments, tickers, snapshots | Mandatory in `CollectorService`; cannot be disabled independently. | Included in both Phase 1-only baselines and every comparison. Live `symbols`, `market_snapshots`, and observations persisted. A reconnect/recovery burst occurred, then the baseline tail stabilized. |
| B — Kline / Universe | Runs in the mandatory Phase 1 bootstrap and REST recovery path; no separate safe switch. | Not independently isolatable from A. Task stacks directly identified Kline recovery tasks in `quant_phase1/pipeline.py`. |
| C — Phase 2 OI / Funding | Engine-owned `Phase2DerivativeRuntime`; not started by collector entrypoint. | Not a collector source group; not run in this collector-only experiment. No Phase 2 rows were created. |
| D — Phase 3 public trades / flow | Collector `PHASE3_ENABLED` switch. | Two Phase 3-only runs. Real public trade-flow/CVD rows were written. Both showed a prolonged Phase 1 Kline-recovery task backlog and positive post-warm memory slopes. |
| E — Orderbook | No orderbook implementation or orderbook source reference exists in the inspected Phase 1–6 packages. | Not implemented; no data was synthesized. |
| F — Phase 4 liquidation | Collector `PHASE4_ENABLED` switch. | Tested only as part of the combined Phase 4 runtime. Public liquidation data was persisted. |
| G — Phase 4 long/short and basis | Same `PHASE4_ENABLED` switch and `Phase4Runtime`; no independent production switch. | Not separable from F without changing semantics. Phase 4-only run also exercised these public REST adapters and persisted rows. |
| H — Phase 5 context | Engine-owned `Phase5Runtime`; not started by collector entrypoint. | Not a collector source group; not run in this collector-only experiment. No Phase 5 rows were created. |
| Phase 6 context (additional check) | Collector `PHASE6_ENABLED`; three ingestion loops can run with zero configured source bindings. | Tested enabled with `PHASE6_SOURCE_REGISTRY_PATH` unset/empty. Registry/news/macro/unlock/AI result tables stayed empty; only health rows were written. No Phase 6-specific growth was observed after the common recovery burst drained. |

### Memory Slopes

All cases had 21 cgroup/process samples at 30-second intervals across 600 seconds after initial Phase 1 bootstrap rows appeared. Slopes are least-squares MiB/min; “warm” uses samples at elapsed time ≥240 seconds. `peak` is cgroup `memory.peak`; memory units are MiB. The container CPU column is the maximum sampled Docker CPU percentage (one-core quota), not whole-WSL host CPU.

| Case | Optional collector sources | Peak | Anon start → end | RSS slope / warm | Anon slope / warm | Max tasks / at final probe | Max FD / threads | PostgreSQL size start → end |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Phase 1-only reference | none | 443.93 | 163.45 → 409.37 | +25.62 / −4.11 | +25.60 / −4.12 | not instrumented / — | 26 / 2 | 48.91 → 111.04 MB |
| Phase 1-only repeat | none | 416.72 | 163.77 → 408.71 | +26.00 / −0.08 | +25.96 / −0.13 | 1,612 / 14 | 27 / 2 | 48.90 → 107.96 MB |
| Phase 3-only run 1 | Phase 3 | 289.68 | 163.72 → 274.44 | +7.86 / +12.25 | +7.91 / +12.35 | 2,992 / 2,991 | 45 / 4 | 48.88 → 80.75 MB |
| Phase 3-only repeat | Phase 3 | 479.72 | 163.62 → 474.04 | +33.43 / +24.86 | +33.48 / +24.89 | 2,977 / 885 | 46 / 4 | 48.86 → 88.09 MB |
| Phase 4-only | Phase 4 | 426.35 | 163.70 → 413.14 | +24.15 / −1.31 | +24.14 / −1.29 | 2,251 / 21 | 30 / 3 | 50.18 → 113.93 MB |
| Phase 6 enabled, unconfigured | Phase 6 empty-source lifecycle | 460.56 | 165.12 → 415.83 | +26.67 / −5.92 | +26.66 / −5.99 | 2,457 / 18 | 23 / 5 | 48.90 → 110.35 MB |

In every case, anonymous memory dominated. `file` stayed at 0–4.3 MiB; `kernel` stayed about 1.9–2.7 MiB. In the Phase 3 repeat, `sock` peaked at 4.8 MiB while anonymous memory reached 474.04 MiB. Sampled collector CPU peaked at 100.29%/100.26% in the two Phase 3 cases, versus 11.48% Phase 1-only repeat, 26.37% Phase 4-only, and 35.59% Phase 6-unconfigured. No claim about total WSL host CPU is made.

### Poll Cycle Correlation

- `market_snapshots.snapshot_timestamp` distinct counts provide a persisted-cycle proxy (not a request counter): Phase 1-only reference 114; Phase 1-only repeat 115; Phase 3-only runs 43 and 52; Phase 4-only 114; Phase 6-unconfigured 114.
- Phase 1/4/6 cases continued near the nominal five-second persistence cadence (about 114–115 distinct snapshot timestamps per ten-minute measurement). The Phase 3-only cases persisted substantially fewer market snapshot cycles while maintaining Phase 3 flow writes, consistent with event-loop/REST recovery work competing with the common collector path; this association is not treated as proof by itself.
- `pg_stat_user_tables` insert/update counters were sampled every 30 seconds. For example, Phase 3 repeat ended at 115,079 inserted and 94,985 updated tuples; Phase 1-only repeat ended at 135,420 inserted and 219,073 updated tuples. Exact final table counts are below.
- The application exposes no supported outbound HTTP request counter. Requests were not intercepted or proxied; exact request counts are **NOT_AVAILABLE**. The observed number of Kline fetch coroutines is a pending-work measure, not a count of completed HTTP requests.

### FD / Thread / Task Evidence

- FD counts fluctuated rather than monotonically increasing: maximums were 27 (Phase 1 repeat), 45/46 (Phase 3), 30 (Phase 4), and 23 (Phase 6-unconfigured). Thread counts plateaued at 2, 4, 3, and 5, respectively. This does not support a persistent FD or OS-thread leak in these windows.
- During Phase 1-only repeat at 180 seconds, `asyncio.all_tasks()` found 1,612 tasks: 1,274 in `pipeline.py:fetch` and 324 in `pipeline.py:_fetch_symbol`. By 300 seconds the task count fell to 14 and stayed at the normal collector count through the end.
- Phase 3-only repeat peaked at 2,977 tasks at about 190 seconds: 2,356 `pipeline.py:fetch` and 598 `_fetch_symbol`. The pending recovery tasks remained measurable at the end (885 total; 684 `fetch` and 179 `_fetch_symbol`). The first Phase 3-only run reached 2,992 tasks and still had 2,991 at its final probe.
- Phase 4-only peaked at 2,251 tasks, primarily the same Phase 1 `pipeline.py` Kline tasks, then fell to 20 at 300 seconds (with a later 537-task Kline recovery batch); final task count was 21. Phase 6-unconfigured peaked at 2,457 tasks, of which 2,439 were the same Kline tasks; it fell to 18 by 360 seconds and was 18 at the final probe. Its three `runtime.py:loop` tasks were the expected empty-source Phase 6 loops.
- Stack summaries place the bulk of tasks at `pipeline.py:37` inside the per-collector semaphore and `pipeline.py:43` in `_fetch_symbol`'s `asyncio.gather`. Those are waiting/running REST Kline recovery coroutines, not Phase 3 trade workers or Phase 6 source workers.

### HTTP Lifecycle

- `BitgetV3UtaRestClient` creates/reuses one `aiohttp.ClientSession` inside the collector's async context and closes owned sessions on exit. `_recover_gaps` reuses the same client.
- Phase 4 opens one shared REST session and closes it from `Phase4Runtime.stop`; one-shot adapter contexts also close their own sessions. No unbounded session count was observed.
- Each recovery call constructs a fresh `MarketDataCollector` with its own semaphore (default 8) and calls `asyncio.gather` for all selected symbols and four intervals. The semaphore bounds concurrent work **within that recovery call**, but the gather creates the full coroutine/task fan-out up front, and concurrent recovery calls do not share a global limiter or coalescing lock. The pending tasks observed in the probe align with this path.
- Exact REST request totals and allocator-level request objects are unavailable; no proxy or request instrumentation was added.

### WebSocket Lifecycle

- Phase 1 opens partitioned public Bitget sockets and owns receive/ping tasks. On a receive error, each `_receive_loop` reconnects its socket and independently invokes `_recover_gaps` for the full selected universe (`collector.py:485–503`). This can fan out multiple overlapping full-universe REST recoveries when more than one partition is disrupted.
- Observed completed recovery log counts: Phase 1-only repeat 6; Phase 4-only 7; Phase 6-unconfigured 7. Phase 1-only repeat recorded one `ws_reconnect_failed`; Phase 4/6 recorded zero such log events. The earlier two Phase 3 runs did not retain the recovery-log counter, so no Phase 3 reconnect count is claimed.
- Phase 3/4 WebSocket transports use context-managed public sockets and their own bounded/runtime-owned tasks. No unbounded FD or thread growth was found. The diagnosed task burst belongs to Phase 1 Kline gap recovery, regardless of which optional collector group is enabled.

### Database Lifecycle

- Each disposable DB started empty; migrations `001–013` applied successfully before the collector. Runtime reruns left 14 migration records and did not break schema.
- Phase 1, Phase 3, Phase 4, and Phase 6 persistence use short-lived psycopg connection context managers (Phase 4 scopes its repository connection per operation). No long-lived application pool was created for these tests.
- Ten-minute exact data counts: Phase 1 repeat `symbols=805`, `market_snapshots=23,605`, `market_observations=32,157`, `klines=79,612`; Phase 3 repeat `trade_flow_windows=827`, `cvd_snapshots=760`, `cross_exchange_flow_snapshots=267`, `sum(total_trade_count)=548,845`; Phase 4 `liquidation_events=25`, `liquidation_windows=20`, `long_short_observations=215`, `basis_snapshots=380`; Phase 6-unconfigured `phase6_source_registry/news/macro/unlock/AI result rows=0`, `system_health=7`.
- Database growth was measured separately from collector memory. For instance, Phase 3 repeat DB grew about 37.3 MiB while collector anonymous memory increased about 310.4 MiB; DB files therefore do not explain the cgroup anonymous-memory increase. All disposable databases were removed with their containers; no production database changed.

### GC Object Evidence

The diagnostic wrapper counted GC-tracked objects and top types at startup, 5 minutes, and 10 minutes. These are Python-GC visibility snapshots, not a complete heap census.

- Phase 1-only repeat: 49,460 tracked objects at startup; 243,446 at 5 minutes, including 92,291 `Candle`; 59,055 at 10 minutes after the recovery batch drained.
- Phase 3-only repeat: 49,486 at startup; 397,350 at 5 minutes, including 153,862 `Candle`; 587,936 at 10 minutes, including 252,452 `Candle`, 6,407 `CanonicalTrade`, and 3,158 coroutine objects. The final active-task sample still had 885 Kline recovery tasks.
- Phase 4-only: 187,249 at 5 minutes, including 63,628 `Candle`; 152,461 at 10 minutes, including 46,848 `Candle`.
- Phase 6-unconfigured: 398,353 at 5 minutes, including 163,677 `Candle`; 61,144 at 10 minutes after the recovery batch drained.

The concurrent growth of Kline fetch tasks and `Candle` counts in the Phase 3-only growing runs, alongside task-frame evidence in the Kline gather/semaphore path, is the direct Python-object evidence for the isolated subsystem. It does not prove every Candle remains referenced by the same task after completion.

### Native Allocation Evidence

No Memray/native allocation profiler was run. No native stack attribution is available, and none is inferred. The observed growth was predominantly cgroup anonymous memory and coincided with Python coroutine/task and `Candle` growth; this points to Python-managed work/state but does not exclude native allocator retention.

### Stable vs Growing Control

- Stable control: Phase 1-only repeat experienced the shared recovery burst and reached a cgroup peak of 416.72 MiB, but after 240 seconds its RSS slope was −0.08 MiB/min and anonymous slope −0.13 MiB/min. Its Kline-task count fell from 1,612 to 14; FD and thread counts also returned to a small steady range. This demonstrates that the initial high-water ramp alone is not proof of a continuously growing leak.
- Growing experiment: two independent Phase 3-only runs with the same immutable image and settings both showed positive post-warm slopes (+12.25 and +24.86 MiB/min RSS; +12.35 and +24.89 MiB/min anonymous). Both exposed thousands of Phase 1 Kline recovery tasks. The second retained 885 such tasks at the final probe while GC-tracked `Candle` objects increased to 252,452.
- Phase 4-only and Phase 6-unconfigured showed the same temporary Kline recovery fan-out, but their post-warm slopes were negative (−1.31 and −5.92 MiB/min RSS). Phase 6 had no configured sources and no event rows. These controls argue against Phase 4 or Phase 6 ingestion as the cause.
- Public feeds are non-deterministic, and recovery durations varied across runs. The repeated positive Phase 3-only warm slope plus repeat task-stack localization satisfies the reproducibility criterion for the observed workload; it does not establish behavior under every network regime.

### Root Cause Status

**COLLECTOR_MEMORY_ROOT_CAUSE_CONFIRMED**

The confirmed root path for the measured collector memory growth is repeated **selected-universe REST Kline gap-recovery fan-out triggered by Phase 1 WebSocket receive/reconnect handling**. Each affected receive loop creates its own `MarketDataCollector`; each call builds and gathers symbol/interval fetch coroutines up front. The semaphore is local to that call, so it limits each batch but does not cap or coalesce overlapping batches. The measured task stacks, task bursts, `Candle` object growth, and two repeatable Phase 3-only positive anonymous-memory slopes localize the common runtime pressure to that recovery subsystem; Phase 3 public-trade load appears to prolong/amplify the shared backlog but is not required for the recovery path to trigger.

This finding describes a repeatable burst/backlog and retained high-water behavior in the tested workload; it is **not** a claim that all growth is permanently leaked, nor a `PHASE6_MEMORY_LEAK` finding. Phase 1-only, Phase 4-only, and Phase 6-unconfigured runs returned to near-flat/declining warm slopes after recovery tasks drained. The native allocator path and complete object-reference retention chain remain unprofiled. No fix, resource-cap change, source semantic change, Phase 7/8/9 work, ECS access, or production restart was performed.
## Kline Gap Recovery Remediation

**Latest result: `COLLECTOR_MEMORY_REMEDIATION_RUNTIME_BLOCKED`.** Code and deterministic tests pass, but the fixed-image runtime did not pass acceptance. This result supersedes earlier runtime conclusions without changing the confirmed common-path root cause or attributing the issue to Phase 6.

### Confirmed Root Cause
Phase 1 WebSocket receive/reconnect failures independently triggered selected-universe × interval Kline recovery fan-out. Each batch had a local semaphore; concurrent batches had no collector-global bound or coalescing.

### Red Tests
- `TEST_RED_CONFIRMED=true`: the deterministic legacy-path test reached 24 concurrent REST recovery calls against the expected global limit of 8.
- Duplicate/in-flight coalescing, reconnect-storm backlog bounds, retry fairness, shutdown cleanup, and collector integration tests initially failed before the coordinator existed.

### Scheduler Design
One `GapRecoveryCoordinator` is owned per collector lifecycle, with a fixed worker pool, bounded outstanding identities, coalesced reconnect signals, `(symbol, interval)` identities, an in-flight rerun marker, centrally scheduled capped-backoff retries, and explicit shutdown/cancel cleanup. At capacity, it retains work via coalescing/backpressure rather than silently dropping requests. Empty, unavailable, or non-closed Klines remain unavailable and are retried.

### Global Concurrency
`GLOBAL_RECOVERY_CONCURRENCY_LIMIT=8`. Runtime health observations never showed more than 8 active/in-flight recoveries. The configured pending bound is 800; observed peak was 792. Deterministic integration tests verify reconnect signals share the same workers.

### Dedup / Coalescing
Repeated `(symbol, interval)` work coalesces while pending/in-flight. A new request during a fetch retains one rerun so newly discovered gaps are not lost. Universe changes backpressure rather than discard work. Recovery fetches latest configured closed bars and do not synthesize candles or advance freshness on missing data.

### Retry Lifecycle
Failures re-enter the same coordinator with capped exponential retry timing; failed keys cannot create independent unbounded tasks or monopolize workers. Recovery errors remain visible in health and are never represented as AVAILABLE.

### Shutdown Lifecycle
One coordinator starts before WebSocket work and closes from collector `finally`, including setup failures. Shutdown rejects new work, cancels/awaits workers, and releases pending/in-flight references. Tests cover drain, cancellation, close rejection, and task ownership returning to baseline.

### Focused Regression
- `tests/test_gap_recovery.py`: 10 tests cover concurrency, coalescing/rerun, bounded storms, backpressure, retry fairness/rescheduling, shutdown, collector integration, receive-loop responsiveness, and missing-data health.
- Phase 1/recovery focused regression: 41 passed. Earlier Task 8/Phase 3 check: 26 passed.
- Mutation check: changing the concurrency from 8 to 24 made `test_collector_reconnect_signals_share_workers_and_merge_closed_bars` fail (`active_count > 8`); restoring 8 passed. `REGRESSION_TEST_PROVES_BUG=true`.

### Full Regression
Fresh suite: **806 passed, 14 skipped, 0 failed**. Skips were live external API probes and PostgreSQL-gated integration tests without `TEST_POSTGRES_DSN`; no skip was silently counted as a pass. `git diff --check` passed. RUFF was unavailable in WSL.

### Reconnect Fault Injection
- Candidate image `quant-phase7:kline-recovery-ecb2e69`, ID `sha256:c657b90044c0dd8e16a8e7e353dd60e521311f00d65e8b98cc5b112823fcab93`; Docker 29.1.3, Python 3.12.3, WSL2 kernel 6.6.87.2, cgroup v2. No `.env.local`, private API credentials, proxy, production mount, or host port.
- Disposable topology: collector 1 CPU/256 MiB hard limit; engine 1 CPU/384 MiB; PostgreSQL 0.5 CPU/768 MiB. Isolated Docker network and disposable PostgreSQL 16, no persistent volume.
- Diagnostic-only wrapper scheduled faults at about 10/30/50 minutes and injected 15 deterministic public-WebSocket resets across five attached receivers. Additional live reconnects occurred; count reached 42 at the final scheduled sample and 43 in follow-up.
- Public runtime data progressed. At the post-window database read: symbols 805; market snapshots 117,410; observations 43,530; klines 82,531; trade-flow windows 4,025; CVD snapshots 4,636; screening runs 11. Earlier in-run sample: snapshots 8,810; trade-flow windows 450; CVD 304; screening runs 1.
- Phase 6 persistence was AVAILABLE. Phase 6 runtime/collector, News/Macro/Unlock, and AI provider/worker stayed NOT_AVAILABLE because their sources/provider were not configured; none was fabricated as AVAILABLE. Phase 4 liquidation/long-short and Phase 5 context also showed source errors/unavailability.

### 60 Minute Memory Verification
The sampler ran **3,601 seconds**, sampling every 30–32 seconds. At elapsed 3,570 seconds: Docker stats 203 MiB/256 MiB; cgroup current 226,631,680 bytes, peak 232,169,472 bytes (~221.5 MiB), anon 197,439,488 bytes, process RSS/HWM 211,140 KiB, 18 threads, 25 FDs; OOM/restart counters were zero. Collector health was ERROR with active=8, in-flight=8, pending=535. The sampler ended without an OOM, but the required final healthy/drained state was not reached.

During read-only follow-up, recurring reconnects continued; health showed active=8, in-flight=8, pending=597. Cgroup current reached 255,447,040 bytes and peak 258,007,040 bytes (~246 MiB); process VmRSS/VmHWM reached 237,336 KiB and anon 224,374,784 bytes. The disposable collector was then kernel-OOM-killed (`OOMKilled=true`, exit 137, RestartCount 0). Thus the runtime acceptance's no-OOM/no-exit-137 and reasonable-headroom criteria failed. The engine later exited 137 during cleanup timeout with `OOMKilled=false`; PostgreSQL exited 0. These were only the isolated candidate containers and no restart was attempted.

Sampler milestone samples nearest 0/5/10/15/20/26/30/45/60 minutes are retained in `/tmp/qmem-rem-20260924-memory-samples.log`; the scheduled 60-minute point is the 3,570-second sample above. The milestone marker used exact elapsed-second equality, so most rows were not tagged with a milestone label; this reporting limitation is acknowledged.

### Phase 6 Runtime Acceptance
Phase 6 remains NOT accepted. Unconfigured News/Macro/Unlock/AI paths correctly stayed unavailable; Phase 6 persistence was available. Because the shared collector failed the memory/backlog gate, do not emit `PHASE6_RUNTIME_INTEGRATION_COMPLETE`.

### Remaining Risks
- The fixed global worker pool bounds active work and the registry bounds pending work, but recurring real reconnects kept pending work near its 800-item cap and prevented reliable drain.
- Although the 60-minute sampler itself completed without OOM, post-window RSS/cgroup memory climbed to the 256 MiB cap and the collector was OOM-killed. Do not increase the formal cap or claim remediation complete. A new, explicitly authorized remediation must address recovery throughput/reconnect behavior and demonstrate drained backlog plus stable post-drain memory.
- Secret scan of candidate collector logs: `SECRET_LEAK_FOUND=false`. Production PostgreSQL volumes/services were untouched. No Phase 7 Runtime Integration, Phase 8/9, ECS, real AI, or live trading was performed.

## Recovery Backlog and Reconnect Amplification Audit

**Audit conclusion: `RECOVERY_SECOND_LEVEL_ROOT_CAUSE_CONFIRMED`.** This confirms recovery over-admission and a WebSocket connect race; it does **not** establish that the prior 256 MiB OOM is fully explained or remediated. No production scheduling or application code was changed.

### Fixed Artifact and Test Boundary
- Branch `phase7`; investigation checkpoint HEAD `8e8b6499e107758b3c378016127c112ae023b659` (`wip(runtime): bound gap recovery while investigating backlog`).
- One immutable collector image reused for both complete harness runs: `quant-phase7:recovery-audit-8e8b649`, image ID/digest `sha256:c657b90044c0dd8e16a8e7e353dd60e521311f00d65e8b98cc5b112823fcab93`; Python `3.12.13`.
- Disposable PostgreSQL `16.15` on an internal-only Docker network; no host port, production mount, external API, credentials, or proxy. Collector cap: 1 CPU / 256 MiB. The auto-created anonymous PostgreSQL volume and network were removed after the run; the fixed diagnostic image was retained.
- Complete deterministic run: 256.03 seconds, 2026-09-24 00:59:18–01:03:35 UTC. A preliminary harness attempt was discarded because fixture symbols had not been seeded; the reported run used a fresh database with migrations and 200 fixture symbols.
- REST responses and faults were local deterministic fixtures. Thus HTTP timings/statuses below are not Bitget measurements. The actual `CollectorService._receive_loop`, `BitgetV3UtaWebSocket`, and recovery coordinator scheduling path were exercised; diagnostic-only counters were added in a harness subclass without changing branch order or queue behavior.

### Reset to Reconnect Trace
Exactly 15 local reset events were injected (three waves over five real v3 adapter instances). For every ID below: one receive exception, one reconnect coroutine attempt, one successful reconnect, 200 subscription arguments restored, then one gap-recovery trigger. The reconnect attempt began about 12 ms after disconnect. Durations below are reset-to-success.

| Reset ID | WS index | Disconnect | Reconnect success | Subscription restore | Gap trigger |
|---|---:|---|---:|---:|---|
| R01 | 0 | observed | 40.567 s | 200 | after success |
| R02 | 1 | observed | 40.568 s | 200 | after success |
| R03 | 2 | observed | 40.568 s | 200 | after success |
| R04 | 3 | observed | 40.617 s | 200 | after success |
| R05 | 4 | observed | 40.641 s | 200 | after success |
| R06 | 0 | observed | 41.812 s | 200 | after success |
| R07 | 1 | observed | 41.812 s | 200 | after success |
| R08 | 2 | observed | 41.812 s | 200 | after success |
| R09 | 3 | observed | 41.865 s | 200 | after success |
| R10 | 4 | observed | 41.887 s | 200 | after success |
| R11 | 0 | observed | 41.821 s | 200 | after success |
| R12 | 1 | observed | 41.821 s | 200 | after success |
| R13 | 2 | observed | 41.821 s | 200 | after success |
| R14 | 3 | observed | 41.884 s | 200 | after success |
| R15 | 4 | observed | 41.884 s | 200 | after success |

The controlled result is **15 resets → 15 receive exceptions → 15 reconnect attempts/successes**, not 43. In code, `ws_reconnect_count` increments in the receive-exception handler; it counts receive exceptions, not successful reconnects or injected-reset IDs. The earlier runtime's 43 therefore cannot be attributed exactly without its missing per-event trace. If all 15 planned resets were received, the remaining 28 counter increments were additional receive exceptions, but whether those came from network disconnects, a later receive after a failed reconnect, or another cause is **not proven** by the old aggregate counter.

### Reconnect Single Flight
- Reconnect coroutine maximum: **1 per WebSocket**; five independent socket reconnects can overlap globally. No old reconnect task or second receive loop was observed.
- A separate connection-open race **was reproduced**: on the heartbeat-aligned first wave, the ping loop called `ping() → connect()` while `reconnect()` had cleared `_socket` and was awaiting its connector. `connect()` has no single-flight lock. One socket reached two simultaneous connector opens; final generations were `[4, 4, 4, 4, 5]` after three resets each (four expected, one extra). This did not add to the receive-exception counter in the controlled run. It is a confirmed race path, but is not proof that it caused the historical 43.
- No subscription restore failure, heartbeat timeout, or intentional reconnect retry occurred in the deterministic trace. Adapter replay of 200 subscriptions took about 40–42 seconds per reset.

### Recovery Arrival Rate
- Fifteen recovery requests were issued; the coordinator's already-set request Event coalesced 7 signals, so 8 scheduler scans ran.
- Each scan visited the complete 200-symbol × 4-interval universe: 800 candidate keys. Counters reconcile to exactly 8 × 800 = 6,400 candidate visits: 2,400 newly admitted work items + 3,960 pending/retry deduplications + 40 in-flight merges.
- Distinct identities: 800. Peak outstanding: 800 (transient pending peak 800; at stop-input snapshot, pending 792 + in-flight 8). Backpressure waits: 0; the capped identity set was already occupied by those same keys.
- From first work admission to stop-input (84.955 s), 2,400 items were admitted: λ = **1,695.02 work items/min**. This is a fault-burst average, not a steady-state Bitget rate.
- The final request signal had already been scheduled before the stop-input boundary; no new admissions occurred after that boundary (0). No recovery request appeared without a reset.

### Recovery Service Rate
At the same 84.955-second window, 1,616 items completed: μ = **1,141.31 items/min**. Therefore λ > μ during the injected burst and the queue reached its cap: **RECOVERY_OVERLOAD_CONFIRMED for this controlled burst**.

Once input stopped, 808 completions drained the 792 pending + 8 in-flight items in **40.437 seconds** (about 1,197 completions/min). Final completed count was 2,424; five simulated 429 failures were retried and all keys ended with zero pending, zero in-flight, zero failed. These rates use a fixed 0.35-second local response delay and are not estimates of live API throughput.

### Stop Input Drain Test
The condition was pending ≥ 500 before stopping reset injection; at stop-input it was pending 792 / in-flight 8. No more reset events were injected; request count remained 15. The queue drained in 40.437 seconds, so the requested 30-minute wait was not necessary.

| Time since stop-input | Pending | In-flight | Completed | RSS KiB | RssAnon KiB | cgroup current bytes |
|---|---:|---:|---:|---:|---:|---:|
| 0 s | 792 | 8 | 1,616 | 69,956 | 50,020 | 56,688,640 |
| 10 s | 705 | 8 | 1,711 | 69,956 | 50,020 | 56,688,640 |
| 20 s | 496 | 8 | 1,920 | 69,956 | 50,020 | 56,705,024 |
| 30 s | 283 | 8 | 2,133 | 69,956 | 50,020 | 56,725,504 |
| 40.4 s (drained) | 0 | 0 | 2,424 | 70,404 | 50,468 | 57,286,656 |
| +60 s hold | 0 | 0 | 2,424 | 70,404 | 50,468 | 57,311,232 |

Result: **queue drains with no new fault input**. This rules out a non-draining coordinator/accounting loop in this controlled fixture. Because RSS did not fall after draining, memory classification is **BACKLOG_DRAINS_BUT_MEMORY_STAYS_HIGH** for this run.

### Memory Correlation
- Before recovery: RSS 58,340 KiB; cgroup current 44,765,184 bytes.
- During work: RSS rose to 70,404 KiB; observed RSS high-water mark 70,916 KiB. Highest sampled cgroup current was 57,315,328 bytes (~54.66 MiB), well below the 256 MiB limit; no OOM or restart.
- After queue drain, RSS stayed at 70,404 KiB through the 60-second hold. Clearing per-item diagnostic timing/enqueue ledgers and running GC reduced traced Python allocations by about 0.8 MiB, but RSS did not drop. This isolates the persistent portion from the observer's per-item histories.
- Across sampled rows, descriptive Pearson values were RSS vs pending **0.093**, in-flight **0.184**, completed **0.924**, reconnect count **0.355**, HTTP outstanding **0.184**; DB-outstanding correlation was unavailable because writes were too brief to intersect a 10-second sample. These are trend-confounded, not causal estimates.
- The high completed-count association is consistent with allocations made while constructing/replacing Kline objects, plus bounded current state below; it does not show unbounded completed-history growth. The harness cannot explain the previous 256 MiB OOM by itself and does not establish a production memory leak.

### Pending Item Payload
Inspection of the actual coordinator and sampled keys shows a pending identity is only `tuple[str, str]` = (symbol, interval). It carries no Kline arrays, HTTP response, DB rows, session/client object, or per-item closure. Shared callbacks/client are coordinator-level references, not copied into every key. Failed identity state stores the key, attempt number, and retry deadline; the exception is not retained in the retry registry. No large pending payload was found.

### Completed Work Retention
- Coordinator ownership is fixed at 9 tasks: one scheduler and eight workers; there is no per-item task history or recovery-wide `asyncio.gather` result list.
- After drain, all eight idle worker frames retained their last `candles` local: one list of 100 canonical Candle objects per worker (up to 800 object references).
- The production canonical WS store retained 3,200 candles: four bars × 800 symbol/interval identities. This is bounded by the configured store capacity.
- These are confirmed bounded completed-result/current-store references. The run does not demonstrate an unbounded completed-work retention leak.

### Actual Gap Ratio
The collector's reconnect path calls the coordinator with all selected symbols and all four intervals; it does not compare expected/local latest closed bars before scheduling. In the deterministic fixture, local state had **20 actual gaps / 800 candidates (2.5%)** and **780 no-gap keys (97.5%)**. The same full-universe scans still occurred. Thus **RECOVERY_OVERBROAD_ADMISSION_CONFIRMED**. The gap ratio is fixture truth, not a claim about live market data.

### Range Duplication
Recovery identity is only `(symbol, interval)`; there is no start/end range in the key. Pending/in-flight duplicate keys were deduped or merged, but a key could be admitted again after completion on a later full scan. The 2,400 admissions over 800 identities show repeated completed-key fetches (3 admissions per identity on average in this run). **OVERLAPPING_RANGE_DUPLICATION was not applicable to the current identity model**; repeated whole-window/latest-Kline refetch is confirmed, while timestamp-window mismatch cannot be measured because the API request is for the latest fixed limit.

### API / DB Service Time
The local fixed-image collector wrote to disposable PostgreSQL. Recovery itself has no per-item DB write: successful recovery updates the in-memory canonical store; the separate periodic persistence loop writes batches.
- Synthetic recovery measurements: real HTTP/API latency is **N/A** (no public request was sent). The local fake callback's elapsed time (0.35 s artificial transport wait plus object construction) was p50/p95/max **0.377 / 0.380 / 1.044 s**; canonical Candle construction alone was **0.026 / 0.027 / 0.028 s**; queue wait was **19.02 / 36.80 / 38.73 s**; enqueue-to-store callback was **19.40 / 37.18 / 39.11 s**. The latter includes queue wait; these stages overlap and must not be added as independent network/processing durations.
- Separate PostgreSQL batch persistence: 5 writes; p50/p95/max **0.631 / 0.655 / 0.655 s**; max one DB operation at a time. Final disposable DB: 200 symbols, 1,000 market snapshots, 600 observations, 1,810 Klines, 1 health row; size **14,056,471 bytes**. PostgreSQL sampled at 44.35 MiB / 768 MiB after the run; no host port.
- Local container resource sample: collector RSS peak 70,916 KiB; cgroup current peak sampled 57,315,328 bytes; CPU peak sampled about 62.6% of its one-core quota. These values are for this short controlled fixture, not the earlier live run.

### Retry / Rate Limit
The fixture simulated five HTTP-429 outcomes at the recovery callback boundary; coordinator failure/backoff was 1 second for each, each retried once, and all recovered. These were not actual HTTP responses from Bitget and did not exercise REST-adapter 429 parsing.
Code inspection: the Bitget v3 REST adapter shares a 20-request/second token bucket (capacity 20); `_get_json` permits four attempts, retries 429 using Retry-After capped at 8 seconds or exponential 1/2/4/8-second delays, and retries client errors/timeouts. No public API request was made in this audit, so real 429/timeout distributions remain unknown.

### Second-Level Root Cause
Evidence supports **MULTIPLE_FACTORS**:
1. **Overbroad admission + repeated full-universe reconciliation** is the principal confirmed backlog amplifier: 8 full candidate scans, 6,400 candidate visits, 2,400 work admissions for only 20 fixture gaps; the queue saturated during the fault burst but drained after input stopped.
2. **Heartbeat/connect race** is a second confirmed WebSocket lifecycle defect: one extra concurrent connection generation was reproducibly opened when ping and reconnect both observed `_socket=None`. It did not explain the 43 receive-exception count in this controlled run.
3. **Bounded completed-result retention** exists (worker-local last batches plus store capacity), and RSS remains high after drain; it is not evidence of a leak and is insufficient alone to explain the prior OOM.
4. **Historical 15-to-43 attribution remains unresolved** because the prior run did not persist RESET_ID → receive exception → reconnect attempt/success traces. The counter semantics explain what 43 counts, not which 28 additional exception events occurred.

### Single-Flight Reconciler Assessment
**SINGLE_FLIGHT_RECONCILER_APPLICABILITY = YES.** Each reconnect currently requests a full universe refresh even while equivalent work is pending or already being processed. A global dirty-bit reconciler would collapse concurrent requests to one active sweep, then perform at most one follow-up gap check if dirtied during the sweep. It should be paired with actual-gap preflight and the existing per-key dedupe. This is design evidence only; it was not implemented or tested as a production change.

### Audit Boundary
No Phase 7/8/9, ECS, public Bitget, real AI, or live trading work was performed. The partial remediation remains a WIP checkpoint; this audit neither changes `active=8` / `pending=800` nor claims memory remediation or Phase 6 acceptance.

## Recovery Admission Remediation

### Previous Admission Evidence
The prior controlled baseline visited 6,400 candidates in eight full scans. Only 20/800 keys had real gaps, but 2,400 recovery items were admitted (lambda 1,695.02/min against mu 1,141.31/min), so the burst accumulated a capped backlog. This demonstrated overbroad recovery admission, not a permanent completed-work leak.

HISTORICAL_RECONNECT_AMPLIFICATION_NOT_PROVEN remains the correct conclusion for the old aggregate reconnect count of 43: that run had no per-reset exception/reconnect trace.

### Actual Gap First Design
The committed implementation checks the latest expected/local closed-bar range before scheduling expensive recovery. No-gap candidates do not become recovery work; reconnect remains a reconciliation trigger, not an unconditional full-universe recovery request. Missing data remains distinct from available data, retry/error semantics remain intact, and Stage1 semantics were not changed.

### Single Flight Reconciler
Collector reconciliation is single-flight. Requests arriving during a sweep mark the shared reconciler dirty instead of starting a parallel full sweep; after the current sweep, dirty state permits a bounded real-gap recheck. Recovery retains the existing global active-worker bound of eight and pending limit of 800.

### Dirty Generation
The deterministic reconnect-A/B/C-during-sweep test observed one active reconciler, retained dirty state, and performed a follow-up real-gap check rather than creating three parallel full-fanout batches.

### Range Coalescing
Pending work for the same symbol and interval extends/merges overlapping ranges instead of enqueuing every progressively enlarged range separately. Completed work is not silently dropped; a newly observed remaining gap can be admitted.

## WebSocket Lifecycle Remediation

### Heartbeat Reconnect Race
A deterministic red test reproduced heartbeat and receive/reconnect paths both trying to open a connection. The fix serializes connection attempts per source. Mutation verification disabling that protection made the race test fail again.

### Connection Generation
Each successful connection advances a generation. Receiver/heartbeat/reconnect work is bound to its generation; stale callbacks cannot reopen a superseded connection. Runtime invariants are at most one active connection attempt and one live connection generation per WebSocket source.

### Single Flight Connection
Fifteen planned resets across five live public WebSocket groups produced 15 observed receive exceptions and 15 successful new generations. Every reset reported max concurrent connection attempts=1 and live generations=1.

### Shutdown
At the end of the 60-minute run, the collector exited normally with exit code 0, OOM=false, restart count 0. The injection summary reported 15 resets and no failures. Container logs contained zero Task-destroyed, unretrieved-exception, pending-task, or traceback shutdown warnings.

## Verification

### Red Green Tests
Actual-gap admission, repeated reconciliation, dirty-generation behavior, disappearance-before-recovery, range coalescing, heartbeat/reconnect race, generation invalidation, and shutdown lifecycle tests pass. The required pre-fix red tests were observed failing before implementation and passing after the fix.

### Mutation Verification
Temporarily disabling actual-gap filtering/reconciler protection caused the admission regression test to fail; restoring it returned the test to pass. Temporarily disabling WebSocket connection single-flight caused the race test to fail; restoring it returned the test to pass.

RECOVERY_REGRESSION_PROVEN=true
WS_RACE_REGRESSION_PROVEN=true

### Focused Regression
Focused Phase 1 gap-recovery, closed-Kline, WebSocket, heartbeat/reconnect, and collector-lifecycle suite: 48 passed, 0 failed.

### Full Regression
Python 3.12.3; 821 passed, 14 skipped, 0 failed. The 14 explicit skips were eight opt-in public live contract probes and six PostgreSQL-gated integration tests because TEST_POSTGRES_DSN was not set for pytest. The separate runtime used its own disposable PostgreSQL; no production database was used.

### Admission Efficiency
The fixed-image admission fixture used 800 candidates, 20 actual gaps, and eight reconciliation requests. It scanned 6,400 candidate keys, admitted 20 recovery items, completed all 20, and ended with zero active, pending, or in-flight work. Subsequent scans did not readmit already repaired/no-gap candidates. The fixture summary did not expose separate item-level deduplicated and coalesced counters; no value is fabricated for those fields.

The 60-minute live runtime health counters ended at 58,400 candidates scanned, 3,734 actual-gap observations, 3,178 recovery admissions, 556 coalesced requests, 124 skipped items (the aggregate health counter does not split skip reasons), 3,054 completed items, 0 failures, and 0 active/pending/in-flight items. There were 73 reconciliation runs. The coalesced counter is not additive to admissions.

### Lambda Mu
After remediation, the fixed-image representative fixture admitted 20 items over 84.966 seconds: lambda=14.12 items/min. All 20 completed in that window, giving full-window mu=14.12 items/min; measured busy-worker service capacity was 1,137.77 items/min with the fixture's local callback. Thus representative sustained fixture lambda did not exceed mu. These local fixture rates are not claimed as Bitget API throughput.

A separate stop-input burst of 20 jobs (8 active, 12 pending) drained in 1.055 seconds with the eight-worker global bound. During live collection, transient queues were observed (maximum sampled pending 511); each sampled burst returned to zero by the next minute sample, failed count stayed zero, and health returned to AVAILABLE. These real-time bursts are recorded rather than hidden.

### WS Fault Injection
Runtime started ready in paper mode at 2026-09-24T02:02:17.828750Z with five public WebSocket groups. Planned reset waves ran at 10, 30, and 50 minutes. Each row had receive_exception_observed=true, connection_attempt_max=1, live_generations=1, result=PASS:

| Reset | Generation before -> successful |
|---|---:|
| R01 | 4 -> 5 |
| R02 | 2 -> 3 |
| R03 | 6 -> 7 |
| R04 | 2 -> 3 |
| R05 | 2 -> 3 |
| R06 | 16 -> 17 |
| R07 | 6 -> 7 |
| R08 | 11 -> 12 |
| R09 | 5 -> 6 |
| R10 | 5 -> 6 |
| R11 | 30 -> 31 |
| R12 | 7 -> 8 |
| R13 | 13 -> 14 |
| R14 | 7 -> 8 |
| R15 | 7 -> 8 |

The aggregate WebSocket receive-exception health counter ended at 80; only the 15 planned reset IDs above have per-event attribution. Historical 43 remains unproven.

### 60 Minute Memory Test
Fixed artifact: Git SHA d29fdb8210d81b031b7a8f4cbd9fdd2334e58fe9; image quant-phase7:recovery-remediation-d29fdb8, ID sha256:2ca59f380190cec40f24642649e09949d588f2bcff6123092a64ab3116137d04; Python 3.12.13. Collector cap remained 268,435,456 bytes (256 MiB), one CPU. Runtime readiness-to-stop duration was approximately 60m36s; the collector container exited 0 with no OOM or restart.

The 0-minute startup spot was taken at approximately +35 seconds (Docker stats only); the first full cgroup sample followed at +98 seconds. Later rows are the nearest recorded sample to each requested milestone. Recovery is active/pending/in-flight:

| Milestone (actual elapsed) | cgroup current / peak bytes | anon bytes | Python RSS / VmHWM KiB | active/pending/in-flight | health |
|---|---:|---:|---:|---:|---|
| 0m (+00:35, startup spot) | not sampled | not sampled | not sampled | not sampled | startup spot |
| 5m (287s) | 208,945,152 / 211,701,760 | 198,950,912 | 213,704 / 213,704 | 8 / 6 / 8 | transient ERROR |
| 10m (601s) | 209,170,432 / 215,363,584 | 198,995,968 | 213,704 / 213,704 | 0 / 0 / 0 | AVAILABLE |
| 15m (915s) | 210,034,688 / 216,948,736 | 199,131,136 | 213,704 / 213,704 | 0 / 0 / 0 | AVAILABLE |
| 20m (1,230s) | 211,570,688 / 216,948,736 | 201,793,536 | 216,168 / 216,168 | 0 / 0 / 0 | AVAILABLE |
| 26m (1,544s) | 218,202,112 / 221,442,048 | 208,535,552 | 222,440 / 222,440 | 0 / 0 / 0 | AVAILABLE |
| 30m (1,796s) | 219,078,656 / 222,826,496 | 209,563,648 | 223,336 / 223,336 | 0 / 0 / 0 | AVAILABLE |
| 45m (2,674s) | 226,414,592 / 229,285,888 | 215,089,152 | 228,712 / 228,712 | 0 / 0 / 0 | AVAILABLE |
| 60m (3,617s) | 231,104,512 / 236,167,168 | 222,064,640 | 234,760 / 234,760 | 0 / 0 / 0 | AVAILABLE |

At 60m, cgroup peak was 225.25 MiB (88.0% of the 256 MiB cap); Python VmHWM was 229.26 MiB. After the final planned reset wave, memory rose to a higher but stable plateau rather than continuing to grow linearly. Collector memory remained below the 90% cgroup threshold, with modest headroom.

PostgreSQL was a disposable Postgres 16.15 container capped at 805,306,368 bytes with a tmpfs data directory. It had no OOM/restart, but its measured cgroup peak reached 763,961,344 bytes during the run (94.9% of its cap). After the 60-minute collector had exited, the separately running disposable engine continued writing until noticed; PostgreSQL then reached 804,487,168 bytes (99.9%, OOM counters still zero). The disposable engine and PostgreSQL containers were stopped immediately. This is a PostgreSQL test-fixture headroom warning, not collector memory growth; do not extrapolate it as a production DB measurement.

At the 60m collector sample, database size was 298,114,071 bytes and collector log size 5,027 bytes. A later audit snapshot, while the engine was still running briefly after collector completion, measured database size 301,947,927 bytes. The latter is not the 60-minute endpoint. The 60m collector sample recorded no restart/OOM for collector or engine.

### Phase 6 Runtime Acceptance
The live run used TRADING_MODE=paper, no API-key/private trading credentials, no order or position route, and no live executor. No Phase 6 advanced-data mock was used. Phase 6 news, macro, unlock, AI analysis, AI extraction, AI usage, and source-registry tables each had zero rows; prompt-version registry had one seeded row.

At the final persisted health snapshot: phase6-news-ingestion, phase6-macro-ingestion, phase6-unlock-ingestion, phase6-ai-provider, phase6-ai-worker, phase6-collector, and phase6-runtime were NOT_AVAILABLE; phase6-persistence, phase6-ai-queue, phase6-ai-runtime, and phase6-ai-budget were AVAILABLE. These AVAILABLE labels do not establish the missing formal ingestion/persistence/worker lifecycle. No AI credential was configured and no AI result was fabricated.

The read-only Phase 6 integration audit remains PHASE6_RUNTIME_PARTIALLY_INTEGRATED: formal News/Macro/Unlock ingestion loops, the Phase 6 persistence writer lifecycle, AI queue worker ownership/lifecycle, and Phase 6-specific runtime health/shutdown wiring remain missing. Therefore PHASE6_RUNTIME_INTEGRATION_COMPLETE is NOT claimed. Phase 6 completion remains blocked by this pre-existing independent integration gap; no Phase 6 feature implementation was added in this remediation.

Other persisted out-of-scope health states were phase4-liquidation=ERROR, phase4-long_short=ERROR, and phase5-context=STALE. They were not modified by this task.

### Safety and Runtime Artifacts
No credential-bearing pattern was found in collector/engine/PostgreSQL logs; shutdown-warning scan count was zero. No host ports were published. Runtime image settings were collector 256 MiB/1 CPU, engine 384 MiB/1 CPU, PostgreSQL 768 MiB/0.5 CPU. The collector runtime used real public market data; TRADING_MODE remained paper.

The disposable runtime database was quant, PostgreSQL timezone was Etc/UTC, and 14 schema migrations were present. At the post-collector audit snapshot (collector stopped; the separate engine was still running briefly), exact counts included symbols 805; market_snapshots 113,810; market_observations 32,790; klines 82,534; open_interest 4,891; funding_rates 3,474; cross_exchange_derivative_snapshots 2,000; stage1_derivative_enrichment 2,600; screening_results 2,600; runtime_health_events 71; trade_flow_windows 3,974; cvd_snapshots 4,484; liquidation_events 147; liquidation_windows 136. Phase 6 ingestion/AI row counts remained zero as above.

Final code branch was phase7 at SHA d29fdb8210d81b031b7a8f4cbd9fdd2334e58fe9 before this report append. .env.local remained ignored and the source working tree was clean before documentation update.

### Final Assessment
COLLECTOR_MEMORY_GROWTH_REMEDIATED

Overall Phase 6 runtime integration is not complete: the separate static/runtime evidence still identifies PHASE6_RUNTIME_PARTIALLY_INTEGRATED. This report does not authorize or perform Phase 7 Runtime, Phase 8/9, ECS, real AI, or live trading.

## Final Phase6 Runtime Wiring + Acceptance (2026-09-24)

**Final status: PHASE6_RUNTIME_RESOURCE_BLOCKED**
**Primary blocker: POSTGRES_RUNTIME_CAPACITY_BLOCKER**
**Additional gate not met: Collector clean shutdown and three Docker restart cycles.**
This section supersedes earlier acceptance conclusions above for the 2026-09-24 run. Historical reports remain intact for audit history.

### Runtime Wiring Map

- **IMPLEMENTED:** Phase 6 V1 contracts, deterministic News classification task preparation, bounded AI queue, Gateway/schema/evidence validation, persistence repositories, usage/provenance linkage, retention, and health writes.
- **WIRED:** The official Collector lifecycle instantiates Phase6CollectorRuntime when enabled and owns its task; the official Engine lifecycle instantiates Phase6EngineRuntime, owns its supervisor/worker tasks, and cancels/gathers tasks during shutdown. The Phase 6 unit/integration suite exercises task ownership, exception isolation, shutdown, and restart/idempotency paths.
- **NOT_CONFIGURED:** No approved production News/Macro/Unlock source bindings and no AI provider/model credentials. No source or AI result was fabricated.
- **NOT_WIRED:** No live external source adapter is configured, so live News/Macro/Unlock ingestion and live AI result/usage rows were not produced. This is an intentionally unconfigured-data limitation, not evidence that lifecycle code is absent.
- Runtime category kinds are News, Macro, and Unlock; each has a dedicated health component. The concrete phase6_source_registry remained empty because no production source definition was approved/configured.

### Source Registry

The Collector owns News/Macro/Unlock lifecycle checks and records phase6-news-ingestion, phase6-macro-ingestion, and phase6-unlock-ingestion health components. With no binding, each kind is checked once, reports lifecycle=ACTIVE, source_count=0, and reason_code=SOURCE_NOT_CONFIGURED, then does not continue pointless polling. Canonical system_health.status is NOT_AVAILABLE for absent data; the details distinguish SOURCE_NOT_CONFIGURED from an operational error. The Phase 6 source-registry table had 0 rows, correctly reflecting no configured concrete source.

### Collector Lifecycle

The runtime path is Collector entrypoint → service initialization → Phase 6 runtime creation → lifecycle-owned News/Macro/Unlock checks → Phase 6 persistence/health → managed shutdown. The no-source path did not create unbounded tasks or repeatedly poll. Collector and Stage1 continued to process real public market inputs in the disposable runtime; this did not create Phase 6 News/Macro/Unlock data.

### AI Worker Lifecycle

The official Engine creates a bounded queue (capacity 32 in this runtime), consumer worker, polling/health tasks, and a lifecycle owner. During live operation the queue remained at depth 0; worker/provider health identified the provider as not configured. No orphan Phase 6 task was found by lifecycle tests. The actual container could not be run through the required three restart cycles because the PostgreSQL capacity stop gate fired.

### No Provider

No provider credential or real AI endpoint was supplied to the candidate containers. While active, health represented the provider as NOT_CONFIGURED; the worker did not invent results or retry indefinitely, and Phase 1–5 processing continued. At shutdown the persisted component health changed to NOT_AVAILABLE with stopped lifecycle as expected.

### Fake Provider

Fake Provider was used only in deterministic tests through the formal Gateway path. The Phase 6 PostgreSQL integration suite verified request processing, prompt selection, result persistence, usage/provenance, and restart/idempotency. The live disposable runtime used no Fake Provider and generated no AI rows.

### Evidence Validation

The Phase 6 focused tests exercised schema validation, allowed-evidence references, unsupported-claim rejection, task/prompt/schema identity and version semantics. The live runtime had no AI candidate or inference output to validate.

### Persistence

The isolated PostgreSQL runtime applied 14 migrations, used database quant with timezone UTC, and accepted Phase 1–5 writes. Final observed core counts were: symbols 805; market_snapshots 10,410; klines 79,930; screening_results 200; runtime_health_events 3; trade_flow_windows 583; cvd_snapshots 320; liquidation_events 2; liquidation_windows 1. Phase 6 counts were: source_registry 0; news 0; macro 0; unlock 0; AI analyses 0; AI extractions 0; AI usage 0; prompt versions 1. These zero counts are expected for unconfigured sources/provider; Fake Provider evidence exists only in the separate disposable test database.

### Health Semantics

During the active run, the Phase 6 AI queue and budget were AVAILABLE; AI runtime lifecycle was active but degraded because the provider was not configured; provider/worker were NOT_AVAILABLE with NOT_CONFIGURED details. News/Macro/Unlock health components were active with NOT_AVAILABLE data status and SOURCE_NOT_CONFIGURED reason. Phase 6 persistence health was available while the database was reachable. After shutdown, Phase 6 components were marked NOT_AVAILABLE/stopped.

Independent degraded states were also observed for Phase 4 basis, long/short and liquidation, and Phase 5 context. They did not prevent Phase 6 lifecycle activity and were not modified; recorded as PRE_EXISTING_NON_PHASE6_DEGRADATION.

### Startup Shutdown Restart

All three requested disposable services started: PostgreSQL at 03:42:52Z, Collector at 03:43:08Z, Engine at 03:44:12Z. The first completed Stage1 cycle was observed at 03:44:59Z. The Collector/Engine/PostgreSQL overlap ended around 03:48:59Z, approximately 4m47s after all three were up.

Engine and PostgreSQL stopped with exit code 0. Collector exited 137 after the default 10-second Docker stop grace; Docker reported OOMKilled=false. This is not classified as an OOM, but clean Collector shutdown is not demonstrated. No Docker restart cycle was attempted after the PostgreSQL growth stop gate, so the required three-cycle container restart acceptance remains incomplete. Unit and isolated PostgreSQL restart/idempotency tests passed.

### Failure Injection

The focused Phase 6 tests covered isolated source failures, AI failure/no-provider behavior, persistence failure paths using disposable test storage, evidence rejection and recovery/idempotency. No fault was injected into production systems or external providers.

### Focused Regression

Phase 6 PostgreSQL/runtime suite: **98 passed, 0 skipped, 0 failed**. It used a disposable PostgreSQL database and included migrations, source/runtime lifecycle, deterministic Fake Provider persistence, UTC, retention and idempotency assertions.

### Full Regression

Composite full regression: **829 passed, 8 skipped, 0 failed**. The main run reported 828 passed, 8 skipped, and 1 deselected; the deselected fresh-database migration idempotency test was run separately against an empty disposable database and passed. The 8 skips were opt-in public live API probes; no Phase 6 PostgreSQL test was silently skipped in the dedicated Phase 6 run.

### 60 Minute Integrated Runtime

A fixed candidate image was used throughout: Git source ffb648fa223065ec926470c1e512b657bc55b94e, tag quant-phase6-runtime:ffb648f, image ID sha256:3e644fc93d15c75b09502690bd80f5c07a163b0365241d8e3bea546c650c36ef, Python 3.12.13. All three services ran together for only approximately 4m47s, not the required 60 minutes. Five-minute sampling and the full-duration gate were stopped early under the explicit PostgreSQL growth rule.

### Resource Usage

Configured caps were unchanged: PostgreSQL 768 MiB / 0.5 CPU, Collector 256 MiB / 1 CPU, Engine 384 MiB / 1 CPU. There were no published host ports; PostgreSQL data used a disposable tmpfs directory and a dedicated network. No existing Quant volume/database was used.

| UTC sample | Collector | Engine | PostgreSQL |
|---|---|---|---|
| 03:45:51 | 171.7 MiB / 67.08%; 40.71% container CPU | 123.1 MiB / 32.05%; 0.76% CPU | 416.1 MiB / 54.18%; 1.09% CPU |
| 03:46:54 | 176.7 MiB / 69.01%; 49.60% CPU | 121.6 MiB / 31.68%; 0.00% CPU | 452.8 MiB / 58.95%; 0.01% CPU |
| about 03:48:15 | 178.9 MiB / 69.90%; 99.64% CPU | 121.6 MiB / 31.68%; 0.00% CPU | 479.7 MiB / 62.46%; 0.09% CPU |
| 03:49:32, after Collector/Engine stopped | stopped | stopped | 503.1 MiB / 65.51%; 0.06% CPU |

WSL host CPU was 3.7% at an earlier sample and 0.3% after the runtime stopped; host memory was 31.99 GiB total with about 1.64 GiB used at the final host sample. Collector approached one full allocated CPU, while overall host CPU remained low.

### PostgreSQL Capacity

PostgreSQL cgroup memory rose across successive observations from 416.1 MiB to 452.8 MiB, 479.7 MiB, and 503.1 MiB (65.51% of the unchanged 768 MiB cap). The growth continued during the short observation window, so Collector and Engine were stopped before the cap was approached. PostgreSQL reported no OOM. Database size grew from 99,253,271 bytes at 03:45:51Z to 119,045,143 bytes in the 03:49Z sample window; this is a short measured sample only, not a long-term projection. The PostgreSQL container then exited 0. No cap increase or persistent database modification occurred.

### Final Acceptance

- **Primary status:** PHASE6_RUNTIME_RESOURCE_BLOCKED
- **Blocker:** POSTGRES_RUNTIME_CAPACITY_BLOCKER — increasing PostgreSQL memory trend under the fixed cap caused the mandated early stop; 60-minute stability is unproven.
- **Additional unmet gate:** Collector clean shutdown/three-container restart acceptance; Collector exit 137 after the 10-second stop grace with OOMKilled=false, and no restart cycle was attempted.
- Phase 6 code wiring and focused/full regression evidence passed, but runtime acceptance is not complete.
- SECRET_LEAK_FOUND=false for all three candidate container logs. No real AI/provider credentials, private trading API, order route, live executor, Phase 7 runtime, Phase 8/9, Jev, ECS, or live trading was used.
- All candidate containers are stopped. Their containers/image/network are retained; no production resource was touched.

## PostgreSQL Memory Characterization

### Config

All A/B/C and final runtime tests used disposable PostgreSQL 16.15 instances,
database `quant`, dedicated Docker volumes/networks, and the unchanged 768 MiB /
0.5 CPU cap. The existing production PostgreSQL container and volume were not
used or modified. A/B/C used the same 68.64 MB logical seed (7,205
`market_snapshots`, 79,335 `klines`) and fixed baseline runtime image
`quant-phase6-runtime:ffb648f`, image ID
`sha256:3e644fc93d15c75b09502690bd80f5c07a163b0365241d8e3bea546c650c36ef`.
The final shutdown-fix candidate was built from branch `phase7`, base HEAD
`393cab7d87a0580a688cb1f9b7131b9cf90c2822` plus the uncommitted worktree
changes, tag `quant-phase6-runtime:shutdownfix2-20260924`, image ID
`sha256:396dde51d7d68de9476713988165c839a89a4a0807253d27db88ddbc527500c1`.
PostgreSQL image ID was
`sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67`.
Runtime environment: Docker 29.1.3, Linux kernel
`6.6.87.2-microsoft-standard-WSL2`, cgroup v2.

Read-only PostgreSQL settings snapshot (no parameters changed):

| Setting | Value |
|---|---:|
| `shared_buffers` | 128 MB |
| `work_mem` | 4 MB |
| `maintenance_work_mem` | 64 MB |
| `effective_cache_size` | 4 GB |
| `temp_buffers` | 8 MB |
| `max_connections` | 100 |
| `autovacuum_max_workers` | 3 |
| `max_worker_processes` | 8 |
| `max_parallel_workers` | 8 |
| `max_parallel_workers_per_gather` | 2 |
| Timezone | UTC |

### Cgroup Breakdown

Cgroup v2 `memory.current`, `memory.peak`, and `memory.stat` were read directly;
Docker stats alone was not treated as the memory total.

| Run / sample | `memory.current` | `memory.peak` | anon | file | shmem | inactive_file | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Collector-only A, end of ~6m40s | 617.8 MiB | 652.3 MiB | 4.8 MiB | 593 MiB | 146.9 MiB | 431 MiB | `memory.events` OOM counters zero |
| Engine-only B, early peak | ~350 MiB | ~381 MiB | low/stable | predominantly file cache | — | cache later reclaimed | Later transient ~397 MiB fell to ~143 MiB without workload/code change |
| Integrated C, ~4.5m | ~600 MiB | ~613 MiB | low/stable | file/cache dominated | — | ~419 MiB at stop sample | Cgroup events zero; after app stop current ~644 MiB / peak ~672 MiB |
| Final integrated, 0m | 187.4 MiB | 196.6 MiB | 4.3 MiB | 174.6 MiB | 60.8 MiB | 105.4 MiB | Seed loaded |
| Final integrated, 1m | 191.3 MiB | 213.1 MiB | 4.3 MiB | 177.9 MiB | 63.8 MiB | 105.4 MiB | |
| Final integrated, 2m | 460.6 MiB | 464.2 MiB | 4.4 MiB | 441.4 MiB | 136.9 MiB | 295.6 MiB | Large rise was file/cache dominated |
| Final integrated, later manual safety sample | 573.4 MiB | 577.5 MiB | ~4.8 MiB | ~550.4 MiB | ~139.8 MiB | ~401.5 MiB | App stop initiated before reaching 90% |
| Same disposable PG after the runtime and isolated shutdown diagnostics | 751.5 MiB | 754.0 MiB | 4.8 MiB | 552.0 MiB | 139.8 MiB | 396.7 MiB | 97.9% current / 98.2% peak of cap; PG was stopped cleanly |

The final integrated run's peak remained below the 90% live-run guard (691.2
MiB); it was stopped sooner because memory rose rapidly without a plateau. A
later sample, after additional isolated Engine diagnostic activity against the
same disposable volume, approached 98% of cap while the applications were not
running. That is a capacity/safety blocker, but is not evidence of anonymous
memory leak or PostgreSQL OOM. PostgreSQL and application `OOMKilled` flags were
false; no cgroup OOM event was observed in the measured A/B/C runs.

### Process RSS

The disk-backed A sample included postmaster RSS ~28.9 MiB; checkpointer samples
~5.7–64.9 MiB; background writer ~6.6–27.0 MiB (one transient ~91.3 MiB); WAL
writer ~10 MiB; autovacuum launcher ~8.7 MiB; logical worker ~8 MiB; and one
autovacuum worker sample ~169,516 KiB. These are process RSS values with shared
buffer mappings, so they must not be summed as unique physical memory. The
large cgroup `file` / inactive-file component, not rising backend anon RSS, was
the dominant measured contributor.

### Connections

The application uses scoped direct `psycopg` connections; no SQLAlchemy pool,
pool size, max-overflow, or connection-recycle configuration was found. In A,
the PostgreSQL background processes plus one monitor client stayed stable. B
connection counts also remained stable. In the final run, sampled application
connections were short-lived and the sampler saw zero non-sampler client
connections at its minute marks; transaction/query counters increased. No
`DB_CONNECTION_GROWTH_SUSPECTED` evidence was found.

### Workload

| Run | Measured workload / database evidence |
|---|---|
| Collector-only A | Database ~68.64 MB to ~153 MB; snapshots 7,205 to 21,810; klines 79,335 to 80,927; 998 trade-flow windows and 608 CVD snapshots at end. |
| Engine-only B | Database ~68.64 MB to ~103 MB; snapshots ~8,010; klines ~80,925; no Phase 6 source/AI data. |
| Integrated C | Database ~148.1 MB at stop; snapshots 16,615; klines 81,923; trade-flow 896; CVD 456; Phase 6 source/news/macro/unlock/AI tables remained empty. |
| Final integrated, 0m → 1m | Database 68,639,767 → 68,885,527 bytes; snapshots 7,205; klines 79,335. |
| Final integrated, 2m | Database 135,166,999 bytes; snapshots 10,015; klines 84,285; runtime-health events 4. Transaction, tuple, and WAL counters increased. |

Final integrated sample showed database growth concurrent with market-data
writes; the cgroup rise was mainly `file`/inactive-file cache while `anon`
remained about 4–5 MiB. Database growth is a separate disk/write-rate concern
from RAM growth. These are measured short samples only; no 30/90/180/365-day
projection is claimed.

### Cache Evidence

`pg_buffercache` was not installed in the disposable PostgreSQL image:
`PG_BUFFERCACHE_NOT_AVAILABLE`. It was not installed for this audit. `file`,
`shmem`, and `inactive_file` were read from `memory.stat`; shmem is a component
of file accounting and these fields should not be summed. Reclaimable inactive
file cache and low stable anon support a cache/write-workload interpretation,
not an anon leak. They do not by themselves establish a safe 60-minute plateau.

### A/B Runtime

All A/B/C used the same 68.64 MB seed, same disk-backed Docker volume mode,
same PostgreSQL image/config, and the prior fixed runtime image/caps. Collector
only A ran about 6m40s; Engine only B about 6.5m; integrated C was proactively
stopped at about 4.5m. A and C grew faster than B, while B showed cache
reclamation. C ended with file/inactive-file dominant and zero OOM counters.
The final shutdown-fix integrated run was also started from an identical fresh
seed on a separate case-E volume; it was stopped after roughly 4.5 minutes
before 90% because of rapid sustained growth. The 60-minute run was therefore
not attempted to completion.

### Classification

Evidence supports `POSTGRES_FILE_CACHE_GROWTH` and a short-window
`POSTGRES_WORKLOAD_PROPORTIONAL_GROWTH` classification: file/cache and database
size rose with inserts, anon and connection counts stayed stable, and the A/B
comparison showed materially less pressure in Engine-only B. It does **not**
support `POSTGRES_ANON_MEMORY_GROWTH` or `POSTGRES_CONNECTION_GROWTH`. The
long-term plateau remains unconfirmed; final operational gate is
`POSTGRES_RUNTIME_CAPACITY_BLOCKER` because the 60-minute run could not safely
be completed and a later same-volume diagnostic sample reached ~98% of cap.

## Collector Clean Shutdown

### Shutdown Timeline

The pre-fix task-stack audit identified the main Collector delay. At T+5/T+9,
three WebSocket receiver cancellation tasks were waiting in the reconnect path
while the old `websockets` socket performed its default 10-second close
handshake. Phase 4 also had a public-stream receive/context-exit task; the
Collector then closed five UTA sockets serially. An instrumented old-image run
measured SIGTERM at elapsed 207.309s; Phase 6 cancellation began at 207.314s;
gap recovery stopped at 207.668s; Phase 4 stopped by 207.776s; five UTA closes
then took about 10 seconds each, with process exit at 257.793s (~50.5s total).
REST close was sub-millisecond. This explained the former 10-second failure and
the longer 60-second success; it was not an OOM.

The first minimal fix introduced a shared public-WebSocket close timeout of 2
seconds for Bitget UTA, Phase 3, and Phase 4 connectors and closes the five UTA
sockets concurrently. A subsequent actual run still showed intermittent
10-second failures. Code path review then established a separate startup
lifecycle defect: Collector and Engine awaited the REST bootstrap inline, so a
stop event could not cancel that await. The minimum follow-up added
`await_or_stop`, which cancels and drains startup work on shutdown. No Docker
grace period was increased.

### Blocking Tasks

The first reproduced blockers were the WebSocket close handshakes and
uncancellable REST bootstrap described above. The new unit tests prove those
paths are interruptible. One final integrated run still produced an Engine
exit 137 after its 10-second grace (`OOMKilled=false`) after Collector had
stopped cleanly. Instrumented Engine-only reruns exited 0, including one that
completed a Phase 2 cycle. The task stack at T+5/T+9 of the failing integrated
Engine stop was not captured, so its exact remaining blocker is **unresolved**;
it is not attributed to Phase 6 or PostgreSQL without evidence.

### Regression Test

`tests/test_collector_clean_shutdown.py` adds six tests: concurrent closing of
all five UTA sockets; bounded close-timeout propagation through Bitget UTA,
Phase 3, and Phase 4; and cancellation of Collector and Engine REST bootstrap.
All six pass. Mutation checks temporarily restored sequential UTA close and
disabled startup cancellation; each corresponding regression failed, then the
fix was restored and tests passed: `CLEAN_SHUTDOWN_REGRESSION_PROVEN=true`.

### Docker Stop Cycles

Using immutable candidate image
`quant-phase6-runtime:shutdownfix2-20260924` under the unchanged 10-second stop
grace and 256/384 MiB application caps:

| Check | Collector stop | Engine stop | Exit / OOM | Result |
|---|---:|---:|---|---|
| Stop during startup bootstrap | 0.33s | 0.39s | both 0 / OOMKilled=false | PASS |
| Cycle 1, ~30s runtime | 0.38s | 0.59s | both 0 / OOMKilled=false | PASS |
| Cycle 2, ~30s runtime | 0.35s | 0.30s | both 0 / OOMKilled=false | PASS |
| Cycle 3, ~30s runtime | 0.36s | 0.31s | both 0 / OOMKilled=false | PASS |
| Later integrated resource-gated run | 3.05s | exceeded 10s, exit 137 | Engine OOMKilled=false | FAIL / unresolved |

The passing cycles each had one Python main process per service and the
disposable PostgreSQL remained queryable with 14 migrations. However, the later
integrated Engine failure means clean shutdown is not accepted for the full
runtime workload despite the three isolated cycle passes.

## Final Runtime Acceptance

### 60 Minute Run

Not completed. Final integrated candidate Collector and Engine started at
2026-09-24 05:40:48 UTC against a fresh, disk-backed seeded disposable
PostgreSQL. The monitored application overlap lasted about 4m29s before the
Collector exit; Engine ended about 10 seconds later. Samples at 0m, 1m, 2m and
a later manual safety check recorded PG cgroup current of 187.4, 191.3, 460.6,
and ~573.4 MiB respectively. The 2m → later sample rise was rapid and had no
observed plateau, so Collector/Engine were stopped before the 90% cap guard and
before OOM. No 5/10/15/20/30/45/60-minute acceptance is claimed.

During the later isolated Engine shutdown diagnostic on the same disposable
volume, PostgreSQL cgroup current/peak reached 751.5/754.0 MiB (97.9/98.2% of
768 MiB), mostly file/shmem/inactive-file. PostgreSQL was then stopped cleanly.
This later sample is not part of the 4m29 integrated measurement window, but
reinforces the resource blocker. PostgreSQL data volume was retained.

### Resource Usage

Final candidate caps were unchanged: PostgreSQL 768 MiB / 0.5 CPU, Collector
256 MiB / 1 CPU, Engine 384 MiB / 1 CPU. At the 2-minute runtime sample,
Collector/Engine cgroup current was 183.1/138.0 MiB, and PostgreSQL was 460.6
MiB; Docker stats showed Collector/Engine/PostgreSQL at about 183.2/137.6/165.7
MiB respectively. PostgreSQL's `memory.stat` cgroup total is authoritative for
its cap. At the later manual sample, PostgreSQL was ~573.4 MiB, with anon ~4.8
MiB and file ~550.4 MiB. No long-term database or log-size growth is
extrapolated from this short run. No Docker ports were published.

### Three Restart Cycles

The three prescribed 10-second stop/start cycles passed **before** the
60-minute gate, using the final shutdown-fix image and disposable case-B
PostgreSQL. The required three restart cycles *after* a successful 60-minute
integrated run were not performed because that run did not pass its resource
gate. Consequently, final restart acceptance remains incomplete.

### Final Status

**`PHASE6_RUNTIME_RESOURCE_BLOCKED`**

Blockers:

1. `POSTGRES_RUNTIME_CAPACITY_BLOCKER`: repeated short runs show rapid
   file-cache/workload-linked cgroup growth without a demonstrated plateau;
   a later same-volume diagnostic sample approached 98% of cap. No 60-minute
   run is safe to claim yet.
2. `PHASE6_CLEAN_SHUTDOWN_BLOCKED`: one integrated Engine stop exceeded the
   10-second grace and exited 137 (`OOMKilled=false`), while later isolated
   Engine diagnostics exited 0. The failing task stack remains uncaptured.

Tests after the changes: shutdown/Phase6/Phase3/Phase4 focused suite **145
passed, 2 skipped**; full regression **829 passed, 14 skipped, 0 failed**.
The 14 skips were 8 opt-in public live API probes and 6 PostgreSQL-gated tests
because `TEST_POSTGRES_DSN` was not configured for pytest. The separate
disposable runtime database was exercised directly; these skips were not
silently counted as passes.

Branch remains `phase7`; HEAD remains
`393cab7d87a0580a688cb1f9b7131b9cf90c2822`. Changes and this report are
uncommitted for review. All disposable PostgreSQL containers were stopped and
their volumes retained; existing Quant/Suixiangji containers, production
PostgreSQL volume, and external systems were not modified. No Phase 7 Runtime,
Phase 8/9, Jev, ECS, real AI, or live trading was started.

## PostgreSQL Reclaimability

### Cgroup Memory Breakdown

- Disposable PostgreSQL image: `sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67`.
- Disposable Collector and Engine image: `sha256:ae54b0caa4bf76c215b7fcdacf0576d36c217ab480e9f25554d1a81bb75fdcdc` (`quant-phase6-runtime:signalwake-20260924`).
- Caps remained PostgreSQL 768 MiB, Collector 256 MiB, Engine 384 MiB. Docker 29.1.3; WSL2 kernel `6.6.87.2-microsoft-standard-WSL2`; cgroup v2; branch `phase7`; WIP checkpoint HEAD `d4331bbb0fd99d8af924284379213e0b843b53e7`.
- In the controlled representative workload, PostgreSQL was stopped at the planned high-water gate: `memory.current=668,114,944` bytes (~637 MiB), `memory.peak=701,829,120` bytes (~669 MiB), `file=642,834,432`, `anon=4,960,256`, `shmem≈140 MiB`; cap was not increased.
- The 60-minute integrated run itself peaked at `616,779,776` bytes (~588 MiB) and ended at `369,025,024` bytes. Five-minute samples stayed below the 90% safety guard.
- During the additional post-restart recovery soak, the cgroup reached `memory.current=804,933,632` bytes and `memory.peak=806,182,912` against the 805,306,368-byte limit. At the captured high point `file=775,700,480`, `anon=5,169,152`, `shmem=146,837,504`, `kernel=20,598,784`, `slab=18,574,128`, `file_dirty=5,103,616`, and `file_writeback=0`. Docker working-set stats at the same check were 342.3 MiB; this is not interchangeable with cgroup `memory.current` because Docker stats discounts file cache.
- PostgreSQL processes were sampled after reclaim via `docker top`: six processes, individual RSS 28,672 / 143,092 / 140,404 / 10,036 / 8,468 / 8,020 KiB (sum 338,692 KiB; sum double-counts shared mappings). The disposable PG container lacks `ps`; `docker top` supplied the process RSS. At the later idle query, `pg_stat_activity` showed one active sampler connection and no idle-in-transaction connection; `pg_isready` passed.

### PSI / Memory Events

- Throughout the 60-minute main run: `low/high/max/oom/oom_kill/oom_group_kill=0`; PSI `some/full` averages were 0.00.
- At the restart-recovery high point: `memory.events` was `max=5,188`, `oom=0`, `oom_kill=0`, `oom_group_kill=0`; `pgscan=77,595`, `pgsteal=77,006`, including direct reclaim. PSI `some/full` averages remained 0.00; total was 67,457 μs at that sample and 72,971 μs after the idle sample. This is brief but real reclaim activity, not evidence of an anonymous-memory leak.
- No cgroup OOM kill occurred. The cgroup peak exceeded the configured limit by about 0.84 MiB during the burst, so the resource gate is conservatively held despite the absence of OOM.

### Idle Test

- Earlier controlled idle window: 2026-09-24 06:09:08–06:19:29 UTC (10m21s). At about minute 8, memory fell from ~668 MB to 175,804,416 bytes (~167.6 MiB); file fell to about 165 MB, anon stayed about 4.75 MiB, shmem about 140 MiB. End sample was about 188.6 MiB. Database remained healthy.
- After the restart-recovery burst was stopped at 08:06 UTC, `memory.current` fell from 770,719,744 bytes at 08:07:28 to 166,322,176 bytes at 08:08:29. At that point `file=154,750,976`, `anon=5,046,272`, `shmem=146,837,504`; PostgreSQL still accepted connections and OOM counters remained zero. A later idle sample was 201,318,400 bytes / 157.2 MiB Docker working set.
- Thus the high-water increment was primarily reclaimable file cache. No global `drop_caches` or host VM change was used.

### Controlled Reclaim

- `/sys/fs/cgroup/memory.reclaim` existed in the disposable cgroup, but container root did not have permission to write it (`POSTGRES_CGROUP_RECLAIM_NOT_AVAILABLE`). No privileged workaround was attempted.
- Natural idle reclaim was observed directly; the database stayed queryable. No production volume was mounted or modified.

### Working Set Estimate

- Observed idle `memory.current` samples after natural reclaim: approximately 166–201 MB (~158–192 MiB). `anon` remained about 5 MB; shmem about 147 MB at the later sample. These idle values, not the transient peak, are the practical observed floor/working-set range. Process RSS sum is higher because it double-counts shared mappings.
- The 60-minute main runtime had substantial headroom, but restart-recovery write bursts drove file cache and direct reclaim to the cgroup limit. Long-term disk/database growth is not extrapolated from this short test.

### Capacity Classification

**`POSTGRES_CAPACITY_NOT_CONFIRMED`** (classification C). File cache is demonstrably reclaimable and there was no OOM or sustained PSI, but the extra restart-recovery soak touched the 768 MiB cgroup maximum and incremented `memory.events:max` thousands of times. The acceptance criteria require no pressure anomaly; this burst prevents a defensible “capacity sufficient” result. Do not raise the cap based on this report alone; quantify/control the restart catch-up write burst in a separately approved follow-up.

## Engine Clean Shutdown

### Signal Timeline

- A real external-thread SIGTERM regression reproduced the old behavior: the signal handler only called `asyncio.Event.set()`, but the selector was not woken promptly; the fallback timer returned after 1.502s. With `loop.call_soon_threadsafe(stop_event.set)`, the loop woke in about 0.1s.
- The fix creates the handler while the loop is running and schedules the stop event thread-safely, then restores prior signal handlers in `finally`. Docker grace remained 10 seconds; no timeout increase was made.
- In the instrumented Engine stop, T+2s/T+5s task snapshots showed the wait points below; the repaired external-signal path exits within the bounded shutdown test rather than waiting for the fallback timer.
- Independent Docker stop timings with the candidate image were 0.331s, 0.327s, and 0.320s; each exited 0, `OOMKilled=false`.

### Pending Task Stack

Diagnostic-only task snapshots during shutdown showed ordinary wait points, not blocked work: Phase 6 worker `queue.get()` (`quant_phase6/runtime.py:612`); AI health/poller `stop_event.wait()` (`runtime.py:638,646`); Phase 6 supervisor `stop_event.wait()` (`runtime.py:754`); Phase 2 loop `stop_event.wait()` (`quant_phase1/entrypoints/engine.py:493`); Engine health probe `stop_event.wait()` (`engine.py:453`). The selector wakeup issue explains why the event could be set from an OS signal without promptly resuming those awaiters.

### Root Cause

The old signal handler mutated an asyncio event directly from the signal callback without explicitly waking the running event loop. An external signal could leave the selector blocked until its next scheduled timer; the issue was not a DB query, HTTP request, WebSocket receive, or worker doing long-running work. The minimal `call_soon_threadsafe` change addressed the confirmed cause.

### Red Green Test

The subprocess regression sends SIGTERM from an external thread. Before the fix it failed at the bounded shutdown assertion (observed 1.502s, exit 17); after the fix it passed. Focused clean-shutdown suite: **7 passed**.

### Mutation Verification

Temporarily disabling the `call_soon_threadsafe` wakeup made the regression fail again; restoring the fix made it pass. `ENGINE_SHUTDOWN_REGRESSION_PROVEN=true`.

### Three Docker Stop Cycles

- Pre-acceptance isolated Engine stop/start cycles, using 10-second grace: all three exit 0, `OOMKilled=false`, no forced kill; measured stop times 0.331s / 0.327s / 0.320s.
- After the 60-minute run, three additional Collector+Engine stop/restart rounds were run against disposable PostgreSQL. Every Collector and Engine stop reported `exited|0|false`; each was restarted with PostgreSQL left running. The post-start 70-second snapshots initially retained old Phase 6 health rows, so those were not counted as health recovery. Subsequent health refresh and writes confirmed recovery: `phase6-persistence=AVAILABLE` at 08:03:37 UTC, Stage1 cycle logged at 08:03:33, and counts advanced from 154,020 snapshots / 2,600 screenings / 2,600 derivative enrichments to 155,630 / 2,800 / 2,800. Exactly one Python PID per app container was observed; no duplicate cycle marker was seen in the captured recovery window. Per-round stop duration was not separately timed; all completed within the configured 10-second grace without exit 137.

## Final Acceptance

### 60 Minute Integrated Runtime

- Disposable run started `2026-09-24T06:57:53Z`, completed `2026-09-24T07:58:00Z`; measured elapsed time 3,606.4 seconds. 30-second safety polls and five-minute formal samples were used.
- Final sample at 07:57:55 UTC: PostgreSQL `memory.current=369,025,024`, `memory.peak=616,779,776`; Collector peak `215,367,680`; Engine peak `232,394,752`. During the 60-minute run all memory-event OOM counters and PSI averages were zero; all three containers remained running.
- Database size grew from 128,384,023 bytes at the initial runtime sample to 409,508,887 bytes at minute 60 (+281,124,864 bytes, measured sample only). At minute 60: `market_snapshots=149,415`, `klines=91,998`, `open_interest=4,890`, `funding_rates=3,381`, `cross_exchange_derivative_snapshots=2,000`, `screening_results=2,400`, `stage1_derivative_enrichment=2,400`.
- No Phase 6 source registry was configured; News/Macro/Unlock tables stayed empty. AI analyses/usage stayed zero; provider `NOT_CONFIGURED`; queue `0/32`; AI runtime lifecycle active/degraded. These are explicit unavailable states, not mocks.

### Regression Tests

- Clean-shutdown focused suite: **7 passed**.
- Phase 3/4/6 focused suite plus clean-shutdown: **422 passed, 4 skipped**.
- Full regression after the Engine fix: **830 passed, 14 skipped, 0 failed**. Skips were 8 opt-in public live API probes and 6 PostgreSQL-gated pytest cases because `TEST_POSTGRES_DSN` was unset. The disposable PostgreSQL workload, migrations, persistence, runtime health, and restart persistence were exercised directly in this acceptance run.

### Resource Headroom

- At minute 60, Docker stats were PostgreSQL 241.4 MiB / 768 MiB, Collector 166 MiB / 256 MiB, Engine 182.6 MiB / 384 MiB. Cgroup peaks during the one-hour run were 588.1 MiB / 768 MiB, 205.4 MiB / 256 MiB, and 221.6 MiB / 384 MiB respectively.
- The later restart-recovery soak hit the PostgreSQL cgroup limit through file-cache growth and reclaim. Collector and Engine were stopped cleanly at the guard; PostgreSQL stayed healthy and naturally reclaimed cache. This is the remaining resource blocker.

### Restart Cycles

**Three post-run restart cycles: container stop/start PASS; eventual Phase 6 health/data recovery PASS; resource-stress extension NOT PASS.** Database remained present and healthy, counts did not decrease, and resumed writes were verified. The subsequent soak was stopped when PostgreSQL crossed the 90% safety guard and reached the cgroup maximum. Disposable Collector/Engine are now stopped; disposable PostgreSQL is running and healthy; all volumes are retained.

### Final Status

**`PHASE6_RUNTIME_RESOURCE_BLOCKED`**

Reasons: the PostgreSQL capacity gate is `POSTGRES_CAPACITY_NOT_CONFIRMED` after restart-recovery reached the cgroup maximum and caused measurable direct reclaim, even though the cache later reclaimed naturally and no OOM occurred. The 60-minute runtime, Engine shutdown regression, and three post-run restart cycles otherwise passed. Do not declare Phase 6 complete or proceed to Phase 7 Runtime/Phase 8/Phase 9/ECS/real AI/live trading under this result.

## PostgreSQL Resource Budget Review

**Review date:** 2026-09-24 UTC

**Review result:** `POSTGRES_RESOURCE_BUDGET_UNDECIDED`

**Phase 6 status:** remains `PHASE6_RUNTIME_RESOURCE_BLOCKED`.

**Formal Compose PostgreSQL limit:** unchanged at 768 MiB.

This addendum records a new paired resource study. It supersedes earlier
resource-study recommendations only where it provides newer measurements; it
does not erase the historical 768 MiB restart-recovery pressure documented
above. It does not change the production Compose file, deploy an image, or
accept Phase 6.

### Fixed Workload

- Branch was `phase7`. The Engine shutdown fix and its regression test were
  already committed as `b02a5434289d631a15f0756542c6c270f6d83375`; this review
  changed no runtime code.
- Frozen application image for both arms:
  `sha256:ae54b0caa4bf76c215b7fcdacf0576d36c217ab480e9f25554d1a81bb75fdcdc`
  (`quant-phase6-runtime:signalwake-20260924`). Collector limit 256 MiB / 1
  CPU; Engine 384 MiB / 1 CPU.
- Frozen PostgreSQL 16.15 image for both arms:
  `sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67`.
  PostgreSQL CPU limit 0.5; shared memory 64 MiB. Only its memory limit varied:
  768 MiB vs 1,024 MiB.
- Both databases were independently restored from the same custom-format seed
  archive (7,743,776 bytes; SHA-256
  `97951f44738063e7a96f0d9a8ec0211cf9d9d89882c983c070e6b2c80e889012`),
  then PostgreSQL was restarted before measurement. Both baselines had
  migration count 14, 805 symbols, 7,205 market snapshots, and 0 screening
  results. Baseline database size was 68,885,527 bytes (768 MiB arm) and
  68,639,767 bytes (1 GiB arm); the small size difference is restore/layout
  variation, not application writes.
- Both arms used `TRADING_MODE=paper`, Phase 2–6 enabled, Phase 6 ingestion
  interval 300 seconds, AI queue capacity 32 and concurrency 2. No private
  API, order API, live executor, AI provider credential, Phase 6 source
  registry, or Phase 7 RPC endpoint was used. The disposable test network used
  PostgreSQL trust authentication and the `postgres` test role; this was the
  same in both arms but is not the production application's database role.
- Each arm ran 60 minutes, three planned Collector/Engine stop-start rounds,
  then a five-minute recovery soak. Sampling was once per minute. The tested
  containers had no host port mappings. The live public market adapters were
  enabled; external market responses and feed timing were not replayed from a
  deterministic capture.
- **Comparability limitation:** although images, seed, declared app settings,
  CPU limits and test procedure were held constant, live public inputs made
  the actual write workload materially different. During the measured hour,
  market-snapshot rows increased by 66,220 in the 768 MiB arm and 150,620 in
  the 1 GiB arm; database growth was 186,032,128 vs 629,030,912 bytes. Thus
  this is a paired same-configuration observation, not a strictly matched
  workload A/B. The latency/throughput differences below must not be
  attributed to the memory limit.
- Phase 6 itself was enabled, and runtime/persistence health was written, but
  feeds reported `NOT_AVAILABLE / SOURCE_NOT_CONFIGURED`; AI provider was
  `NOT_CONFIGURED`. News, macro, unlock, AI analysis, and AI usage tables all
  remained at zero in both arms. No Phase 6 data was mocked. This study
  therefore did not measure resource use for populated Phase 6 source or AI
  persistence workloads.
- Test host was WSL2 with 31 GiB RAM, 28 CPUs and 8 GiB swap, not the small
  Jakarta/production host. At the final read-only host snapshot, existing
  `quant-engine` and `quant-postgres` were still running; they were not part of
  these disposable arms and were not modified. The sampled host capacity
  cannot prove that a different production host has adequate system headroom.
- Restricted raw per-minute samples remain outside Git at
  `/tmp/phase6-resource-768-20260924.csv` and
  `/tmp/phase6-resource-1024-20260924.csv` (mode 0600). Summary results are
  recorded below; no credentials or environment-file contents were collected.

### 768 MiB Control

Run began at `2026-09-24T08:42:55Z`; the 60-minute measurement completed in
3,602.0 seconds. There were 61 main-run samples (baseline plus minutes 1–60),
followed by three planned restart rounds and five recovery samples.

| Measurement | Result |
|---|---:|
| Main-run `memory.current` maximum | 667,656,192 bytes (636.73 MiB) |
| Main-run cgroup `memory.peak` maximum | 703,184,896 bytes (670.61 MiB) |
| Main-run max anon / file / shmem / kernel / slab | 4.56 / 614.77 / 140.09 / 16.70 / 15.06 MiB |
| `memory.events` deltas, main and full run | low 0, high 0, max 0, oom 0, oom_kill 0 |
| PSI `some` / `full` total delta, full run | 0 / 0 μs |
| `pgscan` / `pgsteal` delta, full run | 0 / 0 pages |
| Five `SELECT 1` probe p50 / p95, main run | 165.113 / 183.469 ms |
| Database size, start → minute 60 | 68,885,527 → 254,917,655 bytes |
| Commits / inserted tuples / updated tuples | 4,852 / 146,963 / 829,937 |
| WAL bytes / timed checkpoints / requested checkpoints | 729,979,886 / 12 / 0 |
| Maximum observed connections | 1 |

At the end of the five-minute recovery soak, `memory.current` was
766,255,104 bytes (730.99 MiB) and cgroup `memory.peak` was 748.1 MiB. The
cache approached the cap, but `memory.events` max stayed 0; PSI, OOM, and
direct-reclaim counters also stayed 0 in this fresh-seed run. All three
planned Collector/Engine restarts and final test shutdown completed with
`exit=0`, `OOMKilled=false`; PostgreSQL remained queryable throughout.

### 1 GiB Diagnostic

Run began at `2026-09-24T09:52:32Z`; the 60-minute measurement completed in
3,602.2 seconds, followed by the same restart rounds and five-minute recovery
soak.

| Measurement | Result |
|---|---:|
| Main-run `memory.current` maximum | 1,056,378,880 bytes (1,007.44 MiB) |
| Main-run cgroup `memory.peak` maximum | 1,073,741,824 bytes (1,024 MiB) |
| Main-run max anon / file / shmem / kernel / slab | 4.70 / 974.82 / 140.12 / 27.79 / 26.13 MiB |
| `memory.events` deltas, main run | low 0, high 0, max 148, oom 0, oom_kill 0 |
| `memory.events` deltas, full run | low 0, high 0, max 1,030, oom 0, oom_kill 0 |
| PSI `some` / `full` total delta, main / full run | 5,642 / 18,428 μs |
| `pgscan` / `pgsteal` delta, main / full run | 9,067 / 9,067; 32,262 / 32,260 pages |
| Five `SELECT 1` probe p50 / p95, main run | 164.484 / 194.633 ms |
| Database size, start → minute 60 | 68,639,767 → 697,670,679 bytes |
| Commits / inserted tuples / updated tuples | 5,730 / 650,815 / 1,559,468 |
| WAL bytes / timed checkpoints / requested checkpoints | 1,296,381,789 / 12 / 0 |
| Maximum observed connections | 2 |

During recovery soak, `memory.current` peaked at 1,005.6 MiB; cumulative
`memory.events:max` rose to 1,030. No OOM/oom_kill occurred. PSI averages
rounded to 0.00% at each minute sample; the accumulated `some` and `full`
stall time was 18.428 ms over the full run. This was measurable cache reclaim,
not evidence of an OOM or sustained query stall.

The three planned restart rounds completed; PostgreSQL probes passed and the
Phase 6 persistence health component was `AVAILABLE / ACTIVE`. Source-dependent
components ultimately reported `NOT_AVAILABLE / SOURCE_NOT_CONFIGURED`. At
final test cleanup, the Collector exceeded Docker's 10-second stop grace and
was killed (`exit=137`, `OOMKilled=false`); Engine and PostgreSQL stopped with
exit 0. This is an unresolved Collector shutdown result, not a PostgreSQL
OOM. The final stop issue is included in the remaining Phase 6 lifecycle
blocker.

### Memory Composition

- PostgreSQL `memory.stat` showed very small measured anonymous memory in both
  arms (maximum 4.56 MiB / 4.70 MiB); shared memory was about 140.1 MiB. The
  `file` value includes `shmem`, so those two figures must not be added as
  disjoint totals. `kernel` includes `slab`; slab is shown for diagnosis but
  must not be double-counted.
- Dirty file pages peaked at 3.20 MiB in the 768 MiB main run and 11.83 MiB in
  the 1 GiB main run; writeback was zero at all recorded samples. Kernel/slab
  grew more in the 1 GiB arm, alongside the higher observed write volume.
- Approximate clean non-shmem file cache (`max(0, file - shmem - file_dirty)`,
  sampled per minute) reached about 473.8 MiB in the 768 MiB main run and
  830.2 MiB in the 1 GiB main run. This is a classification estimate, not a
  promise that every page can be reclaimed immediately.

### Cgroup Pressure

- 768 MiB arm: no `memory.events` low/high/max increments, no OOM, no PSI
  stalls, and no direct page scans/steals during the full recorded interval.
- 1 GiB arm: `memory.events:max` increased 148 times during the main run and
  1,030 times overall; `pgscan`/`pgsteal` increased 32,262/32,260 pages.
  Accumulated PSI time was only 18.428 ms overall, with sampled averages
  0.00%; `oom=oom_kill=0`.
- Therefore the 1 GiB arm used the added allowance to retain more cache, then
  encountered the cgroup boundary and reclaimed pages. It did not demonstrate
  a measurable database performance benefit. The 768 MiB arm's five-minute
  soak reached 97.4% of its cap without a max event in this run; the earlier
  historical same-volume soak recorded 5,188 max events and remains valid
  evidence for a different, heavier recovery workload.

### DB Performance

Each minute ran five local `docker exec` → `psql` connection + `SELECT 1`
round trips. This includes process/container invocation overhead; it is a
within-method comparative probe, not an application or network p95 SLA.

| Metric over 60 minutes | 768 MiB | 1 GiB |
|---|---:|---:|
| Probe p50 / p95 | 165.113 / 183.469 ms | 164.484 / 194.633 ms |
| Commit rate | 1.35/s | 1.59/s |
| Inserted / updated tuple rate | 40.8 / 230.5 per second | 180.8 / 433.2 per second |
| WAL generation | 202,772 bytes/s | 360,105 bytes/s |
| Database size growth | 186,032,128 bytes | 629,030,912 bytes |
| Timed / requested checkpoints | 12 / 0 | 12 / 0 |

The 1 GiB arm performed substantially more writes and produced a larger
database. Its p95 probe was about 6.1% higher, but the live input/write-volume
difference prevents causal attribution. There was no request-loss or
PostgreSQL health failure observed by the runner. Because the workload was not
deterministically matched, no throughput or latency winner is declared.

### Reclaimable Cache

The measured file-cache estimate increased substantially with workload and
limit; the 1 GiB cgroup performed page scans/steals while preserving a large
cache. Shared memory was stable near 140 MiB, and file writeback remained zero
at samples. The study did not write `memory.reclaim`, invoke global
`drop_caches`, or include a dedicated post-run idle-decay window. The older
natural-idle observations recorded in the preceding `PostgreSQL Reclaimability`
section are separate experiments and are not presented as a decay result for
these two arms.

### Headroom Policy

#### `RUNTIME_MEMORY_ACCEPTANCE_POLICY`

Apply this policy to future PostgreSQL cap decisions and runtime acceptance:

1. **Matched workload gate:** use identical immutable app/DB image IDs, the
   same verified seed archive and database settings, the same source/provider
   configuration, and deterministic captured/replayed public inputs (not
   invented market or AI observations). Run each arm for at least 60 minutes,
   three orderly restart/recovery cycles, and a five-minute recovery soak.
   Before comparing caps, commits, WAL bytes, and the principal persisted-row
   deltas between arms must be within ±5% of the declared workload target; if
   not, mark the comparison inconclusive.
2. **Hard failures:** any PostgreSQL OOM/oom_kill, failed readiness or
   persistence invariant, unexpected app restart, forced app termination,
   data loss/duplicate violation, or un-recovered runtime health failure
   blocks acceptance. A planned Collector/Engine shutdown must exit 0 within
   the configured grace period.
3. **Pressure interpretation:** `memory.current` or `memory.peak` above 90% is
   a warning, not a failure by itself. Any increase in `memory.events:max`,
   `pgscan`, or `pgsteal` is a reclaim warning. Treat it as a capacity failure
   when it coincides with either (a) `some avg60 >= 1.0%` or `full avg60 >=
   0.1%` for two consecutive minute samples, (b) matched-load p95 probe/query
   latency regressing by more than 20%, or (c) more than 10% throughput loss,
   health failure, or missed persistence. OOM and forced termination remain
   hard failures irrespective of PSI.
4. **Working-set and cache accounting:** report anon, shmem, file, kernel,
   slab, dirty/writeback, and reclaim counters separately. For a conservative
   active/non-reclaimable estimate use `anon + shmem + kernel + file_dirty +
   file_writeback`; do not add slab again because it is included in kernel.
   Report `file - shmem - file_dirty - file_writeback` separately as an
   approximate clean file-cache estimate. Require at least 20% cap headroom
   for the active estimate at the observed peak, unless a reviewed workload
   justifies a different target.
5. **Host budget gate:** confirm physical RAM and concurrent service limits
   on the actual target host. Reserve at least 25% of physical RAM for the OS,
   Docker/runtime overhead, and unmodeled services after accounting for
   simultaneous container limits and measured non-container usage. Local
   WSL2 measurements on a 31 GiB host cannot qualify a low-memory ECS.
6. **Evidence quality:** retain per-minute cgroup/PSI/database samples, exact
   workload and seed hashes, image IDs, actual database-role/config
   differences, and Docker/container stop states. Do not infer long-term disk
   growth from a one-hour run; report measured rates separately from any
   clearly labeled projection.

### Resource Recommendation

**`POSTGRES_RESOURCE_BUDGET_UNDECIDED`**

Keep the current formal PostgreSQL Compose cap at 768 MiB; do not promote the
1 GiB diagnostic value. The 768 MiB arm completed this specific fresh-seed
workload without cgroup pressure, OOM, PSI, or query-probe regression, while
the 1 GiB arm reached its cgroup peak, repeatedly reclaimed cache, and showed
no measured performance advantage. However, the data volume was not matched,
Phase 6 source/AI persistence was unavailable, the local WSL2 host does not
represent the target ECS RAM budget, and the 1 GiB Collector failed the final
10-second shutdown. Those limitations also prevent formally accepting 768 MiB
for the full Phase 6 target workload.

Before revisiting the cap, run a deterministic matched-input replay with the
intended Phase 6 source/provider availability states and production-equivalent
database role on the actual target-size host, collect per-minute Docker and
cgroup statistics for PostgreSQL/Collector/Engine, and resolve the forced
Collector shutdown. No Phase 7 Runtime, Phase 8/9, ECS deployment, real AI, or
live trading was started by this review.

## Deterministic Runtime Replay

### Replay Contract

Added `tests/runtime_replay.py`, a strict replay-record contract, a small parser
smoke fixture at `tests/fixtures/runtime_replay_v1.jsonl`, the immutable public
capture at `tests/fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz`, and
`tests/test_runtime_replay_contract.py`. Records carry a stable event ID,
source, symbol, event type, exchange timestamp, receive order, payload, and
applicable interval/connection-generation/fault metadata. Contract tests check
UTC timestamps, unique IDs, strictly ordered receive sequence, supported event
shape, deterministic digest/order, and acceptance by the real public payload
parsers. Five replay contract tests pass as part of the regression.

The gzip cassette is **3,908,112 bytes**, SHA-256
`1249ef6b7e708726d23469129f11a5fe89fc23023fd09763c3fa15f98a0b600b`. It has
84,660 records, receive-order range 1–84,660, and exchange-time range
`2026-09-07T20:00:00Z`–`2026-09-24T11:39:14.780Z`. It contains public market
payloads only; no account data or credentials were captured.

### Dataset

The cassette contains 805 instruments; 805 REST tickers; 79,136 REST Klines;
3,511 Bitget UTA ticker messages; 112 Bitget UTA Kline messages; and public
trade payloads from Bitget (172), Bybit (43), and Hyperliquid (41). The runtime
selected 200 symbols for the representative Collector replay. Five reset
control records per venue are additional to the venue payload counts below.
Aggregated `event_type_counts` are fault=35, instrument=805, kline=79,248,
public_trade=256, ticker=4,316. The cassette has 809 unique raw symbol strings
across venue-native naming; the Collector's selected Universe was 200 symbols.
The integrated cassette/database contains no synthesized OI, funding,
liquidation, news, macro, unlock, or AI observations; those persisted counts
remain zero. The separate 22-record `runtime_replay_v1.jsonl` parser smoke
fixture includes two synthetic Phase 4 liquidation payloads solely to test
adapter parsing; it is not fed to the integrated runtime or persisted.
Phase 6 source/AI availability remains `NOT_CONFIGURED`/`NOT_AVAILABLE`.

### Fixed Fault Sequence

The cassette fixes 20 `DROP_KLINE_DURING_BOOTSTRAP` events at their recorded
receive orders and 15 `WS_RESET` events. The reset order is round-robin:
Bitget UTA public, Bybit public, Hyperliquid public, repeated five times (orders
81,009 through 84,417, as encoded in the cassette). Each run consumed the same
fixed input and fault sequence. Runtime counters show five Bitget UTA
reconnects and Phase 3 reconnects of Bitget 0, Bybit 5, Hyperliquid 5; the
venue reset controls are not counted as new market trades.

### Clock

Replay payload timestamps, REST fetch timestamps, and parser `now` are derived
from the cassette's UTC timestamps; replay order is independent of network
arrival. The test-only recovery client returns only captured rows and applies a
fixed 30-second cancellable hold to missing-Kline recovery. To remove the
observed one-second wall-clock ticker persistence race, the harness sets its
own ticker persistence interval to 3,600 seconds and writes one final snapshot
after the cassette drains and actual Phase 3 flow persistence is observed.
These are diagnostic-harness controls, not production configuration changes.

The replay does **not** replace every scheduler/monotonic clock in the runtime.
Consequently time-sliced gap-scan counters can advance if shutdown is delayed;
this is visible in the third shutdown cycle below. This limitation is a further
reason not to treat these runs as a resource A/B benchmark.

### Equality Metrics

All three disposable-PostgreSQL cycles drained the identical 84,660 records:
805 instruments, 805 REST tickers, 79,136 REST Klines, 3,623 WebSocket market
messages plus five Bitget UTA reset controls (3,628 UTA records total), and
Phase 3 consumption of Bitget 172, Bybit 48 (43 trades + 5 resets), and
Hyperliquid 46 (41 trades + 5 resets). Each reported 200 selected symbols,
20 injected Kline gaps, 19 recovery admissions, five UTA reconnects, and
observed Phase 3 persistence.

| Loaded stop cycle | Collector runtime before SIGTERM | Recovery candidates at SIGTERM | Admissions / completions | Logical DB row counts |
|---|---:|---:|---:|---|
| e7f0-c1 | 17.101 s | 95 | 19 / 0 | 805 symbols; 79,117 Klines; 2,475 market observations; 825 market snapshots; 50 trade-flow windows; 8 cross-exchange flow snapshots; 10 system-health rows; 14 migrations |
| e7f0-c2 (restart on same test DB) | 13.909 s | 95 | 19 / 0 | exactly equal to c1 |
| e7f0-c3 (SIGTERM was delayed; extended recovery soak) | 62.348 s | 114 | 19 / 8 | exactly equal to c1/c2 |

The c3 candidate/completion difference is wall-clock recovery progress during
the longer pre-stop soak, not an input/fault or persisted-row difference. The
replay-level input equality passes; time-sliced runtime work is not claimed to
be identical. Restart cycles upserted existing rows without increasing any of
the listed logical counts. First-cycle PostgreSQL tuple deltas included 805
symbol inserts; 79,117 Kline inserts / 1,599 updates; 2,475 market-observation
inserts / 540 updates; 825 market-snapshot inserts / 180 updates; 50 trade-flow
window inserts; and 10 system-health inserts / 13 updates. On restart, existing
keys were updated rather than duplicated. OI, funding, liquidation, Phase 6
News/Macro/Unlock, and AI tables stayed empty. The actual replay run disables
Phase 2 and starts
the Collector only; it does not start the Engine because the cassette does not
contain the Engine's Phase 2 inputs. Thus this is representative loaded
Collector/Phase 3 replay, not a full Phase 1–6 production-equivalent replay.

## Loaded Collector Shutdown

### High Load Reproduction

The new diagnostic runner `tests/phase6_loaded_replay_runner.py` executes the
real Collector service and production parsers against captured REST/UTA/Phase 3
public messages, writing to an isolated disposable PostgreSQL 16.15 database
(`quant`, UTC). At shutdown it has completed the 200-symbol replay, persisted
market and trade-flow data, observed `phase3_flow_persisted`, and retains active
Kline-gap recovery work. In c1/c2, SIGTERM arrived with 8 recovery workers
active/in-flight, 11 queued, 19 admissions, 95 scanned candidates, and zero
recovery completions. This is a loaded stop test, not an idle shutdown test.

### Shutdown Timeline

The pre-fix loaded reproduction sampled pending asyncio tasks at T+2, T+5, T+8,
and T+9.5 seconds: 21 remained at each sample. On fixed loaded cycles, the runner
records SIGTERM and final shutdown. SIGTERM-to-completion was 0.037 s, 0.038 s,
and 0.044 s respectively, all within Docker's 10-second stop grace. The
instrumentation records the stuck task stack and total signal-to-exit interval,
not separate timestamps for every Collector subsystem. The source-controlled
shutdown order after SIGTERM is: cancel and gather the Collector-managed WebSocket
receive/ping, persistence, universe, Phase 3 stream/persistence, and Phase 6
supervisor tasks; stop gap recovery and Phase 4/Phase 3 runtimes; close public
WebSockets and mark health `STOPPED`; exit the REST client's async context; then
return from the entrypoint/event loop. The test does not emit separate elapsed
times for each of these steps, so none are inferred here.

### Blocking Task

The reproduced wait was in
`src/quant_phase3/runtime.py:Phase3PublicStreamRunner.run_dynamic`: its owned
`run_all` task awaited exchange workers, whose `run_exchange` coroutine was
blocked at `await websocket.recv()`. Setting `worker_stop` did not wake a socket
receive. The Collector's managed-task gather therefore could not finish. This
was a cancellation/lifecycle wait, not PostgreSQL memory exhaustion: the
reproductions exited 137 with `OOMKilled=false` before the fix.

### Red Green

`test_run_dynamic_cancels_blocked_receivers_when_shutdown_interrupts_refresh_wait`
was first run against the old implementation and failed because the nested
receivers remained alive. The minimal fix cancels the unfinished, owned
`run_all` task in `run_dynamic`'s `finally`, then gathers it with
`return_exceptions=True`; `run_all`'s own `finally` cancels and gathers its
exchange workers. The focused test passes with the fix. No persistence batch is
discarded and Docker grace was not increased.

### Mutation

The fix was temporarily disabled and the regression failed again; the fix was
restored and the test passed. The regression is therefore sensitive to the
specific cancellation fix (`LOADED_SHUTDOWN_REGRESSION_PROVEN=true`).

### Docker Stop Cycles

Three `docker stop --timeout 10` tests used the same replay fixture and shared
isolated test database. All three containers exited 0, `OOMKilled=false`, and
reported an empty `unjoined_tasks` list. Their measured SIGTERM-to-shutdown
intervals were below 0.05 seconds. The database row counts remained exactly
stable across restarts, including c2/c3 upserts. After the three cycles, the
isolated replay database measured 102,816,791 bytes (98 MB); this is a final
size, not a measured growth rate or a comparison against the prior 186/629 MB
arms. A sample immediately before
the c3 stop showed Collector 210.2 MiB / 256 MiB, PostgreSQL 138.2 MiB / 768
MiB, Collector CPU 0.29%, and PostgreSQL CPU 2.95%; these are single Docker
samples, **not** memory peaks or a sustained resource conclusion. Earlier
loaded replays on the old harness were also captured as RED evidence; their
variable intermediate ticker snapshots are excluded from deterministic row
equality.

## Historical PostgreSQL Resource A/B (Superseded by Replay V2 Below)

### Historical 768 MiB

No new 768 MiB resource-comparison arm was run in this turn. The prior supplied
diagnostic evidence was a 60-minute, unmatched workload: no `memory.events:max`,
no OOM, no meaningful PSI, recovery-soak peak approximately 748 MiB, and
measured database growth approximately 186 MB. This is historical evidence,
not a matched replay result.

### Historical 1 GiB

No new 1 GiB resource-comparison arm was run in this turn. Prior unmatched
evidence recorded 1,030 `memory.events:max`, approximately 32,262 reclaimed
pages, no OOM, approximately 18.4 ms accumulated PSI, and approximately 629
MB database growth. These are not comparable to the 768 MiB run and do not
establish that either limit is preferable.

### Historical Workload Equality Gate

**`BENCHMARK_NOT_COMPARABLE` — gate not met.** No paired 768 MiB/1 GiB runs
were executed. The diagnostic replay uses the Collector and Phase 3 only,
`PHASE2_ENABLED=0`, no Engine, no Phase 2 OI/Funding inputs, and unconfigured
Phase 6 source/AI paths. It cannot stand in for the requested
Collector-256 MiB + Engine-384 MiB + PostgreSQL comparison. Although cassette
input/fault totals and logical row counts were identical across the three
shutdown cycles, the third cycle's wall-clock gap-work counters differed, and
these cycles are shutdown/restart validations, not resource A/B arms.

### Historical Resource Metrics

| Metric | 768 MiB prior run | 1 GiB prior run | Matched replay A/B |
|---|---:|---:|---|
| Input events / faults | not captured as deterministic matched input | not captured as deterministic matched input | N/A — no paired arms |
| Logical DB writes / growth | workload-specific; ~186 MB growth | workload-specific; ~629 MB growth | N/A |
| `memory.events:max` | 0 | 1,030 | N/A |
| OOM | no | no | N/A |
| Reclaim / PSI | no meaningful PSI reported | ~32,262 pages reclaimed / ~18.4 ms PSI | N/A |
| p50/p95 latency, throughput, checkpoint comparison | unavailable for a matched pair | unavailable for a matched pair | N/A |

The one-shot Docker samples in the Loaded Collector section are not peaks and
must not be substituted for per-minute cgroup data, anon/file/shmem accounting,
or Engine measurements. No formal Compose cap or PostgreSQL volume was changed.

### Historical Decision

At the time of that earlier unmatched live-data study,
`POSTGRES_RESOURCE_BUDGET_UNDECIDED` and
`PHASE6_RUNTIME_RESOURCE_BLOCKED` were the correct statuses. They are
superseded by the deterministic, image-pinned Replay V2 results below. The
historical live-data measurements remain useful context but are not mixed into
the final A/B comparison.

### Earlier Regression Results

- Focused shutdown/replay/Phase 6 suite: **116 passed, 2 skipped** in 7.25 s.
- Full regression: **836 passed, 14 skipped, 0 failed** in 21.61 s.
- The 14 skips were 8 opt-in public live API probes and 6 PostgreSQL integration
  tests gated on `TEST_POSTGRES_DSN`; the loaded runtime's disposable PostgreSQL
  integration was exercised separately in the three Docker cycles above.

## Phase6 V1 Acceptance Scope

### Current Configured Runtime

The acceptance boundary is frozen to the Phase 6 configuration that is
currently deployed by `docker-compose.local.yml`; this is not a forecast of
future source or AI-provider load. Both the Collector and Engine enable
`PHASE6_ENABLED=1`. The Collector owns the registered News, Macro, and Unlock
ingestion lifecycle, and the Engine owns the AI queue/worker lifecycle and
Phase 6 health/persistence lifecycle.

The current configured availability state is:

| Capability | Current state | Acceptance evidence required |
|---|---|---|
| News source | `NOT_CONFIGURED` | Source registry and worker lifecycle start; health reports unavailable/not configured; no fabricated observations |
| Macro source | `NOT_CONFIGURED` | Same; no fabricated observations |
| Unlock source | `NOT_CONFIGURED` | Same; no fabricated observations |
| AI Provider | `NOT_CONFIGURED` | Queue/worker/poller lifecycle starts; candidates resolve to the configured-missing outcome; no provider call or fabricated AI result |
| Phase 6 persistence | `AVAILABLE` only while the real database operation succeeds; otherwise report its observed degraded/error state | Prompt/schema initialization and health writes use the disposable PostgreSQL instance; no status is assumed |

Phase 6 V1 acceptance therefore covers registration, runtime task ownership,
health semantics, persistence, no-provider behavior, and clean startup,
shutdown, and restart. It does **not** require fake News, Macro, Unlock, or AI
volume for a capacity benchmark. `NOT_CONFIGURED` must remain distinguishable
from a configured source that is stale, unavailable, or failing.

### Future Configured Source Review Boundary

When any real News, Macro, Unlock, or AI provider is configured, its provider-
specific load, rate limits, response sizes, retry behavior, persistence volume,
retention, and resource cost require a separate
`PHASE6_CONFIGURED_SOURCE_CAPACITY_REVIEW`. That future review is not a
prerequisite for deciding the current V1 PostgreSQL cap and is outside this
deterministic replay's input dataset.

## Deterministic Replay V2

### Integrated Components

The replay ran the production Collector and Engine `run_service` entrypoints
against a fresh disposable PostgreSQL database for each resource arm. Each arm
started and stopped Collector/Engine three times against the same database;
the database itself was not reset between those restart cycles. The Docker
network was internal-only, had no published ports, and had no external egress.
`TRADING_MODE=paper` was enforced. No cloud node, live exchange API, private
API, AI provider, order path, or Phase 7 ingestion was used.

Coverage and observed final logical rows for each three-cycle arm:

| Runtime area | Deterministic input / behavior | Final observed result |
|---|---|---|
| Phase 1 REST, Kline, Universe, Stage1 | 84,660 immutable cassette events; 805 instruments/tickers; 79,248 input Kline events; 20 dropped-bar faults | 805 symbols/instruments; 79,136 closed Klines persisted; 200 Universe members; 600 screening results over 3 runs |
| Phase 1 WebSocket lifecycle | UTA, Bybit, Hyperliquid streams with ordered injected resets | UTA 3,628/3,628 consumed; 15 resets replayed; reconnect/subscription lifecycle completed |
| Phase 2 derivatives | Fixed source-shaped synthetic-test-only payloads, parsed by the existing exchange adapters | 600 OI, 800 Funding, 200 cross-exchange derivative snapshots; no adapter errors |
| Phase 3 trade flow | Existing public trade path with reset events | 172 Bitget stream messages; 48 Bybit (43 trades + 5 resets); 46 Hyperliquid (41 trades + 5 resets); 50 trade-flow windows and 8 cross-exchange flow snapshots |
| Phase 3 order book | Source/runtime audit at the replay baseline | No enabled Phase 3 order-book ingestion path exists in the code; no order-book fixture was fabricated and no order-book coverage is claimed |
| Phase 4 | Actual currently wired Phase 4 path, preserving unavailable semantics | 600 Stage1 Phase 4 rows; liquidation, long/short, and basis each `NOT_AVAILABLE`; corresponding observation tables remain empty |
| Phase 5 | Engine bootstrap and context processing | 600 Stage1 Phase 5 context rows; context tables populated (8 leaders, 4 regimes, 1,800 relative-strength rows, 6 sector contexts, 200 memberships) |
| Phase 6 | Engine runtime lifecycle, health, persistence, queue/worker lifecycle with providers absent | News/Macro/Unlock/AI all `NOT_AVAILABLE` / `NOT_CONFIGURED`; Phase 6 runtime active and persistence `AVAILABLE`; no provider execution or fabricated observation/AI rows; source registry has 0 configured rows |

Phase 5's health record is deliberately not described as fully available: its
observed status is `NOT_AVAILABLE`, with `phase5_status=PARTIAL`, a heartbeat,
and `persisted=0` in that health snapshot. The actual Phase 5 data-cycle tables
were populated as listed above. This reflects partial context evidence and the
separate heartbeat detail; it is not upgraded to `AVAILABLE` in this report.

The immutable cassette replay is a test input, not a live API claim. Phase 3
trade counts include the deterministic connection-reset events as indicated in
the table. Phase 4 advanced observations and Phase 6 provider outputs remain
absent rather than being synthesized.

### Engine

The manifest pins `quant_phase1.entrypoints.collector.run_service` and
`quant_phase1.entrypoints.engine.run_service`. Each Engine process completed
bootstrap for 200 symbols (180 classified available; 200 persisted), one
Phase 2 cycle for 200 symbols, Phase 5 context processing, and an active Phase
6 lifecycle. The per-process Phase 2 cycle returned `AVAILABLE`, with 600 OI,
800 Funding, 200 snapshots, and zero errors. Each resource arm repeated this
process three times and performed three loaded 10-second-grace stop checks.

### OI/Funding

The Phase 2 cassette contains 603 fixed input payloads: 400 ticker payloads,
200 current-funding payloads, 2 instruments payloads, and 1 Hyperliquid
`metaAndAssetCtxs` payload. Its fixed UTC timestamp is
`2026-09-24T11:38:53.742093Z`; its selected 200-symbol set is frozen by the
manifest hash. The values are explicitly synthetic test fixtures and are
parsed through the existing adapters/runtime path.

Funding uses a neutral zero test value and asserts no direction. Bitget's raw
OI value remains unit-unconfirmed; the replay does not assert normalized OI
semantics. The observed 600 OI and 800 Funding outputs are deterministic
adapter/runtime persistence counts, not claims of current live exchange data.

### Replay Manifest

Manifest: `tests/fixtures/replays/phase6-replay-v2-manifest.json`.
Dataset version: `phase6-integrated-replay-v2-1`.

- Phase 1: 84,660 events, ordered receive sequence 1–84,660; event timestamp
  range `2026-09-07T20:00:00Z` to `2026-09-24T11:39:14.780Z`.
- Phase 1 event types: 805 instruments, 79,248 Kline events, 4,316 ticker
  events, 256 public-trade events, and 35 fault events.
- Phase 2: 603 events across the four payload types listed above, 200 fixed
  symbols, a fixed UTC time, per-symbol event counts, expected adapter outputs,
  and explicit synthetic/semantic annotations.
- Fault contract: 20 Kline drops; 15 ordered WebSocket resets (five
  Bitget/Bybit/Hyperliquid triplets); zero Phase 2 faults.
- Engine contract: one Phase 2 cycle per Engine process; three Engine/Collector
  restart cycles per resource arm; 300-second production scheduler interval.
- Phase 6 source states: News, Macro, Unlock, and AI Provider all
  `NOT_CONFIGURED`.
- The manifest freezes expected logical row counts for the three-cycle arm;
  the runner hard-gates all 36 listed tables. It also compares all 52 table
  row counts between resource arms.

### Dataset Hash

Combined replay dataset SHA-256:

`55bdcc43adf8a3e32bd457a2be84ccd0b306c337c03247e6c53f1a57246c56a9`

The Phase 1 cassette SHA-256 is
`1249ef6b7e708726d23469129f11a5fe89fc23023fd09763c3fa15f98a0b600b` and the
Phase 2 fixture SHA-256 is
`6c8263d4b895ac93d27b9582effe791aa618b48c388e944f927325d7b6e86cd0`.
The selected-symbol-set SHA-256 is
`d9e20ddca229009018ea17da19f0a66d753ce247580c0a69fb3ba232a2b83ddf`.

### Reproducibility

Final controlled arms used the same committed Git SHA
`8eac96b4f21d0a160212895ec8cee80d1a876b3c`, application image
`sha256:d7e5416e71c760827db0baf54df4e21364a1b7d6d1814e4ee238ba58f6d97fa6`,
PostgreSQL image
`sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`,
dataset hash, fresh empty database seed, event ordering, and fault sequence.
The runner starts PostgreSQL by the pinned image ID, not by resolving a tag
between arms.

768 MiB A/B had identical input counts, Phase 2 outputs, Collector/Engine
logical results, all 52 final table row counts, Phase 4/5 semantics, Git SHA,
both image IDs, and dataset hash. The 1 GiB arm matched the same logical and
provenance fields against A. The manifest's expected-row gate passed in all
three arms.

`REPLAY_V2_REPRODUCIBLE=true`.

Final run summaries and sample streams are retained under `/tmp/phase6-replay-v2/`:

- 768 MiB A: `768a-20260924T132958Z-01e967/summary.json`
- 768 MiB B: `768b-20260924T133225Z-29cb3d/summary.json`
- 1 GiB: `1g-20260924T133453Z-a58474/summary.json`

PostgreSQL cumulative tuple/transaction counters did vary modestly between
otherwise logically equal arms (see the resource table and performance note).
These include health/upsert and database-level runtime activity; they are not
used as a semantic equality substitute. Consequently, the measured gross
tuple-write rate is reported as diagnostic, not as a precise throughput claim.

## PostgreSQL Comparable Resource Review

### Equality Gate

| Gate | Result |
|---|---|
| Dataset hash / input totals / event-type counts | Same |
| OI, Funding, reset counts | Same |
| Collector logical outputs | Same |
| Engine bootstrap and Phase 2 cycle outputs | Same |
| All 52 final database table row counts | Same |
| Phase 4 unavailable and Phase 5 partial semantics | Same |
| Git SHA, app image ID, PostgreSQL image ID | Same |
| Manifest three-cycle expected-row gate | Pass in A, B, and 1 GiB |

The three-cycle expected key rows included 805 symbols, 79,136 Klines, 600
screening results, 600 OI, 800 Funding, 200 cross-exchange derivative
snapshots, 50 trade-flow windows, 8 cross-exchange flow snapshots, 600 Phase
4 rows all `NOT_AVAILABLE`, and zero Phase 4 advanced observations. Phase 6
News/Macro/Unlock/AI observation and execution rows remained zero.

### 768 MiB

PostgreSQL cap: 768 MiB; Collector 256 MiB; Engine 384 MiB. Each arm used a
fresh disposable database and ran three integrated restart cycles. A/B wall
times were 140.330 s and 141.359 s.

### 1 GiB

PostgreSQL cap: 1 GiB; all other resource caps, images, inputs, and restart
cycles were unchanged. Wall time was 138.807 s. The 1 GiB arm passed the same
logical and provenance comparisons against 768 MiB A.

### Performance Comparison

| Measured metric | 768 MiB A | 768 MiB B | 1 GiB |
|---|---:|---:|---:|
| Final database size | 140,246,039 B | 133,880,855 B | 142,105,623 B |
| Measured DB growth from post-migration baseline | 132,546,560 B | 126,181,376 B | 134,406,144 B |
| Peak `memory.current` | 690.9 MiB | 682.3 MiB | 693.1 MiB |
| Cgroup `memory.peak` | 696.9 MiB | 687.7 MiB | 698.4 MiB |
| Peak anon / file / shmem | 9.0 / 664.4 / 139.8 MiB | 10.0 / 656.2 / 137.4 MiB | 9.6 / 666.2 / 139.8 MiB |
| Conservative active estimate / cap headroom | 180.2 MiB / 76.5% | 179.0 MiB / 76.7% | 181.1 MiB / 82.3% |
| Approx. clean file cache peak | 524.5 MiB | 518.8 MiB | 526.4 MiB |
| Kernel / slab / dirty / writeback at active peak | 19.6 / 17.9 / 19.8 / 0 MiB | 19.7 / 18.0 / 19.1 / 0 MiB | 19.6 / 18.3 / 21.6 / 0 MiB |
| `pgscan` / `pgsteal` delta | 0 / 0 pages | 0 / 0 pages | 0 / 0 pages |
| `memory.events` max / OOM / OOM-kill delta | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| PSI `some/full` maximum `avg10` | 0.00 / 0.00 | 0.00 / 0.00 | 0.00 / 0.00 |
| PostgreSQL CPU mean / sampled max | 19.4% / 50.1% | 17.9% / 50.0% | 18.7% / 50.4% |
| DB diagnostic round-trip p50 / p95 | 203.1 / 250.1 ms | 204.1 / 283.2 ms | 192.7 / 253.8 ms |
| Max PostgreSQL connections | 3 | 3 | 3 |
| Tuple inserted / updated | 91,147 / 432,207 | 97,344 / 432,551 | 91,147 / 432,207 |
| Transaction commits | 381 | 384 | 375 |
| Gross tuple inserts / total arm wall-second | 649.5 / s | 688.6 / s | 656.6 / s |
| Transaction commits / total arm wall-second | 2.72 / s | 2.72 / s | 2.70 / s |
| Timed / requested checkpoints | 0 / 0 | 0 / 0 | 0 / 0 |
| `buffers_checkpoint` / `buffers_backend` | 0 / 16,163 | 0 / 8,192 | 0 / 16,188 |
| `memory.current` after 10 s post-replay | 690.9 MiB | 681.8 MiB | 692.4 MiB |

The conservative active estimate is computed per sample as `anon + shmem +
kernel + file_dirty + file_writeback`; `slab` is a subcomponent of kernel and
is not added a second time. Clean file cache is approximated as
`file - shmem - file_dirty - file_writeback`. `memory.stat.file` includes
shared-memory accounting; shmem is shown separately for visibility and must
not be added to file a second time. All three arms exceeded the report's prior
20% active-working-set headroom criterion; sampled scan/steal deltas were
zero. The 10-second post-replay sample showed little immediate reclaim because
PostgreSQL remained running with its shared buffers/cache. No checkpoint was
triggered during these short runs, so these measurements do not claim
long-horizon checkpoint behavior or project long-term database growth.

The query round-trip includes `docker exec` plus `psql` diagnostic overhead;
it is not server-only SQL latency. A/B tuple counters and physical DB growth
varied despite identical logical rows, so write-rate deltas are not treated as
a semantic mismatch or as statistically strong throughput evidence. Physical
growth is reported as measured, not projected.

Collector peak memory in the final pinned-image arms was 230.7–231.0 MiB of
256 MiB; Engine peak was 230.0–232.0 MiB of 384 MiB. All nine Collector/Engine
stops across the three arms had exit code 0, `OOMKilled=false`, no remaining
async tasks, and identical row counts immediately before and after stop. An
earlier exploratory 768 MiB B run, before the PostgreSQL image ID was recorded,
reached exactly 256 MiB on one Collector cycle and reported a `memory.events.max`
delta of 8; it still exited 0 with no OOM or orphan task. The final
image-ID-pinned arms did not reproduce this: Collector `max` deltas were zero
in all nine cycles. This non-reproduced event is disclosed as a warning; the
controlled-arm measurements are the acceptance basis, and Collector headroom
should still be monitored on the smaller production host.

### Resource Decision

`KEEP_POSTGRES_768M`.

For the frozen current V1 workload, 768 MiB produced no PostgreSQL max/OOM
events, no PSI stalls, no observed restart or row-state instability, and no
material diagnostic-latency or logical-throughput improvement from 1 GiB. The
1 GiB cap increased the permitted ceiling but did not reduce the actual
PostgreSQL working set. The measured 768 MiB peak left approximately 71–80
MiB of cgroup headroom in the final A/B runs. Given the previously recorded
small-host memory envelope and the unchanged 256/384 MiB Collector/Engine
limits, raising the PostgreSQL ceiling is not justified by this workload.

The replay ran under WSL2 Docker on the previously measured 31 GiB / 28 CPU /
8 GiB-swap development host. Container cgroup caps were enforced, but this
does not prove whole-host contention behavior on a 1.7 GiB production ECS; no
ECS was accessed. The 1 GiB limit is not selected because this workload did
not demonstrate a database benefit, and increasing a ceiling on the smaller
host would require a separate host-budget check.

### Final Verification

- Focused Phase 6/Phase 5 suite after the final replay-runner changes: **101
  passed, 2 skipped**.
- Full regression after the final replay-runner changes: **837 passed, 14
  skipped, 0 failed**.
- The 8 live public API probes were opt-in and not enabled. Six repository and
  PostgreSQL integration tests were skipped because `TEST_POSTGRES_DSN` was
  unset; PostgreSQL integration for this acceptance was exercised separately
  using the isolated disposable Docker databases in the three resource arms.
- Final controlled Replay V2: 768 MiB A/B reproducible; 1 GiB versus 768 MiB
  logical/provenance comparison passed; three loaded Collector/Engine stop
  cycles per arm passed.

`docker-compose.local.yml` remains unchanged at PostgreSQL 768 MiB, Collector
256 MiB, and Engine 384 MiB. The resource decision is limited to the frozen
V1 configuration; adding real News/Macro/Unlock/AI sources requires the
separate configured-source capacity review defined above.

Final Phase 6 status for this local runtime acceptance:
`PHASE6_RUNTIME_INTEGRATION_COMPLETE`.

This status means the frozen no-provider Phase 6 runtime lifecycle, health,
persistence, restart, and resource checks passed. It does not mean News,
Macro, Unlock, or an AI provider is configured, nor that any Phase 7+ runtime,
ECS deployment, real AI, or trading path was started.
