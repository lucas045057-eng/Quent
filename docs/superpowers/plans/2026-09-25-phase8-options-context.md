# Phase 8 Options Market Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a bounded, read-only Deribit BTC/ETH options context pipeline, persistence, and deterministic acceptance while preserving Phase 1–7 behavior.
**Architecture:** Collector-owned Phase8 runtime; isolated Deribit REST/WS adapters; canonical Decimal/provenance contracts; additive PostgreSQL migration 015; bounded context-only outputs.
**Tech Stack:** Python 3.12, asyncio, aiohttp, websockets, Pydantic 2, psycopg 3, PostgreSQL, pytest/pytest-asyncio.
**Spec:** PHASE_8_DESIGN_SPEC.md

## Global Constraints

- Design self-review passed after the user's amendments; implementation is authorized. Do not pause for another design approval.
- Never change migrations 001–014. Create additive migration 015 only.
- Public Deribit endpoints only. No private API, API key, account/order/position API, live executor, or trade intent.
- Keep TRADING_MODE=paper and PHASE8_OPTIONS_ENABLED=false by default.
- Do not alter Stage 1 eligibility/output, Phase 1–7 semantics, Resource caps (PostgreSQL 768 MiB, Collector 256 MiB, Engine 384 MiB), or the existing Phase 7 acceptance report.
- Do not subscribe to full-chain per-instrument tickers. Subscribe to full-chain `markprice.options.{index_name}` only for dynamically discovered, officially validated indexes; use per-instrument incremental ticker only for the bounded selection. Enforce 64 ticker instruments per underlying / 128 total and 2,048 full-chain records per underlying / 4,096 total by default; all bounds are config-driven. Full-chain overflow rejects the cycle as PARTIAL; it is never silently truncated.
- Universe selection reserves the nearest call and put for every eligible expiry, then allocates OTM call/put wings round-robin across expiries. If required ATM reservations exceed the cap, publish PARTIAL and make no new selection for that underlying.
- Merge markprice and incremental-ticker sparse changes per field. Omitted fields remain unchanged; preserve raw value, field-update/source/receive times, unit/status and provenance. Reconnect or loss of trusted state requires re-seeding/reconciliation.
- Contexts expose `context_timestamp`, source timestamp(s), capture/receive time, `data_age`, coverage, status, reason and provenance per metric. OI/volume age is based on the actual REST `fetched_at`, never context cadence.
- Retention values 7/30/90 days are provisional and config-driven; enforcement defaults false. Feature Acceptance must not automatically delete real observations.
- Preserve exact Decimal values, source-provided provenance, UTC exchange_timestamp/fetched_at/processed_at, explicit null/missing statuses, and no-future-leakage.
- Unit-dependent IV/Greek context remains unavailable until its unit contract is verified. Never scale-convert by guess.
- Do not run a public smoke until deterministic tests and isolated PostgreSQL migration/persistence tests pass. Do not perform a rate-limit stress test.
- Final 60-minute runtime/resource acceptance, global admission/DB scheduling, and three restart cycles are deferred to Data Layer V1 Hardening.

## Review Focus

- Source adapter isolation and validation of requested currency, JSON-RPC id, instrument identity, timestamps, exact decimal tokens, and nullable fields.
- Sparse incremental ticker behavior: initial snapshot replaces state; change messages update present fields only; null remains missing; old/duplicate events do not regress state.
- Sparse markprice behavior: the initial feed seed establishes full-chain index state; subsequent changes update present instrument fields only; omitted instruments/fields do not clear state; reconnect requires a new seed.
- `get_index_price_names` and instruments' `price_index` are validated at the adapter boundary. Production code has no static BTC/ETH index-channel mapping.
- Universe cap and expiry/side stratification are deterministic and configuration validated; full-chain record caps are separate from ticker subscription caps.
- REST summary event time is NOT_PROVIDED; `fetched_at` is the source capture/knowledge time and is the OI/volume age basis.
- IV/Greek values stay raw with unit status. Unknown units block dependent metrics; no magnitude-based inference or implicit REST-to-WS fallback.
- Retention cleanup is tested only against isolated data and remains disabled by default.
- No Stage 1 imports or signal eligibility changes.
- Migration 015 and retention only touch Phase 8 tables.
- Context statuses and coverage cannot turn missing observations into zero or an AVAILABLE result.
- Test output and runtime health never include raw full payloads, authenticated headers, or secrets.

