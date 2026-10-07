# Phase 8 Options Source Baseline Audit

**Status:** Source baseline measured; amended Phase 8 design approved for implementation
**Probe window:** 2026-09-25 15:40:44–15:41:01 UTC
**Repository checkpoint:** branch phase8, HEAD beb637acb22b1ced14f496cc0618101dfa197edf, clean before this design-only change

## Scope and safety

This audit used only unauthenticated, read-only Deribit production public market-data methods. Requests were serialized, JSON response reads were capped at 8 MiB per call for the final universe matrix (the earlier contract probe used a 32 MiB cap), no private API, order API, trading operation, WebSocket subscription, Docker runtime, or database was used. No API credentials were present.

An initial GET-with-body attempt returned a BTC-shaped cached response for both requested currencies and is excluded from every count below. The official API also supports POST JSON-RPC; all accepted measurements below use POST with explicit currency and kind parameters. This observation is a reason to use POST for parameterized REST calls and to assert the requested currency in each response; it is not evidence that Deribit’s GET contract is generally broken.

## Official public endpoints and measured response

Production base: https://www.deribit.com/api/v2/{method}. Four sequential POST JSON-RPC calls were made, with at least 1.35 seconds between calls. The method-specific body and row-count caps were not exceeded.

| Method | Currency | HTTP | Rows | Response bytes | Latency |
|---|---:|---:|---:|---:|---:|
| public/get_instruments (kind=option) | BTC | 200 | 998 | 871,530 | 2,240 ms |
| public/get_book_summary_by_currency (kind=option) | BTC | 200 | 998 | 446,384 | 3,654 ms |
| public/get_instruments (kind=option) | ETH | 200 | 854 | 744,787 | 4,220 ms |
| public/get_book_summary_by_currency (kind=option) | ETH | 200 | 854 | 377,807 | 948 ms |

Both endpoints returned a row for every option instrument in this sample. The instruments response states were all open (BTC 998/998; ETH 854/854). In the book-summary response, instrument_name, volume, open_interest, mark_price, underlying_price, and mark_iv were non-null on all rows in this sample. Bid/mid prices were nullable in the earlier accepted sample and must remain optional.

This is a time-specific public-market sample, not a fixed universe size. Instrument counts, strikes, prices, and expiry sets must be remeasured at acceptance/runtime and must never be treated as constants.

## Expiry and strike distribution

Expiration dates below are derived from the official expiration_timestamp milliseconds and shown as UTC calendar dates. Strike quantiles are over option instruments (including both calls and puts); repeated strikes across call/put and expiry are counted in the quantiles.

### BTC — 998 open options, 92 distinct strikes

| Expiry UTC | Instruments |
|---|---:|
| 2026-09-26 | 60 |
| 2026-09-27 | 62 |
| 2026-09-28 | 58 |
| 2026-09-29 | 58 |
| 2026-10-02 | 48 |
| 2026-10-09 | 52 |
| 2026-10-16 | 40 |
| 2026-10-30 | 112 |
| 2026-11-27 | 96 |
| 2026-12-25 | 118 |
| 2027-03-26 | 104 |
| 2027-06-25 | 108 |
| 2027-09-24 | 82 |

Strike min / p05 / p50 / p95 / max: **20,000 / 54,000 / 85,000 / 160,000 / 250,000**.

### ETH — 854 open options, 88 distinct strikes

| Expiry UTC | Instruments |
|---|---:|
| 2026-09-26 | 66 |
| 2026-09-27 | 70 |
| 2026-09-28 | 60 |
| 2026-09-29 | 58 |
| 2026-10-02 | 40 |
| 2026-10-09 | 40 |
| 2026-10-16 | 34 |
| 2026-10-30 | 84 |
| 2026-11-27 | 72 |
| 2026-12-25 | 88 |
| 2027-03-26 | 84 |
| 2027-06-25 | 86 |
| 2027-09-24 | 72 |

Strike min / p05 / p50 / p95 / max: **800 / 1,500 / 2,740 / 5,200 / 11,000**.

## Measured candidate-universe matrix

underlying_price median from the corresponding book-summary response was used only to count candidate strikes. The selection predicate was: state=open, is_active=true, option type call/put, expiration between the sample UTC date and the configured day horizon, and absolute strike moneyness no wider than the listed band. This matrix is not a market recommendation.

| Underlying | Candidate predicate | Candidate instruments |
|---|---|---:|
| BTC | expiry ≤30d, ±5% | 170 |
| BTC | expiry ≤30d, ±10% | 296 |
| BTC | expiry ≤90d, ±5% | 202 |
| BTC | expiry ≤90d, ±10% | 364 |
| ETH | expiry ≤30d, ±5% | 134 |
| ETH | expiry ≤30d, ±10% | 274 |
| ETH | expiry ≤90d, ±5% | 154 |
| ETH | expiry ≤90d, ±10% | 318 |

