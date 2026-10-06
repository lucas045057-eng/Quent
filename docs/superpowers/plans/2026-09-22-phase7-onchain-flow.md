# Phase 7 On-chain and Spot Flow Implementation Plan

**Status:** Design review plan only — do not execute in this phase

**Base:** `phase7` from `18797b7b6abd890ad50abbb614990388cdf3e0ce`

**Guardrail:** Every task is local WSL2 only. No ECS, no remote PostgreSQL, no
real external API calls in the design phase, no Phase 8, no Phase 9, no AI,
no private API, no order/position/live-executor behavior.

## Execution protocol

Each future implementation task follows:

```text
TEST -> FAIL -> MINIMAL IMPLEMENTATION -> PASS -> REVIEW -> FIX -> RETEST -> COMMIT
```

Migration 012 is written only after the contract tests select the final
columns. Existing migrations 001–011 are immutable. Live external tests are
separate and opt-in after deterministic tests pass.

## Task 1 — Canonical asset and chain contracts

- Add immutable contracts for chain, asset identity, amount units, finality,
  provenance, status and coverage.
- Add BTC UTXO output identity, EVM ERC-20 log identity and native ETH
  `TX_VALUE` identity without forcing them into one fake shape.
- Require exact decimal amounts, non-null exchange-time valuation ordering and
  explicit BTCUSDT/ETHUSDT spot mappings.
- Tests: UTC, raw/normalized amounts, decimals, identity and forbidden
  decision-field scan.

## Task 2 — Migration 012 and repository contracts

- Implement only the reviewed additive tables in the spec, including the full
  logical DDL contract, checks, identities, domain fields, raw-reference bounds
  and checkpoint uniqueness described in section 17.1.
- Add fresh database, repeat migration and existing-database compatibility
  tests.
- Add unique identities and query indexes before persistence code.
- Do not modify 001–011 or delete existing data.

## Task 3 — Source registry and bounded transport

- Add explicit source definitions for Bitcoin RPC, Ethereum JSON-RPC, Binance
  Spot and Bitget UTA v3 Spot.
- Add per-source timeout, retry, 429/backoff, max bytes, max pages and
  concurrency controls.
- Enforce the fixed local catch-up/backfill, queue and worker caps; Bitget Spot
  REST remains disabled until its official contract test passes.
- Add sanitized fixtures only; no runtime network call in deterministic tests.

## Task 4 — Bitcoin normalization

- Parse block headers, transaction inputs/outputs, satoshis and confirmations.
- Produce one output event per `txid:vout_index` with nullable sender set.
- Reject non-main-chain/reorged rows as available evidence.
- Tests: coinbase, multiple inputs/outputs, change output, reorg and invalid
  payload fixtures.

## Task 5 — Ethereum normalization and finality

- Parse chain ID, blocks, receipts and allowlisted ERC-20 Transfer logs.
- Resolve decimals from the versioned asset registry, not symbol text.
- Keep native top-level ETH `TX_VALUE` and ERC-20 logs distinct; reject fake
  log indexes/contracts for native ETH.
- Tests: indexed addresses/topics, log index identity, finalized/safe support,
  missing trace coverage and block-hash replacement.

## Task 6 — Checkpoints, gap detection and recovery

- Add atomic checkpoint persistence and bounded catch-up.
- Validate parent/block hashes and source cursors on restart.
- Emit runtime health details for gaps, lag, 429, reorg and queue pressure.
- Tests: restart from checkpoint, invalid checkpoint, bounded backfill and
  unresolved gap degradation.

## Task 7 — Address labels and exchange flow

- Add versioned label snapshots and the conservative taxonomy.
- Classify only covered `KNOWN_EXTERNAL`-to-exchange or reverse flows;
  `UNKNOWN` is never external and the 0.90 coverage threshold is enforced.
- Exclude exchange-internal and exchange-to-exchange movements.
- Tests: coverage thresholds, label version/effective time, UNKNOWN behavior,
  conflict provenance and no wallet guessing.

## Task 8 — Whale context

- Add asset/chain-aware threshold configuration and versioned tiers.
- Aggregate bounded 1m/5m/15m/1H/4H windows with unknown and missing counts.
- Keep missing USD valuation from becoming a zero or a whale negative.
- Exclude stablecoin and bridge-ineligible domains from generic whale totals.
- Tests: threshold selection, timestamp-safe valuation and partial coverage.

## Task 9 — Spot adapters

- Add separate Binance Spot and Bitget UTA v3 Spot adapters.
- Reuse only transport/queue patterns, not derivative schemas.
- Binance maker flag may produce direction; Bitget side remains unknown by
  default.
- Require `market_kind=SPOT` and a dedicated persistence path; reject v2
  subscription schemas.
- Tests: subscription schema, REST response schema, reconnect, trade identity,
  maker-side semantics and v2-schema rejection.

## Task 10 — Spot flow aggregation

- Aggregate event-time windows with bounded late arrival and source-specific
  semantics.
- Persist unknown volume explicitly.
- Compare spot/perp only descriptively and only after unit/timestamp checks.
- Keep spot rows out of perpetual flow tables and enforce domain checks in the
  database.
- Tests: dedup, gap, mixed-source incompatibility, CVD unavailable for Bitget
  and no Stage1 decision mutation.

## Task 11 — Stablecoin context

- Add allowlisted Ethereum USDT/USDC contracts and category rules.
- Separate ordinary transfer, mint, burn, exchange deposit/withdrawal and
  bridge-not-available.
- Preserve bridge leg IDs while excluding both legs from default net and whale
  aggregates to prevent double counting.
- Tests: zero-address mint/burn, issuer registry requirement, ordinary transfer
  and bridge double-count prevention.

## Task 12 — Phase 7 enrichment, retention and health

- Add context-only Stage1 enrichment after existing result persistence.
- Reuse runtime health and indexed batched retention patterns.
- Add cleanup metrics and failure isolation.
- Tests: absent sources do not remove candidates, retention bounds and health
  recovery.

## Task 13 — Safety and resource gates

- Scan for private API/order/position/executor/AI imports and decision tokens.
- Verify `TRADING_MODE=paper`, no new service, bounded queues and memory caps.
- Run deterministic full regression and isolated PostgreSQL integration.
- No live source test is part of the default test command.

## Task 14 — Whole-branch independent review

- Review every contract and source semantic against the Phase 7 spec.
- Verify BTC/EVM separation, amount units, USD timestamp ordering, reorg,
  labels, side semantics, bridge handling, bounded recovery, Migration 012
  safety, Stage1/Phase5 non-interference and Phase6 independence.
- Verify the explicit source-limit matrix, native ETH `TX_VALUE` contract,
  `KNOWN_EXTERNAL` rule, stablecoin domains, Spot-only database check and
  complete Migration 012 logical DDL contract.
- Resolve findings and rerun all gates before any implementation checkpoint.

## Future runtime acceptance (not part of this design phase)

Only after implementation and human approval:

- configure approved read-only chain endpoints and public spot sources;
- run real head/cursor progression and event/schema checks;
- verify reconnect, bounded backfill, dedup, freshness, coverage, reorg fixtures,
  source/DB outage recovery and resource ceilings;
- observe 30–60 minutes of stable local runtime;
- report `NO_NEW_EVENT_OBSERVED` rather than fabricate events.
