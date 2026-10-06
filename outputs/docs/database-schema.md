# PostgreSQL 数据库 Schema

## 1. 设计原则

PostgreSQL 同时保存原始证据、规范化数据、策略状态、订单状态和恢复记录。每条外部事实必须能够追溯到 `source`、`exchange`、事件时间、接收时间、原始 payload 和解析版本。金额、价格、数量、费率使用 `NUMERIC`；事件和原始响应使用 `JSONB`。

不要只保存“当前值”。当前值是从带时间戳的 observation/history 派生的缓存，历史快照才是回测和审计依据。

## 2. 公共溯源字段

所有 market/context 表至少包含：

```text
id UUID primary key
symbol TEXT nullable
metric TEXT
source TEXT
exchange TEXT nullable
exchange_timestamp TIMESTAMPTZ nullable
fetched_at TIMESTAMPTZ
processed_at TIMESTAMPTZ nullable
status TEXT CHECK (AVAILABLE, STALE, NOT_AVAILABLE, ERROR)
quality NUMERIC nullable
raw_payload JSONB nullable
parser_version TEXT
created_at TIMESTAMPTZ
```

`raw_payload` 脱敏保存，不保存 API secret、signature 或 authorization header。

`exchange_timestamp` 是交易所/来源给出的事件时间；`fetched_at` 是本系统收到完整响应的 UTC 时间；`processed_at` 是规范化或策略处理完成的 UTC 时间。静态 instruments 没有市场事件时间时，`exchange_timestamp` 为 NULL，原始响应的 `requestTime` 放在 `raw_payload`/`raw_reference`，不能把本地抓取时间冒充交易所事件时间。

## 3. 表分组

### 3.1 配置与市场主数据

`symbols`

- `symbol`、`exchange`、`product_type`、`base_coin`、`quote_coin`、`settle_coin`
- `status`、`contract_multiplier`、`min_trade_num`、`size_multiplier`
- `price_place`、`volume_place`、`maker_fee_rate`、`taker_fee_rate`
- `first_seen_at`、`last_config_ts`、`raw_payload`

唯一键：`(exchange, product_type, symbol)`。任何数量或价格计算都引用这一表的最新有效版本。

`universe_runs` / `universe_members`

- run 时间、规则版本、Top N；
- turnover/OI/liquidity/spread 指标 observation id；
- rank、include/exclude、reason；
- 已持仓 symbol 不能因离开 Top 200 被自动改为退出。

Phase 1 的 Universe 只引用 turnover、top-of-book liquidity、spread 和 instrument status；OI observation id 可以为 NULL 或 `NOT_AVAILABLE`，直到 Phase 2 建立 OI contract。

### 3.2 行情与衍生数据

`market_observations`

- 统一存储 price、mark、index、basis、bid、ask、volume 等原子事实；
- `metric`、`unit`、`window`、`source_exchange`、`aggregation_method`。

`ingest_batches`

- source、exchange、endpoint/channel、fetched_at、HTTP/WS status、request id、脱敏 `raw_payload` 或外部 `raw_reference`、parser_version；
- normalized observation 只保存 `raw_reference` 和 JSON path，避免 200 个 symbol 重复复制整包响应。

`market_snapshots`

- snapshot id、symbol、stage（universe/stage1/stage2/trigger）、created_at、processed_at、规则版本、输入 observation ids、quality/status；
- 它是“某次分析看到了什么”的不可变引用，不替代明细行情历史。

`klines`

- `exchange`、`symbol`、`interval`、`open_ts`、OHLCV、`is_closed`；
- mark/index/premium 用 `price_type` 区分；
- 唯一键 `(exchange, symbol, interval, open_ts, price_type)`。

`open_interest`

- `exchange`、`symbol`、`oi_contracts`、`oi_notional`、`unit`、`window`；
- 保存当前值和历史窗口，不跨交易所直接求和。

`funding_rates`

- `exchange`、`symbol`、`funding_rate`、`predicted_rate`、`funding_ts`、`next_funding_ts`；
- 记录 cap/floor/interval（若提供）。

`trades`

- `trade_id`、`price`、`size`、`quote_value`、`side/aggressor`、`trade_ts`；
- 唯一键 `(exchange, symbol, trade_id)`；CVD 从可追溯成交计算。

`cvd_windows`

- `symbol`、`exchange`、`window_start/end`、spot/futures `buy_volume`、`sell_volume`、`cvd`；
- `calculation_version`、输入 trade range。

`orderbook_snapshots`

- `symbol`、`exchange`、`depth_bps`、bids/asks JSONB、spread、bid_depth、ask_depth、sequence；
- 不保存“spoofing confirmed”，只保存可观察统计。

