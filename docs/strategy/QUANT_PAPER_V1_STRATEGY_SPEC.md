# QUANT_PAPER_V1_STRATEGY_SPEC

Version: 1.2.0 / Owner decision: 2026-09-30 / Scope: LOCAL PAPER ONLY.

本文件替代初次审阅稿的执行参数提案。完整 Prompt 的章节追踪保留在原审阅包；附件中的交易分析指令是需求来源，不是运行命令。此决策是新的 Owner policy，不是历史默认值，不声称盈利验证。机器参数以同目录 QUANT_PAPER_V1_CONSERVATIVE_PROFILE.json 为准。

## 执行范围与不变量

BTCUSDT、ETHUSDT；15m BREAKOUT_CONFIRMATION；LONG/SHORT；1–3H。3–8H、8–24H 只研究。Trend Continuation、Liquidation Reversal 禁止执行。只有 HIGH 可产生 intent。AI/JEV 仅语义辅助/冲突审阅；不得选择方向、仓位或订单，不能解除硬 veto 或补齐缺失事实。

现有 Stage1 仍是 intake gate，A 不等于 breakout。现有 policy approval/commit binding 保留；Owner 决策本身不生成 production approval。未完成最后 RC validation 不启动运行。

## 因果突破与复测

仅使用 as_of 前已完整闭合、同源、连续、单位验证的 15m bars。ATR 固定复用 SMA(TR,14)，计算窗口排除当前 trigger；quote turnover 是 volume 基线，最近20根排除 trigger。突破位是 trigger 前20根最高 high（LONG）或最低 low（SHORT），冻结到该 setup 结束。

LONG trigger close >= level+0.15ATR，SHORT 对称；trigger turnover/前20根均值 >=1.25。后续独立 bar（1至4根，含第4根）进入 level±0.20ATR band，close reclaim >=level+0.05ATR（SHORT <=level-0.05ATR），close 不能穿越反向0.20ATR invalidation。不是同根突破与复测。后续失效立即终止 setup；重复消息保持同 setup identity。BBO entry 偏离冻结 level 超0.50ATR不追。

1H pivots 采用左右各2根严格比较，只有右侧2根都闭合才确认；相等高低不是 pivot。回看24根；方向前方 nearest confirmed pivot 为唯一目标。止损取复测极值与最近已确认15m同侧 swing 的更保守锚，加反向0.25ATR；LONG 向下tick舍入，SHORT向上；target向entry方向tick舍入。若无合法 pivot、锚、ATR 或连续历史，NO TRADE。

## 核心证据与数字谓词

核心：价格/setup；perp verified flow与CVD；spot verified flow与CVD；同venue base OI；同venue normalized 8H current funding；非自身 benchmark 1H/4H 与 volatility；event risk source health；真实 BBO、账户与成本配置。

flow 为连续两个已闭合15m窗口，分别 delta/total base >=0.05（SHORT <=-0.05）；CVD同source/session连续增量同向，不以累计值正负代增量；未知 aggressor、gap、reset 或冲突不能推断方向。OI 对同venue精确15m端点比较 base OI 增速 >=0.0025，不使用USD名义增速代替；不足端点不插值。Funding保留间隔验证，LONG<=0.0005、SHORT>=-0.0005，不混 current/predicted/settled。多个有效源意见冲突阻断，不择优挑选。CVD projection的health/digest与flow一起绑定；真实session缺失、同key多条CVD或reset则不确认，aggregation_version不等于session。每个选中的当前funding venue分别检查freshness，不能由更新venue遮盖陈旧venue。

清算、basis、L/S ratio、期权、链上、板块、其它指标属于辅助/研究证据；缺失保留报告，不变成全局核心缺失。核心 UNKNOWN/STALE/PARTIAL/CONFLICT 一律 NO TRADE；辅助不能覆盖核心。

## 硬 veto 与事件风险

