# 当前入口地图

日期：2026-10-07（Asia/Shanghai）。分支：`cleanup/ai-tech-debt-v1`。审计源码基线 `4c2bc9cf7d80ddda20a316d43f18893d7a9d8031`；下述源码行号默认指该基线，实际删除和当前分支变化见清理报告。ACTIVE 指正式入口可达，不能推断本机进程已启用。

## 唯一实现入口


| Owner / classification | Canonical entry | Calls / boundaries |
|---|---|---|
| Collector ACTIVE | `python -m quant_phase1.entrypoints.collector` | All five Compose collector commands; `main` at `collector.py:1715`, Settings at `:1720`, `CollectorService` run at `:1712`. This is one implementation across profiles. |
| Engine ACTIVE | `python -m quant_phase1.entrypoints.engine` | All explicit engine Compose commands; local Compose omits command and inherits `Dockerfile:15`. `main` at `engine.py:911`, Settings at `:916`, Phase9 loading at `:918`. |
| Dashboard ACTIVE | `python -m dashboard.backend {start,serve,status,stop}` | `src/dashboard/backend/__main__.py:11-16`; `scripts/start-dashboard.ps1:34` and `stop-dashboard.ps1:24` WSL wrappers. `start` owns local PID; `serve` foreground same service, not competing implementation. |
| Current Paper ACTIVE | `python -m quant_realtime_paper {start,run,status,stop,acceptance}` | `__main__.py` forwards CLI; `cli.py:140-179` parses commands and builds `assembly.build_default_runtime` for run. `start` supervises run, same runtime; PS start wrapper -> `.sh:80` canonical CLI. |
| Paper controls ACTIVE | paper-status.ps1 / stop-paper.ps1 / verify-paper-acceptance.ps1 | Each calls `realtime-paper-control.sh`; helper validates action and invokes same module. No import caller is expected for operator CLI. KEEP wrappers. |
| Strategy ACTIVE library; generation CLI | `strategies.runtime` | Engine directly imports screen/stage1/persistence (`engine.py:23`) and research factory (`:721`). Paper assembly imports same factory (`assembly.py:323`). `python -m strategies.runtime --policy ... --output ...` at `runtime.py:177-186` only generates manifest, does not run a competing strategy service or issue approval. |
| Nautilus fixture PAPER LEGACY_ACTIVE/TEST | `python -m quant_nautilus.paper`, fixture_setup, paper_acceptance, acceptance | Documented in `docs/NAUTILUS_INTEGRATION.md:64,80,89,119`; fixture/Sandbox/research ownership must be retained until separate semantic review. Not current live-canonical Paper entry. |
| Tests ACTIVE | `python -m pytest -q`; frontend `npm test` | `README.md:18`, `pyproject.toml:25-28`, frontend `package.json:7-10`; formal Phase9 matrix/acceptance CLI is additional safety evidence, not redundant application entry. |
| Offline replay ACTIVE test ability | Data Layer replay/failure matrix, Phase6/7 replay runners | Actual test imports and fixture component provenance. Do not delete as synthetic-only garbage. |


## Paper 与研究入口区别



- **1 套 execution contract/store/risk owner**：quant_execution，版本混合V1 contract + V2 risk/plan，语义升级仍用相同 intent/result/position owner。
- **1 套 native local Paper core**：Nautilus LocalPaper/Sandbox/IntentAdapter/FundingDriver。OwnedLocalPaperRuntime只是缓存、fence、恢复编排，不是第二个撮合器。
- **2 个 Paper 入口**：quant_nautilus.paper.main (`:367`) 是 fixture受限CLI；quant_realtime_paper (`assembly.py:262`) 是当前canonical real-data Paper。旧CLI严格 isolated loopback测试DB和schema，但类LocalPaper ACTIVE。
- **2 代 trade plan语义**：quant_execution.paper_v1.PaperTradePlanV1 (`:21`) 与 trade_plan.PaperTradePlanV2 (`:11`)；risk.py:74/:91 按decision_version分支。phase9.paper_v1实现旧策略与证据，不是执行引擎。
- **backtest/research**：quant_nautilus.adapter.run_intent_backtest (`:308`) 复用 IntentAdapter；quant_nautilus.spike._FeatureStrategy (`:110`) 是另一段 synthetic spike策略，只被 tests/test_spike 和 scripts/run_nautilus_v1_spike 调用；不能把 spike收益当生产结果。

|Nautilus文件|分类|retain决策|
|---|---|---|
|owned_runtime.py / realtime_paper_adapter.py / paper.py / sandbox.py / adapter.py / funding.py / instruments.py|ACTIVE|冻结新增功能；保留当前运行及安全恢复，不删除。Paper类与旧CLI同文件需先分离才能删CLI。|
|spike.py + scripts/run_nautilus_v1_spike.py|FIXTURE_ONLY / research validation|冻结；未来确认研究替代、无有效验收用途后可删，当前不将测试存在误当垃圾。|
|acceptance.py|TEST_ONLY共享 fixture helpers，多测试依赖|KEEP正式测试能力；不是dead。|
|fixture_setup.py / paper_acceptance.py|TEST_ONLY + documented explicit CLI|冻结；测试/restart/backtest文档仍有入口。|
|nautilus_trader dependency|ACTIVE|不能删；Freqtrade已接管当前 execution后，也需确认 backtest/research是否保留才移除。|

注：dashboard/backend/paper.py:18 只认 `-m quant_nautilus.paper`、`:52` native engine报告，仅能读旧fixtureCLI heartbeat。此旧reader是活 API能力，未来接管展示前不能删；当前真实 realtime Paper状态由其他 dashboard service/session路径提供。


## 测试入口与环境

- 后端 `python -m pytest -q`，pyproject testpaths=tests。用项目 Python 3.12 安装 dev/nautilus/dashboard extras。
- 前端在 `dashboard/frontend` 运行 `npm ci`、`npm test`、`npm run typecheck`。已有 bundle/stamp 随仓库保留，启动前校验 exact byte hash。
- DB tests 必须显式提供隔离 `TEST_POSTGRES_DSN`；本轮没有配置它、没有执行 provisioning/migration/DROP/TRUNCATE，缺依赖会明确 skip。不能从 canonical DSN 自动推断测试库。
- 原 Phase9 milestone Git history 缺失时，仅对应 evidence test 明确 skip。正式 acceptance CLI 的历史校验、skip rejection 和 exact-HEAD approval 不变；不能把普通 pytest green 宣称正式验收通过。
- Collector 启动测试、Dashboard 实际 localhost 服务启停测试使用 isolated fixtures/临时目录，未触及用户运行会话。

## 入口收敛结果

- 删除无人调用的 `pipeline.run_strategy_v2`，所有当前策略生产路径继续用 `strategies.runtime.screen_batch`。
- 删除固定 2026-09-25 项目/镜像/基线的一次性 `phase7_acceptance_secret_audit.py`；正式 Data Layer 与 Phase9 安全扫描仍保留。
- `.ps1` 与 `.sh` 是平台 adapter，不是新 runtime；`start`/`run`/`serve` 是同一服务的管理模式，不应计为多套引擎。
- 保留手动 research/backtest、迁移维护、formal replay/acceptance CLI；无法证明外部部署不再使用的入口标 UNKNOWN/LEGACY_ACTIVE，未删除。
- Nautilus fixture CLI 默认冻结；当前 `LocalPaper` 类仍被真实数据 Paper 使用，不能删除整个文件。
