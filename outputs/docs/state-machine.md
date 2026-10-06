# State Machine：系统、币种、仓位与恢复

## 1. 层级

系统有四层状态，不能用一个字符串混合表达：

1. `SystemState`：服务整体是否允许新风险。
2. `SymbolState`：每个 symbol 的唯一策略状态。
3. `PositionState`：已存在仓位的管理状态。
4. `OrderState`：每一笔订单的生命周期和幂等状态。

## 2. SystemState

```text
BOOTING
  → RECOVERY_MODE
  → RUNNING

RUNNING ↔ PAUSED_NEW_ENTRIES
RUNNING/PAUSED_NEW_ENTRIES → SYSTEM_STOPPED
任意状态 → DEGRADED（保留保护，禁止新风险）
DEGRADED → RECOVERY_MODE → RUNNING 或 SYSTEM_STOPPED
```

### 状态含义

- `BOOTING`：加载配置、迁移检查、建立连接；不交易。
- `RECOVERY_MODE`：读取 DB、读取 Bitget positions/orders/balances，执行 reconciliation；未完成不允许 entry。
- `RUNNING`：正常扫描、分析、触发和持仓管理。
- `PAUSED_NEW_ENTRIES`：Dashboard 或故障门主动暂停新开仓；止损、止盈、trailing、平仓仍运行。
- `DEGRADED`：关键 Collector/Engine/Executor/DB/AI 异常；禁止新增风险，已有保护单继续存在。
- `SYSTEM_STOPPED`：用户手动停止或权益不足以满足交易所最小下单要求；不删除已有保护单。

## 3. SymbolState

每个 symbol 必须且只能有一个状态：

```text
REJECT
WATCH
STAGE1_SELECTED
STAGE2_ANALYZING
WAIT_TRIGGER
ENTRY_READY
POSITION_OPEN
POSITION_MANAGEMENT
EXIT
COOLDOWN
```

### 主要转移

```text
REJECT ──Universe eligible──> WATCH
WATCH ──Stage1 A──> STAGE1_SELECTED
STAGE1_SELECTED ──fresh Stage2 start──> STAGE2_ANALYZING
STAGE2_ANALYZING ──chain valid, no trigger──> WAIT_TRIGGER
STAGE2_ANALYZING ──chain invalid──> WATCH 或 REJECT
WAIT_TRIGGER ──trigger zone reached──> ENTRY_READY
ENTRY_READY ──hard rules + risk + slot pass──> POSITION_OPEN
ENTRY_READY ──any gate fails──> WATCH 或 WAIT_TRIGGER
POSITION_OPEN ──entry fill + server stop verified──> POSITION_MANAGEMENT
POSITION_MANAGEMENT ──TP/SL/close──> EXIT
EXIT ──close confirmed──> COOLDOWN
COOLDOWN ──new scan + cooldown elapsed──> WATCH
```

任何旧信号、旧 Stage2 或旧 Evidence Chain 不能从 `COOLDOWN` 直接开仓。反手也必须经过 `EXIT → COOLDOWN → WATCH → STAGE2_ANALYZING`。

## 4. Symbol 级不变量

- 同一 symbol 不允许两个独立 position aggregate；如果交易所返回重复或方向冲突，进入 `UNKNOWN_POSITION`，禁止该币新仓。
- `POSITION_OPEN` 必须有交易所确认的 fill 或 paper fill，以及已验证的 stop plan。
- `WAIT_TRIGGER`/`ENTRY_READY` 的关键 observation 必须在 freshness SLA 内。
- `STAGE1_SELECTED` 不代表可交易；只有 `ENTRY_READY` 经过 Risk Engine 才能产生 OrderIntent。
- Top 200 淘汰不触发 `EXIT`。
- 宏观锁/新闻锁只关闭 entry gate，不自动平已有仓位。

## 5. PositionState

```text
NONE
OPENING
OPEN
ADDING
REDUCING
PROTECTED
EXITING
CLOSED
UNKNOWN_POSITION
UNPROTECTED_POSITION
```

`OPEN` 到 `PROTECTED` 的过渡要求已确认 server-side stop；`UNPROTECTED_POSITION` 必须触发 reduce-only close 尝试和管理员通知。`UNKNOWN_POSITION` 只能通过 Bitget order/trade history + 人工或明确 reconciliation 规则解决。

## 6. OrderState

```text
INTENT_CREATED
VALIDATING
SUBMITTING
ACK_UNKNOWN
OPEN
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELED
REJECTED
FAILED
```

`ACK_UNKNOWN` 绝不能直接重新发开仓请求。Executor 先用 `clientOrderId` 查询订单详情、成交和仓位；只有确认不存在且策略仍有效时才允许一次新的 intent，且使用新的可追踪 attempt id。

## 7. Event 与原子性

所有转移由持久化事件驱动：

```text
STATE_TRANSITION_REQUESTED
STATE_TRANSITION_APPLIED
ORDER_INTENT_CREATED
ORDER_ACK_UNKNOWN
FILL_RECEIVED
PROTECTION_VERIFIED
RECONCILIATION_REQUIRED
ENTRY_GATE_CHANGED
```

写入 state、outbox event、reason、rule_version、observation ids 必须在同一数据库事务中完成。通知发送用 outbox，避免“已成交但通知丢失”或“通知已发但 DB 未落盘”。

## 8. Recovery 流程

```text
服务启动
→ 读取配置与 schema version
→ 读取 DB positions/orders
→ 读取 Bitget balance/positions/open orders/order history
→ 以 Bitget 为事实源比较
→ 补齐 fills、fees、funding、保护单
→ 处理差异
→ 标记未解决差异
→ 健康门通过后 RUNNING
```

差异规则：

- DB 和 Bitget 同 symbol/side/size：恢复 `POSITION_MANAGEMENT`。
- DB 有仓、Bitget 无仓：查询 order/trade history，修正 DB；不能凭猜测保留仓位。
- DB 无仓、Bitget 有仓：`UNKNOWN_POSITION`，禁止该币新交易并通知管理员。
- Bitget 有仓但保护单缺失：`UNPROTECTED_POSITION`，优先补保护或 reduce-only close。

## 9. Lock 优先级

```text
SYSTEM_STOPPED
  > DEGRADED / RECOVERY_MODE
  > PAUSED_NEW_ENTRIES
  > MACRO_LOCK / NEWS_RISK_LOCK
  > MAX_OPEN_POSITIONS / SAME_SYMBOL
  > DATA_FRESHNESS
  > Evidence / Trigger / Risk
```

锁定只影响新风险门；仓位管理路径必须继续运行。所有锁保存 reason、source、started_at、expires_at 和解除条件。
