# 当前开发目录审计

审计日期：2026-09-20（Asia/Shanghai）

## 结果

当前目录为：

`E:\codex\2026-09-20\files-mentioned-by-the-user-prompt`

目录中只有：

```text
outputs/
work/
```

未发现：

- Git 仓库；
- `pyproject.toml`、`package.json`、`Dockerfile`、`docker-compose.yml`；
- `app/`、`collectors/`、`strategy/`、`risk/`、`execution/` 等源代码目录；
- 数据库迁移、测试、部署脚本或既有文档；
- 可读取的交易所配置或 API Key。

## 结论

项目是绿地项目，不能基于既有实现做增量修改。当前交付只建立设计基线和数据可行性边界，不启动代码开发，不连接交易所，不执行任何订单。

## 需要保留的用户约束

- 主执行所：Bitget USDT-M Perpetual；其他交易所只作交叉验证。
- 每小时刷新 Top 200；每 5 分钟 Stage 1；Stage 2 数据 1–3 分钟更新，但 AI 只事件驱动调用。
- Stage 1 完全规则化；AI 只参与 Stage 2 的解释和证据分类。
- `MAX_OPEN_POSITIONS=3`，同一币不能有第二个独立仓位。
- `RISK_PER_TRADE=20%`，按开仓时权益锁定 `risk_budget`；不设置组合风险上限、日亏损上限或回撤熔断。
- 默认 isolated；结构止损必须早于强平；成交后立即提交交易所服务端保护止损。
- 首仓最多一次加仓，整笔交易风险不得超过原始风险预算。
- 默认分批止盈：+1R 平 25%，+2R 再平 50%，余仓 trailing stop。
- 默认 `MIN_RR=1.5`。
- 默认 `TRADING_MODE=paper`，不得擅自开启 live。
- 重启时 Bitget 是仓位事实源，数据库必须与交易所对账。

## 审计后的第一阶段边界

Phase 1 只做：配置、日志、数据库、Docker 基础、Bitget 公共价格/K 线/成交量、市场结构、Universe、基础 Stage 1 及测试。它不包含下单、AI、真实账户访问或 Live 交易。