The proposed initial bounded default is a 90-day expiry horizon and ±5% moneyness candidate band, followed by a deterministic expiry-stratified selector capped at 64 ticker instruments per underlying (128 total). It reduces this sample’s 1,852 full-chain contracts to at most 128 per-instrument ticker subscriptions; the measured candidate pools were BTC 202 and ETH 154 before the cap. Separately, accept at most 2,048 full-chain records per underlying and 4,096 total in one REST cycle. These bounds are configuration-driven; overflow fails closed/PARTIAL and never silently truncates or subscribes to the whole chain.

The selector reserves the nearest call and put for every eligible expiry before allocating remaining slots to OTM call/put wings round-robin across expiries. If two ATM reservations per eligible expiry cannot fit under the per-underlying cap, it must report PARTIAL and make no new ticker selection for that underlying. At this sample’s 90-day horizon there are ten expiry dates per underlying; the cap leaves a small safety margin and does not promise 25-delta coverage.

## Full-chain markprice channel and index-name verification

A follow-up read-only production contract check was made on 2026-09-25 UTC (the individual HTTP start times were not instrumented). It used JSON-RPC POST request IDs 8101–8103:

| Public method | Result relevant to Phase 8 |
|---|---|
| public/get_index_price_names | Returned the supported index-name list, including btc_usd and eth_usd (among other BTC/ETH quote indexes) |
| public/get_instruments, BTC option | 998 rows; the distinct `price_index` set was [`btc_usd`]; quote and settlement currencies were BTC |
| public/get_instruments, ETH option | 854 rows; the distinct `price_index` set was [`eth_usd`]; quote and settlement currencies were ETH |

The official `markprice.options.(index_name)` channel contract describes each data entry with `instrument_name`, `mark_price`, `iv`, and millisecond `timestamp`; the index name is an enumerated supported index identifier. The official change note says the initial event sends all prices, subsequent events propagate changes, and a timestamp is included. It also notes source-side rounding to four decimal places. Preserve the exact received decimal token and timestamp; do not round it again or infer the unit of `iv` from its magnitude. The current observed subscription names are `markprice.options.btc_usd` and `markprice.options.eth_usd`, but production code must derive channel names from validated instrument `price_index` values and cross-check them against `public/get_index_price_names`, not scatter these observed names as constants.

The source uses the field name `iv` on this channel (not `mark_iv`). The field is source-provided but its scale/unit is still not sufficiently defined for Phase 8 normalization. This read-only check verified REST index identifiers and instrument mappings; it did not establish a live WebSocket payload capture or frame-size measurement. The bounded public smoke remains responsible for validating the current WebSocket envelope before any runtime acceptance claim.

## Rate-limit observations

- Official documentation assigns public/get_instruments a sustained limit of 1 request/second with a burst allowance of 50. Official documentation lists public/subscribe at approximately 3.3 requests/second with a burst allowance of 10.
- All four accepted live calls returned HTTP 200; no rate-related response headers were present. They were deliberately spaced at least 1.35 seconds apart.
- Deribit documents public, unauthenticated access as per-IP rate-limited, but does not publish a single universal public threshold for these methods. The safe low-rate probe therefore did not establish the threshold at which this IP would be throttled. No burst-to-429 test was performed.
- Phase 8 will use a serialized configurable REST scheduler with a minimum 1.25-second interval (≤0.8 calls/second), bounded retries and cooldown on 429/too_many_requests; it will not load-test the public-IP limit.

## Data-contract findings

- public/get_instruments supplies instrument name/id, kind, option type, strike, state, is_active, creation time and expiry time. Official documentation states that the listing may include visible non-open states and inactive contracts; subscription eligibility therefore requires the explicit configured state rules, not merely presence in the response.
- The creation_timestamp on an instrument is the instrument’s creation time, not the time the system fetched the listing.
- public/get_book_summary_by_currency documents volume as 24-hour volume in base currency and options open_interest in the underlying base coin. For options, its `underlying_index` field is documented as the generic string `index_price`; it is not the instrument's concrete `price_index` such as `btc_usd`/`eth_usd`. Preserve the summary field separately and never compare it for equality or use it to build a markprice channel. The docs call `creation_timestamp` a Unix-millisecond timestamp but do not make a sufficiently precise freshness/event-time guarantee for our context pipeline; retain it as a source attribute and use local `fetched_at` for ingestion freshness.
- mark_iv and Greek source fields are available, but the official material reviewed here does not establish the numeric scale/unit contract for every IV/Greek field. Store exact decimal values and source-field provenance; do not convert, compare across unverified contracts, or use unit-dependent derived contexts until confirmed.
- In the summary response, bid and mid can be null. Null/missing remain unavailable and are never coerced to zero.

