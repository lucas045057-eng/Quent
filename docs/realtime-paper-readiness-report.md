# Realtime Paper Readiness V1 — 交付与验收报告

审计日期：2026-09-29
分支：integration/nautilus-v1
审计基线：28f120a4（本轮变更前）

历史状态说明：本文描述 2026-09-29 的实现与验收状态；当前架构与依赖以根目录六份 AI 技术债清理报告为准。

## 1. 当前架构

本轮沿用现有 Phase1–8 公共数据链，没有新增 collector、行情表、OI/Funding、Trades/CVD 或 canonical feature pipeline。

    Bitget public REST/WS（既有 Phase1 collector）
      → Phase1 repositories / canonical PostgreSQL
      → 既有 Phase1 Stage1
      → 本轮只读 realtime-paper monitor 与 readiness/safety gate
      → SQLite operations ledger（仅保存会话、周期、决策和事件）
      → Dashboard GET /api/realtime-paper

既有预期下游 Phase9 → Risk → Nautilus Adapter → Local Paper 没有与 Phase1 engine 形成生产运行链。本轮未伪造这段连接，也未改变旧 Local Paper 行为。源码审计见历史 [current-strategy-map.md](archive/current-strategy-map.md)。

## 2. Strategy 真实规则

Phase1 Stage1 使用已关闭的 5m/15m/1H/4H Kline，并检查成交额、spread 与多周期结构。Freshness 按阶段/数据类型判定：Stage1 ticker soft/hard 为 30/60 秒；闭合 Kline 沿用各周期现有 grace；Phase9 当前价格证据 hard 上限 60 秒；Risk/Execution quote hard 上限仍为 5 秒。没有把所有行情全局要求为 ≤5 秒。Stage1 只将 symbol 分类为 A/B/C/D；A 是深度分析候选，不是买入信号。Phase9 默认关闭，当前没有启用且获批的 LONG/SHORT pattern，因此当前生产规则没有 LONG 或 SHORT 下单条件。

Stage1 按顺序执行硬过滤：无成交额为 D；spread 大于 0.2% 为 C；1H/4H 任一为 RANGE 或方向冲突为 C；A 还需 spread ≤0.15%、5m range/ATR ≥2、5m EMA(9/21) 与 1H/4H 同向；否则为 B/等待。Volume 虽被列为输入，但不参与该分类公式。详细规则和源码位置见历史 [current-strategy-map.md](archive/current-strategy-map.md)。

## 3. Evidence

| Evidence | 现有来源/计算 | 当前实际影响 |
|---|---|---|
| Ticker、成交额、spread | Phase1 Bitget ticker → canonical PostgreSQL | Stage1 硬过滤；ticker stale 会拒绝 |
| 多周期结构、EMA、ATR/range | Phase1 已关闭 Kline，Stage1 计算 | Stage1 分类；不直接产生 side |
| Volume | Phase1 OHLCV | Stage1 当前公式不使用 |
| OI、Funding | Phase2 canonical 表；Phase9 可投影 | 当前没有获批规则消费；本轮读取 Funding 只做 freshness 状态 |
| Trades/CVD | Phase3 trade-flow windows | 当前没有获批规则消费 |
| Phase4–8 上下文 | liquidation、regime、options、on-chain 等既有数据 | 没有 active approved pattern 时不决定方向 |
| Jev/GPT | 未配置/未接入 | 不参与决策 |

## 4. Screening

Universe 来自既有 Phase1 select_universe()：在线的 USDT 永续合约，按 24h turnover 降序后最多取默认 200 个。没有代码支持的“100→30→10”多级漏斗。

Stage1 只产出 A/B/C/D 分类，不创建 LONG/SHORT DecisionCandidate。Phase9 intake、evidence 和 pattern evaluation 的实现存在，但默认关闭、未获批，因此没有当前有效的下游筛选路径。

## 5. Risk

既有 RiskPolicyV1.approve_intent() 实现 Paper/Backtest 模式、候选有效性、hash/version、报价/账户新鲜度、账户 reconciliation、open-intent、reserved risk、exposure、margin、spread/slippage、stop 方向/tick、数量和名义金额边界。ExecutionStore 和 Nautilus adapter 也有 intent/order 去重保护。

