# Quant Trading Dashboard V1 交付验收

验收日期：2026-09-29。Dashboard 部署在 `integration/nautilus-v1`，从基线 `4b55633d0650ff232eb4fb176dccf652d49cdd66` 开始实现。控制台使用独立 FastAPI 只读服务与 React、TypeScript、Vite 前端；它观察现有 Quant、PostgreSQL、Phase9 报告和 Paper 心跳，不接入交易控制流。

## 页面与数据接口

| 页面 | API 与数据来源 | 展示规则 |
|---|---|---|
| Overview | `/api/health`、`/api/overview`；只读数据库快照、经进程身份校验的 Paper 心跳 | 显示 SYSTEM、MODE、PAPER、LIVE、Jev、Funding、Database、Reconciliation 和账户指标；无当前账户时为 NO DATA / N/A。 |
| Paper Trading | `/api/paper`、`/api/positions`、`/api/orders`、`/api/trades` | 进程、心跳、会话、持仓、原生订单状态和累计执行审计；已停止的历史快照标为 STALE。没有逐笔成交记录时不生成 Trade。 |
| Decision Inspector | `/api/decisions`、`/api/decisions/{decision_id}` | 按真实持久化对象连接 market、screening、evidence、pattern、decision、risk、intent 和 Nautilus 证据；缺少风险拒绝记录时显示 NOT_RECORDED，不推断拒绝原因。 |
| Backtest | `/api/backtests`、`/api/backtests/{run_id}` | 验证受限目录中报告摘要，分别展示原始 case；只有报告真实记录的时间序列才绘制权益与回撤。没有的指标和逐笔记录保持 N/A。 |
| System Health | `/api/health` | 展示 Core、Database、Backtest、Paper、Nautilus、Funding、Reconciliation、Jev、Live；只呈现已有记录与 Dashboard 自身可测资源。单个组件 RUNNING 不表示整个 Core 健康。 |
| Logs / Events | `/api/logs`、`/api/events` | 合并限定目录日志和持久化事件，按 level、module、关键词筛选；JSON 文本字段和多行、截断 PEM 在展示及搜索前进行脱敏。 |

数据库通过 `QUANT_DASHBOARD_DSN` 显式配置；服务拒绝远程主机，只执行显式列名的 SELECT，并在只读事务、连接超时、语句超时和有界结果下运行。不会读取 `.env`、执行迁移或向原数据库写入。API 只接受本机访问，服务只绑定 `127.0.0.1`；Dashboard 每 3 秒轮询 Overview、Paper、Decision、Health 和 Logs，Backtest 按需读取。表格可局部滚动，1366×768 和 1920×1080 页面不产生整体横向溢出。

## 本机真实状态

最终运行验收访问 `http://127.0.0.1:3000`，所有十个只读 API 均返回 Dashboard API V1 envelope。当前机器没有配置 Dashboard 数据库连接，因此 Database 为 NOT_CONFIGURED，决策、当前账户、持仓、订单、成交和新日志为 NO DATA。Jev 为 NOT_CONFIGURED，Live 为 DISABLED。11 份旧 Paper 快照可在历史会话中查看，但均为 STALE；它们不会被当作正在运行的 Paper。

Backtest API 实际读取两个已有报告，共八条 fixture case；这些工程验收数据不是八次独立策略实验。报告没有可验证的逐时权益曲线或逐笔交易；合成验收组收益为负，策略 edge 仍是 NOT_VALIDATED。页面保留这些限制，不补算虚构曲线、Sharpe、胜率、交易或当前账户收益。

## 验证结果

- 原项目完整测试：1,698 项通过。
- Dashboard 后端：83 项通过，其中覆盖 API 空状态、数据库只读/不可用、Paper 进程新鲜度、决策审计关联、Backtest 数据校验、日志筛选与敏感文本脱敏。
- 合并后完整测试矩阵：1,781 项通过，0 跳过、0 失败、0 错误；最终完整运行的 Phase9 矩阵输出 1,762 项通过，Phase8 矩阵 19 项通过。报告中保留上游及 FastAPI 依赖产生的警告。
- Dashboard 前端：14 项通过，TypeScript 检查和生产构建通过。
- Windows Launcher：实际 Windows PowerShell 启动、数据库环境转发、调用环境恢复、断开数据库时的安全错误以及停止均通过。
- Phase9 compatibility suites：36 项通过，0 跳过、0 失败、0 错误。
- Core 与 Dashboard 敏感信息扫描：0 条发现；原有 225 个 Core/migration 文件的 SHA256 未变。
- 浏览器检查：六个页面、API 断开提示、历史 Paper、Decision 风险与证据链、筛选日志和 1366/1920 分辨率均实际检查；图像和响应式测量附在完整交付包。

最终独立整分支审查提出三项 Important：JSON 字符串内引号字段未脱敏、日志尾部截断时 PEM 起始行被丢弃、Windows PowerShell 5 将 WSL 诊断 stderr 当作终止错误。对应回归用例先复现失败，随后修改 JSON 递归脱敏、在 200 行与 64 KiB 截断前处理私钥片段、并在启动/停止脚本中捕获 stderr 后按退出码和 JSON 判断成功。最终完整测试矩阵及实际 Windows Launcher 检查均通过；审查没有其他可执行发现。

## 安全与 Git

LIVE 未启动；没有真实下单；没有修改 API Key；没有改变交易、风险、Paper、Backtest、Funding 或 Reconciliation 核心实现；没有写入原始数据库。PostgreSQL 集成测试只使用本轮创建的本机一次性容器和临时 schema；结束时已核验原有容器身份、内存限制与重启次数不变，原项目工作树保持 clean。未 push、未 merge。

启动、停止和可选只读数据库配置参见 [DASHBOARD_V1_OPERATIONS.md](DASHBOARD_V1_OPERATIONS.md)。分支提交、验收 JUnit、兼容性结果、运行证明与截图见本目录的最终报告和完整交付包。
