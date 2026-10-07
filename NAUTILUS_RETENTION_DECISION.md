# Nautilus 保留与冻结决定

日期：2026-10-07（Asia/Shanghai）。分支：`cleanup/ai-tech-debt-v1`。审计源码基线 `4c2bc9cf7d80ddda20a316d43f18893d7a9d8031`；下述源码行号默认指该基线，实际删除和当前分支变化见清理报告。ACTIVE 指正式入口可达，不能推断本机进程已启用。

结论：本轮冻结全部 Nautilus 能力演进，保留现行真实调用、历史账本恢复和有效测试。没有实现 Freqtrade，也没有将旧 Paper 账户迁移至另一个执行 owner。



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


## 调用链证明

`quant_realtime_paper.assembly` → `NautilusLocalPaperExecutionAdapter` → `OwnedLocalPaperRuntime.operation` → `paper.LocalPaper` → `SandboxSession` → `NautilusIntentAdapter` → `SandboxExecutionClient`。这是现行 Paper 路径，不是仅 fixture 文件名线索。

`quant_execution` 仍拥有中立 intent/result/position、Risk、reserve、fencing、journal；Nautilus 拥有 native execution。SessionStore 只是运行观测，不是第二个组合资产 owner。

## Freqtrade 后删除条件

1. 证明没有新 Nautilus intent/order，并决定已有仓位的退出或保护迁移；继续保留原账本与所有历史行情。
2. 新 adapter 在 dry-run 环境证明 identity、订单/position、fees、funding、未知发送恢复、fencing 和 reconcile，防止两个执行 owner 操作同一账户。
3. 替换 assembly 的 native instrument / position manager / quote preflight / risk inputs / adapter_factory 绑定。单独换 factory 不够。
4. 旧 fixture CLI 与真正的 LocalPaper 类先分离，再删除无消费者的 CLI/legacy adapters。当前 backtest/research如继续使用 Nautilus，仍保持冻结依赖。
5. 最后确认 backtest、研究、有效验收均有替代，再移除 `nautilus_trader` optional dependency；不删 migration、用户 journal、Paper 历史。

## 验证边界

原生离线 fixture tests 实际运行，相关批次与全套结果见清理报告；数据库 restart/reservation/recovery 因缺隔离 DB 明确 skip，不能宣称已验证用户当前真实账户迁移。