本轮 monitor 不调用该审批函数。当前 Phase9 未启用/无批准策略，Candidate validity 未持久化，因而没有可安全送入该 Risk Engine 的生产候选。本轮 readiness gate 将这些前提标为未就绪并停止新 Paper order。

## 6. Position sizing

既有 Risk Engine 的风险仓位公式为：

    quantity = floor_to_step(min(available_risk / loss_per_unit,
                                 notional_cap / notional_price,
                                 instrument.max_quantity))

随后检查 min quantity/notional。Risk Engine 要求调用方提供 stop；Stage1 没有生成 ATR stop。公式详见 src/quant_execution/risk.py；本轮未另造仓位公式。

## 7. Exit logic

Nautilus adapter 在成交后按累计成交量提交 reduce-only STOP_MARKET 保护单，并处理部分成交后的保护数量。当前没有已连接运行路径中的 take-profit、trailing stop、signal/timeout exit、daily-loss、max-drawdown、cooldown 或独立 max-position 规则。该 adapter 不是本轮实时 monitor 的执行组件。

## 8. 当前实时数据源

CURRENT_PAPER_DATA_SOURCE = FIXTURE：既有 src/quant_nautilus/paper.py 使用隔离 test DB、fixture schema 和 SandboxSession 行情，报告为 FIXTURE_DRIVEN_ACCEPTANCE。既有 Phase1–8 collector 有真实 Bitget 公共行情，但旧 Local Paper 并未直接使用它。

本轮新增 monitor 的输入来自既有 canonical PostgreSQL。运行周期和决策分别标记 REAL_PUBLIC_DATA / SYNTHETIC_FIXTURE / MIXED_REJECTED / UNKNOWN；正式 24h evaluator 只接受全程精确 REAL_PUBLIC_DATA，不接受 fixture 或 unknown。

## 9. Realtime Paper 数据流

本轮已落地的是真实数据 readiness/decision observation：复用 Phase1 repository 和 Stage1，另读取 Phase2 Funding 的新鲜度；每周期执行 freshness/数值/来源/时钟/collector/DB 检查，将 Stage1 结果、Risk/readiness 拒绝原因、Decision 和运行周期写入 operations-only SQLite；Dashboard 只读该 SQLite。

它尚未进入 Nautilus Local Paper。缺失 Phase9 approved policy、完整 candidate-validity、Risk approval caller、Paper order/position、runtime reconciliation 的接线，因此不能声称实现了“真实行情 → Paper 成交”的闭环。

## 10. 自动运行机制

新增统一入口支持短测及 24h、72h、7d：

- Windows：scripts/start-realtime-paper.ps1 -Duration 24h
- stop/status：scripts/stop-paper.ps1、scripts/paper-status.ps1
- 完成会话的正式验收：scripts/verify-paper-acceptance.ps1 -Duration 24h

启动脚本只复用现有 quant-postgres 和 quant-collector 容器；不会创建替代服务或采集 pipeline。monitor 循环读取既有 DB、调用 Stage1、评估安全 gate 并记录结果。所有新单在 gate 未就绪时被禁止。

## 11. 重连机制

行情 WebSocket 的自动重试/重连由现有 Phase1 Bitget adapter 实现（src/quant_phase1/adapters/bitget_v3/websocket.py）；本轮未复制其连接管理。monitor 对 DB/collector 临时不可用会记错、保持 DO NOT TRADE，并继续下一周期重新读取。新增测试覆盖断开/恢复后重新进入观测周期。

本轮开始前发现原有 quant-collector 容器已停止（退出码 137）；启动的是该既有容器。为运行观察，仅对当前容器临时执行资源上限 768 MiB、swap 1536 MiB 的运行态覆盖；提交中的 docker-compose.local.yml 保留项目原有 collector 预算 256 MiB，未改镜像或采集逻辑。容器重建会回到 256 MiB，需继续查明当前启用 Phase2–6 时的内存峰值。

