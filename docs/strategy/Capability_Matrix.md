# Quant Paper V1 Capability Matrix

34项原Prompt能力保留追踪；状态仅针对Owner确认的V1 subset。IMPLEMENTED不证明现实source当前健康。未经最终RC，runtime_status统一PENDING_FINAL_RC_VALIDATION。

| ID | Capability | Final V1 status | Boundary | Owner refs |
|---|---|---|---|---|
| CM01 | USDT universe/A–D/Top5 | IMPLEMENTED_V1_SUBSET_TESTED | BTC/ETH执行scope guard；Top5/全universe融合只研究 | OD01,OD02 |
| CM02 | latest/1H/4H/24H价格及高低 | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 当前price可采；return/highlow需规范连续历史和时点；未查活数据 | OD04,OD07 |
| CM03 | 1m/5m/15m/30m/1H/4H/12H/1D结构 | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | Stage1仅5m/15m/1H/4H；其它不可因有Kline接口宣称策略已支持 | OD04,OD23 |
| CM04 | support/resistance/break/retest/false break | IMPLEMENTED_V1_SUBSET_TESTED | 15m冻结突破位、独立retest、timeout/invalidation与已确认pivots；其它形态deferred | OD05 |
| CM05 | EMA/MA/VWAP/AVWAP/RSI/MACD/StochRSI/BB/ADX | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 仅EMA9/21与SMA TR ATR/range；其它指标无本路径证据 | OD06,OD23 |
| CM06 | ATR/HV/RV与可运行空间 | IMPLEMENTED_V1_SUBSET_TESTED | SMA ATR及cost-inclusive净R/target/stop；其它HV/RV研究 | OD06 |
| CM07 | spot/perp volume、volume变化/spike | IMPLEMENTED_V1_SUBSET_TESTED | 20bar quote-turnover基线与1.25 trigger ratio；其它volume研究 | OD06,OD08 |
| CM08 | perp taker/delta/CVD/大额成交 | IMPLEMENTED_V1_SUBSET_TESTED | 2个15m perp delta ratio +同session CVD增量；source coverage/session缺证明仍UNKNOWN | OD08 |
| CM09 | Spot CVD与spot/perp divergence | IMPLEMENTED_V1_SUBSET_TESTED | BTC/ETH spot delta/CVD确认；UNKNOWN source coverage/freshness不升级；divergence研究deferred | OD07,OD08 |
| CM10 | OI USD/base/change 5m至24H | IMPLEMENTED_V1_SUBSET_TESTED | 同venue验证base OI15m增速与typed numeric GTE；其它OI窗口只研究 | OD09 |
| CM11 | 六场所OI/Funding覆盖 | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | OI/Funding为Bybit/Bitget/HL；Binance spot支持不代表Binance perp，OKX/MEXC不在该adapter集合 | OD07 |
| CM12 | Funding加权/预测/历史/结算/Basis | IMPLEMENTED_V1_SUBSET_TESTED | current normalized8H方向阈值、原始interval和各venue freshness；其它funding/basis研究 | OD10 |
| CM13 | global/top trader账户/position多空比 | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | ACCOUNT_HOLDER_RATIO类型存在；不是真实position金额，top trader/whale不能冒充 | OD12,OD23 |
| CM14 | 已发生liquidation 1H/4H/12H/24H | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 带granularity/coverage；不是完整市场总量；仅在对应窗口/coverage可证明时报告 | OD12 |
| CM15 | 未来heatmap/cluster/map/level | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 实际清算流水不能替代；若硬门槛就阻断 | OD12 |
| CM16 | BBO/Spread/Slippage | IMPLEMENTED_V1_SUBSET_TESTED | 真实BBO<=8bps、slippage<=10bps、max-chase与成本/风险守卫 | OD15 |
| CM17 | ±0.5/1/2%depth/wall撤单/spoof/iceberg | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 不从BBO/挂单墙推断主动资金；whole book能力缺失 | OD15,OD23 |
| CM18 | BTC/ETH趋势/volume/OI/funding/CVD/liquidation | IMPLEMENTED_V1_SUBSET_TESTED | 非自身BTC/ETH 1H+4H benchmark与EXTREME veto；liquidation等aux | OD11 |
| CM19 | Dominance/TOTAL/TOTAL2/TOTAL3/ETHBTC | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 原文指数不能用样本breadth proxy同名替代；ETHBTC需对齐价格派生 | OD11,OD23 |
| CM20 | 板块/相对BTC/ETH强弱 | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | relative return/taxonomy存在，当前Pattern无对应semantic确认 | OD11,OD23 |
| CM21 | 24H/7D/30D correlation/Beta | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 相对收益不是相关性或beta；需独立历史窗/计算定义 | OD23 |
| CM22 | news listing/security/监管等24–72H | IMPLEMENTED_V1_SUBSET_TESTED | 明确hack/exploit/delisting hard veto +当前news health；其他新闻辅助 | OD13 |
| CM23 | unlock/供应/FDV/vesting/treasury | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | amount/value/supply/unlock_pct契约存在；完整tokenomics/vesting范围不等于实现 | OD14,OD23 |
| CM24 | 链上deposit/withdrawal/whale/stablecoin | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | BTC/ETH范围、标签/coverage约束；不是用户列的所有VC/各链钱包全集 | OD07,OD23 |
| CM25 | active addresses/TVL/DEX/bridge/staking/burn/mint | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | 未证明全币种全指标能力；逐项无有效数据须报告，不由onchain模块名推断 | OD23 |
| CM26 | macro calendar/DXY/yields/Nasdaq/SP/VIX/Gold | IMPLEMENTED_V1_SUBSET_TESTED | scheduled高影响macro blackout T-60..T+30与双source覆盖；DXY等研究 | OD14 |
| CM27 | BTC/ETH options/IV/skew/GEX/maxpain/block | AUX_RESEARCH_UNCHANGED_V2_DEFERRED | BTC/ETH metrics与单位/字段时间存在；context不是方向；全部细项/实时配置未核验 | OD07,OD23 |
| CM28 | 证据链/多空patterns/非投票 | IMPLEMENTED_V1_SUBSET_TESTED | 两方向Conservative profile typed predicates及可追踪bundle；其它patterns disabled | OD03,OD04,OD08 |
| CM29 | confidence/hard veto/optional degradation | IMPLEMENTED_V1_SUBSET_TESTED | HIGH only、core/aux scoping、hard veto consumer；aux不能补核心 | OD07,OD22 |
| CM30 | three horizons/reports/scenarios/no trade | IMPLEMENTED_V1_SUBSET_TESTED | 1–3H执行、3–8H/8–24H只研究；其它报告未实现 | OD03,OD23 |
| CM31 | 刷新/复核/TTL/idempotency | IMPLEMENTED_V1_SUBSET_TESTED | 600/30/60s、current event双复查、digest/journal幂等；保留既有更短TTL | OD20 |
| CM32 | entry/stop/sizing | IMPLEMENTED_V1_SUBSET_TESTED | snapshot-bound动态stop/target、成本预算、方向key alias、equity sizing | OD15,OD16,OD19 |
| CM33 | TP/time/signal exit/cooldown/multi-trade | IMPLEMENTED_V1_SUBSET_TESTED | native stop/target/maxhold/emergency、部分fill残余保护与replay；cooldown已有检查；单intent/account复用边界保留 | OD17,OD18,OD21 |
| CM34 | 真实性/优先级/独立数据 | IMPLEMENTED_V1_SUBSET_TESTED | 现有真实来源与clock保留、核心fail-closed、aux/JEV无订单权限；不新增研究覆盖 | OD07,OD24 |

8项REQUIRED的代码/测试位置见Implementation_Delta.md。两份原附件全部40章节的去向仍见Prompt_Traceability.json。Source_Manifest.json的源码定位是原始BASE的审计锚点，当前实现以最终HEAD和Implementation_Delta为准。