`liquidations`

- `exchange`、`symbol`、`side_liquidated`、price、size、bankruptcy_price、event_ts；
- `source_kind=observed_event`；未来 heatmap 不使用此表伪造。

### 3.3 Context 数据

`macro_events`

- `event_type`、title、provider、scheduled_ts、actual_ts、impact_window、source_url、status；
- 支持 FOMC/CPI/PPI/PCE/NFP 等 lock 事件。

`macro_observations`

- DXY、US2Y、US10Y、Nasdaq、S&P500、VIX、Gold 等；
- `series_id`、value、unit、vintage/release timestamp。

`news_events`

- `source`、`source_url`、`published_ts`、`detected_ts`、`symbol`、`event_type`、`impact`、`impact_window`、摘要、原始 hash；
- 重大风险事件必须可重复去重。

`tokenomics_snapshots` / `unlock_events`

- provider、methodology_version、circulating/total/max supply、market cap、FDV、unlock amount/value/percentage、recipient category、scheduled time；
- 未配置 provider 不插入伪造的 0 值，使用 status=`NOT_AVAILABLE` 的 observation。

`context_snapshots`

- 对每次 Stage 2 保存 BTC、ETH、sector、relative strength、宏观和新闻引用的 observation ids。

### 3.4 筛选与证据

`screening_runs`

- `stage`、started/finished、rule_version、input_window、status、error_summary。

`screening_results`

- `run_id`、symbol、classification A/B/C/D、direction、setup_type、reason、key_levels、support/resistance、input_observation_ids JSONB；
- 不保存不可复算的自然语言分数。

`stage1_results` / `stage2_results`

- 可以物理拆表，也可以用 `screening_results.stage` 作为单表分区；设计固定为同一 result contract：`snapshot_id`、symbol、classification、direction、setup_type、required_data_status、reason、rule_version、processed_at；
- Stage 2 额外引用 evidence_chain、AI analysis 和 invalidation reason；Phase 1 只创建/使用 Stage 1 行。

`evidence_chains`

- `chain_id`、symbol、direction、chain_type、state、created/updated、valid_until、rule_version、ai_analysis_id nullable。

`evidence_nodes`

- chain、node_type、polarity、status、metric、window、source ids、value JSONB、explanation、required boolean。

`evidence_edges`

- from/to node、relation（supports/conflicts/depends_on）、rule_version、validity。

`ai_analyses`

- provider、model、prompt_version、request_hash、input_observation_ids、structured_output JSONB、latency、status、error_code；
- 不保存 secret，不能产生直接交易字段。

### 3.5 交易、风控和恢复

`trade_signals`

- signal id、symbol、side、entry_mode、trigger zone、evidence_chain_id、stage2_snapshot_id、expires_at、status；
- 状态包含 invalidated/triggered/expired/rejected。

`trade_plans`

- `equity_at_entry`、`risk_pct`、`risk_budget`、entry、structural_stop、target、expected_rr、fee/slippage assumptions、planned loss、original risk budget；
- 加仓共享同一 trade plan。

`positions`

- local position id、symbol、side、mode、status、exchange_position_id、entry/avg price、quantity、leverage、isolated margin、liquidation price、risk budget、r multiple；
- 唯一约束：同一执行账户 + symbol 只能有一个 active position aggregate。

`orders`

- local order id、position id、intent type、side、reduce_only、order_type、qty、price、trigger_price、client_order_id、exchange_order_id、attempt、state、request/ack timestamps；
- unique `(exchange, client_order_id)`；
- `ACK_UNKNOWN` 不能被普通 retry 覆盖。

`fills`

- exchange fill id、order id、price、qty、fee、fee currency、funding、fill_ts、raw payload；
- unique `(exchange, fill_id)`。

`trades`

- 关闭后的交易聚合账本：trade id、position id、symbol、side、entry/exit fills、gross/net PnL、fees、funding、R、hold time、strategy/evidence/rule versions、opened/closed timestamps；
- 不与 `orders` 或 `fills` 混为一表。

`equity_history`

- exchange equity、available balance、margin used、realized/unrealized PnL、sample ts、source/status。

`backtest_runs`

- run id、dataset range、data source/version、rule version、config hash、started/finished、metrics JSONB、status、artifact reference；
- 回测结果与 live/paper 交易账本分离。

`reconciliation_runs` / `reconciliation_diffs`

- DB snapshot、Bitget snapshot、diff kind、resolution、operator、resolved_at；
- 保存 UNKNOWN_POSITION、missing stop、size mismatch 等差异。

`state_transitions`

- entity type/id、from/to、reason、rule_version、event ids、actor（system/user/recovery）、created_at。