## 12. 按数据类型和阶段冻结 Freshness

门限先用既有 Phase1–8 的 REAL_PUBLIC_DATA 运行样本校准，再按数据语义、采集 cadence 和已有 Stage1 grace 冻结。表内分位数来自当前真实数据：Bitget WS ticker 与 liquidation delivery 使用过去 24 小时 canonical captures；其余稀疏 cross-section 使用修改后 16 分钟短测窗口末样本，并标明 n。小样本只作诊断；近期抓取的历史 Kline 回补不纳入实时 age 分布。软门限进入 DEGRADED，超过 hard 即 STALE 并维持 DO NOT TRADE；获批 Phase9 manifest 只能收紧门限。Execution quote hard cap 仍为 5 秒。

| Data type | Expected cadence | REAL_PUBLIC_DATA age P50 / P95 / P99 | Chosen soft / hard | Reason |
|---|---|---:|---:|---|
| PRICE_STAGE1 ticker | 既有 Bitget public ticker stream，配置/采集目标约 5 秒 | 87.24 / 166.37 / 166.47 秒（过去 24h Bitget WS n=367,000）；16m 短测为 99.87 / 127.70 / 127.70 秒（n=326） | 30 / 60 秒 | 15m–4h Stage1 主要依据闭合 Kline，不要求全市场行情 ≤5 秒；但 ticker 超过 60 秒必须 stale。两组 REAL_PUBLIC_DATA 样本的 P95 都超限，保留门限以揭示 WS source/ingest backlog；同窗 REST ticker p99 为 2.10 秒，问题需在 WS 链路定位。 |
| PRICE_DECISION current price | 与 Stage1 使用相同既有 ticker | 99.87 / 127.70 / 127.70 秒（同上） | 30 / 60 秒 | Phase9 当前价格证据独立于长周期 Kline，不能以长周期策略为由放宽；超 hard 即拒绝。 |
| PRICE_DECISION closed Kline 5m | 每 300 秒闭合 | 214.20 / 349.20 / 361.20 秒（窗口末 BTC/ETH，n=2） | 300 / 330 秒 | 沿用 5m interval + 30 秒 grace。P95/P99 超 hard，表示现有数据落后；不可用扩 TTL 掩盖。 |
| PRICE_DECISION closed Kline 15m | 每 900 秒闭合 | 364.20 / 364.20 / 364.20 秒（窗口末 BTC/ETH，n=2） | 900 / 960 秒 | 沿用 15m interval + 60 秒 grace。 |
| PRICE_DECISION closed Kline 1h | 每 3,600 秒闭合 | 364.20 / 364.20 / 364.20 秒（窗口末 BTC/ETH，n=2） | 3,600 / 3,720 秒 | 沿用 1h interval + 120 秒 grace。 |
| PRICE_DECISION closed Kline 4h | 每 14,400 秒闭合 | 7,564.20 / 7,564.20 / 7,564.20 秒（窗口末 BTC/ETH，n=2） | 14,400 / 14,580 秒 | 沿用 4h interval + 180 秒 grace。 |
| PRICE_EXECUTION | 每笔 intent 前即时 quote | N/A（Paper execution quote caller 尚未接线） | 2 / 5 秒 | 保留严格 RiskPolicy quote hard cap 5 秒；无 fresh quote 一律拒绝。 |
| TRADE_FLOW 1m | 既有 Phase3 聚合窗口，每 60 秒闭合 | 64.20 / 64.20 / 65.16 秒（n≤2） | 60 / 65 秒 | hard 是窗口长度 + 5 秒允许迟到；超限暴露 backlog。 |
| TRADE_FLOW 5m | 每 300 秒闭合 | 616,714.20 / 617,929.20 / 618,040.61 秒（n≤2） | 300 / 305 秒 | 已有闭合窗口积压显著超过 hard，保持 stale；不重抓或另建 pipeline。 |
| TRADE_FLOW 15m / 1h / 4h | 分别每 900 / 3,600 / 14,400 秒闭合 | 15m: 627,664.20 / 638,194.20 / 639,130.20；1h: 641,164.20 / 641,164.20 / 641,164.55；4h: 641,164.20 / 641,164.20 / 641,164.55 秒（各 n≤2） | 各周期 60 / (周期 + 5) 秒 | 使用已收盘 `window_close` 作 freshness clock；这些结果表示现有窗口 backlog，不是安静市场。旧 schema 没有 fetched_at，ingest/processing lag 保持 null。 |
| OPEN_INTEREST | Phase2 provider poll 300 秒 | 195.81 / 195.81 / 195.81 秒（窗口末 BTC/ETH，n=2） | 360 / 600 秒 | 允许一次 cadence 加采集余量，hard 最多两个 poll 周期。最近 24h 真实 BTC/ETH 持久化 gap P50/P95/P99 为 437.6/496.5/1,102.8 秒；hard 低于 P99，能继续显露 backlog。 |
| FUNDING | Phase2 provider poll 300 秒（非 funding settlement interval） | 195.96 / 195.96 / 195.96 秒 observation age（n=2） | 360 / 600 秒 | 同 OI 按 provider poll cadence 设置 TTL。Bitget current-funding endpoint 未提供 source event timestamp；source_event_age / ingest_lag 为 unknown，按 observation_age 判定；短测样本不用于声称稳定 P99。 |
| LIQUIDATION | 事件驱动；无事件不代表断流 | event → receive delivery lag: 2.30 / 11.63 / 18.44 秒（过去 24h REAL_PUBLIC_DATA，n=398）；短测窗口末 event age 为 916.01 / 929.77 / 931.69 秒（n=4） | 20 / 60 秒 delivery lag | freshness 只针对 event → receive 延迟；历史事件年龄和无事件静默不判 stale。过去 24h delivery P99 在 hard 内；若无事件流 heartbeat，仍需单独标明未知。 |

