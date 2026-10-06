# Dashboard V1 浏览器验收记录

2026-09-29，使用实际构建产物和本地 API，在 Codex 内置 Chromium 浏览器逐页验收。默认服务为 `http://127.0.0.1:3000`。屏幕尺寸为 1366×768、1920×1080；两种尺寸的六页均满足 `documentElement.scrollWidth == clientWidth`，表格内容可在局部滚动。正常连接阶段没有浏览器脚本异常；主动停止服务时出现预期的本地连接失败，恢复后回到 API CONNECTED。

| 页面 | 实际检查 |
|---|---|
| Overview | IDLE、Paper STOPPED、数据库 NOT_CONFIGURED、Jev NOT_CONFIGURED、LIVE DISABLED；八项账户卡片 N/A。API 已连通不代表 Quant Core 已运行。 |
| Paper Trading | 当前会话为空；选择现有 restored.json 后显示 HISTORICAL SNAPSHOT / STALE，展示原仓位、费用、Funding、原生保护状态。未知的分项对账保持 NO DATA。1366 下仓位表局部滚动，整页不溢出。 |
| Decision Inspector | 默认未配置 DB 时 NO DATA。额外在独立测试数据库启动 3001 服务，读取真实持久化测试对象；八层链逐一点击，查看证据质量及版本。没有 Intent 为 Risk NOT_RECORDED；另一条有批准 Intent 为 APPROVED / PERSISTED_EXECUTION_INTENT。 |
| Backtest | 从现有 final-backtest.json 和 m7-backtest.json 读取八个报告 case。四种 case 的净 PnL 均为负，合成验收数据及 edge NOT_VALIDATED 明确展示。曲线、逐笔 Trade、Sharpe 等不存在的数据保持 NO DATA / N/A。 |
| System Health | 当前监测与历史事件分开；后台 RSS 来自真实进程。未知 CPU / 磁盘等没有补零，Jev / Live 状态固定可见。 |
| Logs / Events | 默认数据源无事件时为空。在隔离测试 DB 中验证 UNKNOWN 级别、decision 模块、真实关联 ID 的组合筛选，结果从两条缩为一条。 |

主动通过 stop-dashboard 停止 3000 服务后，页面显示 API DISCONNECTED、“无法连接本地 API”、“保留上次数据 · STALE”；随后用启动器重新启动，轮询自动恢复。重复启动、重复停止、立即重启均已从 Windows 实际执行。

浏览器测试数据库是本轮创建的独立 PostgreSQL 容器和独立 `dashboard_browser_*` schema。测试数据没有加入默认 3000 服务，也没有写入原数据库。3001 服务及 schema 在验收后清理。正式浏览器默认读取真实已有项目文件；不存在的项目数据仍为空。

视觉证据随 Windows 交付包保存在 `dashboard-qa`：总览、历史 Paper、原回测、健康页、决策空状态、隔离库 Risk / Evidence、日志筛选、API 断开截图，以及两种尺寸的六页宽度记录。截图中的测试决策仅用于说明测试验收，不表示真实市场信号或可执行交易。
