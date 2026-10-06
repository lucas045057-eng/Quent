# 数据源可行性审计

审计日期：2026-09-20。接口名称和限频以官方文档为准；适配器实现前仍要做一次在线 contract test，不能把文档里的示例响应当作实时数据。

## 1. 统一状态与时间规则

每条数据保存 `symbol`、`value`、`source`、`exchange`、`event_timestamp`、`received_timestamp`、`status`。状态只允许：

```text
AVAILABLE  当前值存在且在该指标 freshness SLA 内
STALE      有值但超过 freshness SLA，只能用于历史展示，不能产生新开仓
NOT_AVAILABLE  来源不提供、未配置或无法可靠推导
ERROR      已配置但请求/解析/校验失败
```

`event_timestamp` 是交易所或发布机构给出的时间；`received_timestamp` 是本系统收到时间。Stage 1/2 使用事件时间排序，使用接收延迟做质量判断。冲突源不互相覆盖，原值全部保留。

## 2. V1 核心数据源

### Phase 1 Bitget 版本决策

Phase 1 采用当前 Bitget UTA v3 public market API，REST 与 public WebSocket 均使用 v3。当前官方 Market Data 目录实际可访问且 smoke test 返回 `code=00000` 的路径是 `/api/v3/market/instruments`；官方旧升级映射页仍出现 `/api/v3/public/instruments`，该路径在本次只读 smoke test 中未返回成功，因此不作为 Phase 1 endpoint。这个差异必须在适配器 contract test 中固定，不能在业务层静默 fallback。

Phase 1 不同时使用 Classic v2 与 UTA v3。若未来必须同时支持两种账户/API：`BitgetV3UtaAdapter` 与 `BitgetV2ClassicAdapter` 分开实现，各自解析原始字段，再输出同一个 `CanonicalInstrument`、`CanonicalTicker`、`CanonicalCandle`；Stage 1、Risk、State Machine 不允许出现 `v2`/`v3` 原始字段名。

