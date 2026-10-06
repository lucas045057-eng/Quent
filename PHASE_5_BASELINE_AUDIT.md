# PHASE 5 BASELINE AUDIT

**Audit date:** 2026-09-22 (UTC)

**Branch:** `phase5`

**Base commit:** `b3dd3741ae7719e1eaa469929db8e676c1a0a62e`

**Base status:** Phase 4 implementation is frozen at `PHASE4_EXTERNAL_SOURCE_GATE_PENDING`. This audit does not reopen the Phase 4 external gate and does not claim Phase 4 acceptance.

**Audit result:** `PHASE5_BASELINE_AUDIT_READY`

## 1. Scope and guardrails

Phase 5 is a local WSL2 design task for the existing Quant repository. It is limited to deterministic public-data context:

- BTC and ETH market context;
- direction, volatility, and breadth-separated market regime;
- candidate relative strength against BTC, ETH, and a snapshot-aligned market benchmark;
- static, version-controlled sector context.

No Phase 5 code, migration, container, remote ECS, or runtime change is included in this audit. Phase 6 remains out of scope.

The project remains paper-only. Phase 5 must not create order, position, risk-budget, live-executor, AI, news, macro, unlock, on-chain, whale, options, or evidence-chain behavior.

## 2. Repository and Phase 4 integrity

The exact Phase 4 accepted implementation base was checked before creating this branch:

| Check | Result |
|---|---|
| Branch | `phase5` |
| Parent/base HEAD | `b3dd3741ae7719e1eaa469929db8e676c1a0a62e` |
| Worktree before Phase 5 docs | clean |
| Phase 4 history | unchanged |
| Private API/order routes | remain out of Phase 5 scope |
| Phase 4 Bitget liquidation gate | previously passed locally: 10 real events, 0 parser errors, 6 ACKs, 17/17 ping-pong, 2 reconnects |
| Phase 4 Bybit external gate | remains `KNOWN_EXTERNAL_ACCEPTANCE_DEBT` / `EXCHANGE_ACCESS_LIMITATION` |

The Bybit 403 condition is an external acceptance debt. Phase 5 must not modify the Bybit adapter, lower acceptance standards, or represent the Phase 4 gate as accepted.

## 3. Existing canonical data surface

Phase 5 can reuse the existing collector/engine/PostgreSQL topology. No new collector or daemon is needed.

| Existing surface | Reusable fields / meaning | Phase 5 use |
|---|---|---|
| `symbols` | canonical symbol identity, exchange, status | symbol universe and mapping validation |
| `klines` | `symbol`, `interval`, OHLCV, `bar_open_timestamp`, exchange/fetched/processed times, `status`, `is_closed` through the canonical contract | primary closed-bar context input for 5m/15m/1H/4H |
| `market_snapshots` | source/exchange, snapshot time, status, bounded JSON snapshot | optional ticker/volume freshness evidence; not a second raw warehouse |
| `universe_runs` / `universe_members` | point-in-time run and membership/rank | breadth and market-benchmark membership without survivorship drift |
| `screening_runs` / `screening_results` | immutable Stage1 result and existing indicators/structure | context-only Stage1 enrichment boundary; never an eligibility input |
| Phase 2 derivative tables | OI/funding observations; four-state source status | not required for Phase 5 core output |
| Phase 3 flow tables | trade flow/CVD observations; selected tables already support source `PARTIAL` | not required for Phase 5 core output |
| Phase 4 context tables | liquidation, long/short, basis, cross-exchange metrics; four-state source status | not required for Phase 5 core output |

The existing local implementations provide deterministic EMA/ATR-style indicators and HH/HL/LH/LL structure classification over a supplied closed-candle sequence. Phase 5 should reuse these semantics or a clearly versioned extension, rather than silently creating a competing structure definition.

## 4. Existing migrations and compatibility

The repository contains additive migrations `001_phase1_core.sql` through `009_phase4_metrics.sql`. Existing core tables already provide:

- canonical symbols, snapshots, observations, and closed-candle storage;
- point-in-time universe and screening runs;
- Phase 2 and Phase 4 context tables with explicit `AVAILABLE`, `STALE`, `NOT_AVAILABLE`, and `ERROR` status semantics; Phase 3 flow tables additionally use `PARTIAL` where their individual schema permits it;
- UTC-capable `TIMESTAMPTZ` fields and source/fetched/processed timing.

No Phase 5 tables or migration 010 currently exist on this baseline. The implementation must add a new idempotent migration only. It must not modify, reorder, drop, truncate, or destructively rewrite migrations 001–009.

## 5. Data availability audit

The existing Phase 1/2/3/4 design and runtime evidence support the following design assumptions:

- Bitget UTA v3 public data is the primary known market-data path.
- Canonical 5m, 15m, 1H, and 4H kline contracts already exist.
- BTCUSDT and ETHUSDT must be resolved through the canonical symbol/instrument path at runtime; Phase 5 must not hard-code a successful availability claim without contract/runtime evidence.
- The existing universe is snapshot-based and was previously observed at hundreds of symbols. Historical Phase 4 runtime evidence recorded 803 `symbols`; this is an observation, not a Phase 5 promise or a current live count.
- Bybit is not a reliable Phase 5 dependency while its external 403 gate remains unresolved.
- Hyperliquid/other optional public sources may be reported as missing or partial, but cannot be silently substituted for a required Bitget contract.
- A static sector map does not yet exist in the repository and is a Phase 5 design/implementation input, not an inferred runtime classification.

Phase 5 must use `NOT_AVAILABLE`, `STALE`, or `PARTIAL` semantics when these inputs are absent or below coverage thresholds. It must not fabricate OI, funding, CVD, liquidation, long/short, news, AI, benchmark, sector, or market-context values.

## 6. Historical runtime/resource baseline

The most recent Phase 4 local runtime report recorded the following measured database counts at that observation point:

| Table | Historical measured rows |
|---|---:|
| `symbols` | 803 |
| `exchange_instruments` | 311 |
| `market_snapshots` | 146,448 |
| `market_observations` | 437,515 |
| `klines` | 82,332 |
| `open_interest` | 3,408 |
| `funding_rates` | 1,838 |
| `long_short_observations` | 2,989 |
| `basis_snapshots` | 2,800 |
| `trade_flow_windows` | 1,833 |
| `cross_exchange_flow_snapshots` | 561 |
| `cross_exchange_derivative_snapshots` | 2,200 |
| `cross_exchange_phase4_snapshots` | 2,958 |
| `stage1_derivative_enrichment` | 2,600 |
| `stage1_flow_enrichment` | 2,600 |
| `stage1_phase4_enrichment` | 2,400 |
| `screening_results` | 2,800 |
| `screening_runs` | 14 |
| `system_health` | 5 |
| `runtime_health_events` | 15 |
| `liquidation_events` / `liquidation_windows` | 0 |
| `cvd_snapshots` | 0 |

These figures are historical evidence from the Phase 4 report. They must not be presented as current Phase 5 runtime counts until remeasured.

The current local compose file has these existing limits:

| Service | Current local compose limit | Phase 5 target ceiling |
|---|---:|---:|
| PostgreSQL | 512 MiB | 768 MiB |
| `quant-collector` | 512 MiB | 256 MiB |
| `quant-engine` | 384 MiB | 384 MiB |

The Phase 5 target is intentionally stricter for the collector and larger for PostgreSQL. This mismatch is an implementation/runtime acceptance item. It is not silently resolved in the design audit; Phase 5 runtime acceptance must use or explicitly document the exact target limits. The collector should not be made larger merely to hide an unbounded Phase 5 cache.

The previous local runtime report also recorded normal fast-shutdown events with exit code 0 and no OOM indication for the local containers. This is an operational lifecycle warning, not evidence of a Phase 5 defect. Phase 5 must include restart and bounded-resource tests.

## 7. Design risks and required controls

1. **Timestamp alignment:** all derived contexts need a single UTC as-of timestamp per timeframe and must reject input skew beyond configuration.
2. **Closed bars only:** an open candle cannot enter return, trend, volatility, volume, breadth, RS, or sector calculations.
3. **Missing is not zero:** unavailable symbols are counted in `missing_count`, not treated as flat or weak.
4. **Universe consistency:** breadth and market benchmark use the same point-in-time `universe_run_id` and must not use today’s membership to explain an older context.
5. **Status vocabulary:** Phase 1/2/4 source tables use the four-state contract, while Phase 3 flow tables already use `PARTIAL` in selected schemas. Phase 5 therefore needs an explicitly separate derived-status contract and tested mapping; it must not describe all Phase 1–4 tables as four-state.
6. **Bounded persistence:** only derived context is persisted; raw kline payloads remain in the existing warehouse. Phase 5 must not duplicate raw klines.
7. **Stage1 isolation:** context is an explanation/enrichment surface. It must never change Stage1 category, eligibility, A/B/C/D decision, or screening result.
8. **External source debt:** Bybit 403 remains visible and cannot be replaced by a fabricated or silently re-routed source.

## 8. Audit conclusion

The existing Phase 1–4 canonical data model is sufficient to design Phase 5 as an additive, engine-derived context layer. A new migration 010, bounded derived tables, a version-controlled sector mapping, and explicit partial/missing/freshness semantics are required before implementation.

No implementation should begin until the independent review of `PHASE_5_DESIGN_SPEC.md` is complete and its findings are resolved.
