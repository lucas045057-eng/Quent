# PHASE 5 DESIGN SPECIFICATION

**Status:** Design only — implementation has not started

**Branch/base:** `phase5` / `b3dd3741ae7719e1eaa469929db8e676c1a0a62e`

**Operating mode:** `TRADING_MODE=paper`

**Design gate:** This document requires independent review before any Phase 5 code or migration is written.

## 1. Objective

Phase 5 adds a deterministic market-context layer for the existing Quant system. It describes BTC/ETH context, separates market direction from volatility and breadth, measures candidate relative strength, and adds static sector context. It produces explanatory context, not trading decisions.

The output vocabulary must never be presented as `BUY`, `SELL`, `LONG`, or `SHORT`. A Phase 5 result can say `TREND_UP`, `HIGH_VOLATILITY`, `OUTPERFORMING`, or `NOT_AVAILABLE`; it cannot authorize an order.

## 2. Hard scope boundary

### In scope

- BTCUSDT and ETHUSDT market context at `5m`, `15m`, `1H`, and `4H`;
- deterministic direction/trend, volatility, volume, and market-structure descriptions;
- point-in-time universe breadth;
- direction, volatility, and breadth as separate regime dimensions;
- relative strength at `15m`, `1H`, and `4H`, with an explicit 5m decision below;
- static, version-controlled sector membership and sector context;
- bounded derived-context persistence;
- context-only enrichment attached to an existing Stage1 run;
- migration 010, additive and idempotent;
- local tests and local resource/restart validation.

### Out of scope

- Phase 6;
- news, macro data, token unlocks, AI/LLM, evidence-chain generation;
- on-chain data, whale data, options, order book strategy, funding/OI semantic expansion;
- new trading signals, risk budgets, position sizing, orders, positions, or live execution;
- any private exchange API, API key, secret, passphrase, or authenticated route;
- any new collector service, broker, cache service, or remote ECS deployment;
- changing Phase 1 eligibility or the Stage1 A/B/C/D result;
- changing Phase 4 adapters or trying to close the Bybit external gate.

## 3. Architecture and ownership

Phase 5 reuses the existing topology:

```text
Bitget public adapters / existing collector
        -> canonical closed klines, snapshots, universe runs
        -> existing PostgreSQL
        -> quant-engine Phase5 context cycle
        -> Phase5 derived tables + context-only Stage1 enrichment
```

The collector owns acquisition and canonicalization. The engine owns deterministic context calculation and persistence. PostgreSQL owns durable derived context and idempotency. No Phase 5 calculation calls an exchange directly.

The Phase 5 cycle reads canonical data in batches. It does not create a parallel raw-kline warehouse and it does not re-fetch or reinterpret exchange payloads. Existing Phase 1–4 source adapters remain the authority for source-specific field semantics.

## 4. Canonical status and reason contract

### 4.1 Source status

The repository has two existing source-status families and Phase 5 must not blur them:

- Phase 1, Phase 2, and Phase 4 source tables use `AVAILABLE`, `STALE`, `NOT_AVAILABLE`, and `ERROR`.
- Phase 3 flow tables additionally use `PARTIAL` where an exchange/source explicitly provides usable but incomplete coverage.

These existing schemas are frozen. Phase 5 must not rewrite either family or promote a Phase 3 `PARTIAL` source to full availability without applying the Phase 5 coverage rules.

### 4.2 Phase 5 derived status

Phase 5 derived contexts add a fifth state because coverage can be usable but incomplete:

```text
AVAILABLE | PARTIAL | STALE | NOT_AVAILABLE | ERROR
```

Mapping rules:

| Derived status | Meaning |
|---|---|
| `AVAILABLE` | all required inputs are aligned, fresh, and meet their configured coverage requirements |
| `PARTIAL` | required core inputs are usable, but optional members/sources/evidence are missing; the missing portion is counted explicitly |
| `STALE` | at least one required input is stale, or the context as-of point cannot be refreshed within its configured grace |
| `NOT_AVAILABLE` | a required input is absent, coverage is below minimum, or no eligible members remain |
| `ERROR` | deterministic calculation or persistence failure; never downgraded to a normal market state |

`PARTIAL` is a Phase 5 output status with a database check constraint. It is kept separate from the Phase 3 source-table meaning even though the spelling is shared.

### 4.3 Status precedence and mapping

For each Phase 5 output, source rows are first classified by required/optional role. The output status is determined in this order:

1. Any calculation or database write failure => `ERROR`.
2. Any required input is `STALE` => `STALE`.
3. Any required input is `ERROR`, or the required sample/coverage minimum is not met => `NOT_AVAILABLE`.
4. All required inputs meet their minimums, but an optional source/member is missing or source coverage is explicitly partial => `PARTIAL`.
5. All required and configured optional inputs are aligned and usable => `AVAILABLE`.

This precedence is evaluated before a human-readable regime label. A label such as `MIXED` never hides `STALE`, `NOT_AVAILABLE`, or `ERROR` input status.

### 4.4 Reason codes

The bounded reason-code set is:

```text
MISSING_INPUT
STALE_INPUT
INSUFFICIENT_COVERAGE
TIMESTAMP_SKEW
NO_ELIGIBLE_MEMBERS
UNKNOWN_SECTOR
SOURCE_UNAVAILABLE
CALCULATION_ERROR
PERSISTENCE_ERROR
```

Unknown or unrecognized reason codes fail the contract test. Reasons are explanatory metadata, not a trading signal.

## 5. Canonical enums

The following strings are versioned Phase 5 contract values. They are stored as text with database checks, not Python-only enum names.

### 5.1 Leader and market direction

```text
TREND_UP
TREND_DOWN
RANGE
MIXED
NOT_AVAILABLE
```

### 5.2 Volatility

```text
LOW
NORMAL
HIGH
EXTREME
NOT_AVAILABLE
```

### 5.3 Volume

```text
BELOW_BASELINE
NORMAL
ELEVATED
NOT_AVAILABLE
```

### 5.4 Structure

```text
HIGHER_HIGH_HIGHER_LOW
LOWER_HIGH_LOWER_LOW
RANGE
MIXED
NOT_AVAILABLE
```

### 5.5 Breadth