| Data | Provider | Endpoint / Channel | WS / REST | Update Frequency | Cost | Rate Limit | Required Auth | Fallback | Can Be Missing? |
|---|---|---|---|---|---|---|---|---|---|
| Phase 1 Bitget 合约列表、最小量、tick/step、手续费 | Bitget UTA v3 official | `GET /api/v3/market/instruments?category=USDT-FUTURES` | REST | 每小时 + 启动时 | 公共接口免费 | 20 req/s/IP | 否 | 无；超过 24h 或请求失败为 STALE/ERROR，停止新 Universe | 否，Universe 不能安全运行 |
| Phase 1 Bitget 全市场 ticker | Bitget UTA v3 official | `GET /api/v3/market/tickers?category=USDT-FUTURES` | REST | 每 5m Stage 1 + 启动补偿 | 公共接口免费 | 20 req/s/IP；公共 market API 总限频按官方域名规则 | 否 | v3 WS ticker 仅用于候选实时观察；全市场失败则 Stage 1 不运行 | 不能用于新筛选 |
| Phase 1 Bitget ticker 实时观察 | Bitget UTA v3 official | `wss://ws.bitget.com/v3/ws/public`; `topic=ticker`, `instType=usdt-futures`, `symbol=BTCUSDT` | WebSocket | 官方当前 futures push 约 200ms；Phase 1 只订阅候选/持仓观察 | 公共接口免费 | 300 connection requests/IP/5min；最多 100 connections/IP；240 subscription requests/hour/connection；最多 1000 channel subscriptions/connection；10 messages/s/connection；建议每连接少于 50 channels | 否 | REST v3 tickers；重连 + REST 补偿 | 候选实时价可缺，过期则不触发 |
| Phase 1 Bitget 5m/15m/1H/4H K 线 | Bitget UTA v3 official | `GET /api/v3/market/candles?category=USDT-FUTURES&symbol=&interval=&limit=` | REST | 每 5m；只保存已闭合 bar | 公共接口免费 | 20 req/s/IP；最多 1000 bars/response | 否 | v3 WS `topic=kline` 只用于候选实时观察；缺口 REST 补偿 | 已闭合结构 K 线不可缺 |
| Phase 1 v3 K 线实时观察 | Bitget UTA v3 official | `wss://ws.bitget.com/v3/ws/public`; `topic=kline`, `interval=5m/15m/1H/4H` | WebSocket | 市场有交易时推送；无交易按粒度推送 | 公共接口免费 | 同上；ping 每 30s，2 分钟未收到 ping 会断开 | 否 | REST v3 candles | 未闭合 bar 可缺，Stage 1 不用它 |
| Phase 1 24h 价格、成交量、成交额、spread 输入 | Bitget UTA v3 official | v3 ticker response fields `lastPrice`, `openPrice24h`, `highPrice24h`, `lowPrice24h`, `bid1Price`, `ask1Price`, `bid1Size`, `ask1Size`, `price24hPcnt`, `volume24h`, `turnover24h`, `indexPrice`, `markPrice`, `ts` | REST + WS | Stage 1 每 5m；候选观察实时 | 公共接口免费 | 依 endpoint/WS domain 上述规则 | 否 | 仅同一 v3 source；失败为 STALE/ERROR | 关键字段不能缺 |
| Phase 1 K 线字段 | Bitget UTA v3 official | REST row `[timestamp, open, high, low, close, base_volume, quote_turnover]`; WS object `start`, `open`, `close`, `high`, `low`, `volume`, `turnover`, outer `ts` | REST + WS | 同上 | 公共接口免费 | 同上 | 否 | REST row 为 canonical fallback | 关键闭合 bar 不能缺 |
| Phase 2 保留但 Phase 1 不使用的 OI/funding | Bitget UTA v3 ticker/raw | v3 ticker fields `openInterest`, `fundingRate`, `nextFundingTime` | REST + WS raw payload only | 原始报文可记录 | 公共接口免费 | 同上 | 否 | Phase 2 专用 adapter/历史接口 | Phase 1 semantic status=`NOT_AVAILABLE`，禁止筛选/信号 |
| Phase 2+ 合约成交、CVD、orderbook、funding history、私有仓位 | Bitget UTA v3 official | v3 market fills/orderbook/funding and private UTA channels | REST + WS | Phase 3/2/10+ | 交易手续费另计 | 各 endpoint 单独限频 | 私有需认证；公共不需 | 等对应 Phase | Phase 1 不实现 |

本次只读 smoke test 已验证 v3 instruments、tickers、candles 返回 `code=00000`；v3 WebSocket public endpoint 已成功订阅 ticker/kline 并收到 snapshot。Phase 1 的公共接口不需要 API Key。REST 与 WS 的原始报文仍必须通过 adapter 解析和 schema 校验后才能进入 canonical contract。

## 3. 交叉验证交易所