运行快照和 Dashboard 分别记录 `source_event_age`、`ingest_lag`、`processing_lag`，并额外提供 `observation_age` 与窗口 age。缺失 provider/fetch 时间就保留 null；负 lag/未来时间戳作为时钟或来源异常处理，不将 heartbeat 当行情新鲜度。health 独立展示 `process_alive`、`collector_heartbeat`、`fresh_market_data`；ticker age >60 秒即使 collector heartbeat 为 AVAILABLE 仍是 STALE。Trade-flow 查询只读既有 canonical 表中的闭合 AVAILABLE windows，排除 PARTIAL/in-progress window。

校准窗口为 session `7a060df472e741c4874a9bf7`，2026-09-29 09:50:02–10:06:04 UTC，共 62 个 REAL_PUBLIC_DATA cycles、21 条 decision；本节短窗口分位数用于发现真实落后和冻结安全门，不代表 24 小时统计稳定性。另查到 canonical Kline 表含近期抓取的历史回补行，因此未把其抓取间隔误算作实时 freshness。正式 24h 验收仍需全程 REAL_PUBLIC_DATA 且 readiness 通过。

## 13. Reconciliation

当前旧 Nautilus Local Paper fixture 流程有独立 reconciliation；本轮 monitor 没有接入 Paper account/order state，也不能证明实时 Paper reconciliation。readiness gate 将 reconciliation 明确置为 NOT READY。只有未来完成实际 Paper 接线并以只读账户/订单对账通过后，才能宣告此项 ready。

## 14. Dashboard 状态

现有 Dashboard 新增 GET /api/realtime-paper 与 /realtime-paper 页面，读取实时 monitor 会话、数据来源、周期、决策和 gate blockers；stale、非真实来源或 Paper not ready 会显式告警。该 API 只读，不提供下单控制。

Dashboard 后端在当前机器上运行的旧进程没有被重启；因此新 API 对运行中的服务生效前，需要在用户安排的本地窗口重启现有 Dashboard backend。前端静态构建产物已更新。

## 15. 测试结果