```text
BROAD_STRENGTH
NARROW_STRENGTH
BROAD_WEAKNESS
NARROW_WEAKNESS
MIXED
NOT_AVAILABLE
```

### 5.6 Relative strength and sector relation

```text
STRONG
WEAK
NEUTRAL
NOT_AVAILABLE
```

Sector relation uses:

```text
OUTPERFORMING
UNDERPERFORMING
IN_LINE
NOT_AVAILABLE
```

## 6. Time and alignment model

All Phase 5 timestamps are UTC `TIMESTAMPTZ` values. A field called `timestamp` without a more precise name is forbidden.

Every derived row has:

- `context_timestamp`: the UTC close/as-of point the result describes;
- `input_window_start` and `input_window_end`: the exact closed-bar window used;
- `processed_at`: when the engine completed the calculation/persistence attempt;
- `source_count` or member/source counts appropriate to the result;
- source references to canonical row identities or query watermarks, not raw payload copies.

For source rows, the existing distinction remains:

- `exchange_timestamp`: when the exchange says the observation/bar was produced;
- `fetched_at`: when the collector obtained the response;
- `processed_at`: when canonicalization/persistence completed.

### 6.1 Closed-bar rule

Only canonical `AVAILABLE` closed candles enter Phase 5 calculations. The existing parser and closed-bar gate must be used. A stored kline is accepted as closed only when its canonical contract has `is_closed=true` and the interval-aware expected-closed-bar check accepts its bar-open timestamp.

The SQL warehouse currently stores the canonical accepted kline row and the repository reconstructs its closed-bar contract on read; Phase 5 must not infer an open candle from a raw payload or from a ticker timestamp. If a future implementation needs durable closedness metadata, it must be an additive design decision reviewed separately; it must not mutate migrations 001–009 in place.

### 6.2 Timestamp alignment

For an interval with duration `D`, Phase 5 defines the canonical bar context time as:

```text
context_timestamp = bar_open_timestamp + D
```

The database continues to retain the source `bar_open_timestamp`; `context_timestamp` is a deterministic derived close/as-of value. For a kline, `exchange_timestamp` remains the exchange-provided event timestamp from the canonical source and is not reinterpreted as the bar close. The exact bar-open and computed close are both retained in `input_reference`.

Each same-timeframe result joins symbols on the exact same `context_timestamp`. The V1 calculations do not combine 5m/15m/1H/4H inputs into one formula, so a 5m close and a 1H close are not incorrectly rejected merely because their natural close times differ. A future cross-timeframe display may use an explicit `anchor_timestamp`: for each timeframe it selects the latest closed bar with `context_timestamp <= anchor_timestamp`, records that timeframe-specific context timestamp, and does not apply same-timeframe skew to the natural difference between intervals.

The configuration key is:

```text
PHASE5_MAX_CONTEXT_TIMESTAMP_SKEW_SECONDS=60
```

For every same-timeframe multi-source or cross-symbol calculation:

```text
max(input_context_timestamp) - min(input_context_timestamp)
    <= PHASE5_MAX_CONTEXT_TIMESTAMP_SKEW_SECONDS
```

If this fails, the result is `NOT_AVAILABLE` with `TIMESTAMP_SKEW` rather than an interpolated value. The threshold is configurable and recorded in the calculation version/config snapshot. Cross-timeframe display alignment uses the explicit anchor rule above instead of pretending that different bar durations share one timestamp.

## 7. Canonical input contracts

### 7.1 Closed kline input

Required input fields:

```text
symbol
interval                  # 5m | 15m | 1H | 4H
bar_open_timestamp        # UTC
open, high, low, close
volume, turnover
exchange_timestamp        # UTC
fetched_at                # UTC
processed_at              # UTC
status                    # source four-state contract
is_closed                 # canonical contract field
source, exchange
raw_reference             # optional bounded reference only
```

Required conditions:

- `status=AVAILABLE`;
- `is_closed=true`;
- interval-aware expected-closed-bar freshness passes;
- no duplicate `(symbol, interval, bar_open_timestamp)`;
- numeric values are finite and valid (`open > 0`, `high > 0`, `low > 0`, `close > 0`, `high >= max(open, close)`, `low <= min(open, close)`, nonnegative volume/turnover); PostgreSQL `NUMERIC` NaN/Infinity-like values are rejected at the input contract boundary.

### 7.2 Universe input

Breadth and market benchmark use exactly one `universe_run_id` per calculation. Required fields are `run_timestamp`, run status, member symbol, rank, and the member’s canonical closed-bar availability at the selected context timestamp.

The calculation stores:

```text
sample_size             # members in the selected universe snapshot
available_count         # members with usable aligned input
missing_count           # sample_size - available_count, explicitly counted
coverage_ratio          # available_count / sample_size, never null when sample_size > 0
universe_run_id
```

The implementation must not replace a historical universe with the current universe, and must not treat missing members as zero returns or weak members.

### 7.3 Sector mapping input

Sector membership is a version-controlled static mapping, loaded without network or AI classification. Proposed file:

```text
config/phase5_sector_map.csv
```

Required columns:

```text
mapping_version,symbol,sector,source_reference,effective_from,effective_to
```

Unknown symbols map to the explicit `UNKNOWN` sector. `UNKNOWN` is not a numeric sector, is not treated as zero, and does not make a candidate weak. Missing/invalid mapping rows are reported as `UNKNOWN_SECTOR` and counted.

### 7.4 Common derived-field contract

The following rules apply to every Phase 5 derived table:

- all timestamp columns are UTC `TIMESTAMPTZ`;
- percentages and ratios are `NUMERIC`; `return_pct`, `relative_return_pct`, and `candidate_vs_sector_pct` are percentage points, while `coverage_ratio`, `positive_ratio`, and structure ratios are decimals in `[0,1]`;
- count fields are `INTEGER >= 0`; `source_count`, `sample_size`, `available_count`, `missing_count`, and member counts cannot be negative;
- `available_count + missing_count = sample_size` wherever a sample is defined;
- `0 <= coverage_ratio <= 1`, and when `sample_size=0`, `coverage_ratio` is `NULL` and status is `NOT_AVAILABLE`;
- values required by the selected label are non-NULL for `AVAILABLE`; a missing optional value is NULL only with `PARTIAL` and an explicit missing-evidence entry;
- `NOT_AVAILABLE` rows have no calculated numeric value for the missing required input; `ERROR` rows have no trusted calculated value and carry only a bounded error reason;
- `input_window_start <= input_window_end <= context_timestamp` for non-empty windows;
- replay tests verify that `processed_at` is a UTC processing time after the calculation attempt and that it is never substituted for an exchange/source time;
- JSON evidence and references are bounded arrays/objects containing identifiers, counts, thresholds, and source references only; raw exchange payloads are forbidden in Phase 5 derived JSON.

