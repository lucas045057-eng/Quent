# Strategy：Universe、Stage 1、Stage 2 与 Evidence Chain

本文描述完整 V1 的目标策略。Phase 1 只实现 Pre-Implementation Review 中明确的 Bitget v3 public 子集；本文出现的 OI、Funding、CVD、Liquidation、Long/Short、News、AI、OrderBook、BTC/ETH 和 Sector 是后续 Phase 的输入，不得提前作为 Phase 1 Stage 1 条件。

## 1. 共同原则

策略不输出“因为 RSI 超卖所以 BUY”之类的单指标结论，也不使用简单多数投票替代证据链。每个候选的结果由可复现规则、时间窗、数据 freshness 和明确的证据节点组成。AI 只能给结构化解释和冲突摘要；交易放行必须由 Hard Rule Engine 和 Risk Engine 完成。

## 2. Universe：每小时 Top 200

完整 V1 输入：Bitget 全部 `USDT-FUTURES` 合约配置、ticker、24h turnover/volume、OI、bid/ask、盘口深度、交易状态。Phase 1 实际只使用 v3 instruments、ticker 的 price/volume/bid/ask/mark/index 字段和已闭合 5m/15m/1H/4H K 线；OI 和 depth 在对应 Phase 前为 `NOT_AVAILABLE`。

过滤：

1. 合约状态正常、未下线、quote/settle 为 USDT、属于配置的加密资产池。
2. 完整 V1 中 24h turnover、OI、1% 深度和 spread 满足配置阈值；Phase 1 只检查 turnover、bid/ask spread 和可用的 top-of-book size，OI/depth 语义条件留到后续 Phase。阈值放入配置，不写死在代码。
3. ticker、contract config 在 freshness SLA 内；Phase 1 不要求 OI freshness。异常价格、零成交、负数、时间倒流直接拒绝。
4. 按 `liquidity_rank`、`turnover_rank`、`oi_rank` 和 `spread_rank` 的确定性排序选 Top 200；这不是交易信号，也不使用 AI。

已有仓位不受 Top 200 变化影响，仍处于 `POSITION_MANAGEMENT`。退出后重新进入扫描，不能复用退出前的旧信号。

## 3. Stage 1：每 5 分钟批量筛选

Stage 1 只读最新已完成的 5m、15m、1H、4H K 线及低成本行情数据。它的目标是减少 Stage 2 候选，不承担最终开仓判断。

### 3.1 Stage 1 输入

- Spot/Perpetual/Mark/Index price 与 basis；
- 5m、15m、1H、4H、24H 变化，高低点，7D 区间；
- EMA/MA/VWAP/Anchored VWAP、RSI、MACD、Stochastic RSI、Bollinger、ATR、ADX；
- volume、volume MA、volume spike、taker buy/sell（若来源可用）；
- 结构节点 HH/HL/LH/LL、support、resistance、recent high/low、break/retest level；
- BTC/ETH 的 15m/1H/4H 方向和数据状态。

指标全部是辅助特征，不能单独触发 A 组。

Phase 1 只启用其中的 v3 public price/volume/bid/ask/mark/index、24h high/low、已闭合 5m/15m/1H/4H K 线和本地可复算指标；basis、taker buy/sell、BTC/ETH context、OI、Funding、CVD、Liquidation、Long/Short、News、AI 等输入为 `NOT_AVAILABLE`，不能被默认成 0、中性或上一轮值。

### 3.2 Stage 1 硬门

候选先过硬门，再做确定性排序：

1. 关键输入不能是 `STALE`；缺失只允许出现在非必要辅助指标。
2. 5m/15m 至少形成一个可描述结构，且 1H/4H 不能出现配置定义的严重方向冲突。
3. 不能处于识别范围的震荡中央；support/resistance 无法定位时拒绝。
4. spread、滑点和预估 ATR 不能使潜在结构止损不可执行。
5. 预估结构止损到合理目标的 R:R 不低于配置下限；默认 `MIN_RR=1.5`。
6. 宏观锁、新闻风险锁、全局暂停新仓、已有 3 仓等条件只决定“不可开仓”，不改变事实记录。

### 3.3 Stage 1 分类

```text
A = DEEP_ANALYSIS       通过结构、流动性、空间和 freshness 硬门，最多 1–5 个
B = WAIT_TRIGGER        方向/结构可观察，但尚未到触发区或证据不完整
C = NO_EDGE             有数据但没有明确方向优势或空间优势
D = REJECT              数据过期、异常、流动性、状态或锁定条件不满足
```

A 组少于 1 个时输出 `NO_HIGH_CONFIDENCE_CANDIDATE`，系统不交易。A 组超过 5 个时按“触发区距离、结构完整度、可实现空间、流动性质量”的确定性排序截取 5 个，不使用模糊分数。

## 4. Stage 2：候选深度验证

Stage 2 必须重新取数，不能直接拿 Stage 1 snapshot。最低输入为：price、volume、spot/futures CVD、OI、funding、long/short、liquidation events、orderbook、basis、spot flow、BTC/ETH、sector、news、unlock、macro；不可用项明确标记。

### 4.1 Stage 2 硬校验

