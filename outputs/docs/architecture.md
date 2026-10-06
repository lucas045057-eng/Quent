# Crypto Perpetual Quant V1.0 总体架构

本地融合版本的当前边界与运行方式见 [Nautilus Integration](../../docs/NAUTILUS_INTEGRATION.md)。当前 Phase10 为 Python Risk Policy + ExecutionIntent + Adapter；Phase11 为共享特征研究层 + Nautilus Backtest；Phase12 只保留 health、审计和恢复；Phase13 Live 延后。下文保留为最初规划背景，旧的自研执行/撮合与大型 Dashboard 不代表本轮实现。

## 1. 目标与非目标

目标是把“扫描 → 两轮筛选 → 证据链 → 触发 → 风控 → 执行 → 持仓保护 → 退出 → 重新扫描”做成可恢复、可解释、可回放的系统。每一笔交易都必须能从订单追溯到当时的市场快照、Stage 1 结果、Stage 2 结果、Evidence Chain、Risk Engine 输入、Hard Rule 判定和退出原因。

V1 不承诺预测价格，不把 AI 当作交易员，不用单一指标交易，不使用无法追溯来源的数据，不把 paper 逻辑改写成一套“更简单”的模拟逻辑。

## 2. 推荐技术基线

- Python 3.12；所有采集、WebSocket、调度、服务间 I/O 使用 `asyncio`。
- `aiohttp` 处理 REST，`websockets` 处理原生 WebSocket；交易所适配器不依赖无法审计的黑盒信号库。
- FastAPI 提供健康检查、管理 API 和 Dashboard；Dashboard 用服务端模板 + HTMX/Alpine，减少前端构建和内存占用。
- SQLAlchemy 2 async + asyncpg + Alembic；PostgreSQL 是唯一持久化事实库。
- Pydantic v2 定义配置、规范化数据合同和 AI 输出合同；所有金额、价格、数量、费率用 `Decimal`，不使用二进制浮点做风控结算。
- JSON 结构化日志；秘密只从 `.env` 或服务器秘密文件读取，日志做字段级脱敏。
- pytest + pytest-asyncio + respx/WebSocket 测试替身；测试替身只存在于测试和 paper 执行，不得成为 Live 数据源。

## 3. 服务边界

为适配 2 CPU / 1.7 GB RAM，V1 先采用 3 个容器：

```text
quant-collector
  └─ Bitget / Binance / Bybit / OKX / Hyperliquid / MEXC / macro adapters
  └─ REST + WebSocket 重连、缺口补偿、规范化、数据质量标记

quant-engine
  └─ Universe / Stage 1 / Stage 2 / Evidence Chain
  └─ AI provider adapter / Hard Rules / Risk Engine / state machine
  └─ FastAPI + Dashboard / outbox consumer / health aggregation

quant-executor
  └─ paper executor 或 Bitget live adapter
  └─ clientOrderId 幂等、订单状态、server-side stop、TP、仓位保护
  └─ Bitget position/order WebSocket、REST 对账、recovery

PostgreSQL（优先复用服务器既有实例）
```

Dashboard 不单独拆容器，挂在 `quant-engine` 中。Redis 不进入 V1 强制依赖；未来若 PostgreSQL outbox + NOTIFY 不能满足吞吐，再增加 Redis，但不改变业务合同。

## 4. 数据流

```text
Exchange WS/REST
       │
       ▼
Collector：原始报文 + 规范化 Observation + freshness/status
       │
       ├─ PostgreSQL raw/normalized tables
       └─ durable outbox event
               │
               ▼
Engine：Universe → Stage 1 → Stage 2 → Evidence Chain
               │                         │
               │                         └─ event-driven AI explanation
               ▼
        Trigger + Hard Rules + Macro/News Lock
               │
               ▼
        Risk Engine：risk budget / size / leverage / liq safety / R:R
               │
               ▼
        Order Intent（不可直接执行的内部对象）
               │
               ▼
Executor：再次校验模式、slot、freshness、幂等、保护单
               │
               ├─ Paper fill simulator
               └─ Bitget REST/WS（未来 Live）
```

