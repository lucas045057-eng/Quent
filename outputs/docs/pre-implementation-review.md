# PRE-IMPLEMENTATION REVIEW

审计日期：2026-09-20（Asia/Shanghai）  
范围：仅审计现有设计文档、官方 Bitget 接口和 Phase 1 边界；不创建正式功能代码，不创建真实订单。

## 审计结论

设计方向可以进入 Phase 1，但必须按本审计收敛后的边界执行：Phase 1 采用 Bitget UTA v3 public market API，业务层只依赖 canonical data contract；Phase 1 只实现真实的 instruments、全市场 tickers、5m/15m/1H/4H 已闭合 K 线、基础结构和规则化 Stage 1。OI、Funding、CVD、Liquidation、Long/Short、News、AI 等即使在部分原始接口中出现，也不属于 Phase 1 的语义输入，不得用于 Stage 1。

本次只读验证结果：

- `GET https://api.bitget.com/api/v3/market/instruments?category=USDT-FUTURES` 返回 `code=00000`，响应含 `symbol`、`category`、`baseCoin`、`quoteCoin`、`symbolType`、`type`、`status`、precision、multiplier、min/max quantity 等字段。
- `GET https://api.bitget.com/api/v3/market/tickers?category=USDT-FUTURES` 返回 `code=00000`，响应含 `lastPrice`、24h high/low、bid/ask、volume/turnover、mark/index、`ts`，同时还返回 `openInterest`/`fundingRate`；后两者 Phase 1 只保留 raw reference，不进入 Stage 1。
- `GET https://api.bitget.com/api/v3/market/candles?...&interval=5m&limit=2` 返回 `code=00000` 和七列 K 线数组。
- `wss://ws.bitget.com/v3/ws/public` 成功订阅 `usdt-futures` 的 `ticker` 和 `kline`，收到 snapshot；public WS 不需要 API Key。
- `GET /api/v3/public/instruments` 本次 smoke test 未成功，因此 Phase 1 不使用该路径；以当前官方 Market Data 目录的 `/api/v3/market/instruments` 为 canonical endpoint，并在代码中加入 contract test。

