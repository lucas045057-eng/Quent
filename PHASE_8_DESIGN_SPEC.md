# Phase 8 Options Market Context — Design Specification

**Status:** PHASE8_DESIGN_APPROVED_WITH_AMENDMENTS; approved for TDD implementation
**Baseline:** Phase 7 freeze HEAD beb637acb22b1ced14f496cc0618101dfa197edf; branch phase8
**Scope:** Deribit BTC/ETH options market context only

## 1. Goal, boundary, and non-goals

Phase 8 adds a read-only options-data pipeline and persists descriptive options context for later consumers. It does not change the meaning, eligibility, ranking, or output of Stage 1.

The Phase 8 package may publish descriptive context such as “put/call OI ratio unavailable due to incomplete coverage.” It must never publish BUY, SELL, LONG, SHORT, order intent, or an executable instruction. It must not call RiskEngine or Executor and must not alter Phase 1–7 decisions.

No private Deribit API, API key, account data, trading route, position route, live executor, dealer GEX, or AI integration is in scope. No IV/Greek value is fabricated or computed to fill a source gap.

## 2. Architecture and ownership

Flow:

Deribit public REST + public WebSocket
→ Phase8 Deribit adapters
→ canonical instrument / observation contracts
→ bounded in-memory current state
→ additive PostgreSQL persistence
→ event-time options context calculations
→ Phase8 context snapshots only

- The collector owns the Phase8 runtime lifecycle: start, readiness, bounded work, health reporting, cancellation, and shutdown. It is disabled by default and runs only when Phase8 is explicitly enabled.
- Deployment reuses the existing quant-collector and PostgreSQL services/database. Compose forwards Phase 8 settings only to the existing collector; no new container, Redis/Kafka service, database instance, or engine wiring is introduced. The existing PostgreSQL/Collector/Engine memory caps remain unchanged.
- The engine, Stage 1, RiskEngine, Executor, Phase 1–7 schemas, and their eligibility rules are unchanged.
- REST and WebSocket payloads terminate at version-specific source adapters. Downstream code receives only canonical Phase8 models; it must not read provider JSON fields directly.
- Full-chain REST snapshots supply chain-wide OI/24-hour volume and reconcile mark/quote/underlying fields. Full-chain `markprice.options.{index_name}` WebSocket channels own latest-state mark price and IV for every option under the validated index. Incremental ticker is subscribed only for the configured bounded universe and supplies source-provided Greeks and other per-instrument ticker detail.
- `index_name` is not hardcoded in business logic: derive distinct `price_index` values from validated active option instruments, validate them against the official `public/get_index_price_names` response, and construct the channel in one adapter-owned formatter. The current production contract probe found only `btc_usd` on the 998 BTC options and only `eth_usd` on the 854 ETH options; these are measured source values, not immutable constants.
- Sparse `markprice.options` and `incremental_ticker` messages update per-instrument latest state field-by-field. Omitted fields mean unchanged, never missing or zero. Preserve each field's exact raw value, field-last-update/source timestamp, receive timestamp, unit/status metadata, and provenance. On reconnect or loss of local state trust, discard that source/index state and require a new complete initial feed snapshot before making it available.

## 3. Official source contract

Production endpoints:

- REST base: https://www.deribit.com/api/v2
- WebSocket: wss://www.deribit.com/ws/api/v2
- These public REST methods and WebSocket channels require no API key, secret, passphrase, or private authentication.
- Public REST methods use JSON-RPC 2.0. Phase 8 will use POST with a request body, assert the returned JSON-RPC id and requested currency, and reject mismatched response data. This avoids the observed parameter-insensitive cached GET result during the source probe; GET itself remains a documented API method.

### REST responsibilities

1. Instruments: POST /api/v2/public/get_instruments with currency BTC or ETH and kind option.
   - Startup seeding: once per currency, serialized.
   - Periodic full reconciliation default: every 24 hours.
   - Immediate re-seed after a WebSocket lifecycle outage longer than the configured 300-second threshold.
   - Do not poll frequently to discover listings; lifecycle WebSocket channels are primary.
   - Validate result rows have the requested base/settlement currency semantics, option kind, unique instrument_name, valid option_type/strike/expiration, and documented state/is_active fields.
2. Index metadata: POST /api/v2/public/get_index_price_names with `extended=false` during startup seed and full catalog reconciliation. Validate the documented simple string-list response; do not use a static BTC/ETH channel-name mapping. For each currency, derive the bounded unique `price_index` set from active option instruments, require every value to appear in this official supported-name response, then build `markprice.options.{index_name}` channels. If the source instrument index and supported-name set disagree, fail closed for that currency and report a sanitized contract mismatch.
3. Full-chain reconciliation: POST /api/v2/public/get_book_summary_by_currency with currency BTC or ETH and kind option.
   - Default cadence: once per hour per currency, sequentially through the shared REST limiter.
   - Enforce both per-underlying and combined full-chain row caps. If either cap is exceeded, reject the whole cycle, mark it PARTIAL/degraded, and do not persist a truncated subset as a complete snapshot.
   - Canonicalize volume as 24-hour base-currency volume and option open_interest as underlying base-coin amount, per official docs.
   - Null fields (including bid/mid values when absent) remain null with NOT_AVAILABLE/PARTIAL status; never substitute zero.

### WebSocket responsibilities

One public WebSocket connection owns four lifecycle channels:

- instrument.creation.option.BTC
- instrument.state.option.BTC
- instrument.creation.option.ETH
- instrument.state.option.ETH

The creation notification carries full instrument data including a source timestamp; the state notification carries timestamp, state, and instrument_name. Lifecycle events update the catalog and trigger a deterministic re-evaluation of only the bounded ticker universe.

Lifecycle states include open, settlement, delivered, inactive, locked, halted, and archivized. A state change away from open immediately removes the instrument from ticker eligibility and schedules an unsubscribe; only an explicit later eligible state can add it again.

The same public connection subscribes to one `markprice.options.{index_name}` channel for each validated, distinct active-option `price_index`, subject to the configured maximum channel count. The current validated names are `btc_usd` and `eth_usd`; production code must derive them from `get_instruments.price_index` and cross-check with `public/get_index_price_names`. The official data entry fields are `instrument_name`, `mark_price`, `iv`, and millisecond `timestamp`. The feed begins with all prices and then sends changes only; represent that first complete event as the index seed, and apply subsequent entries as sparse changes. `iv` is a source-provided raw volatility field whose unit remains unverified. Preserve the exact Decimal token (including the source's documented four-decimal rounding behavior) without any additional conversion/rounding. A REST summary value is reconciliation evidence and must not silently fill a missing WebSocket mark/IV state.

For each selected option, subscribe to incremental_ticker.{instrument_name}. The first notification is a full snapshot; subsequent notifications are sparse change messages. An omitted field means “not changed” only for a change event. An explicit null remains missing. The adapter merges by field while preserving each field’s source timestamp and status. The feed is at most one update per second per instrument.

No full-chain per-instrument ticker subscription is allowed. No raw book, trades, account/private channels, or unrelated broad channel is added in V1. Subscription requests are serialized and bounded: lifecycle plus markprice channels first, then ticker subscriptions in configured batches paced below the official public/subscribe rate.

### Source-to-canonical field map

| Source field | Canonical field | Rule |
|---|---|---|
| instruments.instrument_name | symbol | Exact provider instrument identifier |
| instruments.instrument_id | provider_instrument_id | Preserve provider integer; symbol remains the natural key |
| instruments.base_currency / quote_currency / settlement_currency / price_index | underlying and source unit references | Preserve verbatim; never infer USD when the source index says otherwise |
| instruments.option_type / strike | option_type / strike | call or put; strike is exact Decimal in the instrument’s quote/index reference |
| instruments.expiration_timestamp | expires_at | Source epoch milliseconds converted to UTC |
| instruments.creation_timestamp | instrument_created_at | Instrument attribute, not observation time |
| instruments.state / is_active | instrument_state / is_active | Only state=open and is_active=true may enter the ticker universe |
| summary.volume | volume_24h_raw | 24-hour base-currency volume, unit recorded as source base currency |
| summary.open_interest | open_interest_raw | Option open interest in underlying base coin |
| summary.mark_price, bid_price, ask_price, mid_price, last | corresponding raw price metrics | Nullable source values; retain quote/index reference |
| summary.creation_timestamp | source_creation_timestamp_ms | Preserve the exact source attribute as Unix milliseconds; it is not a reliable per-row market event time and must not replace `exchange_timestamp` or local `fetched_at` freshness. |
| summary.underlying_price / underlying_index | underlying_price_raw / summary_underlying_index | Preserve both values independently. For options, the documented `underlying_index` may be the generic sentinel `index_price`; it is not `instruments.price_index` and must not be used as a markprice channel name or compared for equality with that field. The validated instrument `price_index` remains a separate reference. |
| summary.mark_iv | rest_mark_iv_raw | Preserve as SOURCE_PROVIDED and unit-unverified; not a substitute for the V1 WS ticker IV input |
| supported-index response result | supported_index_names | Exact official list used only to validate instrument `price_index`; never infer a channel name from currency |
| instruments.price_index | markprice index membership | Preserve exact value; eligible distinct values are cross-checked against supported_index_names before channel construction |
| markprice entry.instrument_name / mark_price / iv / timestamp | full-chain mark/IV latest-state fields | Exact raw Decimal values; `timestamp` is per-entry epoch milliseconds; omitted instruments/fields in a change do not clear existing state |
| ticker.timestamp / ticker.type | event timestamp / snapshot-or-change kind | Timestamp is source epoch ms; type determines full replace versus sparse merge |
| ticker.mark_iv / bid_iv / ask_iv | corresponding raw IV fields | SOURCE_PROVIDED, exact Decimal, unit-unverified until verified |
| ticker.greeks.delta / gamma / theta / vega / rho | corresponding raw Greek fields | SOURCE_PROVIDED, exact Decimal, per-field unit status |
| ticker.open_interest / ticker.stats.volume | ticker OI / volume fields | Preserve units and provenance; full-chain ratios use REST summary as their declared source |

An unrecognized field is retained only in a bounded, sanitized raw_reference/fixture path and does not become a business input until a reviewed contract change.

## 4. Measured universe and configurable bounds

The source baseline measured 998 open BTC options and 854 open ETH options (1,852 total). At the sampled underlying prices, the 90-day, ±5% candidate pools were BTC 202 and ETH 154. The complete expiry/strike distribution and probe evidence are in PHASE_8_BASELINE_AUDIT.md.

Proposed conservative defaults, all configurable:

| Setting | Default | Meaning |
|---|---:|---|
| PHASE8_OPTIONS_MAX_EXPIRY_DAYS | 90 | Maximum days from current UTC time for per-instrument ticker eligibility |
| PHASE8_OPTIONS_MAX_ABS_MONEYNESS_PCT | 5 | Absolute strike distance from latest accepted source underlying price |
| PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY | 64 | Hard selected-symbol cap for BTC or ETH |
| PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL | 128 | Cross-currency hard cap |
| PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING | 2048 | Maximum rows accepted for BTC or ETH in one full-chain cycle |
| PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL | 4096 | Maximum combined rows accepted for a two-underlying full-chain cycle |
| PHASE8_OPTIONS_MAX_MARKPRICE_CHANNELS_TOTAL | 4 | Hard bound for source-derived full-chain markprice index channels |

Universe filter order:

1. Require kind=option, underlying BTC/ETH, option_type call/put, state=open, is_active=true, valid positive strike, and unexpired UTC expiry.
2. Require expiry within configured horizon and absolute moneyness within configured band. Use the canonical source-provided `underlying_price` value without unit conversion; if a verified event timestamp exists, freshness is age-based on that timestamp. `public/get_book_summary_by_currency` does not provide a reliable per-row event time, so keep `exchange_timestamp` NOT_PROVIDED and assess its recency only from `fetched_at` against `PHASE8_OPTIONS_CHAIN_STALE_AFTER_SECONDS` (5,400 seconds). Never promote `creation_timestamp` to an event timestamp. Publish the time basis and age in the universe result; missing, future, nonpositive, or stale values cannot create a new selection.
3. Group by UTC expiry. For every eligible expiry, reserve the nearest listed call and nearest listed put to the current source underlying price (ATM pair), with ties broken by strike then instrument_name. If two-per-expiry reservations cannot fit under the per-currency cap, return PARTIAL with no new ticker selection for that underlying; do not silently drop expiry buckets.
4. With remaining slots, round-robin by expiry and side to add the nearest out-of-the-money call wing (strike above underlying) and put wing (strike below underlying); then continue outward in deterministic strike order, alternating sides per expiry. Use expiry timestamp, option side, absolute log-moneyness, strike, and instrument_name as stable tie-break keys. Never exceed per-currency/global caps.
5. The 64-instrument selection is intentionally a bounded sample, so publish selected/eligible counts and coverage status; it is not a complete-chain ticker subscription. Full-chain row-cap overflow is different: reject the entire full-chain cycle as PARTIAL/degraded with no silent truncation. If the required source index/underlying price is missing or stale, do not make a new selection from stale price; retain the previous bounded set only while its own configured validity permits, otherwise unsubscribe and mark the universe unavailable.

The 64-per-currency cap supports up to three nearby call and three nearby put strikes across the ten expiries observed inside 90 days, with a small slot margin. It does not guarantee 25-delta coverage. A source growth/cap breach is visible and fail-closed; operators may change only configuration after reviewing measured resource and coverage evidence.

## 5. Refresh, persistence, and bounded-work defaults

| Work | Config key | Default / hard bound | Purpose |
|---|---|---:|---|
| Instruments REST reconciliation | PHASE8_OPTIONS_INSTRUMENT_REFRESH_SECONDS | 86,400 seconds, plus startup and recovery | Full catalog repair; lifecycle WS remains primary |
| Instruments re-seed after outage | PHASE8_OPTIONS_RESEED_AFTER_OUTAGE_SECONDS | 300 seconds | Repair after extended lifecycle gap |
| Instrument lifecycle WS | Fixed channels | 4 channels | Creation/state changes |
| Full-chain summary REST | PHASE8_OPTIONS_CHAIN_SNAPSHOT_INTERVAL_SECONDS | 3,600 seconds; max 2,048 rows per underlying and 4,096 total | OI/volume/mark reconciliation snapshot |
| Full-chain markprice WS | Derived index set; PHASE8_OPTIONS_MAX_MARKPRICE_CHANNELS_TOTAL | At most 4 validated channels; full seed then changes | Whole-chain mark/IV latest state |
| Markprice state persistence | PHASE8_OPTIONS_MARKPRICE_SNAPSHOT_INTERVAL_SECONDS | 900 seconds; max 4,096 rows per coalesced cycle | Full-chain mark/IV snapshots for context/replay |
| Incremental ticker WS | Universe cap settings in Section 4 | At most 128 selected symbols total | Source ticker/IV/Greeks |
| Ticker state persistence | PHASE8_OPTIONS_TICKER_SNAPSHOT_INTERVAL_SECONDS | 900 seconds; one coalesced row per selected symbol | Bounded current-source snapshots, not every tick |
| Context calculation/persistence | PHASE8_OPTIONS_CONTEXT_INTERVAL_SECONDS | 900 seconds per underlying | Descriptive metrics with independent quality/coverage |
| REST response body | PHASE8_OPTIONS_MAX_REST_RESPONSE_BYTES | 8 MiB default; hard ceiling 32 MiB | Reject oversized responses before unbounded allocation |
| REST request scheduling | PHASE8_OPTIONS_REST_MIN_INTERVAL_SECONDS / PHASE8_OPTIONS_REST_TIMEOUT_SECONDS | 1.25-second minimum interval / 10-second timeout | At most one in-flight request across currencies |
| REST retry cooldown | PHASE8_OPTIONS_REST_MAX_COOLDOWN_SECONDS | 300 seconds maximum | Honor Retry-After but cap local wait |
| WS message queue | PHASE8_OPTIONS_WS_QUEUE_MAX_MESSAGES / PHASE8_OPTIONS_WS_MAX_QUEUED_BYTES | 256 messages / 16 MiB total | Backpressure; overflow marks degraded and forces resync |
| Individual WS frame | PHASE8_OPTIONS_WS_MAX_MESSAGE_BYTES | 1 MiB default; 4 MiB hard ceiling | Allows bounded full-chain markprice seed; reject oversized frame before parsing |
| REST retry attempts | PHASE8_OPTIONS_REST_MAX_ATTEMPTS | 3 maximum | Bounded transient retry |
| WS subscribe pacing | PHASE8_OPTIONS_WS_SUBSCRIBE_MIN_INTERVAL_SECONDS / PHASE8_OPTIONS_WS_SUBSCRIBE_BATCH_SIZE | ≥0.35 seconds / 32 channels | Bounded request size and below public subscribe rate |
| WS reconnect burst | PHASE8_OPTIONS_WS_MAX_RECONNECT_ATTEMPTS / PHASE8_OPTIONS_WS_RECONNECT_MAX_DELAY_SECONDS | 8 attempts / 60-second delay cap | Bounded reconnect burst and cooldown |
| Shutdown deadline | PHASE8_OPTIONS_SHUTDOWN_TIMEOUT_SECONDS | 10 seconds | Cancel/await all owned tasks within a bound |
| Provisional snapshot retention | PHASE8_OPTIONS_SNAPSHOT_RETENTION_DAYS | 7 days | Candidate policy for REST, markprice, and ticker observations |
| Provisional context retention | PHASE8_OPTIONS_CONTEXT_RETENTION_DAYS | 30 days | Candidate policy for compact derived history |
| Provisional lifecycle retention | PHASE8_OPTIONS_LIFECYCLE_RETENTION_DAYS | 90 days | Candidate policy for instrument-change audit |
| Retention enforcement | PHASE8_OPTIONS_RETENTION_ENFORCEMENT | false | Do not automatically delete real collected data during Phase 8 Feature Acceptance |
| Retention delete batch | PHASE8_OPTIONS_RETENTION_DELETE_BATCH_ROWS | 1,000 rows | Bounded cleanup transaction |
| Chain freshness | PHASE8_OPTIONS_CHAIN_STALE_AFTER_SECONDS | 5,400 seconds | Mark last full-chain observation stale |
| Ticker metric freshness | PHASE8_OPTIONS_TICKER_STALE_AFTER_SECONDS | 300 seconds per changed field | Sparse ticker fields age independently |
| Markprice metric freshness | PHASE8_OPTIONS_MARKPRICE_STALE_AFTER_SECONDS | 900 seconds per changed field | Sparse full-chain mark/IV fields age independently |
| Catalog freshness | PHASE8_OPTIONS_CATALOG_STALE_AFTER_SECONDS | 93,600 seconds | Require catalog reconciliation |
| Lifecycle health | PHASE8_OPTIONS_LIFECYCLE_STALE_AFTER_SECONDS | 60 seconds without healthy heartbeat | Connection health; event silence alone is not stale |
| Cross-source timestamp skew | PHASE8_OPTIONS_MAX_SOURCE_SKEW_SECONDS | 5,400 seconds | Prevent incoherent context joins |
| Full-chain coverage | PHASE8_OPTIONS_MIN_CHAIN_COVERAGE | 0.95 | OI/volume context gate |
| Ticker/IV coverage | PHASE8_OPTIONS_MIN_TICKER_COVERAGE | 0.90 | Bounded IV/context gate |
| Concentration output | PHASE8_OPTIONS_CONCENTRATION_TOP_N | 20 buckets | Bound emitted expiry/strike buckets |
| Skew sample | PHASE8_OPTIONS_SKEW_MIN_CONTRACTS / PHASE8_OPTIONS_SKEW_MIN_DISTINCT_STRIKES | 6 contracts / 3 strikes | Minimum source-IV fit coverage |

At the measured inventory, 1-hour REST summaries imply 44,448 per-instrument rows/day and 311,136 in seven days. Fifteen-minute full-chain markprice snapshots imply 177,792 rows/day and 1,244,544 in seven days at the measured 1,852 instruments; at the configured 4,096-row cycle cap the projection is 393,216 rows/day and 2,752,512 in seven days. Fifteen-minute ticker persistence implies at most 12,288 selected-symbol rows/day and 86,016 in seven days. Context adds at most 192 underlying rows/day. These are projections, not observed database growth. Retention durations are provisional and enforcement defaults off; Phase 8 Feature Acceptance must not automatically delete real collected data. Formal retention budget/enforcement and sustained storage growth remain for Data Layer V1 Hardening; do not run an unattended real collector while enforcement is disabled.

Retention cleanup has a Phase 8-only, bounded-delete contract and additive query indexes, but is neither scheduled nor called unless `PHASE8_OPTIONS_RETENTION_ENFORCEMENT=true`; the default is false. Tests may exercise the opt-in cleanup only against an isolated disposable database. Cleanup can never target or rewrite Phase 1–7 rows. Raw source responses are not written into application logs. Persistent raw payload storage is not enabled by default: canonical fields and a SHA-256 raw_reference are stored; deterministic sanitized fixtures are kept under tests/fixtures/phase8.

## 6. Canonical data contract

All canonical timestamps are timezone-aware UTC. Provider epoch milliseconds are parsed as integers and converted to UTC without float arithmetic. Decimal market values are parsed from JSON numeric tokens directly into Decimal and persisted as PostgreSQL NUMERIC or exact JSONB numerics; binary float conversion and scale rounding are forbidden.

### Common observation envelope

| Field | Contract |
|---|---|
| symbol | Provider instrument_name, e.g. BTC-...-C/P; required for contract observations |
| value | Exact Decimal raw source value, or null; metadata and availability are represented separately |
| metric | Canonical metric key (open_interest, volume_24h, mark_price, mark_iv, delta, gamma, theta, vega, rho, etc.) |
| source | Stable source identifier deribit |
| exchange | Stable venue identifier DERIBIT |
| source_method/channel | Exact public method or WebSocket channel family |
| source_field | Original provider JSON path, preserved exactly |
| exchange_timestamp | Nullable timezone-aware UTC source time; never replaced with local time |
| field_last_updated_at | Per-field UTC time of the latest source event that actually contained this field; sparse omission does not change it |
| timestamp_semantics | VERIFIED, UNVERIFIED, or NOT_PROVIDED plus source field name |
| fetched_at | Time the local adapter completed receiving a REST response, UTC; null/not-applicable for WebSocket observations |
| received_at | Time the local adapter received a WebSocket message, UTC; null/not-applicable for REST observations |
| processed_at | Time canonical validation/processing completed, UTC |
| unit_code | Verified unit identifier, or null when unknown |
| unit_status | VERIFIED, SOURCE_NATIVE_UNVERIFIED, NOT_APPLICABLE, or UNKNOWN |
| status | AVAILABLE, STALE, NOT_AVAILABLE, PARTIAL, or ERROR |
| provenance | SOURCE_PROVIDED or COMPUTED; Phase 8 V1 market IV/Greeks are SOURCE_PROVIDED only |
| raw_reference | Optional bounded SHA-256/reference identifier; never a credential-bearing URL |
| quality_reason | Stable non-secret reason code, such as MISSING_FIELD, UNIT_UNVERIFIED, SOURCE_TIME_UNVERIFIED, CAP_EXCEEDED |

### Canonical entities and persistence

1. OptionInstrument: exchange, instrument_name, provider instrument_id, underlying, option_type, strike Decimal, expiration_timestamp, instrument creation time, state, is_active, price_index, quote/base/settlement currency, source/source_field mapping, exchange_timestamp, fetched_at, processed_at, status, raw_reference.
2. OptionMarketObservation: observation_id, exchange, symbol, underlying, instrument-associated `price_index` (nullable for non-instrument events), separately preserved source `underlying_index` (nullable), quote-currency reference, observation_kind (REST_CHAIN_SUMMARY, WS_MARKPRICE_SNAPSHOT/CHANGE, or WS_INCREMENTAL_TICKER_SNAPSHOT/CHANGE), exact source timestamp metadata, REST `fetched_at` or WS `received_at`, `processed_at`, status, schema_version, typed Decimal metric values and per-field metadata (source_field, unit, unit_status, status, provenance, field_last_updated_at, exchange_timestamp, local capture time), raw_reference. A summary `creation_timestamp` is retained as `source_creation_timestamp_ms` with its source field and `unix_ms` unit metadata; it is not treated as `exchange_timestamp`.
3. OptionInstrumentEvent: instrument identity, creation/state event type, state, source timestamp, fetched_at, processed_at, idempotency hash and sanitized raw_reference.
4. OptionContextSnapshot: underlying, `context_timestamp` (UTC evaluation time), processed_at, calculation_version, metric values (nullable), per-metric source_timestamp(s), source `fetched_at` and/or `received_at`, `data_age`, coverage counts/ratio, status, reason, input observation identifiers, source-time quality, unit contract version, and provenance.

Required additive tables in migration 015:

- phase8_option_instruments: current catalog keyed by (exchange, instrument_name); inactive/expired rows are soft-retired, never silently erased.
- phase8_option_instrument_events: append-only bounded lifecycle history with idempotent event identity.
- phase8_option_market_snapshots: bounded per-instrument observations; unique dedup key includes exchange, instrument, observation kind, source timestamp and canonical payload hash.
- phase8_option_context_snapshots: immutable calculation outputs keyed by underlying, as_of, and calculation_version.

The metric-value representation is schema-versioned and validated at the Python contract boundary. Each numeric field is a PostgreSQL NUMERIC value (or JSONB numeric) plus explicit per-field metadata. No provider raw field may be consumed by context code before adapter canonicalization. Migration 015 is additive; migrations 001–014 remain byte-for-byte unchanged.

## 7. Units, precision, provenance, and missingness

- Official docs identify option OI in underlying base coin and 24-hour volume in base currency. Persist these units as BTC or ETH according to the instrument; ratios are dimensionless only after same-underlying/same-unit validation.
- Prices retain the source instrument quote/index reference rather than assuming USD.
- mark_iv, bid_iv, ask_iv, and each Greek retain raw Decimal, exact source field, source timestamp, source, unit_code=null until the unit contract is verified, and unit_status=SOURCE_NATIVE_UNVERIFIED.
- `markprice.options` exposes `iv` (not `mark_iv`) with an official description as underlying volatility; that wording does not establish its numeric scale. Retain it as a raw source value with source_field `data[].iv`, source timestamp, and `SOURCE_NATIVE_UNVERIFIED`; never assume it is 0–1 or percentage points.
- No IV percent conversion, Greek scale conversion, or unit-dependent context calculation is permitted while the relevant unit contract is unverified. Do not infer units from observed magnitudes.
- V1 accepts SOURCE_PROVIDED values only. The model reserves COMPUTED as a provenance enum for future use, but no missing source value is filled by calculation in Phase 8 V1.
- Absent key, explicit null, parse failure, stale observation, and unsupported unit have distinct reason/status metadata. None means numeric zero.

## 8. Freshness and event-time rules

Configurable defaults:

| Input | Stale rule |
|---|---|
| Full-chain REST snapshot | STALE if fetched_at is older than 5,400 seconds (1.5 hourly cycles) |
| Full-chain markprice field | STALE if that field's source timestamp is older than 900 seconds; a sparse change does not refresh fields/instruments that were absent |
| Incremental ticker metric | STALE if its own last source timestamp is older than 300 seconds; a sparse change does not refresh fields that were not present |
| Full instrument catalog | STALE if last successful fetch/reconciliation is older than 93,600 seconds (26 hours) |
| Lifecycle WebSocket | STALE if heartbeat/connection health fails for 60 seconds; absence of a business event alone is not a failure |

Every value keeps distinct times: provider `exchange_timestamp`, REST `fetched_at` or WebSocket `received_at`, local `processed_at`; sparse stream values additionally keep per-field `field_last_updated_at`. Provider epoch milliseconds are parsed as integers and converted to UTC without float arithmetic. For instrument records, creation_timestamp is an instrument attribute and must not be used as exchange_timestamp for the REST listing. The current REST summary contract does not provide a reliable per-row quote/snapshot event timestamp: do not mislabel `creation_timestamp` as one. Store the collection's exact `fetched_at` as the REST snapshot/capture time, set exchange_timestamp null with NOT_PROVIDED semantics, and compute OI/volume `data_age` from that actual snapshot capture time. For each sparse WS update, retain both its provider source timestamp and local `received_at`; a later context calculation cannot replace either. Context output must expose this distinction rather than assigning freshness from the 15-minute context calculation time.

At context as_of T, the calculation may use only observations whose local capture (`fetched_at` or `received_at`) is no later than T and whose verified `exchange_timestamp` is no later than T. Future timestamps are rejected. If source time is absent or semantics are unverified, the observation can be used only under knowledge-time (local capture time) semantics, and every dependent context is marked PARTIAL with source-time quality degraded; it cannot be backdated. A configurable maximum source-time skew defaults to 5,400 seconds. Metrics needing tighter alignment must configure a stricter limit.

No historical context row is recomputed with a later-arriving observation. Late/out-of-order events are persisted as evidence if valid but cannot overwrite newer state or alter an already finalized as_of context snapshot.

## 9. Context formulas and coverage policy

The full-chain OI/volume contexts use all accepted rows in the latest full-chain REST snapshot. Mark/IV contexts use full-chain `markprice.options` state; Greek and other bounded ticker contexts use only selected incremental-ticker state. Each metric has its own status and coverage; one complete metric never upgrades another metric from missing to available.

- Put/Call OI ratio: sum available put OI divided by sum available call OI over the accepted latest full-chain REST summary for one underlying. Requires per-side chain metric coverage at least 95%, common verified base-coin unit, and positive call denominator. Missing rows are excluded from the sum and reduce coverage; denominator zero yields NOT_AVAILABLE, not infinity. Preserve the REST cycle `fetched_at`; the 15-minute context timestamp never refreshes the OI source age.
- Put/Call 24h volume ratio: same formula using source 24-hour volume and base-currency unit; requires per-side coverage at least 95% and positive call volume. Preserve that same REST source snapshot time and source-time quality.
- Expiry concentration: per-expiry available OI divided by total available OI over the same chain snapshot; requires at least 95% chain OI coverage and positive total OI. Emit configured top-N expiries plus coverage metadata.
- Strike concentration: aggregate available OI by exact strike across expiries and option sides, then divide by total accepted OI; requires at least 95% OI coverage. Emit configured top-N strikes, with all denominators and count coverage recorded.
- Mark/IV context freshness and coverage: use the latest-state full-chain `markprice.options` observations, never REST summary or bounded ticker as an implicit fallback. Each field retains its own source `timestamp` and receive time. A 15-minute calculation may use only fields whose field age is within the markprice freshness bound; expose actual source timestamps/data_age and mark coverage.
- ATM IV per expiry: choose the nearest listed strike to a non-stale source underlying/index price. Use the full-chain markprice `iv` fields for a paired call and put at that strike; only calculate the pair mean when both are present, their source-time skew is within the configured limit, coverage passes, and the IV unit contract is verified. Otherwise emit no normalized value and return PARTIAL or NOT_AVAILABLE with a reason.
- IV term structure: ordered per-expiry ATM IV points, no interpolation or forward fill. Requires at least two valid expiries; incomplete expiry nodes are retained as missing/partial nodes with coverage.
- Skew: per expiry, first average paired call/put full-chain markprice `iv` at each exact strike, then calculate the ordinary least-squares slope of those strike-level values against natural-log moneyness ln(strike / underlying_price). Require at least six contracts across at least three paired strikes and configured markprice coverage. Output unit remains “source IV unit per log-moneyness” until verified; no output is made while source scale is unverified.
- 25-delta risk reversal: optional; call IV at the nearest verified +0.25 delta minus put IV at the nearest verified -0.25 delta, same expiry, same source-time window. Requires verified delta and IV units, configured delta tolerance, both sides, and configured coverage. No interpolation is performed.
- 25-delta butterfly: optional; (call IV + put IV)/2 minus ATM IV under the same expiry/unit/coverage gates.

PHASE8_OPTIONS_RR_DELTA_TOLERANCE has no V1 default and must remain unset until the source delta scale is verified. When unset or unverified, RR and butterfly are NOT_AVAILABLE; no tolerance is inferred.

Proposed minimum coverage defaults are 95% for full-chain OI/volume metrics and 90% for bounded ticker/IV metrics. Coverage is counted against the in-scope instrument set, with explicit expected/available/missing counts. Below threshold, the metric is NOT_AVAILABLE; above threshold but incomplete is PARTIAL. All thresholds are configuration-driven.

All context metric envelopes, regardless of status, include `context_timestamp`, source_timestamp(s) (nullable where the source did not supply event time), source REST `fetched_at` and/or WS `received_at`, `data_age`, coverage counts/ratio, status, reason, and SOURCE_PROVIDED/COMPUTED provenance. OI/volume freshness is measured from the hourly REST capture; mark/IV freshness from each markprice field timestamp; Greeks freshness from each bounded ticker field timestamp. Context creation cadence never resets source age.

Dealer GEX is explicitly excluded. Public open interest does not identify dealer ownership or dealer positioning.

## 10. State, retry, reconnect, and shutdown

Phase8 source health states: DISABLED, INITIALIZING, AVAILABLE, DEGRADED, RATE_LIMITED, STALE, ERROR, STOPPING, STOPPED.

Startup order:

1. Validate config and resource bounds; do not request private credentials.
2. Open the public WebSocket and subscribe to the four lifecycle channels first.
3. Seed BTC/ETH option catalogs and supported index names through serialized public REST calls; derive and validate the index set, enforcing the configured markprice-channel cap.
4. Subscribe to each derived full-chain markprice channel. Require its complete initial feed seed before that index's full-chain mark/IV state can be AVAILABLE.
5. Fetch bounded full-chain REST summaries, derive a deterministic expiry-stratified universe from fresh source underlying prices, then subscribe to selected ticker channels in paced bounded batches.
6. Require one full initial ticker snapshot per selected instrument before its field state is ticker-available.
7. Start coalesced snapshot/context tasks. Retention cleanup is not scheduled or called unless the explicit enforcement flag is true; the default is false and Feature Acceptance does not delete real collected data.
8. Mark available only when source health and per-source freshness/coverage gates pass; partial source coverage remains visible.

Retry and overload:

- REST per-operation retries: at most three attempts, exponential delay with jitter, honor Retry-After, configured maximum cooldown, no automatic endpoint fallback.
- REST serialization: at most one in-flight request; minimum interval default 1.25 seconds, shared by both currencies.
- WebSocket reconnect: at most eight attempts per reconnect burst, exponential delay capped at 60 seconds with jitter; then degraded cooldown retry. Reconnect re-subscribes lifecycle first, revalidates/reseeds catalog after an outage beyond threshold, re-subscribes derived markprice indexes and bounded tickers, and waits for complete markprice seeds and fresh ticker snapshots.
- Configure the WebSocket library with max_size=1 MiB (configurable only up to a 4 MiB hard ceiling) and max_queue=256; independently enforce a 16 MiB total queued-payload cap. The larger bounded frame is needed for a full-chain markprice initial seed. On overflow, mark DEGRADED, do not silently drop and continue claiming complete coverage; clear/reseed only the affected source/index state after recording a sanitized health event.
- Contract/schema mismatch is fail-closed and is not retried as a transient error.

Shutdown stops new REST work, cancels retry/timer tasks, drains only the bounded accepted work queue, persists no synthetic final data, closes the WebSocket, flushes the bounded persistence batch, and records a sanitized stopped/degraded health transition. Every owned task is awaited; shutdown has a configured deadline.

## 11. Deduplication and idempotency

- Catalog natural key: DERIBIT + instrument_name. Creation/state event idempotency key: channel + instrument_name + source timestamp + canonical payload hash.
- REST snapshot idempotency key: exchange + instrument_name + observation kind + verified source timestamp + canonical payload hash. If no reliable source timestamp exists, use a persisted collection-cycle ID and payload hash; restart replay of the same fixture/cycle must not duplicate rows.
- Incremental ticker key: instrument + source timestamp + canonical sparse payload hash. Per-field state advances only for newer field timestamps; duplicate messages are no-ops; old events never roll state back.
- Markprice feed key: index_name + instrument_name + source timestamp + canonical payload hash. Per-instrument `mark_price` and `iv` fields independently retain value, `field_last_updated_at`, `exchange_timestamp`, `fetched_at`, status, source field, and provenance. A missing field or instrument in an incremental data array leaves existing state untouched. On reconnect/untrusted state, require the channel's initial all-prices seed again; do not REST-backfill the WS state.
- Context idempotency key: underlying + UTC as_of + calculation_version. A committed context result is immutable; correction/replay uses a new calculation_version or explicit replay identity rather than overwriting unrelated history.
- Persistence is one bounded transaction per accepted snapshot/cycle; on row cap or persistence error, do not publish its context as complete.

## 12. Future Data Layer Replay fixture contract

Sanitized deterministic fixtures will live under tests/fixtures/phase8 and include:

- fixture manifest: source, method/channel, schema version, capture timestamp UTC, expected SHA-256, and whether source timestamp semantics were verified;
- representative BTC and ETH instrument responses, full-chain summary slices with nullable fields, lifecycle creation/state notifications, incremental ticker initial snapshot/change events, duplicate and out-of-order cases, and unknown/missing fields;
- supported index-name response, instrument `price_index` mappings, full initial `markprice.options` array, sparse markprice updates, duplicates/out-of-order updates, and a mismatched unsupported index case;
- exact decimal numeric tokens, explicit nulls, and the distinction between omitted sparse-update keys and present nulls;
- no private account data, API key, URL credentials, auth header, or order/position response;
- deterministic replay clock and assertions for canonical output, statuses, coverage, idempotency, no-future-leakage, persistence rows, and health transitions.

Live production response bodies are not automatically checked into Git. Fixtures are minimized and manually reviewed for public-data safety and bounded size.

## 13. Feature Acceptance versus Data Layer V1 Hardening

### Phase 8 Feature Acceptance includes

- official REST response contract and parser tests;
- official WebSocket lifecycle, full-chain markprice, and sparse incremental-ticker subscription/message contract tests plus bounded public smoke;
- canonical models, config validation, unit/status/provenance behavior;
- additive migration 015 and isolated PostgreSQL persistence/idempotency/retention tests;
- deterministic universe, freshness, context, event-time/no-future-leakage fixtures;
- collector lifecycle start/reconnect/shutdown tests;
- short public smoke only after deterministic and isolated database tests pass;
- full Phase 1–7 regression with every skipped test reported.

### Explicitly deferred to Data Layer V1 Hardening

- Phase 1–8 60-minute runtime;
- final Collector/PostgreSQL resource acceptance and long-term growth characterization;
- global admission controller, global DB writer scheduling, aggregate queue/backpressure budget;
- three restart cycles and integrated deterministic replay across every Phase 1–8 source.

Phase 8 must keep bounded local work and current PostgreSQL 768 MiB / Collector 256 MiB / Engine 384 MiB caps. The existing Phase 7 resource acceptance is not thereby passed; its blocker remains recorded in the Phase 7 report and Data Layer backlog.

## 14. Proposed Phase 8 acceptance gates

1. No migration 001–014 is modified; 015 applies twice with zero second-run changes.
2. Instrument and chain response counts, currencies, fields, decimal precision, nullable values, and source timestamps validate against official contracts and live response examples.
3. WS lifecycle, dynamically validated `markprice.options` indexes, and incremental ticker channels match official request schema; full seed and sparse changes merge deterministically; reconnect resubscribes without exceeding configured caps.
4. Universe never exceeds configured bounds; current sample defaults select at most 64 stratified ticker instruments per underlying and never subscribe to the full 1,852-contract chain. Full-chain cycles enforce 2,048 rows per underlying and 4,096 total and reject over-cap cycles without silently truncating.
5. Missing/null IV, Greeks, OI, or volume remains missing/NOT_AVAILABLE/PARTIAL; no fabricated metrics or zero imputation.
6. IV/Greek units stay raw/unverified; unit-dependent derived values remain NOT_AVAILABLE until evidence is approved.
7. Stale, future, skewed, or out-of-order source data cannot create an AVAILABLE context or overwrite newer state.
8. Context formulas are deterministic, coverage-gated, and cannot alter Stage 1 or generate trade intent.
9. Retry, queues, response bytes, row counts, retention batches, and shutdown are bounded.
10. REST OI/volume context reports the actual collection snapshot time/age; markprice and ticker values retain independent per-field source/receive times. Context creation never refreshes an older source observation.
11. Retention cleanup contract is tested against an isolated test database, but enforcement defaults false and no real collected data is automatically deleted during Feature Acceptance.
12. No private API, live trading path, account/order/position method, or secret is introduced; Phase 1–7 regression has zero failures.

## 15. Source references

- [Deribit production endpoints and API overview](https://docs.deribit.com/index.html)
- [REST instrument listing](https://docs.deribit.com/api-reference/market-data/public-get_instruments)
- [REST full-chain book summary](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency)
- [Options data collection guidance](https://docs.deribit.com/articles/options-data-collection-best-practices)
- [Rate-limit policy](https://docs.deribit.com/articles/rate-limits)
- [Instrument creation channel](https://docs.deribit.com/subscriptions/market-data/instrumentcreationkindcurrency)
- [Instrument state channel](https://docs.deribit.com/subscriptions/market-data/instrumentstatekindcurrency)
- [Incremental ticker channel](https://docs.deribit.com/subscriptions/market-data/incremental_tickerinstrument_name)
- [Full-chain markprice options channel](https://docs.deribit.com/subscriptions/market-data/markpriceoptionsindex_name)
- [Supported index-name discovery method](https://docs.deribit.com/api-reference/market-data/public-get_index_price_names)
- [Official markprice.options seed/change behavior](https://support.deribit.com/hc/en-us/articles/25944782980253-25-June-2021)
- [Current measured counts and distributions](docs/archive/PHASE_8_BASELINE_AUDIT.md)
