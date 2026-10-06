# Crypto Perpetual Quant V1.0 设计文档

这是一份设计与可行性审计交付物，不是交易程序。当前工作区审计结果见 [`audit.md`](./audit.md)。在用户确认设计前，不创建交易代码、不写入交易所 API Key、不进入 Live。

## 文档索引

- [`audit.md`](./audit.md)：当前开发目录审计、绿地项目结论与边界
- [`architecture.md`](./architecture.md)：总体架构、服务边界、数据流与故障原则
- [`data-sources.md`](./data-sources.md)：数据源、接口、频率、成本、限频与不可可靠实现项
- [`database-schema.md`](./database-schema.md)：PostgreSQL 持久化模型、溯源字段与关键约束
- [`strategy.md`](./strategy.md)：Universe、Stage 1、Stage 2、Evidence Chain、触发与通知
- [`risk-engine.md`](./risk-engine.md)：20% 计划最大风险、仓位、杠杆、强平安全与加仓
- [`state-machine.md`](./state-machine.md)：系统、币种、仓位与恢复状态机
- [`deployment.md`](./deployment.md)：阿里云资源约束、Docker Compose、systemd 与健康检查
- [`phases.md`](./phases.md)：从 Phase 1 到 Phase 13 的开发顺序、验收标准与 Live 门槛
- [`pre-implementation-review.md`](./pre-implementation-review.md)：最终 Pre-Implementation Design Audit 与 Phase 1 复核摘要

## 当前结论

1. 这是一个全新的绿地项目，当前目录没有既有代码、Git 仓库或可复用模块。
2. 整体 V1 预留 3 个容器：collector、engine（含 dashboard）、executor；但 Phase 1 只启动 collector + engine，V1 不强制 Redis。
3. Bitget 只作为执行交易所；其他交易所用于交叉验证。交易所官方接口可以覆盖价格、K 线、成交、资金费率、OI、订单簿及部分强平事件。
4. “清算热力图”“全市场 token unlock”“统一官方新闻流”“可靠 spoofing/iceberg 判别”等数据不能在没有明确供应商和授权时假装可用，必须返回 `NOT_AVAILABLE` 或降级为未满足证据。
5. 所有交易必须先经过规则化 Hard Rule、Risk Engine 和保护单检查；AI 只能输出结构化证据解释，不能直接下单。

## 设计确认门

请先审阅这些文档并确认：

- 服务边界与技术栈是否接受；
- 数据缺失时的 fail-safe 行为是否接受；
- Stage 1 / Stage 2 / Evidence Chain 规则是否接受；
- 20% 单笔风险、最多 3 仓且无回撤熔断的高风险设定是否仍然确认；
- Phase 1 是否按文档的验收标准开始。

在确认前，系统保持设计阶段，`TRADING_MODE=paper` 是唯一允许的默认值。
