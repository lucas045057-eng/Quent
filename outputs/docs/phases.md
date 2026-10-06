# 开发 Phase 计划

## 总原则

每个 Phase 都必须有可独立验收的软件或设计产物、核心测试和故障降级。实现期间保持 `TRADING_MODE=paper`；Phase 13 只是“具备进入 Live 评审的条件”，不是自动开启 Live。

## Phase 0：设计与数据可行性审计（本次）

交付：架构、数据源、Schema、策略、Risk、State Machine、部署和本计划。退出条件：用户确认设计，明确接受或修改风险模型、数据缺失边界和技术栈。

## Phase 1：Project Foundation

范围：Python 项目、配置、结构化日志、PostgreSQL migration、Docker 基础、Bitget UTA v3 public price/Kline/volume、市场结构、Universe、基础 Stage 1。Phase 1 不解析或使用 OI/Funding，即便 ticker raw payload 中含有这些字段。

必须测试：v3 REST/WS contract、配置默认 paper 且拒绝 private key、秘密脱敏、Observation status/freshness、K 线闭合、interval-aware expected-closed-bar freshness、Universe 过滤、结构节点、重连与 REST gap recovery。Kline retention 使用 `KLINE_RETENTION_<INTERVAL>_DAYS` 配置，不得统一硬编码为 90 天。

退出条件：在没有交易凭据的情况下可持续采集并解释 Top 200/Stage 1；服务重启不丢历史数据；没有任何 order/position/executor/live 代码路径被启动。

## Phase 2：OI、Funding、多交易所交叉

范围：Bitget 当前 OI/funding，Binance/Bybit/OKX/Hyperliquid 可选交叉适配器，单位和时间窗口归一化。

退出条件：来源冲突保留多值；任一交叉源失败不拖垮 Stage 1；OI/funding 不单独产生方向信号。

## Phase 3：Trades、Taker、CVD、OrderBook

范围：实时成交、主动买卖近似、spot/futures CVD、depth、spread、slippage、订单簿统计。

退出条件：CVD 可从成交回放重算；WS 缺口可 REST 补；无法证明 spoofing/iceberg 时输出未确认而不是结论。

## Phase 4：Liquidation、Long/Short、Basis

范围：官方强平事件、long/short ratio、basis；不实现虚假的未来 liquidation heatmap。

退出条件：已发生强平事件可回放；缺少数据时 Evidence Chain 不升级；多空账户数量不等同资金多空。

## Phase 5：BTC/ETH、Regime、Relative Strength、Sector

范围：BTC/ETH context、market regime、relative strength、配置化 sector mapping。

退出条件：候选快照含 BTC/ETH 状态和相对强弱；源过期时标记 STALE，不能用默认“中性”掩盖缺失。

## Phase 6：News、Macro、Unlock、AI Integration

范围：官方 FOMC/BLS/BEA 日历、可配置新闻 adapter、宏观锁、DeepSeek/OpenAI-compatible provider、结构化 AI 证据解释。

退出条件：AI 无法访问 Executor；AI 失败停止依赖 AI 的新仓但继续保护；宏观锁前 30m/后 15m 可配置并可测试；未配置新闻/unlock provider 明确 NOT_AVAILABLE。

## Phase 7：On-chain、Whale、Spot Flow

范围：只有确认供应商、授权和成本后才接入；否则保留 adapter contract 与 NOT_AVAILABLE。

退出条件：每个供应商有 source、methodology、timestamp 和 retention；没有来源时不允许伪造 0。

## Phase 8：Options

范围：只有明确 options 数据源、合约映射和成本后才实施；不让 options 缺失阻塞基础 perp paper 策略。

退出条件：options 证据与 perp symbol 关联可追溯，缺失不会变成中性。

## Phase 9：Evidence Chain、Stage 2、Entry Trigger

范围：七类链模板、冲突/失效、Stage 2 重取数、事件触发 AI、trigger persistence、锁门、反手流程。

退出条件：每条链能回答“哪些原始数据支持它”；Stage 1 snapshot 不能冒充 Stage 2；没有高置信候选时不交易。

## Phase 10：Risk Engine、Paper Execution、Position Management

范围：20% 锁定 risk budget、仓位/杠杆/强平安全、最多 3 仓、一次加仓、server-side stop 的内部合同、分批 TP、trailing、通知。

退出条件：Risk/Position/State/Reconciliation/Idempotency/Failure Mode 测试全部通过；paper 与未来 live 共用 OrderIntent 和状态机。

## Phase 11：Backtest

范围：事件时间无前视的历史回放、paper fill/fee/slippage/funding、指标统计：Trades、Win Rate、Profit Factor、Average/Median R、Max Drawdown、Sharpe、Sortino、Hold Time、方向和 setup 分组。

退出条件：同一输入和 rule version 可复现；报告显示数据缺失和样本量，不把回测结果当收益保证。

## Phase 12：Dashboard、Monitoring、Recovery

范围：账户、最多 3 仓、候选、系统健康、PAUSE NEW ENTRIES、CLOSE ALL POSITIONS、recovery UI、审计查询。

退出条件：任何订单能反查证据链；Dashboard 的暂停和清仓动作不同；重启/DB/WS/Executor 故障演练通过。

## Phase 13：Live Bitget Executor

范围：Bitget 私有 REST/WS、clientOrderId 幂等、实际保护单、对账、最小权限 API key、人工确认门。

Live 前硬门：

1. paper 运行达到约定观察周期，且没有未解释状态差异；
2. recovery、reconciliation、server-side stop、TP/trailing、重复订单和断线演练通过；
3. 只读/最小权限 key 检查通过，提现权限关闭；
4. 账户权益满足最小下单要求，Risk Engine 的 liq safety 端到端通过；
5. 用户明确确认切换，部署环境仍保留 `PAUSE_NEW_ENTRIES`；
6. 初次 live 采用极小资金和人工观察，任何异常自动回到暂停新仓而不是删除保护。

## Phase 完成定义

一个 Phase 只有在代码、迁移、测试、运行日志、健康检查、故障注入和文档同步完成后才算完成。不能用“进程还活着”替代业务健康，也不能用“有信号”替代可解释和可恢复。