## Scope and outstanding evidence

This audit did not make a WebSocket connection or subscription and did not execute parser, persistence, migration, fixture, or Phase 1–7 regression tests. Those belong to Phase 8 Feature Acceptance after design approval and implementation. It did not probe the public-IP limit by inducing throttling. Final 60-minute runtime/resource acceptance, global admission/database scheduling, and three restart cycles remain deferred to Data Layer V1 Hardening.

## Official sources

- [Deribit API overview and production endpoints](https://docs.deribit.com/index.html)
- [public/get_instruments contract and endpoint-specific limit](https://docs.deribit.com/api-reference/market-data/public-get_instruments)
- [public/get_book_summary_by_currency contract and fields](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency)
- [Options data collection best practices](https://docs.deribit.com/articles/options-data-collection-best-practices)
- [Deribit rate-limit rules](https://docs.deribit.com/articles/rate-limits)
- [instrument creation subscription channel](https://docs.deribit.com/subscriptions/market-data/instrumentcreationkindcurrency)
- [instrument state subscription channel](https://docs.deribit.com/subscriptions/market-data/instrumentstatekindcurrency)
- [incremental ticker subscription channel](https://docs.deribit.com/subscriptions/market-data/incremental_tickerinstrument_name)
- [full-chain options markprice subscription contract](https://docs.deribit.com/subscriptions/market-data/markpriceoptionsindex_name)
- [public/get_index_price_names contract](https://docs.deribit.com/api-reference/market-data/public-get_index_price_names)
- [official markprice.options change note](https://support.deribit.com/hc/en-us/articles/25944782980253-25-June-2021)

## Bounded public WebSocket / REST smoke measurement

**Result:** `PASS` — final instrumented smoke started at **2026-09-26T05:56:30.327171Z** and completed in **12,186 ms**. This was an unauthenticated public-only check; it did not persist or print raw market payloads.

The final run used exactly five REST POSTs: one supported-index query, one BTC and one ETH option-instrument listing, and one BTC and one ETH option book-summary listing. All returned HTTP 200 and passed the canonical parsers. BTC instrument/summary row counts were 998/998; ETH counts were 856/856. Response sizes were 871,532 bytes (BTC instruments), 746,531 (ETH instruments), 446,024 (BTC summary), and 378,479 (ETH summary); the index list was 4,111 bytes. Per-request latencies were 804 ms, 358 ms, 343 ms, 490 ms, and 337 ms respectively. The four measured gaps between REST request starts were **1,251.496 ms, 1,251.748 ms, 1,251.161 ms, and 1,251.250 ms**, all above the required 1,250 ms. No retries were made.

The deterministic production selector produced a bounded `PARTIAL` sample as expected: BTC 220 eligible / 64 selected; ETH 156 eligible / 64 selected (`BOUNDED_TICKER_SAMPLE`). One smoke ticker per underlying was subscribed; the other selected subscriptions were not opened.

Exactly one connection was opened to `wss://www.deribit.com/ws/api/v2`. One subscription request received an exact successful acknowledgement for eight channels: four BTC/ETH lifecycle channels, two dynamically derived index markprice channels (`btc_usd`, `eth_usd`), and one bounded ticker channel per underlying. The initial markprice messages passed the canonical schema and complete-seed checks with 998 BTC and 856 ETH instruments. Both ticker channels delivered valid initial snapshots. Three subsequent ticker changes were observed; at least one was sparse, and the canonical merge preserved omitted fields: **`LIVE_SPARSE_UPDATE_OBSERVED=true`**. No creation/state lifecycle event occurred within this short window (count 0); this is not a failure under the approved smoke contract. **`DETERMINISTIC_STATE_MERGE_PASS=true`** is independently supported by the deterministic snapshot/change/out-of-order merge test and the Phase 8 replay suite.

One earlier, non-network preliminary harness run stopped after the same five successful REST checks because the probe incorrectly required the bounded universe's expected `AVAILABLE` status instead of accepting a valid sampled `PARTIAL` result. No WebSocket was opened in that run. The probe was corrected to accept only `AVAILABLE`/`PARTIAL` underlying selections with a non-empty candidate for both assets, and the final smoke above passed. This was a local acceptance-gate correction, not a source/API failure.

The live check remained bounded at five REST requests, one public WebSocket connection, an 8 MiB REST response cap, a 1 MiB WebSocket frame cap, and a 75-second global deadline. No private API, authentication, order/trading method, retry, or full-payload logging was used.