服务间不直接传递“BUY/SELL/CLOSE”自然语言。Engine 只产生结构化 `OrderIntent`，Executor 只接受满足 schema 的开仓、加仓、减仓、保护单和关闭意图；AI 没有 Executor 权限。

## 5. 数据合同与来源冲突

所有事实统一包装为：

```text
symbol
metric
value
unit
source
exchange
exchange_timestamp
fetched_at
processed_at
status: AVAILABLE | STALE | NOT_AVAILABLE | ERROR
quality
raw_reference
```

同一指标来自多个来源时，所有原始值都落库；规范化层只生成带 `aggregation_method`、时间窗和来源集合的派生值。禁止无记录地覆盖冲突值。`NOT_AVAILABLE` 不等于 0，也不等于利空/利多；需要该证据的规则直接判定为“证据未满足”。

Phase 1 的 Bitget 适配器只使用当前 UTA v3 public market API：REST `/api/v3/market/instruments`、`/api/v3/market/tickers`、`/api/v3/market/candles`，以及 `wss://ws.bitget.com/v3/ws/public`。v3 WebSocket 统一使用 `topic="ticker"` 或 `topic="kline"`；Kline 粒度通过独立的 `interval="5m|15m|1H|4H"` 指定，不能拼接成 `candle5m` 等 v2 风格 channel。业务层只依赖 canonical contracts，不依赖 `lastPrice`、`bid1Price`、`data[0]` 等交易所字段。Classic v2 不进入 Phase 1；未来若因账户模式或接口缺口必须使用 v2，必须建立独立 `BitgetV2ClassicAdapter`，再映射到同一 canonical contract。

## 6. 调度与事件

- Universe 每小时运行一次，读取 Bitget v3 USDT 永续合约列表并重算 Top 200。
- Stage 1 每 5 分钟运行一次；Phase 1 只消费 v3 public instruments/tickers/candles 的完整 K 线和满足 freshness SLA 的公共数据。ticker 中虽然会返回 `openInterest`/`fundingRate`，Phase 1 只保留原始报文引用，不把它们写入 Stage 1 语义字段，也不用于筛选；它们属于 Phase 2。
- Stage 2 候选数据每 1–3 分钟刷新，但只在 `BREAKOUT`、`BREAKDOWN`、`OI_SPIKE`、`FUNDING_ANOMALY`、`CVD_DIVERGENCE`、`LIQUIDATION_SPIKE`、`ENTRY_ZONE_REACHED`、`NEWS_EVENT`、`REGIME_CHANGE` 等事件上调用 AI。
- 交易所 WebSocket 断线后指数退避：1s、2s、5s、10s、30s、60s；重连后重新订阅并用 REST 补缺。
- PostgreSQL outbox 是可重放事件源；NOTIFY 只用于低延迟唤醒，消费者必须能在通知丢失后轮询恢复。

## 7. 故障降级

核心规则是“停止增加新风险，而不是停止已有仓位保护”：

- AI 故障：标记 `AI_ANALYSIS_UNAVAILABLE`，禁止新的 AI 依赖型开仓；已有仓位继续依赖交易所 stop/TP/trailing。
- Collector 严重异常或关键数据过期：禁止新仓，保留已有保护单。
- 单一外部源故障：该指标为 `ERROR`，其他来源继续运行；只有依赖该指标的 Evidence Chain 失效。
- Executor 或 Bitget 连接异常：不重试开仓盲发；先按 `clientOrderId` 查询，再决定恢复或人工介入。
- 数据库不可用：禁止新仓；Executor 不删除交易所已有保护单，恢复后先进入 `RECOVERY_MODE`。

## 8. 安全边界

- `TRADING_MODE` 只能是 `paper` 或 `live`，默认 `paper`；live 启动需要显式配置、健康门、恢复对账通过和人工确认。
- Bitget API Key 只配置交易权限，不开提现；密钥不出现在日志、异常堆栈、Dashboard 或 AI prompt。
- AI Provider 只接收脱敏后的结构化快照，不能访问交易所凭据、数据库写权限或 Executor API。
- Dashboard 的“PAUSE NEW ENTRIES”和“CLOSE ALL POSITIONS”是不同操作：前者只改变 entry gate，后者生成 reduce-only close intents 并要求逐仓确认。
