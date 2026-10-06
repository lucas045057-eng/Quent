# Risk Engine：20% 计划最大风险模型

## 1. 风险定义

用户指定：`RISK_PER_TRADE=20%`。含义是每一笔新交易在开仓时按账户实时权益计算计划最大风险；它不是极端行情下的绝对损失保证。

```text
equity_at_entry = executor/Bitget 在开仓前确认的账户权益
risk_pct        = 0.20
risk_budget     = equity_at_entry × risk_pct
```

创建交易记录时必须锁定 `equity_at_entry`、`risk_pct`、`risk_budget`。之后账户权益变化不能改变该笔交易的原始风险预算。用户明确接受最多 3 笔各自 20% 的风险，不设置 portfolio risk cap、daily loss limit 或 consecutive loss stop；Dashboard 必须持续显示该风险暴露。

## 2. 输入与前置检查

Risk Engine 只接受结构化、带来源和 freshness 的输入：

- `side`、`entry_price`、`structural_stop`、`target_price`；
- Bitget contract config：数量步长、价格步长、最小数量、乘数、手续费档；
- 估计 entry/exit fee、entry/exit slippage；
- 可用保证金、isolated margin 模式、交易所最大可开数量；
- Bitget 计算的强平价或可靠强平价接口结果；
- 已有仓位槽位、同一 symbol 状态、宏观/新闻/全局 entry gate；
- 最新 Stage 2/Evidence Chain snapshot。

缺少 entry、stop、contract config、强平安全检查或 freshness 时直接 `REJECT_RISK_INPUT_NOT_AVAILABLE`，不猜默认值。

## 3. 仓位计算

先计算结构性止损距离：

```text
stop_distance_pct = abs(entry_price - structural_stop) / entry_price
```

对线性 USDT 合约，使用合约实际乘数和交易所数量单位计算每单位到止损的损失。概念公式为：

```text
stop_loss_cost = abs(entry_price - stop_price) × quantity × contract_multiplier
entry_fee      = entry_notional × entry_fee_rate
exit_fee       = stop_notional × exit_fee_rate
entry_slip     = entry_notional × expected_entry_slippage
exit_slip      = stop_notional × expected_exit_slippage

planned_loss = stop_loss_cost + entry_fee + exit_fee + entry_slip + exit_slip
planned_loss <= risk_budget
```

求解最大 quantity 后，向下取整到 exchange `sizeMultiplier`；不能向上取整突破预算。默认采用保守的 taker fee 和压力滑点，实际成交后保存实际 fee/slippage；实际损失可能因跳空、流动性、网络或交易所风险超过计划值。

## 4. 预估 R:R

```text
one_R = abs(entry_price - structural_stop)
expected_reward = abs(target_price - entry_price)
expected_rr = expected_reward / one_R
```

开仓前必须 `expected_rr >= MIN_RR`，默认 `MIN_RR=1.5`。目标价必须来自结构、有效的下一个流动性区或明确 trailing 规则；不能用任意远目标制造 R:R。手续费和滑点加入审查说明，不降低 Hard Rule。

## 5. 杠杆与保证金

系统不先固定 10x 或其他杠杆。先根据风险预算和 stop distance 求可执行 notional，再由保证金约束确定杠杆：

```text
required_initial_margin = position_notional / candidate_leverage
```

算法从交易所允许的最低必要杠杆/保证金方案开始，优先降低杠杆、提高 isolated margin，并满足：

1. `required_initial_margin <= available_isolated_margin`；
2. 实际 quantity、tick/step 和交易所 max-open 通过；
3. 强平价处于结构止损之后；
4. 不能通过移动结构止损来勉强满足强平条件；
5. 向交易所设置的最终杠杆和 margin mode 与数据库记录一致。

如果交易所接口对强平价、最大可开数量或档位返回 `NOT_AVAILABLE`，拒绝新仓，不以估计值代替。杠杆是执行约束，不是信号强度倍增器。

## 6. 强平安全

对多单，价格向下运动，因此必须满足：

```text
liquidation_price < structural_stop - safety_buffer
```

对空单，价格向上运动，因此必须满足：

```text
liquidation_price > structural_stop + safety_buffer
```

安全 buffer 由 mark/index 波动、接口误差和最小 tick 配置决定。用户给出的 `entry=100、long stop=96、liquidation=97` 必须拒绝，因为 97 会先于 96 触发。强平价如果与 stop 顺序不确定，也拒绝。

## 7. 加仓

每笔交易 `MAX_ADD=1`。加仓前必须重新 Stage 2 和 Risk Engine，不得使用旧信号：

```text
total_trade_planned_loss_after_add <= original_risk_budget
```

实现上保存一条 `trade_plan`，包括首仓、加仓、当前 stop 和每个订单的风险贡献。加仓可以缩短整体风险距离或调整止损，但不得放宽原始结构逻辑来制造容量。加仓失败不影响首仓保护单。

## 8. 止损、止盈与保护

开仓成功后必须立即提交 Bitget server-side stop；只依赖本地 Python 监控是失败条件。建议将 stop 和必要的 TP 作为 reduce-only/position-protection 订单，并保存 exchange order id 与 clientOrderId。

若实盘开仓成交但保护单提交失败：

1. Executor 立即禁止新的 entry；
2. 按 clientOrderId 查询保护单是否已存在；
3. 若确认不存在，提交一次幂等的 reduce-only close/market close；
4. 标记 `UNPROTECTED_POSITION`，通知管理员并进入故障门。

默认分批止盈：

```text
+1R → 平 25%
+2R → 再平 50%
余下 25% → trailing stop
```

所有参数可配置；每次减仓后重新计算剩余 quantity、stop、已实现 PnL 和 R Multiple，但不改变原始 risk_budget 记录。

## 9. 全局限制

- `MAX_OPEN_POSITIONS=3`，包括 LONG 和 SHORT。
- 同一 symbol 只有一个独立仓位，不能重复开仓。
- 已有 3 仓时返回 `REJECT_ENTRY_MAX_POSITION`。
- Top 200 变化不平仓；现有仓位只按 exit/management 规则运行。
- 不设置自动回撤熔断、每日亏损停止或连续亏损停止，这是用户明确选择，但不是安全建议。
- 只有交易所最小下单要求无法满足或用户手动停止时进入 `SYSTEM_STOPPED`；系统停止后已有仓位保护仍必须保留。

## 10. Paper 与 Live 一致性

Paper executor 接收与 Live 相同的 `OrderIntent`、RiskPlan、protection plan 和 state transition，只替换成交和费用实现。Paper 必须模拟：部分成交、滑点、fee、funding、stop、TP、断线恢复、重复请求和最小数量拒绝。不得用“信号一出现就按收盘价全成”的另一套简化策略。

## 11. 必测矩阵

- equity=100/80 时 risk_budget 分别为 20/16，且建仓后锁定；
- stop distance 小、交易所最小数量导致风险预算不足；
- 向下取整数量后 risk 不超预算；
- long/short 强平价顺序正确；
- `expected_rr < 1.5` 拒绝；
- 3 个仓位后第 4 个被拒；
- 同一 symbol 第二次开仓被拒；
- 加仓后总风险超过原始预算被拒；
- 保护单超时重试不产生重复订单；
- AI、Collector 或数据库失败不删除已有保护。