| Provider | 可用数据 | 官方接口/通道 | 认证 | 限制与结论 |
|---|---|---|---|---|
| Binance USDⓈ-M Futures | price/mark/index、K 线、OI、OI history、funding、taker buy/sell、global/top trader long-short、depth、aggTrade、forceOrder | REST 常见路径包括 `/fapi/v1/openInterest`、`/futures/data/openInterestHist`、`/fapi/v1/fundingRate`、`/futures/data/takerBuySellVol`、`/futures/data/globalLongShortAccountRatio`；WS 采用官方 USDⓈ-M public/market streams，如 `@aggTrade`、`@depth`、`@markPrice`、`@forceOrder` | 公共市场数据多数不需 key；私有需 key | 请求权重和 WS 连接/stream 数受官方动态规则约束；官方公告显示 WS URL 架构会变更，因此 base URL 必须配置化。可作为 OI/funding/CVD/liquidation 的强交叉源 |
| Bybit V5 | linear ticker/K 线/orderbook/trades、OI history、funding history、long-short ratio、all liquidation | `GET /v5/market/open-interest`、`/v5/market/funding/history`、`/v5/market/account-ratio`、`/v5/market/tickers`；WS `v5/public/linear`，topic `tickers.*`、`publicTrade.*`、`orderbook.*`、`allLiquidation.*` | 公共市场数据不需 key | 官方文档明确 OI 可按 5m/15m/1h/4h 查询，强平流每 500ms；限频按 endpoint。适合作为 Stage 2 的 liquidation 事件源，但不是未来清算热力图 |
| OKX V5 | tickers/K 线/mark/index、books、trades、OI、funding/history、instrument config | REST `/api/v5/market/candles`、`/api/v5/market/books`、`/api/v5/public/open-interest`、`/api/v5/public/funding-rate` 等；public WS `/ws/v5/public`，private `/ws/v5/private` | 公共不需 key；私有需 key | 官方文档给出 candles 40 req/2s、full order book 10 req/2s 等 endpoint 级限频；仅用于交叉验证，不作为执行事实源 |
| Hyperliquid | all mids、perp asset context（mark/funding/OI/oracle/volume）、trades、L2 book、candle | REST `POST https://api.hyperliquid.xyz/info`，body type `metaAndAssetCtxs`/`allMids`；WS `wss://api.hyperliquid.xyz/ws`，订阅 `trades`、`l2Book`、`candle`、`activeAssetCtx` | 公共市场数据不需 key | 官方文档有 weight/rate-limit 规则，断线需重连和 snapshot 补偿；可用作第三方市场上下文，不作为 Bitget position source |
| MEXC Futures | contract detail、index/fair price、funding、K 线、ticker、成交、盘口、holdVol | 官方文档列出 `https://contract.mexc.com`、`/api/v1/contract/detail`、`/index_price/{symbol}`、`/fair_price/{symbol}`、`/funding_rate/{symbol}`、`/kline/{symbol}`；WS `wss://contract.mexc.com/edge`，`sub.ticker`/`sub.deal`/`sub.depth`/`sub.funding.rate` | 公共市场数据不需 key；交易权限另行核验 | 文档版本较旧且官方公告存在域名/API 访问变化；V1 只把它列为可选交叉源，启用前必须通过在线 smoke test，失败即 `NOT_AVAILABLE`，不得阻塞 Bitget |

这些来源的值只用于“交叉验证”和派生上下文，不能把各交易所的 OI、合约乘数、成交量单位直接相加。每个适配器必须先转成统一单位并保存转换规则。

## 4. 宏观数据

| Data | Provider | Endpoint / Feed | Update | Cost / Auth | V1 处理 |
|---|---|---|---|---|---|
| FOMC 会议日历/声明 | Federal Reserve | 官方 FOMC calendar 页面 | 日历变化时同步；事件窗口实时读取 | 免费，无 API key | 作为宏观事件锁；用官方发布日期和时区，不用搜索摘要 |
| CPI、PPI、NFP、失业率 | U.S. BLS | 官方 release schedule 与 BLS calendar ICS | 日历同步；发布后读取 | 免费；ICS 不需 key | 事件时间可用于 30m before / 15m after macro lock；数值快照另行接入前不参与方向判断 |
| GDP、PCE、Personal Income/Outlays | U.S. BEA | 官方 Release Schedule；正式 API 需另行配置 | 事件日历同步 | 页面免费；API 权限/Key 需单独配置 | 先支持日历和锁，不把预测或修订值当实时交易输入 |
| DXY、US2Y、US10Y、VIX、Nasdaq、S&P500、Gold | FRED / 官方市场源 | FRED `fred/series/observations`；具体 series_id 在配置中维护 | 日/发布频率；不是加密实时 tick | FRED Web Services 要 API key | 只作为宏观上下文；若 freshness 不满足，状态为 STALE，不阻塞已有仓位保护 |

## 5. 明确不可可靠实现或需要额外供应商

### 清算热力图

官方交易所能提供的是“已发生的公开强平事件”（例如 Bybit `allLiquidation`、Binance `forceOrder`），不能直接提供全市场未来清算价分布。V1 只做已观测强平事件聚合；`Liquidation Heatmap`、`Liquidation Map`、未来 liquidation levels 默认 `NOT_AVAILABLE`。不能用图表网站截图、文章或猜测补齐。

### 新闻

不存在一个覆盖所有项目官方 X、官方 Blog、GitHub、交易所公告和可信媒体、且稳定免费统一授权的官方接口。V1 采用可插拔 `NewsProvider`：官方 RSS/Atom、GitHub Releases API、交易所公告 feed 逐源配置；X API、付费媒体和聚合服务在没有密钥/授权前为 `NOT_AVAILABLE`。任何新闻事件必须保存 URL、source、published_at、detected_at、impact、impact_window 和原始摘要。

