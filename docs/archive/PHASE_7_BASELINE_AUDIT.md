# PHASE 7 BASELINE AUDIT

**Audit date:** 2026-09-22 UTC

**Worktree:** `/home/lucas045057/projects/quant` (WSL2 Local only)

**Branch created for design:** `phase7`

**Phase 7 base:** `18797b7b6abd890ad50abbb614990388cdf3e0ce`

**Audit status:** `PHASE7_BASELINE_AUDIT_COMPLETE`

## 1. Boundary and historical checkpoints

This audit is design-only. It does not create Migration 012, connect to a
blockchain or spot provider, change Docker, access ECS, or modify Phase 6.

The verified starting point is the clean Phase 6 report commit. The following
historical debts remain visible and are not reopened:

| Debt | Treatment in Phase 7 |
|---|---|
| `PHASE4_EXTERNAL_SOURCE_GATE_PENDING` / Bybit `EXCHANGE_ACCESS_LIMITATION` | Preserve. Do not change the Bybit adapter, use a proxy/VPN, or fabricate a pass. |
| Phase 1 ticker/snapshot WebSocket reconnect limitation | Preserve unless Phase 7 is proven to depend on it. No opportunistic refactor. |
| `PHASE6_AI_PROVIDER_CREDENTIAL_REQUIRED` | Preserve. Phase 7 is deterministic and does not require an AI provider. |

The Phase 6 code-phase and local runtime report are evidence about the existing
baseline only. Phase 7 does not claim Phase 6 live AI acceptance.

## 2. Existing topology and ownership

The repository already uses the three-container local boundary:

```text
public exchange market sources / existing collector
        -> canonical observations and closed bars
        -> quant-engine deterministic context cycles
        -> PostgreSQL
```

Existing runtime containers are `quant-collector`, `quant-engine`, and
`quant-postgres`. The current resource ceilings are PostgreSQL 768 MiB,
collector 256 MiB, and engine 384 MiB. Phase 7 must reuse these services. A
new `onchain-daemon`, `spot-daemon`, broker, cache, or queue service is not
approved by this audit.

Ownership boundary:

| Component | Existing responsibility | Phase 7 reuse/extension |
|---|---|---|
| Collector | Public REST/WS acquisition, schema parsing, canonicalization, bounded queues and reconnect evidence | Add bounded chain-head/block polling and spot public trade ingestion only after design review. |
| Engine | Deterministic aggregation, context calculation, persistence and Stage1 enrichment hooks | Add Phase 7 context calculation in an isolated, additive hook. |
| PostgreSQL | Canonical state, derived context, idempotency, timestamps, health and migration history | Additive Migration 012 only; no rewrite of 001–011. |

## 3. Existing exchange adapter audit

### 3.1 Bitget

`src/quant_phase3/adapters/bitget.py` implements a public Bitget UTA v3
derivatives trade adapter. It deliberately preserves the exchange `side` as
evidence and sets canonical aggressor direction to `UNKNOWN`. This behavior is
correct for Phase 7 reuse: it cannot be copied into a spot adapter and treated
as directional without a separate spot semantic contract.

`src/quant_phase4/adapters/bitget_uta_v3.py` and
`src/quant_phase4/adapters/bitget_classic_v2.py` are version-isolated
derivatives adapters. Phase 7 must not mix their subscription schemas with a
spot UTA v3 adapter. A future Bitget spot adapter must use its own adapter
class, endpoint declaration, parser fixture, and semantic test.

### 3.2 Bybit

`src/quant_phase3/adapters/bybit.py` and
`src/quant_phase4/adapters/bybit_v5.py` preserve the existing V5 public
contracts, but the external network gate is still blocked. Phase 7 does not
depend on Bybit and must not use it as a hidden fallback for on-chain or spot
data.

### 3.3 Hyperliquid

The existing Hyperliquid public adapter is a derivatives/public market source.
It is not an on-chain event source and cannot be used to infer exchange wallet
flows or stablecoin transfers. It remains outside Phase 7 V1 source scope.

### 3.4 Reusable adapter abstractions