---

## Implementation Tasks

## Task 1: Add disabled-by-default Phase 8 configuration

**Files:** create src/quant_phase8/config.py; create tests/test_phase8_config.py; update .env.example.

**RED:** Add tests asserting the absent PHASE8_OPTIONS_ENABLED setting produces disabled configuration; 90-day/5%/64-per-underlying/128-total ticker defaults, 2,048-per-underlying/4,096-total chain defaults, at most four markprice channels, 900-second markprice age, 1 MiB default/4 MiB max frame, 0.35-second subscription pacing/32-channel batch, provisional 7/30/90-day retention with enforcement false, and existing bounded defaults load. Invalid nonpositive/inconsistent bounds, per-currency cap above total, full-chain per-underlying cap above total, response body >32 MiB, invalid cadence/retention, and non-paper trading mode fail validation. Assert repr/validation errors do not include secret-like values.

Representative assertion:

    settings = Phase8Settings.from_env({})
    assert settings.enabled is False
    assert settings.max_ticker_instruments_per_currency == 64
    assert settings.max_full_chain_records_per_underlying == 2048
    assert settings.max_full_chain_records_total == 4096
    assert settings.retention_enforcement is False

Run: .venv/bin/pytest -q tests/test_phase8_config.py

**GREEN:** Implement a Phase8Settings dataclass/model isolated in quant_phase8/config.py. Add only empty/commented Phase 8 non-secret variables to .env.example. Keep the collector’s existing global Settings contract untouched for now.

**VERIFY:** Re-run the focused config tests and tests/test_config.py.

**COMMIT:** feat(phase8): add bounded options configuration

## Task 2: Define canonical option contracts first

**Files:** create src/quant_phase8/contracts.py; create tests/test_phase8_contracts.py.

**RED:** Test OptionInstrument, OptionMetricValue, OptionMarketObservation, OptionInstrumentEvent, and OptionContextSnapshot with Decimal values; UTC-only nullable source timestamps; source/source_field/exchange; REST `fetched_at` versus WS `received_at` and `processed_at`; per-field last-update time; status; unit metadata; SOURCE_PROVIDED/COMPUTED provenance; raw_reference. Test missing and explicit-null values remain null with distinct reason codes. Reject naive timestamps, non-finite Decimal, invalid option side, unknown status, and float coercion.

Representative assertion:

    metric = OptionMetricValue(metric="open_interest", value=Decimal("0.125"))
    assert metric.value == Decimal("0.125")
    assert metric.provenance is Provenance.SOURCE_PROVIDED

**GREEN:** Implement immutable canonical models and JSON Decimal parsing helpers. Mark source units unverified rather than normalizing them.

**VERIFY:** Run tests/test_phase8_contracts.py and existing tests/test_phase7_contracts.py.

**COMMIT:** feat(phase8): define canonical options contracts

## Task 3: Add Deribit REST fixtures and parser RED tests

**Files:** create tests/fixtures/phase8/rest_instruments_btc.json, rest_instruments_eth.json, rest_book_summary_btc.json, rest_book_summary_eth.json; create tests/test_phase8_deribit_rest_parser.py.

