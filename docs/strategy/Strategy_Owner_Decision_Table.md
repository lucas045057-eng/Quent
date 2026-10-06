# Strategy Owner Decision Table

Authority: latest explicit Strategy Owner policy decision. Profile: QUANT_PAPER_V1_CONSERVATIVE.

22项DECIDED，OD02/OD23为DEFERRED_RESEARCH。DECIDED只说明策略Owner选择已确定；不等于部署approval、source health证明或盈利验证。

| OD | Owner selection | Status |
|---|---|---|
| OD01 交易与研究 universe | Execution universe=BTCUSDT/ETHUSDT；线性永续与native symbol/venue binding沿用现有显式配置；不扩展研究universe或默认交易所。 | DECIDED |
| OD02 候选截断与融合分组 | 候选截断、融合排名与near-trigger名单仅研究；V1无需Top5，保持DEFERRED_RESEARCH。 | DEFERRED_RESEARCH |
| OD03 规则与三个 horizon | BREAKOUT_CONFIRMATION ×15m ×LONG/SHORT；执行horizon=1–3H；3–8H/8–24H仅研究；TREND_CONTINUATION/LIQUIDATION_REVERSAL disabled。 | DECIDED |
| OD04 结构定义与lookback | swing_left=2，swing_right=2，swing_lookback_1h_bars=24；严格比较，只用已闭合右侧确认bars。 | DECIDED |
| OD05 关键位、突破与复测 | breakout_lookback_bars=20；breakout_buffer_atr=0.15；retest_band_atr=0.20；reclaim_buffer_atr=0.05；invalidation_buffer_atr=0.20；retest_timeout_bars=4；max_chase_atr=0.50。 | DECIDED |
| OD06 volume、ATR与空间 | volume_lookback_bars=20，volume_ratio_min=1.25；沿用SMA(TR,14)、quote turnover；净空间含fees/funding/slippage，min_net_R=1.5。其它指标不进入V1。 | DECIDED |
| OD07 必需source与覆盖 | 7类core：breakout/perp flow/spot flow/base OI/current funding/benchmark+volatility/event risk；UNKNOWN/STALE/PARTIAL/CONFLICT NO TRADE；aux缺失保留报告；source session/完整覆盖必须有显式证明。 | DECIDED |
| OD08 CVD与flow语义 | flow_window=15m，flow_confirmation_windows=2，delta_ratio_min=0.05；同venue真实session、CVD增量与delta一致；unknown aggressor/gap/reset/conflict阻断。 | DECIDED |
| OD09 OI确认 | oi_window=15m，oi_base_change_min=0.0025；同venue验证base单位的精确端点；不插值，不以USD涨幅替代。 | DECIDED |
| OD10 Funding与Basis | normalized current funding8H：LONG<=0.0005，SHORT>=-0.0005；原始interval必须验证；每个当前venue都需fresh；predicted/settled/Basis仅研究。 | DECIDED |
| OD11 BTC/ETH冲突与强弱豁免 | 非自身benchmark1H与4H同时反向VETO；EXTREME volatility VETO；relative-strength override=false。 | DECIDED |
| OD12 清算与squeeze/reversal范围 | LIQUIDATION_REVERSAL disabled for execution；清算/squeeze/heatmap为aux/research，不能产生V1订单。 | DECIDED |
| OD13 消息与事件风控 | 明确exploit/hack/delisting禁止新开仓；calendar/security健康各自有完整覆盖与时钟；AI不能判定无事件。 | DECIDED |
| OD14 Unlock与宏观事件窗口 | 高影响scheduled macro inclusive [T-60min,T+30min]禁止新开仓；unlock/supply等未覆盖研究能力deferred。 | DECIDED |
| OD15 Entry与流动性 | confirmed retest后MARKET/IOC；max_spread_bps=8，max_slippage_bps=10；超过max_chase_atr不追。 | DECIDED |
| OD16 stop生成与合法性 | stop=retest/swing更保守anchor+反向0.25ATR buffer；合法tick与方向；LONG/SHORT和BULLISH/BEARISH键归一化，冲突alias拒绝。 | DECIDED |
| OD17 target与时间退出 | target=nearest confirmed1H pivot；net_R<1.5 NO TRADE；protective stop/target/max_hold10800s/emergency safety全量退出；partialTP/trailing=false。 | DECIDED |
| OD18 失效及异常持仓处理 | setup invalidation/超时阻断新入场；既有position由native reduce-only退出并保护残余；intent过期/approval不可用不关闭恢复退出路径。 | DECIDED |
| OD19 风险预算与cost buffer | risk_per_trade=paper equity0.25%；max_position_notional=equity10%；max_leverage=1x；显式fee与max-hold funding上界；不放宽既有RiskPolicy。 | DECIDED |
| OD20 TTL与再验证 | decisionTTL=600s，intentTTL=30s，revalidation=60s；已有更短Stage1/source TTL继续收紧；未配置不自动补默认。 | DECIDED |
| OD21 多规则、重入与账户复用 | max_simultaneous_positions=1；cooldown=实际flat后1800s；同setup不可重入；pyramiding/average down=false。现有LocalPaper单intent/isolated-account限制保留，未实现跨trade账户复用。 | DECIDED |
| OD22 confidence与Jev | HIGH only ExecutionIntent；AI/JEV只语义辅助/conflict review，不能决定方向、仓位或订单，不能补核心UNKNOWN或覆盖veto。 | DECIDED |
| OD23 可选研究覆盖与近触发名单 | 可选研究覆盖、近触发报告与其它timeframes不属8项REQUIRED；DEFERRED_RESEARCH；本轮不实现V2。 | DEFERRED_RESEARCH |
| OD24 策略实例与审批边界 | profile=QUANT_PAPER_V1_CONSERVATIVE；这是新Owner policy且未盈利验证；现有approval/commit binding继续；本轮不生成production approval，停在最终RC之前。 | DECIDED |