The Phase 3 `PublicTradeAdapter`, bounded deduplicator, event-time window
router, gap event, recovery manager, health registry, and bounded queue are
usable design patterns. Phase 7 must generalize them without changing Phase
3 semantics or pretending that perp trade side and spot trade side have the
same meaning.

## 4. Phase 3 trade-flow audit

The existing flow layer provides:

- `CanonicalTrade` with exchange symbol, price, base quantity, exchange time,
  received time, processed time, raw-side evidence and canonical side;
- event-time windows with allowed lateness;
- explicit `UNKNOWN` side volume;
- `PARTIAL` evidence for gaps or incomplete coverage;
- bounded queues and bounded deduplication;
- cross-exchange aggregation only after capability checks.

Phase 7 can reuse the windowing, deduplication, backpressure, and persistence
patterns. It must use a separate `spot_flow_windows` contract and table. A
spot window is never inserted into `trade_flow_windows`, and a perpetual
window is never inserted into the spot table.

## 5. Phase 4 derivative-context audit

Phase 4 has separate contracts for liquidation, long/short, basis and
cross-exchange derivative context. It preserves unit, source granularity,
coverage semantics, timestamp skew, status, reason, and raw-reference fields.

Phase 7 may reuse these provenance and status patterns, but must not reuse
derivative field names for on-chain transfers. In particular:

- exchange wallet inflow/outflow is not liquidation or open interest;
- spot trade volume is not perpetual trade volume;
- on-chain token amount is not contract quantity;
- public address labels are not exchange account state.

## 6. Phase 5 context-system audit

Phase 5 already establishes the correct context boundary:

- deterministic calculations;
- exact UTC context timestamps and input windows;
- closed-bar-only source inputs;
- `AVAILABLE | PARTIAL | STALE | NOT_AVAILABLE | ERROR` derived status;
- explicit missing/coverage evidence;
- bounded JSON evidence;
- separate context-only Stage1 enrichment;
- no change to Stage1 eligibility or A/B/C/D classification.

Phase 7 adopts this derived-status vocabulary. `UNCONFIRMED` is reserved for
the separate finality field, not added as a general status. Missing data is
never converted to numeric zero.

## 7. Phase 6 architecture audit

Phase 6 has bounded source definitions, parser/normalization contracts,
provenance, content hashes, source registry persistence, failure isolation,
bounded raw references, retention, and an AI gateway. The design is useful for
bounded source policy but Phase 7 must not depend on the Phase 6 AI gateway.

Phase 7 will use a separate source-kind namespace and deterministic parser
contracts. It may reuse the health, retention, bounded-reference, and
idempotency patterns, but it must not place on-chain events into the Phase 6
news/macro/unlock tables.

## 8. Database and migration audit

The migration runner in `src/quant_phase1/db.py` discovers ordered SQL files,
records applied versions in `schema_migrations`, and validates the known
Phase 5/6 schemas. Migrations 001–011 are treated as immutable.

Current durable surfaces relevant to Phase 7:

| Surface | Phase 7 use |
|---|---|
| `symbols`, `exchange_instruments` | Canonical exchange symbols and aliases only; never asset identity by symbol alone. |
| `market_snapshots`, `market_observations` | Historical market-price lookup for timestamp-safe USD valuation. |
| `trade_flow_windows`, `cvd_snapshots` | Perpetual context comparison only; never mixed with spot rows. |
| Phase 4 metric tables | Existing derivative context only; no on-chain writes. |
| Phase 5 context tables | Reference context only; no overwrite. |
| Phase 6 source/event tables | Separate external-event domain; no reuse for chain transfers. |
| `runtime_health_events`, `system_health` | Reuse for Phase 7 source health and lag details. |
| `schema_migrations` | Migration 012 remains additive and idempotent. |

Migration 012 should add only the minimum Phase 7 tables selected by the final
design. It must not alter columns, checks, indexes, or data in 001–011.

## 9. Retention and raw-data audit

Phase 1–6 already use bounded, configuration-driven retention and avoid an
unlimited raw warehouse. Phase 7 follows the same rule:

- no raw blockchain archive;
- no raw spot-trade warehouse;
- only normalized events, bounded provenance references, sanitized fixtures,
  checkpoints, and derived windows are durable;
- raw payloads may be retained only as bounded, short-lived references if a
  source contract requires them.

Phase 7 retention is per data class, not a single global period. Cleanup must
be batched, indexed, restart-safe and reported through health events.

## 10. Config and queue audit

`Settings.from_env` already rejects non-paper mode and private Bitget
credentials. It has Phase 2–6 retention, queue, retry, timeout and resource
controls. Phase 7 configuration must follow the same environment-only
injection pattern and must never read exchange trading credentials.

Existing bounded queue and recovery classes record drops, gaps, retry bounds,
and source coverage degradation. Phase 7 needs chain/source-specific limits;
it must not make queue capacity or backfill unbounded.

## 11. Health and recovery audit

The shared health model currently persists service/component health and
runtime events. Phase 7 health details must include source, chain/exchange,
head lag, checkpoint, rate-limit state, gap count, reorg count, queue depth,
and last successful observation. The existing `RUNNING`/`DEGRADED` health
event states are sufficient; data `status` remains the richer
`AVAILABLE/PARTIAL/STALE/NOT_AVAILABLE/ERROR` contract.

Recovery must be checkpoint-based:

1. read the last committed chain/trade cursor;
2. validate the cursor's block hash or trade sequence;
3. backfill only within a configured bounded window;
4. mark unresolved gaps explicitly;
5. commit normalized rows and the new checkpoint atomically.

## 12. Baseline risks and controls required by Phase 7

| Risk | Required design control |
|---|---|
| EVM logs do not cover all native ETH internal calls | Restrict complete claim to allowlisted ERC-20 logs; direct native transfers are partial unless a trace-capable source is approved. |
| Bitcoin is UTXO-based | Use `txid:vout` identity and output events; do not model BTC as ERC-20 logs or infer one sender when inputs are ambiguous. |
| Exchange labels are incomplete | Versioned label snapshots, coverage counts, `UNKNOWN`, and no flow classification below the configured coverage threshold. |
| Spot side semantics differ by exchange | Binance `m` may support directional side; Bitget spot defaults to unknown direction until independently verified. |
| Bridge source legs double count | V1 bridge aggregation disabled; source/target legs remain separate and marked `NOT_AVAILABLE` for bridge context. |
| Price time leakage | Require non-null `exchange_timestamp <= event_time`; never substitute snapshot/fetched/processed clocks, and otherwise set `amount_usd` to `NOT_AVAILABLE`. |
| Reorgs | Store block hashes and finality; persist `finality_status=REORGED` with `status=STALE` and `reason=REORGED_EVENT`, exclude it from all aggregates, and retain any replacement under a new identity. |
| Public source limits | Per-source limiter, explicit local caps for timeout/retry/backfill/pages/bytes/queue, 429/backoff and no fallback that hides primary failure. |
| Identity and units | Native ETH uses `TX_VALUE`, ERC-20 uses `LOG_INDEX` plus contract, BTC uses `VOUT_INDEX`; raw integer amounts and registry decimals use exact arithmetic with no rounding. |
| Label semantics | Only versioned operator-reviewed labels qualify; `KNOWN_EXTERNAL` is explicit, `UNKNOWN` is never external, and coverage below 0.90 is unavailable. |
| Spot/perp isolation | Spot persistence requires `market_kind=SPOT` and a dedicated repository/table; no write path reaches perpetual flow tables. |
| Resource growth | No daemon, bounded queues, no raw warehouse, conservative retention, indexed cleanup and measured growth gates. |
| Stage1 coupling | Context-only enrichment after the existing deterministic Stage1 result; no eligibility or category mutation. |

## 13. Audit conclusion

The current codebase can support Phase 7 as an additive deterministic data and
context layer using the existing collector, engine, PostgreSQL, queue,
provenance, health and retention patterns. The supported-chain and spot-source
boundaries must remain narrow and conservative. Implementation may not begin
until the accompanying Phase 7 design specification, implementation plan and
independent review are complete.