**RED:** Fixtures contain a minimized documented response shape, exact JSON numeric tokens, nullable bid/mid, missing optional fields, wrong currency, duplicate symbol, unexpected option kind, malformed timestamp, per/total row-cap boundaries, `public/get_index_price_names` (`extended=false`) supported-index results, BTC/ETH instrument `price_index` mappings, the separate generic summary `underlying_index`, `creation_timestamp` as a source attribute, and raw `mark_iv`. Tests assert requested currency matching, exact Decimal, base-coin units for OI/volume, raw unverified IV unit, UTC expiry, dynamic index validation (including mismatch rejection), correct summary/instrument index separation, and no zero-fill. Tests must be offline and deterministic.

Run: .venv/bin/pytest -q tests/test_phase8_deribit_rest_parser.py

**GREEN:** Keep fixtures sanitized and small; record method/channel/schema and capture time in a fixture manifest. Do not copy full live production responses into Git.

**COMMIT:** test(phase8): define Deribit REST source contracts

## Task 4: Implement REST canonical parser

**Files:** create src/quant_phase8/adapters/deribit_rest.py; update tests/test_phase8_deribit_rest_parser.py only as needed for contract behavior.

**RED/GREEN:** Implement parser functions for supported index names, instruments, and full-chain summary. Assert kind/currency/state/option side, preserve instrument creation time separately from source observation time, retain documented nullable fields, attach field-level source/unit/status metadata, and reject schema mismatch. Keep instrument `price_index` distinct from summary `underlying_index` (`index_price` for options in the official example); preserve both and use only the validated instrument field to construct markprice channels. Parse numeric tokens directly to Decimal.

**VERIFY:** Run parser tests plus tests/test_phase8_contracts.py. No network calls.

**COMMIT:** feat(phase8): parse Deribit options REST contracts

## Task 5: Implement bounded REST transport and rate scheduler

**Files:** update src/quant_phase8/adapters/deribit_rest.py; create tests/test_phase8_deribit_rest_transport.py.

**RED:** Using aiohttp test server/fakes, assert POST JSON-RPC method path and params, matching request id, timeout, 8 MiB default body bound and 32 MiB hard ceiling, one in-flight request, at least configured interval between calls, three-attempt maximum, Retry-After cooldown, and no endpoint fallback. Assert HTTP 4xx/schema mismatch is not retried as transient.

**GREEN:** Implement a single serialized public REST client. Keep transport errors sanitized and never include the full response body in logs.

**VERIFY:** Run REST transport/parser focus. No production API request.

**COMMIT:** feat(phase8): bound public REST transport

## Task 6: Add WebSocket lifecycle, markprice and incremental ticker RED tests

**Files:** create tests/fixtures/phase8/ws_instrument_creation.json, ws_instrument_state.json, ws_ticker_snapshot.json, ws_ticker_change.json, ws_ticker_out_of_order.json; create tests/test_phase8_deribit_ws_parser.py.

**RED:** Assert public/subscribe JSON-RPC contains the four lifecycle channels, dynamically derived `markprice.options.{index_name}` channels, and only bounded incremental_ticker channels. Markprice initial seed establishes full index state; a sparse update changes only present instrument fields; omitted instruments/fields preserve values and timestamps; explicit null remains unavailable; duplicates are idempotent; older field timestamps cannot roll back state. Ticker snapshot replaces one instrument's state; ticker changes sparsely merge. Verify value, field-last-update time, source timestamp, receive timestamp, status and provenance are preserved independently; creation/state timestamps are milliseconds converted to UTC.

Run: .venv/bin/pytest -q tests/test_phase8_deribit_ws_parser.py

Representative assertion:

    merged = apply_incremental_ticker(snapshot, change)
    assert merged["mark_iv"].value == snapshot["mark_iv"].value  # omitted field is unchanged
    assert merged["bid_iv"].value == snapshot["bid_iv"].value
    assert merged["bid_iv"].status is snapshot["bid_iv"].status

**GREEN:** Implement lifecycle/ticker/markprice parser and field-merge contract only. No WebSocket connection yet.

**COMMIT:** test(phase8): define options WebSocket contracts

## Task 7: Implement bounded WebSocket session lifecycle

