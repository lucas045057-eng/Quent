# PHASE 7 DESIGN REVIEW

**Review status:** `PASS`

**Review mode:** Independent read-only review after the design correction pass

**Review scope:**

- `PHASE_7_BASELINE_AUDIT.md`
- `PHASE_7_DESIGN_SPEC.md`
- `docs/superpowers/plans/2026-09-22-phase7-onchain-flow.md`
- Existing Phase 1–6 adapters, migrations, contracts, health, retention and
  runtime boundaries referenced by those documents

**Review boundary:** No Phase 7 implementation, Migration 012 SQL, live source
call, ECS access, remote database access, Docker change, or Phase 8/9 work was
performed.

## Gate result

`PASS`

The independent reviewer confirmed that the prior blockers and warnings are
closed. The specification is design-ready and may be implemented later only
through the reviewed test-first plan and human approval.

## Resolved review items

| Item | Resolution |
|---|---|
| Native ETH identity | Native top-level ETH uses `TX_VALUE` with `(chain,tx_hash,tx_index,NATIVE)`; ERC-20 uses `LOG_INDEX` plus contract; BTC uses `VOUT_INDEX`. Invalid cross-domain combinations are rejected. |
| USD timestamp leakage | A non-null `exchange_timestamp <= event_time` is required; `valuation_fetched_at <= event_time` is persisted and required; snapshot/fetched/processed clocks cannot substitute for market event time. BTC/ETH map explicitly to `BTCUSDT`/`ETHUSDT`. |
| Migration 012 completeness | All nine tables have an explicit logical column/type/nullability/check/index contract, conditional identities, retention/idempotency rules and additive boundaries. No SQL is created in design phase. |
| Exchange flow semantics | Only explicit `KNOWN_EXTERNAL` ↔ `KNOWN_EXCHANGE` transitions classify exchange flow. `UNKNOWN` is never external. |
| Reorg handling | `REORGED` persists as `STALE` with `REORGED_EVENT`, is excluded from every aggregate, and replacement events are separate identities. |
| Stablecoin and bridge scope | Stablecoin has its own domain/table; bridge legs retain `bridge_leg_id` and are ineligible by default, preventing double counting. |
| Source limits/recovery | BTC, Ethereum, Binance Spot and Bitget Spot have bounded endpoint, timeout, retry, backoff, catch-up/backfill, page/event/byte, cursor, queue and worker contracts. Bitget REST remains `PENDING_CONTRACT` with no v2 fallback. |
| Labels | Only operator-reviewed versioned snapshots are approved; `PHASE7_MIN_LABEL_COVERAGE=0.90`; missing/conflicting labels remain unavailable/partial. |
| Amount precision | Raw amounts are decimal digit strings; BTC is 8 decimals; V1 available EVM assets are limited to storage-supported precision; exact decimal arithmetic has no rounding. |
| Spot/perpetual isolation | Spot persistence requires `market_kind='SPOT'`, uses a dedicated repository/table, and has no write path to perpetual flow tables. |
| Existing-phase boundaries | Phase 1–6 tables and semantics are not rewritten; Stage1 enrichment is additive/context-only; Phase 6 AI is independent; Phase 8/9 are out of scope. |

## Remaining explicit limitations

- No default Bitcoin or Ethereum provider is selected.
- Bitget Spot REST schema is not frozen until a later official contract test.
- Bitget Spot direction remains unknown by default.
- Native internal ETH calls, arbitrary tokens, balances and bridge correlation
  remain unavailable.
- No live source availability claim was made in this design phase.

These are documented `NOT_AVAILABLE`/contract-test gates, not hidden mocks or
fallbacks.
