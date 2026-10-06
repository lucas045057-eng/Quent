# V2 source unit contract and archived event-risk assessment

The native Bitget OI unit section remains current. The event-risk policy and event assessment below are archived: GNews, Bitget official notices and Xoomar macro calendar were retired from V2 runtime requests and admission on 2026-10-06. The former config/event_risk_policy.json was removed. Historical receipts and Paper records remain readable; absence is not a zero-risk assertion.

The 2026-10-04 user confirmation remains recorded in
`config/bitget_oi_unit_contract.json` for BTCUSDT and ETHUSDT. On 2026-10-06,
the user explicitly confirmed that the `openInterest` values returned by
Bitget UTA `/api/v3/market/open-interest` for all 478 symbols in
`config/strategy_policy_v2_full_market.json` are USDT quote notional.
`config/bitget_oi_unit_contract_full_market.json` records that expanded
user-confirmed scope. Neither confirmation is a Bitget attestation or changes
the exchange response. Explicit contradictory units are rejected. Historical
rows retain their original embedded contract proof; symbols outside the current
478-symbol contract remain unqualified.

Each new accepted row includes the full contract/digest and same-symbol native
mark-price binding with actual event/fetch clocks (maximum 30 seconds). Quote
notional is the reported value; base quantity is reported value divided by the
bound mark price. The original source projection verifies this proof and the
lossless conversion. Original 15-minute OI selection still requires qualified
endpoints 870–930 seconds apart; it never substitutes the oldest available row.
`BITGET_OI_UNIT_CONTRACT_PATH` optionally selects a reviewed contract file.

## Archived: scoped event assessment (through 2026-10-04)

`config/event_risk_policy.json` defines the versioned BTC/ETH-only source scope:
two GNews queries over at least 72 hours, and Bitget security, API/trading,
maintenance and delisting notices over the initial available 29–30 day window.
BTC/ETH native protocols have no project-team vesting schedule in this scope;
mining issuance, staking withdrawals and protocol changes remain news risks.
Other tokens cannot inherit this exemption. This is a bounded source assessment,
not proof that the world has no event or pre-bootstrap incident.
The current title/description classifier requires English news; a different
language is rejected rather than treated as no matching risk.

Set `events.enabled` to true in the existing local research-service config to
enable it. Defaults retain legacy fail-closed behavior. No new key is required.
The authorized local config has this switch enabled; Coinalyze, GNews and AI
keys are preserved. A standalone environment-only config does not enable this
scope implicitly. Source-unit/event rules are separate from runtime policy
approval and execution activation.

The original candidate research lifecycle owns backfill. There is no separate
service. Each call has a finite request budget and the original candidate
deadline. The public JSON journal lives under ignored
`var/realtime-paper/research-events/`. Completed fixed windows cache actual
receipts for at most one hour; persisted split plans continue large or shifted
result windows, while partial pages retain their records. Current tails are
really queried. Incomplete windows, corrupt journals, unknown bodies and stale
receipts remain PARTIAL with no neutral risk scalar.

Official bodies require the exact HTTPS www.bitget.com article URL, no redirect
or key forwarding, bounded parsed article text and text/link digests. Short
notice summaries are not accepted as full bodies. News title/description rules
are deliberately conservative; a match is a review item, not a verified claim
that a particular hack or outage occurred. An unresolved item survives its
source window. Only an official recovery linked to the exact incident, with
bound full body and consistent publication/change clocks, closes that item.
A changed item reopens assessment. A generic recovery headline cannot clear it.
Inaccessible bodies retain UNKNOWN and a five-minute bounded retry clock, so
one blocked article cannot consume every subsequent batch. A retry is not a
resolution; only a newly fetched valid official proof can fill the body gap.

COMPLETE means every registered source interval is covered and no classification
is unknown. A complete assessment with active review items emits risk flag 1
and still blocks new entry. Flag 0 requires complete scoped evidence and no
unresolved item. Missing or inaccessible sources never emit 0. Assessment
freshness is 600 seconds; the original strategy gates remain in force.

Confirmed GNews daily-limit 403 responses create a per-key process-wide cooldown
until the next 00:00 UTC, shared across research instances. The body is inspected
privately and not exported; checks retain only the safe failure code/reset time.
An expired-subscription 403 does not get mistaken for a daily reset. A process
restart may make one denied request before learning the limit again. This does
not extend old news receipt validity. 429/transient retries remain bounded.

## Archived: actual verification and remaining acceptance (as of 2026-10-04)

The bounded original 16-minute public collection on 2026-10-03 UTC completed
32 derivative cycles and stopped. At 19:10:39 UTC BTC/ETH latest and baseline
unit proofs were valid, endpoints 898.742/898.721 seconds apart, and canonical
OI observations usable. Price/flow freshness after stopping is separate.

The authorized source diagnostic retained 185 BTC and 79 ETH news records, plus
120 partial notices per symbol. There were 21/22 conservative news matches and
120 unknown-body items in each journal. These counts are diagnostic snapshots,
not admitted strategy candidates or confirmed incident counts. A subsequent
single request at 19:12:25 UTC confirmed GNews daily quota exhaustion (next reset
2026-10-04 00:00 UTC / 08:00 Asia/Shanghai); a single exact official body returned
403. Neither cause is fixed by pretending coverage is complete.

Continue the original bounded research lifecycle after quota resets, or use a
GNews plan with sufficient page/call limits. Restore access to actual official
article bodies on the existing authorized network route; this code does not
circumvent access controls or trust unrelated mirrors. Then finish backfill and
resolve each active/unknown item with evidence. Re-run actual candidate deadline
and source freshness checks. Current-code approval, production migrations/
configuration, actual admitted A-to-Paper fills and 24-hour acceptance remain
separate unfinished steps; none is established by these source diagnostics.

Public references: [Bitget notices](https://www.bitget.com/docs/catalog/classic-common-notice/classic-common-notice),
[GNews search](https://docs.gnews.io/endpoints/search-endpoint),
[GNews error handling](https://docs.gnews.io/error-handling).
