# Phase9 与 Nautilus 实施验收记录

测量 source：`c9ecdd1d8d0ca1235c354470361bc6c2076eb466`。分支：`integration/nautilus-v1`。此报告提交后，由原生验收脚本执行 A8（diff check）和 A9（clean status），核对 source→report-only 的 ancestry，再生成 `artifacts/phase9/final/result.json`。完整 A1–A9 最终状态以该机器证据为准。

## 已完成的代码与实际测量

Phase9 Task1–9 已实现，十九项 C1 任务祖先映射已验证。保留原 Task0 closure `2f9cbab0ed8b0371e467c7b69d723439a5cf112e`。当前保护树摘要 `b9d24fed637125464630c6bdae86cad213472fef33c15ab6b0cbfc9b2d194247`；修改保护路径后已重新运行全部四个兼容性套件。

| 检查 | 通过数 / 结果 | 跳过 / 失败 / 错误 |
|---|---:|---:|
| A1 兼容性闭包 | 36 | 0 / 0 / 0 |
| A2 Phase9 | 253 | 0 / 0 / 0 |
| A3 受影响回归 | 407 | 0 / 0 / 0 |
| A4 全量本地回归 | 1698 | 0 / 0 / 0 |
| A5 固定 replay | PASS | 0 / 0 / 0 |
| A6 secret scan | PASS | 0 / 0 / 0 |
| A7 实际持续运行 | PASS | 0 / 0 / 0 |


A7 实测持续 `900` 秒，runtime integration、failure semantics、persistence audit 全为 true。Replay 六个摘要一致，provider/AI 调用均为零。必需本地测试跳过为零；八个显式 opt-in 公共网络探测测试在本地确定性矩阵外，不记为通过。

NautilusTrader 固定 1.231.0。四个真实原生 Backtest 案例包含 BTC/ETH LONG/SHORT、TREND_CONTINUATION 和 BREAKOUT_CONFIRMATION。每个案例资金费率均非零，净收益与原生账户现金变化按精度完全一致。两组真实 Sandbox Paper 各运行超过 60 秒、采集 300 次 heartbeat，然后更换进程，保持订单、仓位、保护止损、funding 和不可变 result audit。恢复方式为 LOCAL_SANDBOX_REPLAY_RECONCILED。真实崩溃后重启修复也由全量测试中的子进程与 PostgreSQL 验证。

Funding=ADAPTER_IMPLEMENTED；共享 canonical funding 事实调整原生账户现金。费用模型包含真实模拟成交费用、spread、slippage、资金费率；缺失资金费率不产生已结算 net PnL。特征与 ResearchFrame 共享定义、时间/质量/来源语义；known_at backward-only，Parquet 有内容摘要与数据库 lineage，五个假设使用共同样本和 purged chronological OOS。

Backtest peak RSS=357192 KiB（348.82 MiB）；Paper peak RSS=342425600 bytes（326.56 MiB）。单独本地 pilot 低于 384 MiB，未修改原 PostgreSQL/Collector/Engine 预算。组合生产负载与 Collector 压测未验证。

## 结果与边界

所有案例均为 FIXTURE_DRIVEN_ACCEPTANCE；四组回测真实亏损，不构成市场 edge。生产 policy 保持 DRAFT/未批准，Phase9 默认 disabled。Real Jev=NOT_CONFIGURED 合法；Jev 只审阅语义冲突，Python 决定放行。LIVE_TRADING_STARTED=false，external order routes=0；未使用交易所凭证或真钱账户。

一次独立 whole-branch reviewer 在用量上限处中断，没有完整评级报告，状态 INCOMPLETE_USAGE_LIMIT。作者完成本地 review、实际失败复现与一次集中 Important 修复，随后全量及正式验收；不声称独立审核通过，不启动第二轮 reviewer。已有 Pydantic/pandas/aiohttp 警告记为 deferred minor。

每个私有 Paper 账户支持一个 intent，journal 最多 4096 事件。恢复仅适用于本地 Sandbox。真实场内 reconciliation、多 intent 生产 Paper、长期运行资源、native Windows、真实历史数据 edge、生产策略审批、真实 Jev、Live 均未验。历史 PIT_UNVERIFIED/UNVERIFIED 不自动升级；冻结 evidence 不支持的数值 predicate 保持 UNKNOWN/不放行。

## 实际证明文件

- `artifacts/phase9/final/commands/A1..A7` 与 C1 completion、最终 A8/A9/result。
- `artifacts/phase9/final-backtest.json`：摘要 `4fc736636756261fafcae3159aaa04fe8b78818575d6414f04a3dd451c4620bf`。
- `artifacts/phase9/final-backtest-memory.txt`：实际进程资源测量。
- `artifacts/phase9/final-paper/paper-acceptance.json`：摘要 `2ed7db42c6ff2604bc67bb5123a92933ebbdfe073e27c2620a090f7dd0ddbef7`。
- `docs/NAUTILUS_INTEGRATION.md`：环境、Backtest、Paper 启停/health/审计/恢复、Research 与 formal commands。
- `docs/NAUTILUS_BRANCH_REVIEW.md`：一次 review/fix、处理的重要问题、rulings 与 deferred minors。

原项目工作树与原有 quant-engine/quant-postgres/agentops 保留。只在 integration/nautilus-v1 提交；不 push/PR/merge/tag/release。验收临时数据库在工作进程退出且证据保存后删除。
