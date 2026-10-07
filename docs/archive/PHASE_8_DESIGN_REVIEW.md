# Phase 8 Options Market Context — Design Review

**Review status:** PHASE8_DESIGN_SELF_REVIEW_PASS
**Reviewed artifacts:** PHASE_8_BASELINE_AUDIT.md, PHASE_8_DESIGN_SPEC.md, docs/superpowers/plans/2026-09-25-phase8-options-context.md
**Repository baseline:** branch phase8, HEAD beb637acb22b1ced14f496cc0618101dfa197edf
**Design gate:** PHASE8_DESIGN_APPROVED=true

## Executive review

The amended hybrid design is internally consistent: `get_instruments` and supported-index discovery seed/reconcile the catalog; lifecycle channels track listing/state changes; dynamically derived `markprice.options.{index_name}` channels provide full-chain mark/IV latest state; hourly REST summaries provide OI/volume reconciliation; and per-instrument incremental ticker is restricted to a deterministic, expiry-stratified bounded universe. Sparse messages merge per field without clearing omitted fields. Contexts preserve each source's real time and quality envelope. The design remains context-only, uses an additive migration, keeps Phase 1–7 behavior unchanged, and explicitly defers integrated resource hardening.

The live REST sample supports configurable defaults of a 90-day expiry horizon, ±5% moneyness candidate band, at most 64 ticker instruments per underlying (128 total), and full-chain acceptance caps of 2,048 records per underlying / 4,096 total. Ticker slots are allocated by expiry: reserve each expiry's nearest call/put pair, then round-robin OTM call/put wings. If expiry reservations do not fit, selection is PARTIAL with no new ticker set. These are config defaults, not exchange constants. A full-chain cap breach rejects the whole cycle; no silent truncation.

No functional code was changed during design review. No database, Docker runtime, WebSocket, collector, or engine was started. The short official public REST source probe is recorded in the baseline audit; it did not stress the public API. The probe discovered `btc_usd` / `eth_usd` through the official supported-index method and validated them against 998 BTC and 854 ETH option instruments. Production logic must still derive these names dynamically. No live WebSocket payload probe has yet been run.

## Review findings

### ACCEPTED

- **Source ownership:** public/get_instruments and public/get_index_price_names seed/reconcile the catalog and index contract; instrument.creation and instrument.state update lifecycle; dynamically derived markprice.options channels own full-chain mark/IV latest state; public/get_book_summary_by_currency captures whole-chain OI/volume/mark reconciliation snapshots; incremental_ticker is restricted to the selected instruments.
- **Index semantics:** instruments' `price_index` is the concrete WS markprice index and is cross-checked against `get_index_price_names`; options summary `underlying_index` may be the generic `index_price` sentinel and is preserved separately, never treated as the channel index.
- **Sparse state:** Markprice and incremental ticker changes preserve omitted fields and their prior value/timestamps; explicit null is distinct from omission. Untrusted state after reconnect requires a new source seed/reconciliation.
- **Bounded selection:** full-chain caps are 2,048 per underlying and 4,096 total; ticker caps are 64 per underlying and 128 total. Selection is deterministic and expiry-stratified, not global-nearest-only.
- **Independent freshness:** 15-minute context cadence does not renew source age. Mark/IV, Greeks/ticker, and REST OI/volume each report their own source timestamp/capture time, data age, coverage, status, reason, and provenance.
- **Retention:** 7/30/90-day values are provisional configuration only; enforcement defaults false, and no real observations are automatically deleted during Feature Acceptance.
- **Measured bounds:** source counts, expiry/strike distribution, response sizes, and 30/90-day candidate matrices are recorded with UTC probe time. Counts are not treated as stable exchange constants.
- **Version/transport isolation:** source JSON is translated by Deribit REST/WS adapters before business logic; parameterized REST uses POST JSON-RPC and validates currency/id. No generic context code depends on raw provider fields.
- **Canonical quality:** exact Decimal values, separate exchange_timestamp/fetched_at/processed_at, per-metric status/unit/provenance/source-field metadata, and explicit missing/null behavior are specified.
- **Database compatibility:** migration 015 is additive; migrations 001–014 remain unchanged. The new schema separates current instruments, lifecycle events, source observations, and immutable context snapshots.
- **Scope safety:** no Stage 1 change, trade intent, private API, RiskEngine, Executor, AI, or dealer GEX. TRADING_MODE remains paper; Phase8 is disabled by default.
- **Resource scope:** response, row, universe, subscription, queue, retry, batch, and retention bounds are defined. Existing PostgreSQL/Collector/Engine caps are not raised.
- **Acceptance boundary:** deterministic/source/parser/persistence/migration/context/replay/short-smoke and Phase 1–7 regression belong to Feature Acceptance; 60-minute runtime, final global resource acceptance, aggregate scheduling, and three restarts stay in Data Layer V1 Hardening.

### WARNING