1. 重新验证 symbol 仍可交易，关键数据时间戳在 SLA 内。
2. 重新计算结构止损、可执行空间、spread/slippage、资金费率拥挤、OI/价格关系。
3. Stage 1 结论与新数据冲突时输出 `INVALIDATE_STAGE1_SIGNAL`，状态返回 `WATCH` 或 `REJECT`。
4. 任何必须证据为 `NOT_AVAILABLE` 或 `ERROR` 时，不能把它当作“中性”；对应模板不成立。
5. 触发宏观锁、新闻风险锁或交易暂停时，保留分析但不生成可执行 entry。

### 4.2 AI 事件边界

Fast AI 只在 Stage 2 事件触发时处理新闻分类、宏观解释、异常摘要和证据初分类。强模型只在 `POTENTIAL_TRADE`、`CONFLICTING_EVIDENCE`、`ENTRY_ZONE_REACHED` 等状态变化调用。AI 输出必须通过 schema 校验：

```text
evidence_labels[]
conflicts[]
missing_data[]
event_impact
impact_window
analysis_timestamp
provider/model
prompt_version
```

AI 输出不能包含可执行订单指令，不能写入 `OrderIntent`，不能访问 Executor。

## 5. Evidence Chain 模型

一条链由 `chain`、`nodes`、`edges`、`invalidation_rules` 构成。每个节点保存：指标、原始 observation ids、来源、时间窗、status、方向、解释、计算版本。证据节点不是手工文本；文字只是对可复算节点的摘要。

### 5.1 HIGH_QUALITY_LONG

必需节点：

```text
突破关键压力
AND Spot CVD 上升（若配置为必需）
AND 成交量相对窗口扩大
AND OI 增长但 funding 未极端过热
AND 主动买盘持续占优
AND 回踩突破位得到承接
AND BTC/ETH 不处于反向强风险状态
```

入场模式可为 `BREAKOUT_ENTRY` 或 `RETEST_ENTRY`。价格刚接触压力位但未完成确认时，不放行。

### 5.2 HIGH_QUALITY_SHORT

必需节点：

```text
跌破支撑
AND 反抽失败
AND Spot CVD 下降（若配置为必需）
AND Taker Sell 持续
AND OI 增长且未被异常数据污染
AND 下方观察到已发生/可靠的长仓清算证据（可选或必需由配置决定）
AND BTC/ETH 不支持强反向上涨
```

入场模式可为 `BREAKDOWN_ENTRY` 或 `RETEST_SHORT`。

### 5.3 SHORT_SQUEEZE / LONG_SQUEEZE

`SHORT_SQUEEZE`：价格上升、OI 上升、funding 仍为负、空头定位增加、上方短仓清算事件和 taker buy 持续。`LONG_SQUEEZE` 对称地要求价格下降、OI 上升、funding 仍明显为正、多头拥挤、下方长仓清算和 taker sell 持续。

强平事件只能证明已发生的清算，不可自动推断未来热力图。缺少真实强平源时，squeeze 模板不得升级为 High Quality。

### 5.4 Exhaustion / Divergence / Fake Breakout

- `BULL_EXHAUSTION`：价格创高或接近阻力，价格继续上行但 CVD/主动买盘/成交量质量恶化，且出现反转触发；不能仅用 RSI 超买。
- `BEAR_EXHAUSTION`：价格创低或接近支撑，价格继续下行但 CVD/主动卖压恶化，且出现反转触发；不能仅用 RSI 超卖。
- `CVD_DIVERGENCE`：价格与现货/合约 CVD 在相同时间窗和同一 symbol 上出现方向背离，至少两个数据窗口确认；缺少一侧 CVD 则为未满足。
- `FAKE_BREAKOUT`：突破后在规定确认窗口内重新回到区间内，且成交/订单流不支持延续；只能生成反向观察，不自动反手。

## 6. Trigger 与 Hard Rule 顺序

```text
Stage2 refreshed
→ evidence chain valid
→ price enters trigger zone
→ re-fetch trigger snapshot
→ macro/news/entry pause check
→ max 3 slots / same-symbol check
→ RR / stop / liquidation safety
→ Risk Engine
→ OrderIntent
→ Executor re-checks idempotency and protection order
```

价格到关键位不是无条件开仓。触发必须有 persistence/confirmation window；任一关键输入变 stale、证据冲突或锁定状态开启，都让链失效。

## 7. 方向、反手、持仓与通知

- LONG、SHORT 对称处理，不对方向设偏好。
- 已有 LONG 产生强 SHORT 时先关闭 LONG，进入 `COOLDOWN`，重新 Stage 2、建立新链、等待新 trigger，禁止立即反手。
- 已有 3 个仓位后，后续信号一律 `REJECT_ENTRY_MAX_POSITION`；已完成触发优先，但不抢占已有 slot。
- 只通知 `OPEN`、`ADD`、`REDUCE`、`TAKE_PROFIT`、`STOP_LOSS`、`CLOSE`；普通 Stage 1/2 变化只在 Dashboard 保存。

## 8. 可复现要求

每个 `screening_result` 和 `evidence_chain` 保存规则版本、指标参数、输入 observation ids、计算时间和缺失字段。未来回测必须使用同一规则版本，禁止用当时不可见的未来数据。历史分析以事件时间和数据披露时间为界，不用重启后的最新修订值替代当时快照。
