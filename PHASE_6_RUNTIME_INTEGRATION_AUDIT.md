# PHASE 6 RUNTIME INTEGRATION AUDIT

审计时间：2026-09-23T07:05:26Z

审计范围：`/home/lucas045057/projects/quant`

分支：`phase7`

审计时 HEAD：`7fc99ad73dc6d7ff2707406a3e7705ce6e766d76`

审计性质：只读静态调用图审计 + 当前本地 Quant PostgreSQL 只读证据检查。

本轮未修改 Python、配置、migration 或 Docker 运行环境；未配置/调用真实 AI；未访问 ECS。

## Final conclusion

`PHASE6_RUNTIME_PARTIALLY_INTEGRATED`

Phase 6 不是完全未接入：正式 engine cycle 在 `PHASE6_ENABLED=true` 且存在有效 `screening_run_id` 时，会同步调用 `run_phase6_context_hook()` 并构造 `Phase6ContextRuntime`。

但这只是 Stage1 后的 context-only inline hook。当前没有由 collector/engine 正式拥有的 News、Macro、Unlock ingestion loop，没有 Phase 6 persistence loop，没有 AI queue worker，也没有 Phase 6 专属 health/status lifecycle。因此不能判定为 `PHASE6_RUNTIME_INTEGRATED`。

## Actual call graph

### Collector

```text
docker-compose.local.yml
  -> python -m quant_phase1.entrypoints.collector
  -> CollectorService.run()
  -> REST bootstrap / WebSocket subscriptions
  -> canonical persistence loop
  -> optional Phase 3 runtime
  -> optional Phase 4 runtime
  -> shutdown cancellation / stop
```

审计结果：该链路没有 `quant_phase6` import、service construction、task creation、ingestion caller 或 Phase 6 persistence caller。

### Engine

```text
docker-compose.local.yml
  -> quant_phase1.entrypoints.engine
  -> main()
  -> Settings.from_env()
  -> run_service()
  -> run_once() / run_database_cycle()
  -> Stage1 + Phase 2/3/4/5 processing
  -> if settings.phase6_enabled:
       run_phase6_context_hook()
         -> Phase6ContextRuntime()
         -> build(Stage1 results, refs_by_symbol or {})
         -> return bounded context counts
  -> generic quant-engine health heartbeat
  -> scheduler shutdown/cancel
```

证据：`src/quant_phase1/entrypoints/engine.py:166-205, 301-307, 379-385`。

`run_phase6_context_hook()` 的 docstring 明确写明 source/provider adapters 由后续 runtime integration 提供；当没有 configured inputs 时只生成 bounded `NOT_AVAILABLE` context。该函数没有调用 `ExternalEventIngestor`、`Phase6Repository` 或 `AIService`。

`Phase6ContextRuntime.build()` 只消费已经传入的 `results` 与 `refs_by_symbol`，构造 `Phase6Stage1Context`；它不拉取数据，也不写数据库。证据：`src/quant_phase6/enrichment.py:86-159`。

## Required question matrix

| 检查项 | 结论 | 证据与说明 |
|---|---|---|
| 1. Collector 启动 Phase 6 service | **NO** | collector entrypoint 没有 Phase 6 import、实例化或 task。现有 service lifecycle 只覆盖 Phase 1/3/4。 |
| 2. Engine 启动 Phase 6 service | **PARTIAL** | engine cycle 有同步 `run_phase6_context_hook()`，但没有长期运行的 Phase 6 service/worker。 |
| 3. News ingestion 正式 caller | **NO** | `ExternalEventIngestor` 只存在为可直接调用的模块类；entrypoints 没有 `ingest("news", ...)` 或等价 caller。 |
| 4. Macro ingestion 正式 caller | **NO** | 同上；无正式 collector/engine source loop。 |
| 5. Unlock ingestion 正式 caller | **NO** | 同上；无正式 collector/engine source loop。 |
| 6. AI Gateway queue/worker lifecycle | **NO** | `BoundedAIQueue` 只有同步 `put/get`；`AIService.complete()` 在同一调用中 put 后立即 get，并直接调用 provider。没有 queue consumer、`asyncio.create_task`、start/stop worker 或 recovery loop。证据：`src/quant_phase6/ai.py:191-207, 266-392`。 |
| 7. Phase 6 persistence writer | **NO** | `Phase6Repository` 提供 upsert/insert 方法，但正式 entrypoints 没有调用它。没有 ingestion result 到 repository 的 runtime persistence loop。 |
| 8. Phase 6 health/status 更新 | **NO（专属）** | engine 会更新通用 `quant-engine` health；没有 Phase 6 component health、source health heartbeat 或 AI worker health writer。`phase6_source_registry.status` 也没有正式 runtime 更新 caller。 |
| 9. Phase 6 完整 worker lifecycle | **NO** | 没有 Phase 6 startup ownership、background task registry、worker exception boundary、shutdown/cancel、restart/recovery supervisor。`RecoveryTracker` 与 `safe_persist_events()` 是工具函数/对象，不等于运行中的 worker lifecycle。 |
| 10. Compose Phase 6 配置与容器 | **PARTIAL/NO** | 两个容器读取 `.env.local`，但 compose 明确设置的只有 `TRADING_MODE`、Phase 2/3/4 与 DSN；没有显式 `PHASE6_ENABLED`、source registry、Phase 6 worker 或 Phase 6 service。配置默认 `phase6_enabled=False`。证据：`docker-compose.local.yml:15-42`、`src/quant_phase1/config.py:274-298, 598-656`。 |

