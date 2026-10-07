# Current Strategy Map (code audit, 2026-09-29)

This document describes the checked-in runtime, not the intended design. The audited branch is `integration/nautilus-v1` at `28f120a4` before this task's changes.

## Current end-to-end chain

```text
Bitget public REST/WS
  src/quant_phase1/adapters/bitget_v3/{rest.py,websocket.py}
  ↓
CollectorService REST bootstrap + WS ticker/closed Kline updates
  src/quant_phase1/entrypoints/collector.py
  ↓ writes canonical PostgreSQL
symbols, market_snapshots, market_observations, klines
  src/quant_phase1/repositories.py / migrations/001_phase1_core.sql
  ↓
Phase1Repository.load_latest_market_batch → run_stage1 → persist_stage1
  src/quant_phase1/entrypoints/engine.py
  ↓ immutable Stage1 A/B outbox event
Phase9 intake → snapshot/projections → evidence → pattern policy → DecisionCandidate
  src/quant_phase9/{intake.py,snapshot.py,sources/,evidence.py,patterns.py,decision.py,runtime.py}
  ↓ currently gated off by default and no approved active pattern policy
RiskPolicyV1.approve_intent (implemented, no production caller)
  src/quant_execution/risk.py
  ↓ ExecutionIntentV1 (currently created only by acceptance fixtures)
NautilusIntentAdapter → reduce-only stop on fills
  src/quant_nautilus/adapter.py
  ↓
LocalPaper fixture harness (not a public-data runtime)
  src/quant_nautilus/{paper.py,paper_acceptance.py}
```

The actual runtime does not currently form a connected autonomous trade path. The Phase1 engine bootstraps public REST once and then evaluates persisted canonical snapshots on its configured cycle. Phase9 is created only when `PHASE9_ENABLED=1` and then requires an approved manifest and approval record. Risk, intent creation, Nautilus, and LocalPaper remain separate from that engine path.

## Screening and Stage1 rules

**Universe** — `select_universe()` in `src/quant_phase1/universe.py` retains instruments whose ticker is available, category is `USDT-FUTURES`, quote is USDT, symbol type is crypto, contract is perpetual, status is online, and base asset is not `RWA`. It sorts by 24h turnover descending, symbol ascending, then takes `Settings.universe_limit` (default 200). There is no coded “100 → 30 → 10” waterfall.

**Stage1 inputs** — `evaluate_stage1()` in `src/quant_phase1/stage1.py` requires an available ticker no older than 5 seconds and available, closed latest candles for 5m/15m/1H/4H within 30/60/120/180 seconds of their interval close; at least two 5m and two 1H bars are required. It computes 5m EMA(9/21), ATR/range; classifies 1H and 4H structure. Although `volume` is listed in `inputs_used`, Stage1's category formula does not use candle volume.

**Ordered hard filters and categories** (first matching branch wins):

1. `turnover24h <= 0` → D / `INVALID_LIQUIDITY`.
2. spread ratio `(ask-bid)/last > 0.002` → C / `WIDE_SPREAD`.
3. either 1H or 4H structure is `RANGE` → C / `NO_DIRECTIONAL_STRUCTURE`.
4. 1H/4H trends disagree or are not both directional → C / `MULTI_TIMEFRAME_CONFLICT`.
5. A / `HIGH_CONFIDENCE_ALIGNED_TREND` iff spread ratio `<= 0.0015` AND `(5m range high - range low)/ATR >= 2` AND 5m EMA(9/21) agrees with the aligned 1H/4H trend.
6. Otherwise B / `WAIT_FOR_TRIGGER`.

This is ordered hard-filter logic plus a conjunction; there is no weighted score, OR-based signal, state machine, volume confirmation, OI/CVD/funding threshold, breakout predicate, entry, stop, target, or trade side in Stage1. A means deep analysis; it does not mean “buy.” B means wait.

## Evidence and LONG/SHORT

| Evidence | Existing source/calculation | Used by current trading rule? | Directional effect today |
|---|---|---:|---|
| Price structure / trend | Stage1 `classify_structure()` on closed 1H and 4H bars; 5m EMA(9/21), ATR and range expansion | Stage1 screening; Phase9 source | Stage1 outputs aligned structure only; it does not choose side/order |
| Ticker, spread, turnover | Phase1 Bitget UTA ticker persisted in canonical market snapshot | Yes, Stage1 filters | zero turnover rejects; spread threshold rejects/waits |
| Candle volume | Phase1 OHLCV | No Stage1 category predicate uses it | no effect |
| Open interest | Phase2 adapters and canonical `open_interest`; Phase9 projection can emit `OI_OBSERVED` | No active approved pattern | unknown direction; observation only |
| Funding / basis | Phase2 `funding_rates`; Phase4 may add funding/basis/long-short context; Phase9 projection | No active approved pattern | generic context; no active directional predicate |
| Trade flow / CVD | Phase3 public trades → `trade_flow_windows`; Phase9 trade-flow projection reads delta/unknown count | No active approved pattern | positive delta maps to weak bullish, negative to weak bearish; unknown trades suppress direction |
| Liquidations / regime / options / on-chain | Phase4/5/7/8 context and Phase9 projections when available | No active approved pattern | context only absent approved directional predicate |
| Jev review | Phase9 optional conflict review | Jev not configured | not a trade signal |