## 8. Leader context contract

One `MarketLeaderContext` is produced for each leader in `{BTCUSDT, ETHUSDT}`, each timeframe `{5m, 15m, 1H, 4H}`, and each `context_timestamp`.

Required fields:

```text
context_timestamp
symbol
timeframe
input_window_start
input_window_end
return_pct
trend_state
structure_state
volatility_state
volume_state
volatility_value
volume_ratio
freshness_status
data_quality
source_count
missing_count
support_evidence JSONB
conflict_evidence JSONB
missing_evidence JSONB
status
reason_code
calculation_version
processed_at
input_reference JSONB
```

`support_evidence`, `conflict_evidence`, and `missing_evidence` contain bounded dimension names, thresholds, and source references. They must not contain unbounded raw exchange payloads.

A leader row is `NOT_AVAILABLE` when the required aligned closed-bar window is absent or below the minimum. A leader row is `STALE` when the required latest bar fails interval-aware freshness. An optional source gap can produce `PARTIAL`, but Bitget canonical kline availability is not silently replaced by an unverified source.

## 9. Deterministic calculations

All numeric calculations use Decimal-compatible quantization at the contract boundary. Intermediate floating-point operations must not produce non-finite values. Every formula version is included in `calculation_version`.

### 9.1 Return

For a timeframe window with current close `C_t` and close `C_{t-k}` at the configured lookback:

```text
return_pct = (C_t / C_{t-k} - 1) * 100
```

The two closes must be aligned closed bars for the same symbol/timeframe. Missing history yields `NOT_AVAILABLE`, not a zero return.

### 9.2 Direction/trend

Use the existing deterministic local EMA/structure semantics as versioned primitives:

- EMA fast period 9 and slow period 21, subject to Phase 5 config;
- normalized close slope over a configured closed-bar window;
- existing HH/HL/LH/LL structure classification over the same aligned window.

The state is evidence-based, not a universal score:

- `TREND_UP` requires at least two configured up evidences and no required down contradiction;
- `TREND_DOWN` requires at least two configured down evidences and no required up contradiction;
- `RANGE` requires neutral EMA/slope and range structure;
- `MIXED` means usable but conflicting evidence;
- `NOT_AVAILABLE` means the minimum window is absent.

The exact periods, slope threshold, and contradiction rule are configuration values and are persisted with the calculation version. A single EMA crossover cannot alone create a Stage1 or trading decision.

### 9.3 Structure

The existing `classify_structure` primitive counts higher highs/lows and lower highs/lows and reports `BULLISH`, `BEARISH`, or `RANGE`. Phase 5 maps those deterministic outputs exactly to the canonical `HIGHER_HIGH_HIGHER_LOW`, `LOWER_HIGH_LOWER_LOW`, or `RANGE` vocabulary. It does not invent a second mixed-structure heuristic. `MIXED` is reserved for a future explicitly versioned structure primitive; in V1 an ambiguous input remains `RANGE` with conflict evidence. If the window is too short, the result is `NOT_AVAILABLE`.

This mapping must be tested against the existing Phase 1 structure tests to prevent a second incompatible structure implementation.

### 9.4 Volatility

Use closed-bar log returns over a configured rolling window:

```text
r_i = ln(C_i / C_{i-1})
realized_volatility = sqrt(mean((r_i - mean(r))^2))
```

Phase 5 reports the unannualized per-bar value and does not compare raw values across timeframes without timeframe-specific thresholds. The normative initial defaults are standard-deviation decimals per bar:

```text
5m:  low=0.0010, high=0.0040, extreme=0.0080
15m: low=0.0015, high=0.0060, extreme=0.0120
1H:  low=0.0030, high=0.0120, extreme=0.0250
4H:  low=0.0060, high=0.0250, extreme=0.0500
```

The exact rule is `LOW` if value `< low`, `NORMAL` if `low <= value < high`, `HIGH` if `high <= value < extreme`, and `EXTREME` if value `>= extreme`. The threshold table is recorded with the calculation version and requires a new version when changed. No volatility value is invented when history is missing.

### 9.5 Volume

For the latest closed bar and a configured baseline of prior closed bars:

```text
volume_ratio = latest_volume / median(prior_baseline_volumes)
```

The baseline must contain the configured minimum number of valid bars. Fewer than the minimum valid historical bars produces `NOT_AVAILABLE` with `MISSING_INPUT`. Once the minimum exists, a zero/negative or non-finite baseline volume is a malformed canonical input and produces `ERROR` with `CALCULATION_ERROR`. Volume state thresholds are configurable per timeframe. Missing volume is not treated as low volume.

### 9.6 Market breadth

For each member of the selected point-in-time `universe_run_id`, calculate aligned closed-bar return and, where available, structure. Then report:

```text
positive_ratio = positive_members / available_count
up_structure_ratio = up_structure_members / structure_available_count
down_structure_ratio = down_structure_members / structure_available_count
coverage_ratio = available_count / sample_size
```

The output always includes `sample_size`, `available_count`, `missing_count`, `coverage_ratio`, and the selected universe identity. Missing members remain missing. The implementation must not define `missing_return=0`.

Use these normative defaults:

```text
PHASE5_MIN_UNIVERSE_SAMPLE=20
PHASE5_MIN_BREADTH_COVERAGE=0.80
PHASE5_BREADTH_NEUTRAL_BAND_PCT=0.05
PHASE5_BREADTH_BROAD_RATIO=0.60
PHASE5_BREADTH_NARROW_RATIO=0.55
```

For a member return `r`, classify it as up when `r > +neutral_band`, down when `r < -neutral_band`, and neutral otherwise. With sample and coverage minimums met, apply this exact table:

| Condition | Breadth state |
|---|---|
| `up_ratio >= 0.60` | `BROAD_STRENGTH` |
| `0.55 <= up_ratio < 0.60` | `NARROW_STRENGTH` |
| `down_ratio >= 0.60` | `BROAD_WEAKNESS` |
| `0.55 <= down_ratio < 0.60` | `NARROW_WEAKNESS` |
| otherwise | `MIXED` |

The first matching row is not ambiguous because up and down members are disjoint. Structure ratios are supporting evidence, not a second breadth classifier. If sample or coverage minimums fail, state is `NOT_AVAILABLE`; missing members are never classified as neutral, zero, or weak. The breadth result is not a “market score.”

### 9.7 Market regime

Market regime is persisted as three independent dimensions:

```text
direction_regime
volatility_regime
breadth_regime
```

Each dimension has its own evidence arrays:

```text
support_evidence
conflict_evidence
missing_evidence
```

Direction uses BTC/ETH leader states and breadth direction as separate evidence. Its exact decision table is:

| BTC/ETH leaders | Breadth label/status | Direction label | Direction status |
|---|---|---|---|
| both `TREND_UP` | `BROAD_STRENGTH` or `NARROW_STRENGTH` / usable | `TREND_UP` | breadth status |
| both `TREND_DOWN` | `BROAD_WEAKNESS` or `NARROW_WEAKNESS` / usable | `TREND_DOWN` | breadth status |
| both `RANGE` | `MIXED` / usable | `RANGE` | breadth status |
| both usable, any other combination | usable | `MIXED` | `AVAILABLE` or `PARTIAL` |
| both leaders usable | breadth `NOT_AVAILABLE` | `NOT_AVAILABLE` | `PARTIAL` |
| exactly one leader usable | any usable breadth | `NOT_AVAILABLE` | `PARTIAL` |
| no usable leader | any | `NOT_AVAILABLE` | `NOT_AVAILABLE` |

Any stale required input overrides the dimension status to `STALE`; an input error overrides it to `ERROR`. `NARROW_STRENGTH` and `NARROW_WEAKNESS` are strength/weakness breadth for the table above.

Volatility uses the worst available BTC/ETH state under `EXTREME > HIGH > NORMAL > LOW`. Both available leaders produce `volatility_status=AVAILABLE`; one available leader produces that leader’s label with `volatility_status=PARTIAL`; both missing produce `volatility_state=NOT_AVAILABLE` and `volatility_status=NOT_AVAILABLE`. Stale/error precedence is the same as above. Breadth stores its own label and status: if coverage meets minimum and `missing_count > 0`, the label is calculated but `breadth_status=PARTIAL`; if coverage meets minimum and no members are missing, it is `AVAILABLE`; below minimum is `NOT_AVAILABLE`.

The row stores `direction_status`, `volatility_status`, and `breadth_status` separately, in addition to the three labels. Overall status is `ERROR` if any dimension is error, else `STALE` if any required dimension is stale, else `AVAILABLE` only when all three are available, else `PARTIAL` when at least one dimension is usable, else `NOT_AVAILABLE`. Missing dimensions are labelled `NOT_AVAILABLE` and listed in `missing_evidence`; they are never replaced by a score or zero. There is no breadth dispersion or concentration formula in Phase 5 V1 and no scalar market score.

### 9.8 Relative strength

Relative strength is calculated for a candidate against each benchmark:

```text
benchmark = BTCUSDT | ETHUSDT | MARKET_UNIVERSE_EQUAL_WEIGHT
timeframe = 15m | 1H | 4H
```

For candidate return `R_c` and benchmark return `R_b`:

```text
relative_return_pct = R_c - R_b
```

The market benchmark is an equal-weight arithmetic mean over the same point-in-time universe snapshot, using only available members and excluding the candidate symbol itself. If exclusion would violate the minimum sample/coverage rule, the market benchmark is `NOT_AVAILABLE`. It includes explicit `sample_size`, `available_count`, `missing_count`, and `coverage_ratio`; it is not a survivorship-free claim unless the selected `universe_run_id` is preserved. For `benchmark=BTCUSDT` or `ETHUSDT`, `universe_run_id` is NULL by contract; for `MARKET_UNIVERSE_EQUAL_WEIGHT`, it is required and forms part of replay identity. Comparing a symbol to itself as a BTC/ETH benchmark returns `NOT_AVAILABLE` with `MISSING_INPUT`.

The classification uses configurable positive/negative thresholds per timeframe:

- `STRONG` at or above positive threshold;
- `WEAK` at or below negative threshold;
- `NEUTRAL` between thresholds;
- `NOT_AVAILABLE` when candidate/benchmark input is missing or below minimum.

**5m decision:** Phase 5 V1 does not persist a 5m relative-strength signal. The 5m leader context exists, but RS is limited to 15m/1H/4H to reduce microstructure noise and write volume. A future 5m RS change requires a separate design review.

Relative strength is descriptive context only and cannot change Stage1 eligibility.

### 9.9 Sector context

For each known sector and selected timeframe:

```text
sector_return = arithmetic_mean(member_returns)
sector_positive_ratio = positive_members / available_count
```

The aggregate row includes `member_count`, `available_count`, `missing_count`, and `coverage_ratio`. Minimum member and coverage thresholds are configurable. `UNKNOWN` sector membership is stored explicitly and is never interpreted as a weak sector. The aggregate table has no candidate-specific columns.

For a candidate attached to a Stage1 run, the candidate-specific calculation is stored in the enrichment contract:

```text
candidate_vs_sector_pct = candidate_return - sector_return
```

It includes the candidate symbol, sector, mapping version, universe run identity, candidate/sector returns, and sector relation. This prevents two candidates in the same sector/timeframe from colliding in one aggregate row.

Sector relation uses configurable thresholds:

- `OUTPERFORMING` if candidate-vs-sector is at or above the positive threshold;
- `UNDERPERFORMING` if at or below the negative threshold;
- `IN_LINE` otherwise;
- `NOT_AVAILABLE` if mapping or sector coverage is insufficient.

No runtime AI or network classifier is allowed to modify sector membership.

### 9.10 Normative window, coverage, and threshold defaults

The first implementation must pin these defaults in a configuration snapshot. A change requires a new `calculation_version`:

```text
PHASE5_MIN_CLOSED_BARS=21
PHASE5_RETURN_LOOKBACK_BARS=3
PHASE5_VOL_WINDOW_BARS=20
PHASE5_VOLUME_BASELINE_BARS=20
PHASE5_MIN_SECTOR_MEMBERS=5
PHASE5_MIN_MARKET_BENCHMARK_SAMPLE=20
PHASE5_MIN_BREADTH_COVERAGE=0.80
PHASE5_MAX_CONTEXT_TIMESTAMP_SKEW_SECONDS=60
PHASE5_MAX_EVIDENCE_BYTES=65536
```

The existing interval-aware Phase 1 grace settings remain authoritative for source freshness:

```text
KLINE_INGESTION_GRACE_5M_SECONDS=30
KLINE_INGESTION_GRACE_15M_SECONDS=60
KLINE_INGESTION_GRACE_1H_SECONDS=120
KLINE_INGESTION_GRACE_4H_SECONDS=180
```

Relative-strength and candidate-vs-sector classification defaults are symmetric percentage-point bands:

```text
15m: +/-0.20%
1H:  +/-0.50%
4H:  +/-1.00%
```

At least `PHASE5_MIN_CLOSED_BARS` valid bars are required for trend/structure context. The return, volatility, and volume formulas use their named lookbacks; if any required lookback cannot be assembled from aligned closed bars, the corresponding result is `NOT_AVAILABLE`.

## 10. Persistence model and migration 010

Migration 010 will be additive and idempotent. It may use `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and bounded checks/constraints on newly created Phase 5 tables. It must not alter, drop, truncate, or delete data from migrations 001–009. It must not create a second raw kline table.

`IF NOT EXISTS` is not sufficient schema validation. After applying 010, the migration runner must introspect each expected table, column type/nullability, check constraint, foreign key, unique expression/partial index, and retention index. If a same-named object exists with a missing or incompatible shape, the runner fails with a schema-mismatch error and does not record version 010. It never silently repairs an existing object and never performs a destructive migration. The migration and validation run in one transaction where PostgreSQL permits it.

### 10.1 `phase5_market_leader_context`

Natural key:

```text
(symbol, timeframe, context_timestamp, calculation_version)
```

Core columns:

```text
id BIGSERIAL PRIMARY KEY
symbol TEXT NOT NULL REFERENCES symbols(symbol)
timeframe TEXT NOT NULL CHECK (timeframe IN ('5m','15m','1H','4H'))
context_timestamp TIMESTAMPTZ NOT NULL
input_window_start TIMESTAMPTZ
input_window_end TIMESTAMPTZ
return_pct NUMERIC
trend_state TEXT NOT NULL CHECK (trend_state IN ('TREND_UP','TREND_DOWN','RANGE','MIXED','NOT_AVAILABLE'))
structure_state TEXT NOT NULL CHECK (structure_state IN ('HIGHER_HIGH_HIGHER_LOW','LOWER_HIGH_LOWER_LOW','RANGE','MIXED','NOT_AVAILABLE'))
volatility_state TEXT NOT NULL CHECK (volatility_state IN ('LOW','NORMAL','HIGH','EXTREME','NOT_AVAILABLE'))
volume_state TEXT NOT NULL CHECK (volume_state IN ('BELOW_BASELINE','NORMAL','ELEVATED','NOT_AVAILABLE'))
volatility_value NUMERIC CHECK (volatility_value IS NULL OR volatility_value >= 0)
volume_ratio NUMERIC CHECK (volume_ratio IS NULL OR volume_ratio >= 0)
freshness_status TEXT NOT NULL CHECK (freshness_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
data_quality JSONB NOT NULL DEFAULT '{}'::jsonb
source_count INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0)
missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0)
support_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
conflict_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
reason_code TEXT
calculation_version TEXT NOT NULL
input_reference JSONB NOT NULL DEFAULT '{}'::jsonb
processed_at TIMESTAMPTZ NOT NULL
```

Indexes: `(symbol, timeframe, context_timestamp DESC)`, `(processed_at)`, and:

```text
UNIQUE (symbol, timeframe, context_timestamp, calculation_version)
```

### 10.2 `phase5_market_regime_snapshots`

Replay identity:

```text
(timeframe, context_timestamp, universe_run_id, calculation_version)
```

`universe_run_id` may be NULL only when no universe snapshot was available for a `NOT_AVAILABLE` diagnostic row. Migration 010 enforces uniqueness with an expression index using `COALESCE(universe_run_id, 0)` and a foreign key when non-NULL.

Core columns:

```text
id BIGSERIAL PRIMARY KEY
timeframe TEXT NOT NULL CHECK (timeframe IN ('5m','15m','1H','4H'))
context_timestamp TIMESTAMPTZ NOT NULL
direction_regime TEXT NOT NULL CHECK (direction_regime IN ('TREND_UP','TREND_DOWN','RANGE','MIXED','NOT_AVAILABLE'))
volatility_regime TEXT NOT NULL CHECK (volatility_regime IN ('LOW','NORMAL','HIGH','EXTREME','NOT_AVAILABLE'))
breadth_regime TEXT NOT NULL CHECK (breadth_regime IN ('BROAD_STRENGTH','NARROW_STRENGTH','BROAD_WEAKNESS','NARROW_WEAKNESS','MIXED','NOT_AVAILABLE'))
direction_status TEXT NOT NULL CHECK (direction_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
volatility_status TEXT NOT NULL CHECK (volatility_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
breadth_status TEXT NOT NULL CHECK (breadth_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
universe_run_id BIGINT REFERENCES universe_runs(id)
sample_size INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0)
available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0)
missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0 AND available_count + missing_count = sample_size)
coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1)
support_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
conflict_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
missing_evidence JSONB NOT NULL DEFAULT '[]'::jsonb
status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
reason_code TEXT
calculation_version TEXT NOT NULL
input_reference JSONB NOT NULL DEFAULT '{}'::jsonb
processed_at TIMESTAMPTZ NOT NULL
```

Indexes: `(timeframe, context_timestamp DESC)`, `(universe_run_id, timeframe, context_timestamp DESC)`, and `(processed_at)`, plus:

```text
UNIQUE INDEX phase5_regime_replay_uq
ON phase5_market_regime_snapshots
    (timeframe, context_timestamp, COALESCE(universe_run_id, 0), calculation_version)