`outbox_events`

- aggregate type/id、event type、payload、created_at、published_at、attempts、last_error；
- 用于 Dashboard、通知、Engine/Executor 消费。

`system_health`

- component、last heartbeat、last market tick、last Stage 1/2 run、last successful DB/exchange/AI operation、status、reason。

## 4. 关键约束与索引

- 所有外部时间和内部时间存 UTC `TIMESTAMPTZ`。
- 关键历史表按月份或 event time 分区；raw payload 设 retention，交易、证据、状态、对账永久保留。
- `positions` active 唯一约束、`orders.client_order_id` 唯一约束、`fills` exchange fill 唯一约束必须在数据库层实现，不能只靠 Python。
- `screening_results(symbol, stage, created_at)`、`evidence_nodes(chain_id)`、`orders(position_id, state)`、`market_observations(exchange, symbol, metric, event_ts)` 建索引。
- JSONB 只放原始或不稳定扩展字段；风控和状态字段必须是显式列，便于约束和查询。

## 5. Phase 1 Schema 使用边界

Phase 1 创建 migration 时至少创建：`symbols`、`ingest_batches`、`market_observations`、`klines`、`market_snapshots`、`universe_runs`、`universe_members`、`screening_runs`、`screening_results`、`system_health` 和 `outbox_events`。未来的 `evidence_chains`、`trade_plans`、`positions`、`orders`、`fills`、`trades`、`equity_history`、`reconciliation_runs`、`backtest_runs` 保留上述稳定字段和唯一约束，但 Phase 1 不创建交易执行行为。

## 6. 时间与 Freshness

所有数据库时间统一为 UTC `TIMESTAMPTZ`。Phase 1 freshness 规则固定为：ticker 使用 age-based freshness；Kline 使用 interval-aware freshness。对于 interval 为 `I` 的 Kline，系统按 UTC 当前时间计算理论上最新已闭合 bar 的 `bar_open_ts`：`floor(now / I) * I - I`。在该 bar 的 ingestion grace period 内不判缺失；超过 grace 后，只有本地最新 closed bar 的 `bar_open_ts` 小于理论值时才标记 `STALE`。因此 `10:30 UTC` 的 1H 最新完整 bar 是 `09:00–10:00`，它不会因为年龄达到 30 分钟而自动过期。

| Data | AVAILABLE 最大年龄 | 超时行为 |
|---|---:|---|
| v3 REST instruments | 24h | STALE；停止 Universe/新筛选 |
| v3 REST all-tickers snapshot | 30s | STALE；本轮 Stage 1 不运行 |
| v3 WS ticker | 5s | STALE；该 symbol 不得进入新结果 |
| 已闭合 1m bar（后续候选/Order Flow；非 Phase 1 全市场输入） | interval-aware；使用该粒度 grace | STALE；只影响依赖该粒度的结构 |
| 已闭合 5m bar | interval-aware；默认 grace 30s | STALE；Stage 1 关键输入缺失则不产生 A/B |
| 已闭合 15m bar | interval-aware；默认 grace 60s | STALE；Stage 1 关键输入缺失则不产生 A/B |
| 已闭合 1H bar | interval-aware；默认 grace 120s | STALE；Stage 1 关键输入缺失则不产生 A/B |
| 已闭合 4H bar | interval-aware；默认 grace 180s | STALE；Stage 1 关键输入缺失则不产生 A/B |

`processed_at` 延迟不能延长上述 TTL。STALE 数据可以展示和回放，但不得产生新的交易信号；Phase 1 本身不产生真实交易信号。

## 7. 数据保留建议

40GB 磁盘下，V1 不把所有 symbol 的毫秒级盘口永久保存：

- 交易成交、订单/仓位/证据/状态：永久保留；
- K 线保留期必须配置化，不得把所有 timeframe 硬编码为同一个天数。默认配置为 `KLINE_RETENTION_5M_DAYS=30`、`KLINE_RETENTION_15M_DAYS=90`、`KLINE_RETENTION_1H_DAYS=180`、`KLINE_RETENTION_4H_DAYS=365`；这些默认值适配 40GB 低配服务器，扩容前必须通过磁盘容量测试和备份恢复演练；
- Phase 1 不保存全市场 1m K 线和每个 WS tick；需要更长回测历史时归档为压缩外部 artifact，不改变核心表；
- 盘口原始深度：只保留候选和持仓 symbol 的窗口，非候选保存聚合统计；
- raw payload：按表和大小设 7–30 天 retention，保留 hash、解析结果和关键引用；
- 每日压缩归档前先验证备份可恢复，不能在未验证时删除交易审计数据。
