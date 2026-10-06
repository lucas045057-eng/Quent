# 部署方案：阿里云 2 CPU / 1.7GB RAM / 40GB Disk

## 1. 资源判断

目标服务器还运行随想记应用，因此不能假设 1.7GB 全部可用。部署前必须读取实际内存、磁盘、CPU、已有容器和 PostgreSQL 连接数；如果余量不足，Quant PAPER 先以低频、少量候选运行。Swap 只能防止瞬时 OOM，不能作为正常容量。

V1 不起 Redis，不创建第二个 PostgreSQL 容器，优先复用已有 PostgreSQL 实例建立独立 database/user。若现有实例不允许隔离账户或版本不满足 asyncpg/JSONB/partition 需求，才评估独立实例。

## 2. 容器布局

整体产品 topology 仍预留三个边界，但 Phase 1 只启动两个容器：

```text
Phase 1:
quant-collector  v3 public market 采集与规范化
quant-engine     Universe / Stage 1 / health API（不含交易 executor）
existing postgres 独立 quant database + quant user

Phase 10+:
quant-executor   才加入 paper/live OrderIntent 执行与保护
```

Dashboard 在 Phase 12 再挂入 engine；Phase 1 不为未实现功能额外启动服务。Redis 不启动。

Phase 1 建议起始内存上限（不是未经测量的保证）：

```text
collector  256 MB
engine     384 MB
```

Phase 10+ 加入 executor 后，再按 paper 负载重新预算 256–384 MB；不能在 Phase 1 预留一个空 executor 消耗资源。

每个容器都设置 CPU quota、memory limit、日志大小上限和 `restart: unless-stopped`。最终额度必须按随想记的实际占用和 Paper 采样结果调整，不能为了“跑起来”牺牲已有应用。

## 3. Compose 要点

Compose 只编排服务，不在 compose 文件中放 secrets：

```yaml
services:
  quant-collector:
    restart: unless-stopped
    env_file: .env
    depends_on:
      quant-engine:
        condition: service_healthy

  quant-engine:
    restart: unless-stopped
    env_file: .env
    healthcheck:
      test: ["CMD", "python", "-m", "app.healthcheck", "engine"]

  quant-executor:
    restart: unless-stopped
    env_file: .env
    healthcheck:
      test: ["CMD", "python", "-m", "app.healthcheck", "executor"]
```

实际 Dockerfile、Compose 和 healthcheck 在用户确认本设计后、Phase 1 才创建。此处只规定边界，不提前创建运行服务。

## 4. PostgreSQL 共存

创建独立：

```text
database: quant
user: quant_app
schema: public 或 quant（按现有实例策略）
```

`quant_app` 只拥有 quant database 的业务表和 migration 权限；生产部署可以拆分 migration user 与 runtime user。限制连接池：collector/engine/executor 合计从 10–20 个连接起步，避免在 1.7GB 服务器打爆已有应用。`shared_buffers`、`work_mem` 等 PostgreSQL 参数必须先读取现有实例配置再调整。

## 5. `.env` 和模式门

```text
TRADING_MODE=paper
# Phase 1 must not require or load Bitget private keys.
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_API_PASSPHRASE=
DATABASE_URL=postgresql+asyncpg://...
AI_PROVIDER=deepseek
AI_FAST_MODEL=
AI_REASONING_MODEL=
MACRO_LOCK_BEFORE_MIN=30
MACRO_LOCK_AFTER_MIN=15
MAX_OPEN_POSITIONS=3
RISK_PER_TRADE=0.20
MIN_RR=1.5
```

`.env` 不进 Git，不挂到 Dashboard，不写入日志。Phase 1 启动校验必须拒绝 private key presence as a prerequisite and must not instantiate a live executor. Live 需要明确 `TRADING_MODE=live`、执行账户检查、恢复对账通过和人工确认；代码默认值与样例配置必须始终为 `paper`。