## Module and test inventory

Phase 6 domain package exists and contains contracts, normalization, ingestion orchestration, source registry, persistence repository, AI service primitives, recovery helpers, security, prompts and offline evaluation:

```text
src/quant_phase6/
  ai.py
  contracts.py
  enrichment.py
  ingestion.py
  normalization.py
  offline_eval.py
  persistence.py
  prompts.py
  recovery.py
  security.py
  sources.py
```

对应的 `tests/test_phase6_*.py` 覆盖 contracts、config、normalization、ingestion、persistence、AI、reliability、security、resources、migration 与 enrichment。测试中存在对 `ExternalEventIngestor`、`Phase6Repository`、`AIService` 与 `Phase6ContextRuntime` 的直接调用；这证明模块行为有测试覆盖，但不证明正式 collector/engine runtime integration。

特别是 `tests/test_phase6_enrichment.py` 直接调用 `run_phase6_context_hook()`，而 ingestion/persistence/AI 测试直接构造各自对象。没有发现把这些对象挂接到正式 service lifecycle 的测试或正式 runtime caller。

## Ingestion and adapter findings

`src/quant_phase6/ingestion.py` 提供：

- `ExternalEventIngestor`
- `SourceFetcher` protocol
- News/Macro/Unlock normalization dispatch
- freshness、dedup、bounded batch、source error isolation

但当前 package 没有实际的 News/Macro/Unlock adapter implementation 被 entrypoint 创建，也没有定时 fetch loop。`SourceFetcher` 是 protocol，不是 runtime-owned fetcher instance。

`src/quant_phase6/sources.py` 只提供 allowlisted source registry/policy，不执行 network I/O。

## Persistence findings

Migration 011 设计的表和 repository 方法均存在：

- `phase6_source_registry`
- `phase6_news_events`
- `phase6_macro_events`
- `phase6_unlock_events`
- `phase6_prompt_versions`
- `phase6_ai_analyses`
- `phase6_ai_extractions`
- `phase6_ai_usage`

`Phase6Repository` 的 `upsert_news()`、`upsert_macro()`、`upsert_unlock()`、AI insert 方法和 retention cleanup 是可调用 persistence surface，但没有从正式 collector/engine loop 进入这些方法的路径。

## Database evidence

本地当前连接为 `quant`，数据库 timezone 为 `UTC`。当前 `schema_migrations` 包含：

```text
011_phase6_external_context.sql
012_phase7_onchain_spot_context.sql
```

Migration 011 的 8 张表全部存在，但当前只读 row counts 为：

```text
phase6_source_registry | 0
phase6_news_events     | 0
phase6_macro_events    | 0
phase6_unlock_events   | 0
phase6_prompt_versions | 0
phase6_ai_analyses     | 0
phase6_ai_extractions  | 0
phase6_ai_usage        | 0
```

结论：当前数据库没有可归因于正式 Phase 6 runtime 的 rows。表存在只能证明 migration 已执行，不能证明 runtime integration；本次也没有把测试直接插入视为 runtime evidence。

## AI credential rule assessment

没有 AI Provider credential 不能成为拒绝 runtime wiring 的理由。正确的集成应当能够启动 Phase 6 runtime，并把 provider 缺失降级为 `NOT_CONFIGURED` / `NOT_AVAILABLE`，同时保持 Phase 1–5 运行。

当前问题不是“credential 缺失导致 degraded”，而是：正式 runtime 没有创建 Phase 6 AI service/worker，也没有把 provider 缺失状态纳入 Phase 6 health/persistence lifecycle。

## Missing integration items

以下项目是从当前调用图直接确认的缺失项：

1. **Missing collector wiring**：collector 未创建 Phase 6 source registry、fetcher、ingestor 或 Phase 6 task。
2. **Missing engine wiring**：engine 只有 context-only inline hook，没有 Phase 6 service owner。
3. **Missing ingestion loops**：News、Macro、Unlock 均无定时/事件驱动正式 loop。
4. **Missing persistence loop**：normalized ingestion results 没有进入 `Phase6Repository` 的正式 writer loop。
5. **Missing AI worker lifecycle**：没有异步 queue consumer、startup、exception isolation、shutdown、restart/recovery。
6. **Missing Phase 6 health lifecycle**：没有 Phase 6 source/ingestion/AI worker 的专属 heartbeat/status 更新。
7. **Missing shutdown lifecycle**：Phase 6 没有被现有 `run_service()` 的 task cancellation/gather 体系管理。
8. **Missing restart/recovery ownership**：现有 recovery helper 未被长期运行的 Phase 6 worker 持有和恢复。
9. **Missing explicit compose wiring**：没有显式 Phase 6 enable/config contract，也没有独立 worker/container（不代表必须新增容器，但必须有明确的现有容器 service ownership）。

## Audit safety result

- Code modified: **NO**
- Database modified: **NO**
- Runtime containers restarted: **NO**
- Real AI provider contacted: **NO**
- Credentials configured or read into report: **NO**
- ECS accessed: **NO**
- Phase 8 entered: **NO**

## Final status

`PHASE6_RUNTIME_PARTIALLY_INTEGRATED`

在补齐上述 runtime wiring、正式 ingestion/persistence/AI worker lifecycle、Phase 6 health/recovery，以及相应 integration tests 之前，不应把 Phase 6 标记为 Runtime Integrated，也不应把 Migration 011 的表存在当作 runtime acceptance evidence。