Phase9 pattern types in `src/quant_phase9/patterns.py` include trend continuation (1H/4H), breakout confirmation (15m/1H), and liquidation reversal (15m/1H). Predicates within one configured policy are conjunctive; missing inputs yield partial/unknown rather than a pass. LONG or SHORT comes from an approved manifest entry, not from a live Stage1 side. The tracked policy has no enabled patterns and the policy approval file is absent. `Phase9RuntimeConfig.enabled` defaults to false. Thus there is currently **no active LONG rule and no active SHORT rule**.

The Stage1 persistence call supplies `candidate_valid_until=None` (`src/quant_phase1/service.py`); Phase9 treats missing/expired validity as `STAGE1_EXPIRED`, another independent block on execution.

## DecisionCandidate schema actually present

`DecisionCandidateV1` in `src/quant_phase9/contracts.py` contains: `decision_id`, `evaluation_id`, `stage1_candidate_id`, `symbol`, `market`, `timeframe`, `created_at`, `valid_until`, `eligible`, `direction_bias`, `confidence_band`, `matched_pattern`, `pattern_status`, `supporting_evidence_ids`, `conflicting_evidence_ids`, `degraded_evidence_ids`, `missing_evidence`, `veto_reasons`, `jev_review_id`, `reason_codes`, `short_summary`, `input_snapshot_hash`, `evidence_schema_version`, `pattern_policy_version`, `freshness_policy_version`, `decision_policy_version`, `ttl_policy_version`, `prompt_version`, `code_version`, and `supersedes_decision_id`. It has no entry price, stop, target, or quantity.

## Risk, sizing, and exits

`RiskPolicyV1.approve_intent()` in `src/quant_execution/risk.py` checks an active eligible directional DecisionCandidate, decision/evidence hash and supported versions, PAPER/BACKTEST mode, instrument/venue/symbol binding, quote and account freshness, reconciled account, open-intent count, reserved risk, exposure, margin, spread, stop side/tick and slippage bounds, plus min/max quantity/notional.

Implemented policy bounds include `max_notional`, `max_margin`, `max_leverage`, `max_risk`, `max_exposure`, `max_reserved_risk`, `max_open_intents`, quote/account max age, max spread/slippage, intent TTL, and supported versions. `ExecutionStore.reserve()` durably deduplicates by `(decision_id, account_id, mode)`, rejects content conflict, and serializes account reservations. Adapter submission additionally guards duplicate `client_order_id`.

Position size is risk-based, not fixed: `raw_quantity = min(available_risk / loss_per_unit, notional_cap / notional_price, instrument.max_quantity)`, rounded down to `quantity_step`, then min quantity/notional checked. `available_risk = min(max_risk, max_reserved_risk - account.reserved_risk)`; `notional_cap` is bounded by max notional, remaining exposure, margin/balance × leverage. The caller must supply a stop; there is no Stage1 ATR stop generation.

Exit/protection: Nautilus adapter submits a STOP_MARKET reduce-only order for cumulative filled quantity and adjusts protection for partial fills. There is no take-profit, trailing stop, signal exit, timeout exit, daily-loss limit, max-drawdown control, cooldown, or separate max-position count in the inspected production risk path. Account reconciliation is required for risk approval. The risk function's only repository caller found is acceptance code, not the long-running engine.

## Current Paper data source

`CURRENT_PAPER_DATA_SOURCE = FIXTURE`.

`src/quant_nautilus/paper.py` explicitly labels its path fixture-driven, creates local SandboxSession quotes/instruments, requires an isolated `quant_phase9_test` loopback database and fixture schema, and reports `FIXTURE_DRIVEN_ACCEPTANCE` with `network_order_routes=0`. This does not consume the Phase1 canonical public-market stream. Separate from Paper, Phase1/2/3 do have real public data collectors and canonical PostgreSQL repositories.

## Runtime state observed during this audit

The Docker runtime had `quant-engine`, `quant-postgres`, and `agentops` running; the existing `quant-collector` container was stopped (exit 137), so no collector was running. The engine's Stage1 code reads canonical PostgreSQL, but without the collector its persisted public snapshots cannot prove continuing freshness. Keep Live disabled; GPT disabled; Jev not configured.