```

The reserved expression value `0` is not a valid `universe_run_id`.

### 10.3 `phase5_relative_strength_snapshots`

Replay identity:

```text
(symbol, benchmark, timeframe, context_timestamp, universe_run_id, calculation_version)
```

For BTC/ETH benchmarks `universe_run_id` is NULL; for the market benchmark it is NOT NULL. Migration 010 uses separate partial unique indexes for these two cases so NULL cannot create duplicate market results.

Core columns:

```text
id BIGSERIAL PRIMARY KEY
symbol TEXT NOT NULL REFERENCES symbols(symbol)
benchmark TEXT NOT NULL CHECK (benchmark IN ('BTCUSDT','ETHUSDT','MARKET_UNIVERSE_EQUAL_WEIGHT'))
timeframe TEXT NOT NULL CHECK (timeframe IN ('15m','1H','4H'))
context_timestamp TIMESTAMPTZ NOT NULL
candidate_return_pct NUMERIC
benchmark_return_pct NUMERIC
relative_return_pct NUMERIC
relative_class TEXT NOT NULL CHECK (relative_class IN ('STRONG','WEAK','NEUTRAL','NOT_AVAILABLE'))
universe_run_id BIGINT REFERENCES universe_runs(id)
sample_size INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0)
available_count INTEGER NOT NULL DEFAULT 0 CHECK (available_count >= 0)
missing_count INTEGER NOT NULL DEFAULT 0 CHECK (missing_count >= 0 AND available_count + missing_count = sample_size)
coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1)
status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
reason_code TEXT
calculation_version TEXT NOT NULL
input_reference JSONB NOT NULL DEFAULT '{}'::jsonb
processed_at TIMESTAMPTZ NOT NULL
```

The table also has a check that requires `universe_run_id IS NOT NULL` exactly for `MARKET_UNIVERSE_EQUAL_WEIGHT`, and NULL for the BTC/ETH benchmark rows.

Indexes: `(symbol, timeframe, context_timestamp DESC)`, `(benchmark, timeframe, context_timestamp DESC)`, `(universe_run_id, timeframe, context_timestamp DESC)`, and `(processed_at)`. The two executable partial unique indexes are:

```sql
CREATE UNIQUE INDEX phase5_rs_leader_replay_uq
ON phase5_relative_strength_snapshots
    (symbol, benchmark, timeframe, context_timestamp, calculation_version)
WHERE benchmark IN ('BTCUSDT','ETHUSDT');

CREATE UNIQUE INDEX phase5_rs_market_replay_uq
ON phase5_relative_strength_snapshots
    (symbol, benchmark, timeframe, context_timestamp, universe_run_id, calculation_version)
