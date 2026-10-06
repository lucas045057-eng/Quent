# Local Quant Operations Dashboard V1 — Phase 0

调查日期：2026-09-29。真实基线 `integration/nautilus-v1`，HEAD `4b55633d0650ff232eb4fb176dccf652d49cdd66`，工作树 clean。复用已有 linked worktree `/home/lucas045057/projects/quant-integration-nautilus-v1`；原 `/home/lucas045057/projects/quant` 保留。

## 真实架构与接入点

1. Phase1–8 Data Intelligence → Phase9 immutable snapshot/evidence/pattern/decision → Python risk → neutral execution intent → Nautilus 1.231.0。Dashboard 只读取，不参与以上流程。
2. 数据来源包括 PostgreSQL 审计、`artifacts/phase9/` 的实际 Backtest/acceptance JSON、Paper worker 的 JSON heartbeat 及限定范围的运行日志。缺失不得替换成零或假交易。
3. 数据库为 psycopg/PostgreSQL；migration 016 保存 Phase9，017 保存 execution，018 保存 revalidation。新增后台不执行 migration、DDL、INSERT、UPDATE、DELETE，也不自动读取 `.env`。
4. Backtest 真实报告如 `artifacts/phase9/final-backtest.json`，schema `QUANT_BACKTEST_ACCEPTANCE_V1`，四个独立 fixture 案例包含 net PnL、fees、signed funding、account change、pattern/side/quantity 和摘要。它没有逐时点权益、逐笔成交、Sharpe/Sortino、初始资金或可信起止时间。不能把独立案例累计成同一条资金曲线。
5. Paper heartbeat 如 `artifacts/phase9/final-paper/BTCUSDT-LONG/initial.json`，包含 PID、as_of、native state、order states、position、result/funding digests、RSS 和 restored。当前这些是已停止的历史 fixture；不能据旧 PID 或旧 heartbeat 显示 RUNNING。
6. PositionSnapshotV1 包含 equity、available balance、native margin、realized trade PnL、unrealized PnL、fees、funding、source order/fill references、保护与对账状态。execution_positions 保留不可变快照。缺失 take profit/open time 不补造。
7. ExecutionIntentV1 在 execution_intents 中保存 identity、policy/input hashes、approved quantity、reference price、market IOC、stop/risk constraints、policy versions 和 client order ID。ExecutionResultV1 在 execution_results 保存累计成交数量/均价/费用以及事件时间。它不是逐笔 Trade，重复保护结果不会变成新成交。
8. DecisionCandidateV1 在 phase9_decision_candidates 中保存 direction_bias、eligible、confidence、pattern、supporting/conflicting/degraded/missing evidence、reason/veto、review reference、hash/version 和 lifecycle references。原始可解释链连接 snapshots、items、chains、pattern matches、Jev review 与 status events。
9. Risk 为纯 Python 放行函数；批准后的 intent 可证明一次批准，但拒绝结果目前没有独立持久化审计表。无 intent 不能推断为 Risk REJECT；应显示 Risk NOT_RECORDED，另展示 Phase9 本身的实际 veto/reason。
10. Reconciliation 使用本地原生 Sandbox input replay 与不可变审计核对。健康快照含 RECONCILED，但 positions/orders/trades/database 的独立 match 标志未全部保存；缺失 match 显示 NOT_AVAILABLE，不能全部标成 true。FundingAdapter 已实现，有单次 payment identity；实时是否可用取决于实际 heartbeat/audit。
11. 现有 HealthRegistry 为进程内对象，runtime_health_events 为持久化状态事件；没有 FastAPI/dashboard/HTTP health endpoint。日志分布在 artifact 子目录和既有 structured events，没有统一可远程控制 Paper 的安全 lifecycle API。

## 技术栈调查

项目 Python >=3.12，已有 aiohttp/websockets/pydantic/psycopg，Nautilus exact 1.231.0。Windows Node 24.14.1 可用，WSL 没有原生 node。本轮前端在 Windows 工作副本中构建，经过 hash-guard 同步源码和构建产物到 WSL；正式启动使用已构建静态资源，不要求同时运行 Node/Vite 服务。

采用独立 FastAPI API + React/TypeScript/Vite + Recharts。使用 `[FastAPI StaticFiles](https://fastapi.tiangolo.com/tutorial/static-files/)` 服务构建产物，与 API 共用 loopback 3000；[Vite build](https://vite.dev/guide/build) 提供静态 bundle；[Recharts](https://recharts.github.io/en-US/api/LineChart/) 用于有真实采样数据时的权益/回撤和实际报告比较。

两种替代方案：分别启动 Vite/8000 API 会增加进程和跨域管理；Python HTML 管理页更少依赖，但不符合用户优先 React/TypeScript 的前端方向。选择单一端口、独立只读 backend。

## 展示与安全决定

- 六个页面：Overview、Paper Trading、Decision Inspector、Backtest、System Health、Logs。3 秒 polling；Backtest 按需读取。
- 默认读现有项目 artifacts，显式 `QUANT_DASHBOARD_DSN`/schema 配置启用只读 PostgreSQL。未配置/库不可用/表未就绪分别报告，不使用开发 fixture 填充正式页面。
- 每次 DB 连接使用 READ ONLY transaction、2 秒 connection timeout、1.5 秒 statement timeout、有界 SELECT。只接受本地数据库连接；不读取或传递交易所 credentials。
- GET-only API；禁止 Live 路由、交易控制、Risk 编辑。没有可复用的安全 Paper lifecycle API，所以 V1 不新增 Start/Stop/Restart Paper。Dashboard 自身有独立 Windows 一键启动/停止。
- DTO allowlist + recursive sensitive-field/text redaction；原始错误不回显、访问日志不记录 query secrets。原始信息只在过滤后展开。
- 旧 Paper snapshot 作为历史资料，当前 account/positions 与历史严格分开；fresh heartbeat + 同一实际 Paper process 才能确认运行。
- 没有真实 curve/trade/metric 时显示 N/A 与具体缺失原因；可读取有真实时间序列的扩展历史记录，图表不模拟数据。
- 暗色运维终端风格，突出 LIVE DISABLED、Jev NOT_CONFIGURED、数据时点/来源/fixture，1366×768 和 1920×1080 均验收。

原始 Core/Risk/Adapter/Paper/Backtest/Reconciliation/Funding 源码不改。测试仅创建新 disposable loopback PostgreSQL，不写原数据库；退出后清理。用户明确要求 Phase 0 后连续执行，因此设计/计划自审后直接实施，不重复请求人工确认。