**Files:** create src/quant_phase8/adapters/deribit_ws.py; create tests/test_phase8_deribit_ws_runtime.py.

**RED:** Fake-server tests cover connect, lifecycle-first subscription, all configured markprice channels, initial per-index markprice seed readiness, bounded ticker batches (32 channels at ≥0.35-second pacing), no full-chain ticker subscriptions, ping/heartbeat, disconnect and state invalidation, bounded exponential retry, reseed/reconciliation before readiness, 1 MiB default/4 MiB hard frame cap, queue and aggregate payload caps, cancellation, task ownership and shutdown deadline.

**GREEN:** Implement one public WebSocket client and bounded queue. Derive markprice channels only from adapter-validated index names. Do not add full-chain ticker subscriptions, trading channels, or private authentication.

**VERIFY:** Run WS parser/runtime tests and tests/test_collector_clean_shutdown.py.

**COMMIT:** feat(phase8): add bounded public WebSocket runtime

## Task 8: Implement deterministic option universe selector

**Files:** create src/quant_phase8/universe.py; create tests/test_phase8_universe.py.

**RED:** Fixtures representing measured expiry/strike shape assert eligibility rules, configurable 90-day/±5% defaults, 64-per-underlying/128-total ticker caps, 2,048-per-underlying/4,096-total full-chain caps, deterministic nearest ATM call+put reservation per expiry, then round-robin OTM call/put wings, stable tie-breaks, stale-underlying fail-closed behavior and no full-chain incremental-ticker subscription. Exercise freshness from a verified source timestamp and, when the source contract says event time is NOT_PROVIDED, from local `fetched_at` against the chain-stale budget without inventing an exchange timestamp. If two ATM reservations per eligible expiry exceed the cap, assert PARTIAL and no new selected set. If a REST chain exceeds either chain cap, assert whole-cycle rejection/PARTIAL rather than truncation.

Representative assertion:

    selected = select_ticker_universe(instruments, underlying_price, as_of, settings)
    assert len(selected["BTC"]) <= settings.max_ticker_instruments_per_currency
    assert len(selected["BTC"]) + len(selected["ETH"]) <= settings.max_ticker_subscriptions_total

**GREEN:** Implement pure selection functions with no network or database dependency. Publish eligible/selected counts, per-expiry coverage and selection reason. Candidate ticker subset is intentionally sampled; full-chain source data is never truncated into a falsely complete snapshot.

**VERIFY:** Run tests/test_phase8_universe.py repeatedly and assert byte-identical selected symbol sets.

**COMMIT:** feat(phase8): bound and stratify options universe

## Task 9: Add migration 015 with isolated migration tests

**Files:** create migrations/015_phase8_options_context.sql; update tests/test_migrations.py; create tests/test_phase8_migrations.py.

**RED:** Isolated PostgreSQL tests assert migration 015 creates the four Phase 8 tables, indexes, foreign keys/check constraints, UTC timestamptz columns, separate nullable exchange/source timestamp and fetched/processed times, per-field last-update metadata, dedup constraints, and bounded retention query indexes for REST summary, markprice, ticker, context, and lifecycle rows. Run the migration twice; the second run creates zero changes. Compare hashes of migrations 001–014 before/after.

**GREEN:** Add only Phase 8 objects. Never drop or alter earlier tables, roles, extensions, or unrelated schemas.

**VERIFY:** Set TEST_POSTGRES_DSN to an isolated disposable PostgreSQL database. The integration test must execute rather than skip; never point it at production or Suixiangji.

**COMMIT:** feat(phase8): add additive options context migration

## Task 10: Implement persistence, deduplication, and bounded retention

**Files:** create src/quant_phase8/persistence.py; create tests/test_phase8_persistence.py; update tests/test_migrations.py if required.