WHERE benchmark = 'MARKET_UNIVERSE_EQUAL_WEIGHT';
```

### 10.4 `phase5_sector_membership`

Natural key:

```text
(mapping_version, symbol, effective_from)
```

Core columns:

```text
id BIGSERIAL PRIMARY KEY
mapping_version TEXT NOT NULL
symbol TEXT NOT NULL REFERENCES symbols(symbol)
sector TEXT NOT NULL
source_reference TEXT NOT NULL
effective_from TIMESTAMPTZ NOT NULL
effective_to TIMESTAMPTZ
status TEXT NOT NULL CHECK (status IN ('AVAILABLE','NOT_AVAILABLE','ERROR'))
CHECK (effective_to IS NULL OR effective_to > effective_from)
processed_at TIMESTAMPTZ NOT NULL
UNIQUE (mapping_version, symbol, effective_from)
```

`UNKNOWN` is a valid sector value. The loader is idempotent and versioned. Mapping data is not an AI inference and is not embedded as a huge unbounded JSON object.

The loader rejects overlapping effective intervals for the same `(mapping_version, symbol)` before insertion. Migration 010 adds lookup indexes on `(mapping_version, symbol, effective_from DESC)` and `(symbol, effective_from DESC)`; overlap validation is a deterministic loader/contract test because a PostgreSQL CHECK constraint cannot compare neighboring rows.

### 10.5 `phase5_sector_context_snapshots`

Replay identity:

```text
(sector, timeframe, context_timestamp, universe_run_id, mapping_version, calculation_version)
```

Core columns are:

```text
id BIGSERIAL PRIMARY KEY
sector TEXT NOT NULL
timeframe TEXT NOT NULL CHECK (timeframe IN ('15m','1H','4H'))
context_timestamp TIMESTAMPTZ NOT NULL
universe_run_id BIGINT REFERENCES universe_runs(id)
mapping_version TEXT NOT NULL
sector_return_pct NUMERIC
sector_positive_ratio NUMERIC CHECK (sector_positive_ratio IS NULL OR sector_positive_ratio BETWEEN 0 AND 1)
member_count INTEGER NOT NULL CHECK (member_count >= 0)
sample_size INTEGER NOT NULL CHECK (sample_size >= 0)
available_count INTEGER NOT NULL CHECK (available_count >= 0)
missing_count INTEGER NOT NULL CHECK (missing_count >= 0 AND available_count + missing_count = sample_size)
coverage_ratio NUMERIC CHECK (coverage_ratio IS NULL OR coverage_ratio BETWEEN 0 AND 1)
status TEXT NOT NULL CHECK (status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
reason_code TEXT
input_reference JSONB NOT NULL DEFAULT '{}'::jsonb
calculation_version TEXT NOT NULL
processed_at TIMESTAMPTZ NOT NULL
```

The table contains no candidate-specific return or relation. Its lookup and retention indexes are `(sector, timeframe, context_timestamp DESC)`, `(universe_run_id, timeframe, context_timestamp DESC)`, and `(processed_at)` plus:

```text
UNIQUE INDEX phase5_sector_context_replay_uq
ON phase5_sector_context_snapshots
    (sector, timeframe, context_timestamp, COALESCE(universe_run_id, 0), mapping_version, calculation_version)
```

The reserved expression value `0` is not a valid `universe_run_id`. A diagnostic row with `universe_run_id IS NULL` is allowed only when `status='NOT_AVAILABLE'` and `reason_code IN ('MISSING_INPUT','INSUFFICIENT_COVERAGE','NO_ELIGIBLE_MEMBERS')`; a usable sector aggregate must carry a real universe run.

### 10.6 `stage1_phase5_context_enrichment`

Natural key:

```text
(screening_run_id, symbol)
```

Core columns:

```text
id BIGSERIAL PRIMARY KEY
screening_run_id BIGINT NOT NULL
symbol TEXT NOT NULL
FOREIGN KEY (screening_run_id, symbol) REFERENCES screening_results(run_id, symbol) ON DELETE CASCADE
universe_run_id BIGINT REFERENCES universe_runs(id)
sector TEXT NOT NULL DEFAULT 'UNKNOWN'
mapping_version TEXT
candidate_return_pct NUMERIC
sector_return_pct NUMERIC
candidate_vs_sector_pct NUMERIC
sector_relation TEXT NOT NULL CHECK (sector_relation IN ('OUTPERFORMING','UNDERPERFORMING','IN_LINE','NOT_AVAILABLE'))
context_status TEXT NOT NULL CHECK (context_status IN ('AVAILABLE','PARTIAL','STALE','NOT_AVAILABLE','ERROR'))
leader_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb
regime_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb
relative_strength_ref JSONB NOT NULL DEFAULT '{}'::jsonb
sector_context_ref JSONB NOT NULL DEFAULT '{}'::jsonb
reason_code TEXT
processed_at TIMESTAMPTZ NOT NULL
context_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (context_only IS TRUE)
UNIQUE (screening_run_id, symbol)
```

This table is an attachment to an existing `(screening_run_id, symbol)` screening result. The composite foreign key prevents orphan candidates. `context_status` is the semantic Phase 5 result; a row’s existence and `processed_at` mean the context write succeeded, so there is no ambiguous second status column. It does not update `screening_results`, does not alter category/reason/eligibility, and cannot be read by an order or executor path because no such Phase 5 path exists.

Indexes are `(symbol, processed_at DESC)`, `(screening_run_id, processed_at DESC)`, and `(processed_at)` for bounded retention. The existing `screening_runs` parent receives an additive `(run_timestamp)` index in migration 010 solely to support the child cleanup predicate; no parent rows are deleted by Phase 5 retention.

### 10.7 Migration safety

The migration runner must report migration 010 exactly once on first application and `0 new migrations` on subsequent applications. A failed migration must leave no partially accepted Phase 5 runtime state. Migration tests must run against an empty database and an already migrated 001–009 database.

The validation sequence is normative:

1. begin a transaction;
2. create the six Phase 5 tables, constraints, and indexes with additive/idempotent statements;
3. introspect the expected schema contract, including the composite Stage1 foreign key and every unique/partial/expression index;
4. if any check fails, roll back the transaction, verify version 010 is absent, and return a schema-mismatch error;
5. only after all checks pass, insert version 010 into `schema_migrations` and commit.

The test matrix includes: empty database; valid 001–009 database; repeat application; a pre-existing same-named table missing a required column; a wrong status check; a missing composite foreign key; a missing partial unique index; a partially-created object followed by retry; and a transaction failure. Every invalid case must leave version 010 unrecorded and must not drop or alter the pre-existing object.

### 10.8 Database versus contract responsibilities

Migration 010 enforces identity, enum/check values, non-negative counts, ratio ranges, non-overlapping effective endpoints, symbol/universe/screening foreign keys, replay uniqueness, and indexed retention lookups. The application contract tests additionally enforce:

- `NUMERIC` values are finite; prices/close are strictly positive and volume/turnover are non-negative;
- percentage precision is quantized to the configured scale before persistence;
- `volatility_value` is an unannualized per-bar standard deviation decimal;
- `sample_size=0` implies `coverage_ratio IS NULL` and `status='NOT_AVAILABLE'`;
- `AVAILABLE`/`PARTIAL` nullability rules from section 7.4;
- JSON evidence/reference objects are at most `PHASE5_MAX_EVIDENCE_BYTES=65536` bytes, contain only the bounded contract keys, and never contain raw payloads;
- mapping intervals do not overlap for the same version and symbol;
- the Stage1 enrichment row is only written after the composite parent screening result exists.

The division is intentional: SQL protects durable relational invariants; contract tests protect numeric meaning, bounded payload shape, and calculation semantics that are not safely expressed as generic PostgreSQL checks.

## 11. Retention and storage policy

Retention is configuration, not a hard-coded universal 90-day rule:

```text
PHASE5_CONTEXT_RETENTION_5M_DAYS=30
PHASE5_CONTEXT_RETENTION_15M_DAYS=90
PHASE5_CONTEXT_RETENTION_1H_DAYS=180
PHASE5_CONTEXT_RETENTION_4H_DAYS=365
PHASE5_ENRICHMENT_RETENTION_DAYS=30
```

These are conservative defaults for the constrained server and must be overrideable. Regime, RS, sector, and leader rows use their timeframe retention. Sector membership is versioned and retained while referenced; it is not deleted by ordinary context cleanup. Stage1 enrichment has its own 30-day retention and is deleted in bounded batches by the parent `screening_runs.run_timestamp`; it does not delete the parent screening history.

The scheduling contract writes one row only when a new closed context timestamp is observed. At a five-minute engine cadence, the theoretical upper bounds are approximately 828 leader rows/day and 828 regime rows/day for the four timeframes, up to 50,400 RS rows/day for 200 candidates at 15m/1H/4H, and 57,600 enrichment rows/day if every 5m run enriches 200 screening symbols. These are upper-bound estimates, not measured runtime growth; later acceptance must report measured samples separately. Retention tests must exercise the upper-bound batch path and ensure cleanup remains indexed and bounded.

Cleanup is batched by timeframe/timestamp using indexed columns, records health metrics, and never runs as an unbounded table scan. Raw klines remain governed by the existing configurable Phase 1 retention policy.

## 12. Stage1 integration boundary

The Phase 5 engine may run after a Stage1 screening run and write one context-enrichment row per `(screening_run_id, symbol)`. The sequence is:

```text
Stage1 computes immutable A/B/C/D
        -> Phase5 computes descriptive context
        -> Phase5 writes context-only enrichment
```

The following must remain byte-for-byte/semantically unchanged by Phase 5:

- Stage1 category;
- Stage1 eligibility and capacity guard;
- Stage1 reason and reason codes;
- screening result inputs used for the A/B/C/D decision;
- any live/order capability (none is permitted).

Tests must compare Stage1 output with Phase 5 disabled and enabled for identical inputs and require equality.

The comparison is over the complete immutable result contract: `category`, `reason`, `reason_codes`, `inputs_used`, `key_metrics`/indicators, structure, status, and symbol/run identity. The Phase 5 context reader is not permitted in the Stage1 eligibility query or evaluator call graph; a static import check and a runtime test with deliberately conflicting context must prove that context cannot influence A/B/C/D.

## 13. Reliability and idempotency

- Natural-key upserts make a repeated context cycle idempotent.
- Restart resumes from canonical timestamps and does not duplicate derived rows.
- A repeated migration is safe.
- A duplicate universe run is not substituted for the original run during replay.
- PostgreSQL outages produce bounded retry/backoff and health events; they must not create an unbounded crash loop or fabricate `AVAILABLE` rows.
- A calculation exception is stored/reported as `ERROR` with a bounded reason and does not become `MIXED`.
- Input status and timestamp evidence are stored with each output for reproducibility.
- The engine uses bounded per-symbol windows, bounded batch sizes, and no unbounded in-memory history.

## 14. Resource and deployment constraints

Use the existing `quant-postgres`, `quant-collector`, and `quant-engine` services only. No Redis, Kafka, extra worker, or separate Phase 5 service is justified.

Target ceilings for Phase 5 acceptance:

```text
PostgreSQL:     768 MiB
quant-collector: 256 MiB
quant-engine:    384 MiB
```

Implementation controls:

- collector remains bounded and does not cache Phase 5 windows;
- engine reads only the minimum closed-bar window per symbol/timeframe;
- batch database reads/writes use explicit limits;
- derived JSON evidence is bounded and excludes raw payloads;
- context retention is indexed and configurable;
- no all-symbol/all-history load is allowed;
- PostgreSQL indexes are limited to natural-key lookup, timestamp lookup, and retention cleanup.

The current local compose file is `512/512/384 MiB` for PostgreSQL/collector/engine. It is a documented baseline mismatch. Runtime acceptance must either update the local profile to the Phase 5 target as a separate reviewed change or explicitly record why the tested profile differs; no acceptance may claim the target ceiling was tested when it was not.

## 15. Test-first implementation plan

Each implementation task follows:

```text
TEST -> FAIL -> IMPLEMENT -> PASS -> REVIEW -> FIX -> RETEST -> COMMIT
```

Required test groups:

1. **Contract tests:** enums, required fields, UTC, calculation version, status/reason mapping, no raw payload duplication.
2. **Closed-bar/alignment tests:** 5m/15m/1H/4H expected-closed-bar logic, missing interval, grace, timestamp skew, open-bar rejection.
3. **Leader context tests:** return, EMA/trend, structure mapping, volatility, volume, BTC/ETH symbol resolution.
4. **Breadth tests:** fixed universe snapshot, available/missing/coverage counts, missing-not-zero, threshold boundaries, no survivorship substitution.
5. **Regime tests:** independent direction/volatility/breadth states and support/conflict/missing evidence.
6. **Relative-strength tests:** BTC/ETH/market benchmarks, 15m/1H/4H, 5m absence by design, threshold boundaries, unequal availability.
7. **Sector tests:** versioned static mapping, `UNKNOWN`, sample/coverage thresholds, no AI/network classifier, candidate-vs-sector arithmetic.
8. **Persistence tests:** migration 010 empty DB, repeated migration, natural-key idempotency, all timestamps UTC, retention cleanup bounded.
9. **Stage1 boundary tests:** Phase5 on/off produces identical Stage1 categories and decisions; enrichment is context-only.
10. **Reliability tests:** restart, PostgreSQL outage, retry bound, duplicate cycle, stale input, calculation error.
11. **Resource tests:** target memory ceilings, bounded query/window sizes, database growth sample, no duplicate raw kline table.
12. **Safety/regression tests:** no private API/order/live executor imports, Phase 1–4 regression, Bybit gate remains an external debt rather than silently changing.

## 16. Acceptance criteria for a future Phase 5 implementation

Phase 5 cannot be called accepted unless all of the following are demonstrated locally:

- branch is derived from the exact Phase 4 base without rewriting Phase 4 history;
- migration 010 applies from empty and existing 001–009 databases and repeats with zero new migrations;
- BTC/ETH contexts exist only for real canonical closed-bar inputs and use interval-aware freshness;
- missing/partial/stale/error statuses and evidence are correct;
- regime dimensions are independent and breadth includes point-in-time coverage counts;
- RS exists only at 15m/1H/4H and uses aligned BTC/ETH/market benchmarks;
- sector membership is static/versioned, with explicit `UNKNOWN` behavior;
- no raw kline duplication occurs;
- Stage1 output is identical with and without context enrichment;
- restart/idempotency and PostgreSQL persistence pass;
- target resource ceilings are measured, not inferred;
- all Phase 1–4 regression and safety tests pass, with the known Bybit external gate still clearly reported;
- no private API, order API, live executor, AI, Phase 6, or remote ECS operation is introduced;
- a Phase 5 completion report records actual rows, timestamps, database size, logs, resource usage, skipped tests, and known debt.

The final runtime state, if all future gates pass, may be `PHASE5_LOCAL_RUNTIME_ACCEPTED`. This design document alone does not grant that state.

## 17. Independent-review checklist

The reviewer must independently check:

- no conflict with Phase 1–4 canonical contracts or migration ownership;
- source status versus Phase 5 derived `PARTIAL` semantics;
- closed-bar and UTC alignment correctness;
- breadth sample/coverage/missing logic and survivorship safety;
- relative-strength benchmark and 5m decision;
- sector `UNKNOWN` and static mapping behavior;
- migration 010 additive/idempotent safety;
- Stage1 non-interference;
- resource limits and bounded persistence;
- explicit out-of-scope protections and Bybit debt preservation.

Implementation is blocked until the independent review is complete and all blocker findings are resolved.