- 当前改动的 Python 定向验证：96 passed（Stage1、Phase9 decision/source、Execution risk、Phase2 config/normalization/runtime/adapters、realtime-paper freshness/runtime）；2 条既有 Pydantic warning。
- Dashboard realtime-paper backend API：2 passed；Dashboard Vitest：15 passed；TypeScript typecheck 与生产 build：通过。
- Python compileall、Bash 脚本语法和 `git diff --check`：通过。Phase9 需要 PostgreSQL 的完整矩阵本轮未重跑；直接执行时因环境未提供 TEST_POSTGRES_DSN 无法建 DB fixture。此前基线官方矩阵为 core 1,782 passed、Phase8 19 passed，不作为本轮变更后的验证结果。
- 修改后 REAL_PUBLIC_DATA 短测 session `7a060df472e741c4874a9bf7`（2026-09-29 09:50:02–10:06:04 UTC）：62 cycles、21 decisions、最大 cycle gap 17.619 秒、observed 961.293 秒；来源计数仅 REAL_PUBLIC_DATA，duration/source provenance 通过。最终 evaluator 为 FAIL / PAPER_NOT_READY；readiness blockers 包含 market_data stale、无 active Phase9 policy、candidate validity、execution wiring、Risk、Paper engine 和 reconciliation。62 周期全部 DO NOT TRADE，errors=0、orders=0、trades=0。该结果证明安全门拒绝了不满足 readiness 的 Paper 调用，不是 24h acceptance 通过。
- 24.1h REAL_PUBLIC_DATA 观察已启动：session `f95080688fb54cc8bd429a46`，2026-09-29 10:14:36 UTC 起，运行目标 86,760 秒，代码版本 `37e163f47b03b45726a405475137e140fe58f559`。记录时已有 3 cycles / 4 decisions，来源 REAL_PUBLIC_DATA；process/database/collector heartbeat 正常，market_data gate 为 BLOCKED，4 次拒绝、0 errors、0 orders、0 trades。最终 24h acceptance 尚未完成，不能据此宣告通过。

## 16. 已知问题

1. 当前不存在活动且获批的 Phase9 方向策略。
2. Stage1 candidate validity 不持久化。
3. Stage1 ticker source_event_age 超过 60 秒 hard limit 时，market data 必须保持 STALE；REAL_PUBLIC_DATA 短测实测超限，即使 collector heartbeat AVAILABLE 也不放行。
4. Risk、Nautilus Local Paper、订单/仓位状态与 monitor 未连接；reconciliation 未就绪。
5. Dashboard 当前运行中的后端进程尚未重启，新接口需重启后可见。
6. 24h 正式 Paper acceptance 尚未通过；短时真实数据 observation 不能替代 24h，也不能替代实际 Paper readiness。
7. 为恢复已停止的 collector，当前容器运行态预算临时为 768 MiB，但仓库 Compose 仍按项目原预算 256 MiB；容器重建后可能再次退出 137，需分析内存峰值并在项目资源约束内解决。

## 17. Strategy Gaps

未修改交易参数，也未启用 Phase9、GPT、Jev 或 Live。其余问题、证据和建议实验见 STRATEGY_GAPS.md。本阶段发现的核心缺口是没有获批 directional policy 与完整执行接线，而不是需要临时调整 Stage1 阈值。

## 18. 下一阶段建议

1. 先确认 Bitget ticker event_time/source-age 延迟的来源（WS 订阅/消息类型/数据库写入时序），在保留 Stage1 60 秒 hard gate 与 Execution 5 秒 hard gate 的前提下消除真实延迟；用独立证据验证后持续观察 24h。
2. 由策略负责人批准并冻结一个 Phase9 LONG/SHORT policy，补齐 Stage1 candidate validity。
3. 复用现有 DecisionCandidate → RiskPolicyV1 → NautilusIntentAdapter → Local Paper 组件完成 Paper-only 调用链和 reconciliation；Live 保持 DISABLED。
4. 继续观察 session `f95080688fb54cc8bd429a46` 至结束；按全程 source 与 readiness ledger 判定 24h acceptance，未通过时保留 DO NOT TRADE；通过后再延长到 72h/7d。
5. 安排现有 Dashboard backend 受控重启，验证运行页面展示。
6. GPT 保持 DISABLED，Jev 保持 NOT_CONFIGURED；只累积结构化决策记录。