**RED:** Isolated database tests cover instrument upsert/soft-retire; append-only lifecycle idempotency; REST, markprice, and ticker observation dedup keys; decimal round-trip; null/status/unit/provenance/source-time/receive-time/field-update metadata round-trip; context immutability; transaction rollback on full-chain cap; bounded cleanup batches; retention isolation from all non-Phase-8 tables. Assert retention defaults disabled and cleanup is never called by ordinary Phase8 startup/runtime.

**GREEN:** Implement repositories over the new tables only. Persist canonical metrics and hashes, not raw full provider payloads. A full-chain cycle is all-or-nothing for its accepted row set. Implement an opt-in, bounded cleanup contract scoped only to Phase8 tables; do not schedule it in Feature Acceptance while enforcement is false.

**VERIFY:** Run Phase 8 PostgreSQL tests twice against a fresh isolated database and verify no second-run duplicates.

**COMMIT:** feat(phase8): persist bounded options observations

## Task 11: Implement freshness and event-time input selection

**Files:** create src/quant_phase8/freshness.py; create tests/test_phase8_freshness.py.

**RED:** Use a frozen UTC clock to test 5,400-second REST chain staleness from `fetched_at` (event time NOT_PROVIDED), 900-second per-field markprice staleness, 300-second per-field ticker staleness, 93,600-second catalog staleness, 60-second lifecycle health, source-skew gate, missing/unverified source-time quality, future timestamp rejection, local `fetched_at`/`received_at` as-of cutoff, out-of-order field updates, independently aged metrics within one context, and late-arriving observations not rewriting a prior context. A 15-minute context recomputation must not make one-hour-old OI/volume appear fresh.

Representative assertion:

    eligible = select_context_inputs(observations, as_of=context_time, settings=settings)
    assert all(item.fetched_at <= context_time for item in eligible)
    assert all(item.exchange_timestamp is None or item.exchange_timestamp <= context_time for item in eligible)

**GREEN:** Implement pure freshness/input-selection functions with explicit reason codes.

**VERIFY:** Run freshness tests together with Phase 1 tests/test_freshness.py and Phase 7 time/provenance tests.

**COMMIT:** feat(phase8): enforce UTC freshness and event time

## Task 12: Implement context formulas and coverage gates

**Files:** create src/quant_phase8/context.py; create tests/test_phase8_context.py.

**RED:** Deterministic fixtures test put/call OI and 24-hour volume ratios, per-expiry and per-strike concentration, paired ATM IV and markprice IV term/skew, optional RR/BF gates, min coverage, zero denominator, missing/null values, raw unverified markprice `iv` and ticker IV/Greeks remaining PARTIAL/NOT_AVAILABLE (without scale inference, magnitude heuristic, or REST summary fallback), separately reported `context_timestamp`/source timestamps/capture times/data_age/coverage/status/reason/provenance, and no dealer GEX.

Representative assertion:

    context = calculate_options_context(inputs, as_of=context_time, settings=settings)
    assert context.metrics["atm_iv"].value is None
    assert context.metrics["atm_iv"].status is DataStatus.NOT_AVAILABLE
    assert context.metrics["atm_iv"].reason == "UNIT_UNVERIFIED"

**GREEN:** Implement formulas using Decimal where applicable. Derived metric status/provenance must be COMPUTED and reference exact input snapshot IDs; source inputs remain SOURCE_PROVIDED.

**VERIFY:** Run test vectors twice and compare serialized output exactly. Assert no Stage 1 import and no BUY/SELL/LONG/SHORT fields.

**COMMIT:** feat(phase8): calculate coverage-aware options context

## Task 13: Integrate a collector-owned Phase 8 runtime

**Files:** create src/quant_phase8/runtime.py; update src/quant_phase1/entrypoints/collector.py and docker-compose.local.yml; create tests/test_phase8_runtime_integration.py.

