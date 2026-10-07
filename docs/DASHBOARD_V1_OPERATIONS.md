# Quant Trading Dashboard V1 操作指南

这是读取已有 Quant + Nautilus 数据的本地控制台。所有浏览器 API 都只读；没有 Paper / Backtest 启停、Live、下单或修改凭据功能。

## 当前机器：启动与退出

在 Windows PowerShell 中运行。用实际 WSL 仓库路径和项目 Python 环境替换下面的 `/path/to`；这两个参数必填，启动器不再猜测开发者机器路径：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-dashboard.ps1 -RepoPath '/path/to/Quent' -PythonPath '/path/to/project-venv/bin/python'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-dashboard.ps1 -RepoPath '/path/to/Quent' -PythonPath '/path/to/project-venv/bin/python'
```

浏览器访问 <http://127.0.0.1:3000>。重复启动会返回现有进程；重复停止安全返回 STOPPED。启动器只停止自己的 PID、启动时间、实例 ID、工作目录与命令行全部匹配的 Dashboard 进程。端口被其他程序占用时退出，不结束其他程序。

默认发行版为 Ubuntu、端口为 3000；通过 `-Distro`、`-Port` 覆盖。`-RepoPath`、`-PythonPath` 使用 Linux 绝对路径。只绑定 127.0.0.1。

Windows 启动器保留一个隐藏的 WSL 宿主，让 WSL 在 Dashboard 运行期间保持活动；停止后台后宿主自行退出。没有修改 WSL、Docker 或原交易服务的配置。

## 可选数据库连接

不配置连接也能读取项目的历史 Paper 和 Backtest 文件；决策及数据库状态显示 NOT_CONFIGURED / NO DATA。不会自动读取 `.env` 或原交易系统凭据。

需要决策数据时，在当前 PowerShell 会话设置 `QUANT_DASHBOARD_DSN`，可选设置 `QUANT_DASHBOARD_SCHEMA`，然后启动后台。请从自己的安全凭据来源设置值，不要把实际密码写入仓库、聊天或操作日志。连接必须明确使用 `127.0.0.1`、`localhost` 或 `::1`；推荐已有只读数据库用户。更改配置后先停止再启动。

启动器通过子进程环境转交这两个变量，临时使用并恢复调用者的 WSLENV。不会把 DSN 放入启动参数、状态文件或 API。[Windows / WSL 环境传递依据](https://learn.microsoft.com/en-us/windows/wsl/filesystems#share-environment-variables-between-windows-and-wsl-with-wslenv)。后台额外启用只读事务、连接及查询超时，只执行明确列名的 SELECT 和只读会话设置，不创建表或运行迁移。

## 数据怎么解释

- 总览的账户指标只来自一个经过进程身份和新鲜度验证的 Paper 心跳。多账户不相加，改在 Paper 页选择会话。没有当前会话时显示 N/A，旧 Paper 记录只能在历史会话中查看。
- 订单来自 Paper 的原生订单状态；累计 ExecutionResult 在单独审计区展示。累计成交量不是逐笔 Trade，逐笔成交记录不存在时显示 NO DATA。
- Decision Inspector 读取持久化 evaluation、snapshot、evidence、pattern、DecisionCandidate、Intent 与 ExecutionResult。没有 Intent 只能说明风险批准记录未持久化，不能推断为 Risk REJECT。UNKNOWN / PARTIAL 保持原含义。
- Backtest 校验已有报告 digest，逐个展示报告里的 case。当前两个报告各包含 BTC / ETH 多空四组，列表共八条报告 case，不代表八次独立策略实验。工程 PASSED 不代表收益验证；当前合成验收数据的四组收益均为负，edge 为 NOT_VALIDATED。
- 现有验收报告没有逐时净值和逐笔交易，因此曲线、回撤、Sharpe、胜率等显示 NO DATA / N/A。绘图仅接受带实际时间戳的已记录序列，不会拼接独立 case 的盈亏。补充报告格式见后端 `backtests.py` 和对应测试。
- Health 区分当前观测与历史 health event。只读取已有监控和后台自身内存；未有资源数据时保持 N/A。Jev 为 NOT_CONFIGURED；Live 始终 DISABLED。
- Logs / Events 读取限定目录的日志尾部和持久化事件，支持级别、模块、关键词筛选。没有级别的记录是 UNKNOWN，不冒充 INFO。返回前递归过滤凭据和敏感文本。

总览、Paper、Decision、Health、Logs 以 3 秒轮询；Backtest 历史按需读取。请求不重叠，超时中止；断开显示错误并标明保留的旧数据。

## 新机器安装及前端重建

在 Linux / WSL 项目根目录创建独立环境，不覆盖原环境：

```bash
python3 -m venv ../quant-dashboard-v1-env
../quant-dashboard-v1-env/bin/python -m pip install -e '.[dev,nautilus,dashboard]'
../quant-dashboard-v1-env/bin/python -m dashboard.backend start --hold
```

后台实例通过上面的 `start --hold` 启动；停止用 `../quant-dashboard-v1-env/bin/python -m dashboard.backend stop`，查看状态用 `../quant-dashboard-v1-env/bin/python -m dashboard.backend status`。诊断时可运行 `../quant-dashboard-v1-env/bin/python -m dashboard.backend serve` 在前台提供服务，按 Ctrl+C 退出。启动命令需要完整交付包中已构建的前端。

前端源代码在 `dashboard/frontend`。有 Node.js 的同一系统中执行：

```text
npm ci
npm test
npm run build
```

构建产物及 `build-stamp.json` 随源码提交。后台启动前核验源码和产物 SHA256；改源码后没有重建会拒绝启动。当前机器采用 Windows Node 构建并同步到 WSL，前端和后端同源提供，无需另开开发服务器。

## 故障与证据

API 不可用时页面有 API DISCONNECTED 提示。数据库失联显示 ERROR；部分表缺失显示 PARTIAL，保留可读取数据。BACKEND_UNAVAILABLE 等错误是经过过滤的代码，不暴露异常中的密码和 DSN。

后台自身状态及日志保存在项目 `artifacts/dashboard`，目录权限 0700，文件权限 0600。Windows 宿主日志位于 `%LOCALAPPDATA%\QuantDashboard`。没有 Web 下载任意文件的接口；原始报告、日志和数据库地址不会作为静态站点目录公开。

完整测试、浏览器验收、原核心文件校验和提交记录见 `docs/DASHBOARD_V1_ACCEPTANCE.md`。