1. **Public IP limit is not numerically measurable from this safe probe.** The four calls succeeded at ≥1.35-second spacing with no rate-related response headers. Official docs specify the get_instruments method-specific sustained/burst policy and state that anonymous public calls are per-IP limited, but do not publish one universal threshold. Deliberately inducing 429 is not part of this design.
2. **No live WebSocket contract probe has been run.** Official channel documentation defines the schema; implementation must add deterministic contract fixtures and run a short bounded public smoke only after local tests and isolated PostgreSQL tests pass. A markprice seed/channel mismatch is a stop condition for that source, not permission to fall back silently.
3. **IV/Greek scale is not verified.** Raw source fields may be ingested, but ATM IV, term structure, skew, 25-delta RR, and butterfly remain PARTIAL/NOT_AVAILABLE unless the complete official unit/delta/coverage contract is satisfied. This is a metric-level gate, not a reason to invent a conversion. OI/volume contexts can proceed with documented units and their actual REST capture time.
4. **REST summary has no reliable per-row observation event timestamp.** Its `fetched_at` is the honest source-capture/knowledge time, while `exchange_timestamp` is null with NOT_PROVIDED semantics. Context data_age must use the summary capture time.
5. **Phase 7 overall runtime/resource acceptance remains blocked/pending.** The existing Phase 7 report and Data Layer V1 backlog remain authoritative. Phase 8 Feature Acceptance must not be represented as Phase 1–8 resource acceptance.
6. **Persistence growth is projected, not measured.** The row-count envelope follows the live sample and proposed cadence; byte growth and active-write memory pressure remain for deferred hardening. Retention stays unenforced by default.
7. **The original GET-body probe was ambiguous due to a repeated BTC-shaped cached response.** It is excluded from counts. POST JSON-RPC was then used successfully and distinct BTC/ETH result counts were verified. Implementation will guard against currency mismatch.

### BLOCKER

There is no blocker to implement the bounded data contract, persistence, quality statuses, or non-IV contexts after design approval. There is one explicit output blocker: do not publish numeric unit-dependent IV/Greek-derived context while source units remain unverified. For that period the relevant metric status must be NOT_AVAILABLE or PARTIAL with UNIT_UNVERIFIED; this is required behavior, not a reason to fabricate or silently normalize.

## Amendment disposition and self-review result

All requested amendments are incorporated in the baseline audit, design specification, and task plan. The three artifacts agree on these decisions:

1. Full-chain mark/IV uses dynamically discovered and instrument-validated `markprice.options.{index_name}` channels; there is no all-contract incremental-ticker subscription.
2. Sparse merge applies independently to markprice and ticker state. Omitted fields are unchanged; field value, field-update time, source time, receive time, unit/status, and provenance survive independently. Reconnect invalidates untrusted feed state until reseeded.
3. Full-chain limits are 2,048 per underlying / 4,096 total. Ticker limits are 64 per underlying / 128 total with deterministic expiry-stratified ATM-pair reservations followed by alternating OTM call/put wings. All bounds are config-driven and cap overflow is fail-closed/PARTIAL.
4. Context envelopes carry calculation time and per-source time/capture time, age, coverage, status, reason, and provenance. Hourly REST OI/volume age is never reset by 15-minute context generation.
5. Retention defaults 7/30/90 days are provisional; cleanup is bounded and testable but enforcement defaults false and cannot delete live Phase 8 data during Feature Acceptance.
6. Unknown IV/Greek scales remain raw and unit-unverified. Unit-dependent IV/Greek metrics cannot be AVAILABLE until official contracts and required coverage are met. No Stage 1/RiskEngine/Executor/dealer-GEX coupling.
7. Feature Acceptance includes deterministic tests, additive migration/persistence tests, short bounded public smoke, and Phase 1–7 regression only. Data Layer V1 Hardening retains soak/resource acceptance and global scheduling/restart requirements.

Self-review found no remaining design contradiction requiring another approval. Implementation may start using the updated plan. The WS contract smoke remains an implementation acceptance gate; it has not been represented as already passed.

### Post-amendment source-index contract check (2026-09-26)

The final REST-field cross-check confirms two distinct concepts: instrument `price_index` is the concrete supported index used to derive `markprice.options.{index_name}`; an options summary's `underlying_index` can be the generic `index_price` sentinel. The canonical observation and fixtures preserve these fields separately, and the summary value cannot select or invalidate a markprice channel. Summary `creation_timestamp` is kept as its own source-attribute metric (exact Unix milliseconds), never passed off as `exchange_timestamp` or as a freshness clock. The index-name call explicitly uses `extended=false` and validates the documented string-list response. The amended spec, audit, fixtures, and Task 4 parser plan now agree on these rules. No remaining design contradiction was found: `PHASE8_DESIGN_APPROVED=true`.

## Artifact review

- Baseline audit: includes measured row counts, expiries, strike quantiles, candidate matrix, API responses/rate-limit observations, and evidence limitations.
- Design spec: defines sources, default bounds, contracts, time/freshness, formulas/coverage, migration/data model, dedup, retry/reconnect/shutdown, replay fixtures, acceptance/deferred scope.
- Implementation plan: task-by-task RED/GREEN sequence with explicit paths, checks, and commits; updated to implement the approved amendments.

Final design decision is recorded as approved with the requested amendments; implementation may proceed under the amended plan.