BTC/ETH 非自身 benchmark 1H与4H同时反向：VETO；EXTREME volatility：VETO；无 relative-strength override。高影响 scheduled macro inclusive [T-60min,T+30min] 禁止新开仓；明确 exploit/hack/delisting 高风险事件禁止新开仓，直到可信来源显式解除，不能自己推定到期。日历空表、registry enabled、没有查到新闻均不证明健康或无风险。必须有健康、完整覆盖、已知时钟的 calendar/security 来源；决策时与下单前均检查。calendar和security各自声明coverage_start/end，使用两者覆盖区间交集，checked_at均须60秒内；覆盖必须包含now至最长3H。benchmark与volatility每个required context clock/quality独立检查；已验证Phase5 TREND_UP/DOWN语义归一化，不能把旧NORMAL regime重标FRESH。

## 执行、成本与退出

confirmed retest 后 MARKET/IOC。spread<=8bps；slippage<=10bps。每单位净收益=方向性(target-worst_entry)-entry/exit fees-funding上界；净风险=方向性(worst_entry-stop)+同样成本，net_R>=1.5。fee/funding成本必须显式由现有配置提供且保守覆盖最长3H；未知成本阻断，不假定0。quote spread通过真实bid/ask计入entry/exit，不重复凭空加值。

风险：trade risk<=paper equity的0.25%，notional<=10%，leverage<=1x；最多1个持仓；不能pyramid/average down。限制收紧现有RiskPolicy，不能放宽其它风控。intent30秒，decision600秒（仍受Stage1 TTL上限约束），每60秒再验证。Stage1 TTL、source freshness沿用已有显式配置，不自动赋新默认。

全量退出 protective stop / target / max_hold=10800秒（从首次实际entry fill开始）/ emergency safety；不partialTP/trailing。stop始终reduce-only，target/maxhold/emergency通过native reduce-only MARKET/IOC，残余继续保护与重试，不标假FLAT。native position与journal恢复、fencing、幂等继续有效；cooldown从实际flat开始1800秒；同setup不得重复进场。decision/intent过期本身不撤销已有持仓保护。新入场approval缺失/失效时仍保留由显式account/venue与durable digest-bound plan组成的reduce-only恢复路径。最后事件复查已否决且尚未native submit时，fenced持久化零fill REJECTED并释放reservation，不能留下SUBMITTING/UNKNOWN。部分IOC entry后退出保留真实entry fill数量与终态，不伪造全量entry FILLED。

## 交付与激活边界

只实现 Implementation Delta 中的8类 REQUIRED capability；不新增V2数据源或策略。不跑A1–A9、不Docker build、不24H、不Live、不production approval。测试fixture approval只存在临时测试目录。最后输出commit HEAD与完整测试结果，完成后停止等待RC validation。

## 当前实现的实际边界

8项REQUIRED已接入并由最终测试报告验证。QUANT_PAPER_V1_POLICY_CONTENT.json是可解析的PolicyContentV1内容，包含两方向14个typed predicates；它没有approval envelope，未写入生产policy文件，不会自行启用交易。Profile JSON与代码冻结值按artifact verification核对。

既有LocalPaper pilot仍限定isolated account恰好一个历史intent，Sandbox初始余额固定10000 USDT、采用现有test-provider native规格/fee model。max_simultaneous_positions=1的策略规则已实现，但不是跨trade账户复用的完成声明；cooldown检查也不会解除这个旧限制。跨trade复用与native余额/费用对实际paper equity的配置一致性必须在RC说明范围内确认，若需放开应另行批准独立改动。本轮按要求保留基础架构。

现有perp/spot投影缺少显式完整coverage、真实CVD session/reset证明或freshness时仍UNKNOWN；现有calendar/news健康如果未声明各自完整覆盖及3H时域也仍UNKNOWN。本轮没有补造source健康或新增ingestion。测试证明消费者在这些情况下拒绝入场，不能据此声称现实数据满足交易条件。