官方参考：[当前 v3 Market Data](https://www.bitget.com/docs/catalog/market/market-data)、[v3 Public Ticker WebSocket](https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel)、[v3 Quick Start 与 WS 限制](https://www.bitget.com/docs/uta/quick-start)、[v2/v3 映射说明](https://www.bitget.com/docs/classic/uta-api-upgrade-guide)。

## BLOCKER

### 未解决 Blocker：无

本次审计已将 v2/v3 混用问题收敛为 Phase 1 v3-only，并对关键 v3 REST/WS endpoint 做了只读 smoke test。

### 实现前强制门槛

以下不是架构矛盾，但未通过前不得合并 Phase 1：

1. 为三个 REST endpoint 和 v3 `topic=ticker` / `topic=kline&interval=...` 写 contract test，固定成功码、必需字段、字段类型、空值行为和未知字段容忍策略；禁止使用 v2 Classic 的 channel subscription schema。
2. contract test 必须在无 API Key、无私有权限的环境中通过；禁止因缺少私有 Key 让 Phase 1 启动失败。
3. 如果线上接口返回结构与本审计不同，停止实现并更新 adapter contract；不得在 Stage 1 中加入 v2 fallback 或猜测字段。
4. CI/启动检查必须证明 Phase 1 没有 order、position、private WS 或 live executor import path。

## WARNING

1. 官方 v2/v3 升级页与当前 Market Data 目录的 instruments 路径存在文档差异。当前选定 `/api/v3/market/instruments`，未来不得把两个路径混在业务层；如需兼容，必须分离 `BitgetV3UtaAdapter` 与 `BitgetV2ClassicAdapter`。
2. v3 ticker 返回 OI/funding，但 Phase 1 的“可用数据”定义按功能范围而不是按 raw payload 定义；这些字段在 Phase 1 的 semantic contract 必须是 `NOT_AVAILABLE`，不能因为“接口有返回”而提前进入策略。
3. instruments 是静态/准静态配置，不一定有 market event timestamp；其 `exchange_timestamp` 可以为 NULL，不能把本地抓取时间冒充交易所产生时间。
4. 全市场 200 symbols × 4 个 Phase 1 K 线周期每 5 分钟约 800 次 REST 请求；必须使用受控并发、token bucket、429 退避和 gap recovery，不能在每个 symbol 建线程。
5. 40GB 磁盘与随想记共存时，Phase 1 K 线 retention 按 timeframe 配置，默认 `5m=30d`、`15m=90d`、`1H=180d`、`4H=365d`；任何增长都必须做容量/恢复演练。全市场 1m 和每个 WS tick 不进入 Phase 1 永久存储。
6. `STALE` 不是中性值。Stage 1 在必需数据 stale 时应输出不可交易的结果或跳过本轮；不得用上一轮值静默续期。
7. 用户确认的 20% 单笔计划风险和最多 3 仓没有组合风险上限，保留为后续 Risk Engine 的设计输入；Phase 1 不执行它，也不把“有结构信号”包装成可交易建议。

## ACCEPTED

- 绿地项目，没有既有代码和 Git 历史；不做迁移兼容。
- Python asyncio、aiohttp、websockets、Pydantic、SQLAlchemy async、PostgreSQL、Alembic、pytest。
- 全部外部事实带来源、交易所、时间、状态和 raw reference；冲突源全部保留。
- 时间统一 UTC，并区分 `exchange_timestamp`、`fetched_at`、`processed_at`。
- `AVAILABLE`、`STALE`、`NOT_AVAILABLE`、`ERROR` 四态模型。
- Phase 1 v3 public REST + v3 public WS；不需要 Bitget 私有 API Key。
- Phase 1 只启动 collector 与 engine 两个容器；PostgreSQL 复用既有实例；Redis 不启动；executor 在 Phase 10+ 才加入。
- 默认 `TRADING_MODE=paper`；Phase 1 没有真实开仓能力、真实订单能力或可误触发的 Live Executor。
- 未来所有订单走 `clientOrderId` 幂等、Bitget reconciliation 和 recovery；Phase 1 只预留 schema，不执行。

## Bitget Phase 1 API Decision

### REST API version

```text
Version: UTA v3 public market REST
Base URL: https://api.bitget.com
Authentication: none
```

| 用途 | Endpoint | 真实响应字段 | Rate limit | Phase 1 语义 |
|---|---|---|---|---|
| 合约列表/规格 | `GET /api/v3/market/instruments?category=USDT-FUTURES` | `data[].symbol`, `category`, `baseCoin`, `quoteCoin`, `symbolType`, `type`, `status`, `minOrderQty`, `maxOrderQty`, `pricePrecision`, `quantityPrecision`, `priceMultiplier`, `quantityMultiplier`, `minOrderAmount`, `maxMarketOrderQty`, `makerFeeRate`, `takerFeeRate` | 20 req/s/IP | Universe 主数据；每小时/启动刷新 |
| 全市场行情 | `GET /api/v3/market/tickers?category=USDT-FUTURES` | `data[].category`, `symbol`, `lastPrice`, `openPrice24h`, `highPrice24h`, `lowPrice24h`, `bid1Price`, `ask1Price`, `bid1Size`, `ask1Size`, `price24hPcnt`, `volume24h`, `turnover24h`, `indexPrice`, `markPrice`, `ts` | 20 req/s/IP | 5m Stage 1 全市场快照 |
| K 线 | `GET /api/v3/market/candles?category=USDT-FUTURES&symbol=<symbol>&interval=5m|15m|1H|4H&limit=<n>` | 每行 `[timestamp, open, high, low, close, base_volume, quote_turnover]` | 20 req/s/IP；单响应最多 1000 bars | 只保存已闭合 K 线，归一化为 canonical candle |

`openInterest`、`fundingRate`、`nextFundingTime` 即使出现在 ticker response，也不进入 Phase 1 canonical Stage 1 input；Phase 2 再建立专门的 OI/Funding contract 和历史口径。

### WebSocket API version

```text
Version: UTA v3 public WebSocket
Endpoint: wss://ws.bitget.com/v3/ws/public
Authentication: none
```

订阅格式：

```json
{
  "op": "subscribe",
  "args": [
    {"instType": "usdt-futures", "topic": "ticker", "symbol": "BTCUSDT"},
    {"instType": "usdt-futures", "topic": "kline", "symbol": "BTCUSDT", "interval": "5m"}
  ]
}
```

实时 response contract：

- ticker：`arg.instType/topic/symbol`，`data[]` 含 `lastPrice`、`bid1Price`、`ask1Price`、24h volume/turnover、`indexPrice`、`markPrice`，outer `ts`；官方当前 futures push 约 200ms。
- kline：`arg.instType/topic/symbol/interval`，`data[]` object 含 `start`、`open`、`close`、`high`、`low`、`volume`、`turnover`，outer `ts`；只把已闭合 bar 放入 Stage 1。

WS 限制：300 connection requests/IP/5min、最多 100 connections/IP、240 subscription requests/hour/connection、最多 1000 channel subscriptions/connection、10 messages/s/connection；官方建议每连接少于 50 channels。连接每 30s 发送 `ping`，2 分钟没有 ping 会断开。REST 与 WS 使用同一官方域名限频体系，适配器必须统一限速。

## Phase 1 Data Contract

### 统一 observation envelope

```text
CanonicalObservation:
  symbol: string | null
  value: Decimal | string | bool | null
  source: bitget_v3_rest | bitget_v3_ws | derived_phase1
  exchange: bitget
  exchange_timestamp: UTC datetime | null
  fetched_at: UTC datetime
  processed_at: UTC datetime | null
  status: AVAILABLE | STALE | NOT_AVAILABLE | ERROR
  raw_payload: JSONB | null
  raw_reference: string | null
```

规则：`raw_payload` 与 `raw_reference` 至少一个非空。大包响应保存一次脱敏 raw batch，单个 normalized row 保存 `raw_reference` + JSON path；不把整包重复复制 200 次。`processed_at` 只由本系统写入，不能替代 `exchange_timestamp`。

### 字段合同

| Canonical field | Source response field | Type / unit | exchange_timestamp | Status / requiredness |
|---|---|---|---|---|
| `symbol` | instruments/tickers `data[].symbol`; WS `arg.symbol` | string | instruments NULL；ticker outer `ts` | 缺失=`ERROR`；Universe/行情必需 |
| `category` | instruments/tickers `category` | enum `USDT-FUTURES` | 同上 | 非 USDT-FUTURES=`REJECT` |
| `base_coin` / `quote_coin` | instruments `baseCoin`/`quoteCoin` | string | NULL | 缺失=`ERROR`；必需 |
| `symbol_type` / `contract_type` | instruments `symbolType`/`type` | string；必须 crypto/perpetual | NULL | 不匹配=`REJECT` |
| `instrument_status` | instruments `status` | string；Phase 1 接受 online | NULL | 非 online=`REJECT` |
| `min_order_qty` / `max_order_qty` | instruments `minOrderQty`/`maxOrderQty` | Decimal base quantity | NULL | 缺失=`ERROR`；保存但 Phase 1 不下单 |
| `price_precision` / `quantity_precision` | instruments precision fields | integer | NULL | 缺失=`ERROR` |
| `price_multiplier` / `quantity_multiplier` | instruments multipliers | Decimal | NULL | 缺失=`ERROR` |
| `last_price` | ticker `lastPrice` / WS `data[].lastPrice` | Decimal quote price | `ts` | 5s WS / 30s REST；必需 |
| `open_24h`, `high_24h`, `low_24h` | ticker `openPrice24h`, `highPrice24h`, `lowPrice24h` | Decimal quote price | `ts` | 同 ticker；Stage 1 必需 |
| `bid_price` / `ask_price` | ticker `bid1Price`/`ask1Price` | Decimal quote price | `ts` | 同 ticker；spread 必需 |
| `bid_size` / `ask_size` | ticker `bid1Size`/`ask1Size` | Decimal base quantity | `ts` | 缺失=`NOT_AVAILABLE`；spread 可继续但流动性门失败 |
| `price_change_24h` | ticker `price24hPcnt` | Decimal ratio | `ts` | ticker 新鲜时可用 |
| `volume_24h` / `turnover_24h` | ticker `volume24h`/`turnover24h` | base / USDT | `ts` | Universe/Stage 1 必需 |
| `index_price` / `mark_price` | ticker `indexPrice`/`markPrice` | Decimal quote price | `ts` | futures 必需 |
| `candle_open/high/low/close` | REST row[1:5] / WS object | Decimal quote price | REST row timestamp或 WS outer `ts`，另存 `bar_open_ts` | 只接受已闭合 bar；按 interval TTL |
| `candle_volume` / `candle_turnover` | REST row[5:6] / WS object | base / quote | 同 candle | Stage 1 必需 |
| `open_interest` | ticker `openInterest` | Decimal | `ts` | Phase 1 semantic=`NOT_AVAILABLE` |
| `funding_rate` | ticker `fundingRate` | Decimal ratio | `ts` | Phase 1 semantic=`NOT_AVAILABLE` |
| `cvd`, `liquidation`, `long_short`, `news`, `ai` | No Phase 1 provider/contract | null | null | `NOT_AVAILABLE`；不得 mock |

### Freshness

| Data | STALE threshold | Stage 1 behavior |
|---|---:|---|
| instruments | fetched_at 超过 24h | 停止 Universe refresh 和新候选 |
| REST all-tickers | `fetched_at` 超过 30s | 跳过本轮 Stage 1 |
| WS ticker | outer `ts` 距当前超过 5s | 该 symbol 不得产生新 Stage 1 candidate |
| 5m closed candle | interval-aware；理论最新 closed bar 缺失且超过 30s grace | 5m 结构不满足 |
| 15m closed candle | interval-aware；理论最新 closed bar 缺失且超过 60s grace | 15m 结构不满足 |
| 1H closed candle | interval-aware；理论最新 closed bar 缺失且超过 120s grace | 1H 结构不满足 |
| 4H closed candle | interval-aware；理论最新 closed bar 缺失且超过 180s grace | 4H 结构不满足 |

Kline freshness 不使用 `now - candle_timestamp > threshold`。系统先按 UTC 计算理论最新已闭合 bar，再与本地最新 closed bar 的 `bar_open_ts` 比较；`KLINE_INGESTION_GRACE_5M_SECONDS` 等配置只用于容忍边界时刻的正常传输延迟。

`STALE` 可以展示、记录和回放，但不得产生新的交易信号。Phase 1 不产生真实交易信号；此规则为后续阶段预留并在测试中固定。

## Phase 1 Scope

- 项目基础、配置、UTC 时间、结构化日志、PostgreSQL migration、Docker 基础；
- Bitget v3 public instruments/tickers/candles adapter；REST/WS 重连、限频、raw reference、status/freshness；
- 5m/15m/1H/4H 完整 K 线保存；全市场 ticker 每 5m；候选 ticker/kline WS 观察；
- Top 200 Universe；price/volume/spread/24h range；EMA/MA/VWAP/ATR/Bollinger/RSI/ADX 辅助计算；
- HH/HL/LH/LL、support/resistance、range center、基础结构分类；
- Stage 1 A/B/C/D 结果、compact snapshot、system health、重启不丢历史。

## Phase 1 Out of Scope

- 任何 Bitget private REST/WS、API Key、余额、仓位、订单、保护单或 live executor；
- OI/Funding 的语义使用和历史分析；
- CVD、主动买卖、Liquidation、Long/Short、OrderBook imbalance；
- News、Macro impact、Unlock、On-chain、Sector、BTC/ETH cross-exchange regime；
- AI provider、Evidence Chain、Stage 2、entry trigger、Risk Engine 执行；
- Paper order/fill、通知、Dashboard 控制、Backtest。

## Phase 1 Acceptance Criteria

1. 新环境无 Bitget private key 可以启动 collector/engine；配置默认 `TRADING_MODE=paper`。
2. 三个 v3 REST endpoint 和 v3 public WS ticker/kline contract tests 通过；未知字段不破坏解析，必需字段缺失会明确 ERROR。
3. 200 symbols 的全市场采集遵守 20 req/s/IP token bucket，不创建 200 个线程；WS 按连接建议少于 50 channels。
4. 所有 canonical observation 有 `symbol/value/source/exchange/exchange_timestamp/fetched_at/processed_at/status/raw_reference`。
5. instruments、ticker、closed candles 的 `AVAILABLE/STALE/NOT_AVAILABLE/ERROR` 转换有单元测试；STALE 不进入新 Stage 1 candidate。
6. REST/WS 断线、429、坏 JSON、缺字段、时间倒退、K 线缺口均可降级且不生成新风险。
7. Stage 1 只使用真实 v3 public 数据和本地可复算指标；OI/Funding/CVD/Liquidation/News/AI 的 Phase 1 semantic status 均为 NOT_AVAILABLE，不存在 mock fixture 被生产路径读取。
8. PostgreSQL 重启后仍能恢复历史 K 线、universe run、stage1 result、health；raw batch 按 retention 清理而不删除结构审计记录。
9. Phase 1 Docker 只有 collector、engine；无 executor、private client、order route 或 live config path。
10. 资源基线在随想记共存服务器上通过：容器 memory limit 生效、日志轮转、磁盘余量 health gate、PostgreSQL connection pool 在预算内。

## Phase 1 Test Plan

### Contract / parser

- instruments 成功响应、空 data、HTTP 429、非 00000、缺 `symbol`/`type`、未知字段；
- tickers 成功响应、单 symbol、全市场响应、负数/空字符串、旧 `ts`、`openInterest`/`fundingRate` 被 raw 保存但 semantic 不被 Stage 1 读取；
- candles 七列长度、坏 Decimal、倒序 rows、未闭合 row、缺 bar、跨日/时区边界；
- v3 WS subscribe ack、ticker snapshot、kline snapshot、ping/pong、断线重连、重复消息、sequence/gap recovery。

### Freshness / time

- 固定 UTC 时钟测试 ticker 的 5s/30s 边界，以及 Kline 在 interval boundary、ingestion grace、缺失理论最新 closed bar 和 `10:30 UTC -> 1H latest=09:00` 场景；
- `exchange_timestamp`、`fetched_at`、`processed_at` 顺序和静态 instruments 的 NULL exchange timestamp；
- 本地时钟偏移、未来时间、时间倒退均标记 ERROR/STALE，不静默修正为现在。

### Stage 1 rules

- 在线 crypto perpetual 进入 Universe；delivery、offline、RWA、非 USDT 被拒；
- spread 过大、turnover/OI semantic 不可用、K 线缺失、range central、1H/4H 严重冲突进入 C/D；
- 每个指标只作为辅助；单 RSI/MACD 不得生成 A；
- A 最多 1–5 个；没有 A 输出 `NO_HIGH_CONFIDENCE_CANDIDATE`；
- stale 必需数据不得输出可触发的 A/B。

### Failure / resource

- REST 429 退避和限速；WS 100 connections/10 msg/s 边界测试；
- 每个服务独立失败，collector 失败时 engine 停止新候选但历史可查；
- Docker memory limit、日志轮转、磁盘阈值、PostgreSQL connection pool；
- 进程重启、容器重启、PostgreSQL 重启后历史状态和 health 可恢复。

## Recommended Project Structure

Phase 1 推荐只创建这些边界；未来目录可扩展，但业务层不直接 import exchange raw models：

```text
quant_v1/
  app/
    config.py
    logging.py
    health.py
    contracts/
      observation.py
      instrument.py
      ticker.py
      candle.py
      screening.py
    adapters/
      bitget_v3/
        rest_market.py
        ws_public.py
        parser.py
        rate_limiter.py
        models.py
    collectors/
      market_collector.py
      reconnect.py
      gap_recovery.py
    market/
      indicators.py
      structure.py
      freshness.py
    screening/
      universe.py
      stage1.py
    storage/
      models.py
      database.py
      repositories.py
      migrations/
    services/
      collector_service.py
      engine_service.py
    tests/
      contracts/
      adapters/
      market/
      screening/
      storage/
      integration/
  docker/
    collector.Dockerfile
    engine.Dockerfile
    compose.phase1.yml
  docs/
```

`adapters/bitget_v3` 只负责 v3 原始字段解析和错误映射；`contracts/` 输出 canonical objects；`market/`、`screening/` 只依赖 canonical objects。未来 v2 若加入，放在 `adapters/bitget_v2_classic/`，不能穿透到业务模块。

## A. Architecture

整体产品保留 collector、engine、executor 三个边界；Phase 1 只启动 collector + engine，PostgreSQL 复用已有实例。Collector 负责 v3 public REST/WS、限频、重连、缺口补偿、规范化和 freshness；Engine 负责 Universe、指标、结构和 Stage 1；Executor、AI、Risk、Evidence、Dashboard 在后续 Phase 才启用。所有服务通过 canonical contracts、PostgreSQL tables 和 outbox 交互，不传递自然语言交易指令。

## B. Phase 1 Data Sources

- Bitget UTA v3 public REST：instruments、all tickers、5m/15m/1H/4H candles；免费、无需认证、20 req/s/IP。
- Bitget UTA v3 public WS：`wss://ws.bitget.com/v3/ws/public`，ticker/kline；无需认证；300 connections/IP/5min、100 connections/IP、240 subscription requests/hour/connection、1000 channels/connection、10 messages/s/connection，建议每连接少于 50 channels。
- OI/Funding raw 字段不作为 Phase 1 semantic data；其他交易所、新闻、AI 和宏观均为后续阶段。

## C. Database Core Schema

Phase 1 创建 `symbols`、`ingest_batches`、`market_observations`、`klines`、`market_snapshots`、`universe_runs`、`universe_members`、`screening_runs`、`screening_results`、`system_health`、`outbox_events`。未来已预留 `stage1/stage2 result` contract、`evidence_chains`、`trade_plans/risk_budget`、`positions`、`orders/clientOrderId`、`fills/trades`、`equity_history`、`reconciliation`、`backtest_runs`，并固定时间、唯一键、状态和溯源字段。

## D. Stage 1 Phase 1 Rules

每小时从在线 USDT perpetual 组成 Top 200；每 5m 用 v3 all-ticker + 已闭合 5m/15m/1H/4H K 线计算 price/volume/spread/range、EMA/MA/VWAP/ATR/Bollinger/RSI/ADX 和 HH/HL/LH/LL。通过 freshness、流动性、结构可识别、非 range central、非严重多周期冲突等硬门后输出 A/B/C/D。A 最多 1–5 个；无 A 即不产生高置信候选。不存在 OI/Funding/CVD/Liquidation/News/AI 的 mock 或隐式默认值。

## E. State Machine

Phase 1 实际使用 `BOOTING → RECOVERY_MODE → RUNNING/DEGRADED` 的系统健康子集，以及 `REJECT → WATCH → STAGE1_SELECTED → WATCH/REJECT` 的币种筛选子集。`STAGE2_ANALYZING`、`WAIT_TRIGGER`、`ENTRY_READY`、`POSITION_OPEN`、`POSITION_MANAGEMENT`、`EXIT`、`COOLDOWN` 在后续实现；Phase 1 不产生 order/position 状态。任何 stale、解析错误或数据库故障都不能进入新风险路径。

## F. Risk Engine 核心公式

后续 Phase 10 使用：

```text
equity_at_entry = 开仓前确认权益
risk_budget = equity_at_entry × 0.20
planned_loss = stop_loss_cost + entry_fee + exit_fee + entry_slippage + exit_slippage
planned_loss <= risk_budget
expected_RR = abs(target - entry) / abs(entry - stop) >= 1.5
```

多单要求 `liquidation_price < structural_stop - safety_buffer`；空单要求 `liquidation_price > structural_stop + safety_buffer`。风险预算在交易创建时锁定；最多 3 仓、最多一次加仓且总风险不超过原始预算。Phase 1 只保存设计和 schema，不执行公式和订单。

## G. Deployment topology

Phase 1：`quant-collector`（256MB 建议上限）+ `quant-engine`（384MB 建议上限）+ 既有 PostgreSQL 独立 database/user；不启用 executor、Redis 或第二个 PostgreSQL。日志轮转，单次 Kline fetch 默认 `KLINE_FETCH_LIMIT=100`，raw batch 7–30 天，K 线按 `5m=30d`、`15m=90d`、`1H=180d`、`4H=365d` 配置，磁盘低余量触发 health gate。Phase 10+ 再加入 executor，并重新做内存预算。

## H. Phase 1 验收条件

无私有 Key 可启动；v3 REST/WS contract test 通过；全字段有 UTC 三时间模型和四态 status；REST/WS 断线、429、坏响应、缺字段和缺 bar 可降级；STALE 不产生新候选；Stage 1 只读真实 v3 public data；无 OI/Funding/CVD/Liquidation/News/AI mock；数据库重启不丢历史；只有 collector/engine 容器；无 order/private/live 代码路径；资源、日志、磁盘和连接池在随想记共存环境的预算内。

## Final Gate

本审计完成后停止。下一步只有在用户确认本审计和上述 Phase 1 边界后，才进入 Phase 1 实现计划；在此之前不写正式功能代码。