**RED:** Integration tests assert disabled-by-default behavior performs no Phase 8 network call/task; enabling starts only Phase8 collector tasks; compose passes Phase 8 settings to the existing quant-collector only and creates no service; startup order (lifecycle, catalog/index validation, markprice seeds, REST summary/universe, bounded ticker snapshots), health states, bounded schedule, reconnect invalidation/reseed, stale-source degradation, no scheduled retention cleanup by default, and shutdown/cancel are owned and awaited. Assert engine, Stage 1, RiskEngine, Executor, and Phase 1–7 runtime contracts are untouched.

**GREEN:** Wire the optional runtime into collector lifecycle only. Do not modify Phase 1–7 data semantics or engine entrypoint.

**VERIFY:** Run Phase8 runtime integration and tests/test_collector_clean_shutdown.py.

**COMMIT:** feat(phase8): wire options context into collector lifecycle

## Task 14: Add deterministic replay fixtures and runtime health contract

**Files:** create tests/fixtures/phase8/manifest.json; create tests/test_phase8_replay.py; update src/quant_phase8/runtime.py only if necessary.

**RED:** Replay fixture tests cover startup seed, dynamically validated index names, full-chain markprice initial seed/change, lifecycle creation/state change, stratified selected ticker snapshot/change, duplicate/out-of-order sparse field events, REST cycle with fetched_at and null exchange_timestamp, context persist with source-specific age, health degradation/recovery/reseed, and deterministic rerun without duplicate rows. Assert fixture manifest hashes and no secrets/raw production dump.

**GREEN:** Keep replay bounded by configured instrument/event caps and use an injected UTC clock.

**VERIFY:** Run replay twice against isolated PostgreSQL and compare canonical result hashes and row counts.

**COMMIT:** test(phase8): add deterministic options replay

## Task 15: Run the short public source smoke

**Files:** create scripts/phase8_source_contract_probe.py; create tests/test_phase8_source_contract_probe.py; update PHASE_8_BASELINE_AUDIT.md with measured results after execution.

**RED:** Offline tests enforce exact official host/methods/channels, `public/get_index_price_names` dynamic index discovery, `markprice.options.{index_name}` derived only from validated instrument metadata, `incremental_ticker` only for the bounded selection, no credential configuration, response/frame byte/read/time bounds, no response body dump, no retry storm, and no private/trading route.

**GREEN:** Implement a manual bounded probe that calls supported-index REST and instrument REST for BTC/ETH, validates each instrument `price_index`, fetches one full-chain summary per currency, subscribes to lifecycle plus the derived markprice index channels and at most one selected incremental ticker per underlying, validates the official subscription/message schemas and full markprice seed, and exits within a fixed deadline. If live source behavior contradicts the documented markprice contract, stop this source path and report the exact mismatch; do not silently fall back to another endpoint/channel. It must not induce throttling.

**VERIFY:** Run all deterministic and isolated PostgreSQL tests first. Only then run the short public smoke, recording request counts, status, schema result, subscription result, latency, and any 429 without secrets or full payloads.

**COMMIT:** test(phase8): add bounded public source probe

## Task 16: Full regression, secret/scope audit, and feature report

**Files:** create the user-required `PHASE_8_FEATURE_REPORT.md`; update only Phase 8 docs and tests.

Run focused Phase 8 tests, migration/persistence tests with TEST_POSTGRES_DSN, Phase 1–7 regression, then full pytest. The recorded pre-Phase8 repository baseline is 939 passed, 15 skipped, 0 failed; rerun rather than assume this baseline. Every skip is listed and justified.

Audit git diff/status, migration hashes 001–014, .env.local ignored/untracked, secrets, private/order/position routes, Stage 1 eligibility, live executor, resource caps, and no Phase 8 table writes outside the configured cap.

Record the exact test totals, public smoke evidence, source-unit gates, row/table counts, and deferred Data Layer V1 Hardening items. Do not claim final integrated runtime or resource acceptance.

**COMMIT:** docs(phase8): record feature acceptance

Stop after Phase 8 Feature Acceptance. Do not start Data Layer V1 Hardening, Phase 9, or any trading operation without a new explicit instruction.