### Tokenomics / Unlock

流通量、FDV、未来 24h/3d/7d 解锁通常需要项目级数据或付费供应商，且不同供应商定义不一致。V1 不选定供应商，不虚构 unlock endpoint；未配置时相关 Evidence Chain 不能声称已确认。后续接入必须保留 provider、methodology、retrieved_at 和版本。

### On-chain / Whale / TVL / Bridge Flow

这些数据需要链节点、索引服务或授权供应商，无法由交易所行情接口可靠替代。V1 设计接口但默认 `NOT_AVAILABLE`，不会用“没有数据”推导中性或方向。

### Spoofing / Iceberg

公开订单簿快照只能观察挂单变化，不能可靠证明撤单者意图。V1 可以输出 `ORDERBOOK_IMBALANCE`、墙体持续时间和成交后撤单统计，但不输出确定的 `SPOOFING_DETECTED` 或 `ICEBERG_DETECTED`；证据状态只能是 `UNCONFIRMED`。

## 6. Phase 1 实际数据边界

Phase 1 可以真实使用：

- v3 instrument metadata：symbol、category、base/quote、crypto type、perpetual type、online status、quantity/price precision、min/max quantity、multipliers、min order amount；
- v3 ticker：last/open/high/low、bid/ask、bid/ask size、24h percentage、24h volume/turnover、mark/index price、exchange `ts`；
- v3 REST/WS completed candles：5m、15m、1H、4H 的 OHLC、base volume、quote turnover；1m 不做全市场持久化，留给后续候选/Order Flow 阶段；
- 基于上述真实数据计算的 price change、EMA/MA、VWAP、ATR、Bollinger、RSI、ADX（指标是本地派生值，不是外部实时事实）；
- 结构化 HH/HL/LH/LL、support/resistance、range 中央判定和基础 A/B/C/D 分类。

Phase 1 必须保持 `NOT_AVAILABLE`，不能 mock，也不能用于 Stage 1 交易信号：OI、Funding、CVD、Liquidation、Long/Short、News、Macro impact interpretation、Unlock、On-chain、AI、OrderBook imbalance、spot flow、sector/regime cross-exchange confirmation。v3 ticker raw payload 中出现 OI/funding 不改变这个边界。

## 7. 可行性结论

Phase 1 只实现 Bitget v3 public market foundation；OI/funding 进入 Phase 2，CVD/orderbook 进入 Phase 3，liquidation/long-short/basis 进入 Phase 4，BTC/ETH/sector 进入 Phase 5，news/macro/AI 进入 Phase 6。Phase 7–8（on-chain、unlock、options、heatmap）不是 V1 基础开仓条件，在数据源确认前必须保持可缺省。Live 开关的前置条件是 Bitget 私有接口、保护单、对账和幂等通过测试，而不是“所有可选数据都齐全”。

## 7. 参考官方文档

- [Bitget API catalog](https://www.bitget.com/docs/classic/catalog?tab=contract)
- [Bitget current v3 market data](https://www.bitget.com/docs/catalog/market/market-data)
- [Bitget current v3 public ticker WebSocket](https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel)
- [Bitget v3 UTA WebSocket quick start and limits](https://www.bitget.com/docs/uta/quick-start)
- [Bitget v2/v3 upgrade mapping](https://www.bitget.com/docs/classic/uta-api-upgrade-guide)
- [Binance Futures developer docs](https://developers.binance.com/docs/derivatives)
- [Bybit open interest](https://bybit-exchange.github.io/docs/v5/market/open-interest)
- [Bybit liquidation stream](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation)
- [OKX API guide](https://app.okx.com/docs-v5/en/)
- [Hyperliquid info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [MEXC contract API](https://mexcdevelop.github.io/apidocs/contract_v1_en/)
- [Federal Reserve FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
- [BLS release schedule](https://www.bls.gov/schedule/2026/)
- [BEA release schedule](https://www.bea.gov/news/schedule)
- [FRED series observations](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