## 6. systemd 自动恢复

通过 systemd 管理 Compose 项目：

```text
Linux boot
→ Docker
→ systemd quant-compose.service
→ docker compose up -d
→ container health checks
→ RECOVERY_MODE
→ reconciliation
→ RUNNING 或 SYSTEM_STOPPED
```

systemd 只负责启动和失败重试，不绕过应用级 recovery。容器重启后必须先读 DB 与 Bitget 状态，不能直接从内存缓存继续开仓。

## 7. 业务健康检查

进程存活不是健康。每个服务的 health endpoint 或内部检查至少包括：

- Collector：`last_market_tick`、各来源最近成功时间、WS reconnect count、gap count；
- Engine：`last_stage1_run`、`last_stage2_run`、outbox lag、当前 entry gate、AI provider status；
- Executor：Bitget REST/WS、DB、heartbeat、未确认订单数、保护单覆盖率、最近 reconciliation；
- DB：连接、migration version、磁盘余量、最近事务失败。

若健康门失败，系统进入 `DEGRADED`，暂停新开仓，但不能删除已有 stop/TP。

## 8. 备份、日志与监控

- PostgreSQL 每日逻辑备份 + 关键交易表的增量/归档策略；每周做恢复演练。
- JSON logs 按大小轮转，禁止记录 API secret、签名、passphrase；保存 event id、symbol、state、order id、reason、rule version。
- Dashboard 展示 Collector/Strategy/Executor/Database/Bitget REST/Bitget WS/AI Provider 的业务健康。
- 只对 OPEN、ADD、REDUCE、TAKE_PROFIT、STOP_LOSS、CLOSE 发用户通知；普通 Stage 变化写库和 Dashboard。

## 9. Phase 1 数据与资源保留

- 只保存 Bitget v3 5m/15m/1H/4H 已闭合 K 线；Phase 1 不保存全市场 1m K 线和每个 WS tick。
- 全市场 ticker 每 5m 保存一次 canonical batch；每个 symbol 保存 compact Stage 1 result；候选/持仓观察的 WS ticker 只保存事件摘要和候选 raw batch。
- 200 symbols × 4 intervals 的 5m REST 拉取约 800 requests/5m，平均约 2.7 req/s；使用受控并发和 token bucket，预留 20 req/s/IP 余量，遇到 429 退避，不无限重试。
- 单次 Kline fetch 默认 `KLINE_FETCH_LIMIT=100`，只保留 Stage 1 计算所需窗口；历史 retention 由 PostgreSQL 按 timeframe 维护，避免 200 symbols 全量内存驻留超过 256MB collector 预算。
- K 线 retention 必须按 timeframe 配置：`KLINE_RETENTION_5M_DAYS=30`、`KLINE_RETENTION_15M_DAYS=90`、`KLINE_RETENTION_1H_DAYS=180`、`KLINE_RETENTION_4H_DAYS=365`；raw payload/batch 7–30 天，交易审计表未来永久保留。任何 retention 增长都必须先通过容量测试和恢复演练。
- JSON 日志按 10–20 MB 单文件、保留 7–14 天起步；具体值先按磁盘余量和随想记共存基线调整。
- PostgreSQL 连接池总量起步 10–20；40GB 磁盘必须有磁盘余量 health gate，低于阈值暂停采集扩张而不是继续写满。

## 10. 部署前检查

1. 与随想记做资源基线采样；
2. 确认 PostgreSQL 可以建立独立 database/user；
3. 检查服务器是否已有 Swap，没有则评估约 2GB Swap；
4. 确认防火墙只开放 Dashboard 所需端口，交易服务不直接暴露；
5. 使用 paper API/本地模拟器跑断线、重启、DB 恢复、保护单和重复订单测试；
6. Live 前保留 `PAUSE_NEW_ENTRIES`，先执行只读账户对账；
7. 任一关键检查失败，保持 `TRADING_MODE=paper`。
