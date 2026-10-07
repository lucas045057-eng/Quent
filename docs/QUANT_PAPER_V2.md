# Quant Paper V2

V2 produces new screening and Phase9 research candidates. V1 modules remain for historical decoding and protective exits. Paper only; there is no private venue order route.

## Current source status (2026-10-06)

GNews searches, Bitget official-announcement queries and Xoomar macro-calendar queries are retired from V2. The runtime does not request or cache them. Legacy gnews, events and xoomar settings are ignored when reading an older local service config; their old receipts and Paper records remain readable as historical data. A retired, missing or unqueried source is not converted into RISK_FLAG=0, “no risk” or any other safety fact.

These three coverage kinds no longer appear in V2 analysis requests, market hypotheses, prompts or execution-admission checks. Current admission still requires fresh price structure and volume, local OI and funding, spot and perpetual flow, benchmark and cross-market OI/funding evidence, conditional retest evidence, liquidity, source freshness, risk limits, and an execution quote within its age limit. The Paper-only bindings remain TRADING_MODE=paper, PAPER_ONLY=true, and LIVE_ALLOWED=false. Broader candidate admission from removing these event gates is a scope change, not evidence of improved strategy quality.

## Research and entry boundary

The engine screens canonical collector data into A/B/C/D with at most five A candidates. B must become A after a fresh trigger check. An immutable screening artifact is persisted in the same transaction as Stage1 intake. The V2 Phase9 evaluator then requests fresh candidate receipts, produces exactly three research horizons (1_3H/3_8H/8_24H; bar timeframes 15m/1H/4H), and applies deterministic execution admission. Source availability, freshness, quality and coverage remain separate. Retired event coverage is excluded from current admission and is never inferred from a raw HTTP success.

The default manifest file name is retained for existing deployment tooling, but its content is V2. `config/strategy_policy_v2.json` owns the approved execution-policy digest. To change the symbol allowlist or research execution rules, edit that file and regenerate the manifest:

```sh
PYTHONPATH=src python -m strategies.runtime --policy config/strategy_policy_v2.json --output policies/phase9_policy_v1.json
```

Approval continues to use the existing exact-commit Phase9 workflow. Do not reuse a V1 approval, a fixture approval, or invent an approver. A new manifest requires a human approval for the final code commit before startup.

## Risk config

`QUANT_RISK_CONFIG_PATH` selects `config/risk_policy_v2.json` by default. Raise revision whenever changing the file. The runtime polls at each new-intent boundary; invalid edits retain the last validated display but block new risk. Config changes do not rewrite existing intents or protective plans. Quotes remain bounded by a hard five-second safety limit even if the configurable age is higher.

Defaults: single risk 0.25% of equity, single notional and total exposure 10%, total reserved risk 0.25%, leverage 1, one open position/intent, 1800-second cooldown, 10800-second hold for each horizon. These are configurable defaults, not hidden absolute 25/1000 caps. Optional absolute budgets can further restrict quantity. Risk-based and fixed-notional-ratio sizing both respect stop-loss risk after fees, slippage and funding.

Multi-symbol positions use one native Sandbox account and one intent-bound journal. Terminal history is retained. Filled open intents retain their reservation until a reconciled flat snapshot identifies their orders. Unknown submissions and inconsistent native checkpoints block new exposure. Same-symbol pyramiding/averaging protection is unsupported by the pinned adapter and is rejected explicitly.

## Runtime bindings

Use `TRADING_MODE=paper`, `PAPER_ONLY=true`, `LIVE_ALLOWED=false`, `PHASE9_ENABLED=1` and the exact final `QUANT_BUILD_REVISION`/`PHASE9_CODE_VERSION`. Existing Phase9 intake TTL is still mandatory. The execution profile contains an existing `account_id`, `venue: BITGET_PAPER` and explicit nonnegative `strategy_costs` fields: entry_fee_rate, exit_fee_rate, funding_cost_rate_max_hold. The native market-order simulator charges the explicit entry/exit fee rate, serialized with its public instrument. Its pinned maker/taker model requires equal entry and exit rates; asymmetric schedules fail explicitly as UNSUPPORTED_PAPER_FEE_SCHEDULE. New instruments come from actual Bitget public metadata (priceMultiplier, quantityMultiplier, maxMarketOrderQty), never BTC/ETH test-kit substitutes. Historical V1 plans continue to use their original instrument definitions.

Existing account provisioning is required; startup does not fabricate equity or reconciliation. Production database migrations and process cutover have not been automatically applied by development tests.

## Data-source capability limits

Canonical local OI/funding and BTC/ETH benchmark returns are normalized with source clocks. Stream flow without an explicit coverage attestation is UNKNOWN; a numerical ratio or AVAILABLE label alone cannot establish complete ingestion. Altcoin spot input must identify the actual symbol. Existing phase7 collection coverage must be verified before admitting trades.

Current external research providers are Coinalyze for cross-market derivatives and the explicitly enabled public Binance/Bybit cross-market provider. Coinalyze resolves actual symbols from its future-market/exchange catalogs, selects at least two non-Bitget USDT perpetual markets, requests USD OI history, and normalizes closed hourly bars. Selected-market coverage is not whole-market coverage. Current funding is documented in percent and normalized to a ratio, but its native funding period is not verified; it cannot confirm an eight-hour cross-market funding thesis. Liquidations are retained as raw receipts. Heatmap, top-trader ratio and unlock capabilities are explicitly unsupported. Historical CoinGlass decoding remains available only for old records.

QUANT_V2_SERVICES_CONFIG_PATH selects a strict local JSON credentials file; otherwise config/research_services.local.json is read when present. config/research_services.example.json contains only current provider settings. The default local credential file is excluded from Git and Docker build contexts. A configured file is authoritative; an invalid file blocks external requests rather than falling back to environment credentials. No credentials appear in receipts, errors or configuration reprs. A legacy config file can still be read; its GNews, Xoomar and event-service sections are discarded.

CanonicalResearchFactory automatically binds an approved Responses provider when a valid local key is present. Allowed endpoints are https://api.openai.com/v1/responses (the default, pinned GPT-5.4 mini) and the user-selected https://xfastapi.ai/responses. Both use store=false, stream=false, strict JSON-schema output and no tools. Provider provenance distinguishes xfastapi from openai. The existing AIService owns budgets, cache, rate limits, deadline handling and cited-fact/schema validation. Each prompt supplies the exact observation fact_digest required by that validator; source_digest is not a substitute.

The default network_transport is native. Explicit windows_system_proxy uses one bounded Windows PowerShell HTTPS request through the existing host system proxy, without changing proxy/firewall settings or creating a daemon. Credentials and payload travel over stdin, not command arguments or temporary files; redirects and unrelated endpoints are rejected. Catalog discovery has a ten-second network timeout, capped by the remaining candidate deadline; other research calls retain two seconds. A longer AI configuration timeout does not extend the actual runtime deadline. Default assembly passes its environment snapshot to both AI binding and external sources.

Returned usage is costed even for refused/incomplete/invalid structured responses; unavailable usage uses a conservative request cost bound at the configured prices. Default pricing was checked for the official GPT-5.4 mini model on 2026-10-03; xfastapi/gpt-5.6-sol prices are unverified and inherited rates are estimates only. Review prices against the provider's bill before relying on monetary bounds. The existing budget ledger is process-local; it does not establish a durable all-process account spend cap. Blank or invalid configuration still yields three WAIT theses. Filling keys does not imply coverage, approval or readiness.


## Dashboard

The existing Dashboard adds Strategy V2 and Risk Policy pages. GET /api/strategy-v2 displays recorded categories, waiting triggers, horizons, source clocks, theses and deterministic results. GET /api/risk-policy displays current/last-valid revision, digest, defaults or edited values, and invalid-reload blockers. Both are read-only; there is no mutation POST.

Research records, fixture execution results, native fills, current positions and historical totals must retain their respective scopes. No order count is inferred from an empty research list.

## Verification and activation status (2026-10-03)

Core compatibility regression: 2117 passed, six role-specific advisory-lock cases skipped because isolated app/migration DSNs are absent; one fresh-Postgres fixture case deselected. The original affected matrix cannot finish because its existing quant_phase8_test database is absent. No database or server was provisioned to bypass that restriction. After review, the monitor's stale V1 labels were replaced; its full file passed 18 tests. Final follow-up verification: 271 passed/one explicit-DB integration skip in the targeted V2/runtime selection; 93 Dashboard/intent tests passed; 65 strategy/source tests passed. Frontend: 17 tests, TypeScript check, and production build passed.

Genuine Bitget public instrument/ticker/closed-bar calls succeeded for BTC, ETH and SOL. Those records were inserted into a temporary schema in the existing test database and consumed by one existing Paper monitor cycle. Three decisions were persisted; zero orders/trades/errors. Missing funding/5m data/collector heartbeat and missing final policy approval correctly produced PAPER NOT READY / DO NOT TRADE. That is a bounded system-blocking verification, not an approved A-to-Paper execution demonstration. Independently, native synthetic execution tests cover fills, exits, consecutive intents, two simultaneous symbols, partial-fill reservation accounting, explicit fees, and restart recovery.

Activation remains pending: normalized cross-funding and unlock completeness receipts; attested perpetual/spot flow; fresh second-stage receipts for every required fact; explicit existing account/cost configuration; final exact-HEAD human policy approval; production migrations and a deliberate cutover window. Real external credentials and AI connectivity have now been verified separately, as recorded below. Only tested breakout/retest/range components are enabled; squeeze/sweep/reversal components remain disabled until deterministic rules and tests exist.

The original V1 24-hour session was inspected read-only. Its recorded collector/engine/runner PIDs were absent; the last persisted cycle was 2026-10-03T05:47:03.353456Z, before the planned end at 14:11:30Z. Its stale RUNNING label does not establish completion. Original evidence and runtime files were preserved; V2 was not started or mixed into that session. Live-order count zero is established only for the bounded public probe / unconfigured monitor and isolated synthetic Paper execution; it is not an independently audited all-account historical total.


## Service replacement verification (2026-10-03)

At the user's request, the active provider set was changed from CoinGlass/CoinDesk/Trading Economics to Coinalyze/GNews/XOOMAR, with three local credentials total including OpenAI. The official OpenAI adapter is bound automatically from a valid key through the existing AIService; no new runtime, database or account was created. A three-blank-key JSON template was delivered, with XOOMAR explicitly keyless.

Final affected regression: 209 passed, one explicit isolated-DB run skipped. This covers strategies, original Phase6 AI/security/config/safety checks, original Phase2 adapter/normalization checks and the existing realtime Paper runtime. Initial new provider tests failed on the old implementation; initial AI binding tests failed without the new adapter. Inline review found and repaired missing Docker credential exclusion and a mixed future funding clock, each reproduced before repair. No private or paid request was made.

Genuine keyless XOOMAR request returned 32 calendar records at 2026-10-03T15:02:33Z; source updatedAt was 2026-10-02T14:30:17.798Z. Coverage remains PARTIAL and stale by the conservative receipt freshness rule. Missing Coinalyze/GNews keys were NOT_CONFIGURED and AI stayed UNCONFIGURED. This is connection/configuration verification, not a full strategy execution acceptance or readiness claim. Earlier broader test totals above are the historical pre-replacement verification at edea5eb.

## Saved-key acceptance follow-up (2026-10-04, Asia/Shanghai)

The user's filled three-key file was tested without exporting secrets to Git, shell arguments or reports. Coinalyze returned 5,468 catalog markets; genuine SOL contracts SOLUSDT_PERP.3 and SOLUSDT_PERP.4 supplied normalized USD OI history and raw liquidation history. A bounded funding retry returned both markets and normalized the percent rate; quality remains PARTIAL because the native funding period is unverified. GNews authenticated/search returned actual news. XOOMAR returned 32 records with limited, stale coverage. Earlier timeout/403 reports are historical attempts, not the final state.

The user identified xfastapi with gpt-5.6-sol and a Responses base URL. Windows existing system proxy completed a real three-horizon structured request in 35,884 ms: 2,501 input tokens and 561 output tokens, with schema and actual fact citations validated by the existing AIService. This was an explicitly non-admitted candidate connectivity diagnostic, returning three WAIT/INSUFFICIENT proposals. The reported monetary cost is an unverified estimate at inherited configuration prices.

One new genuine Bitget BTC/ETH/SOL batch was consumed through default assembly and the existing monitor in a temporary schema of the existing test database. Three decisions were persisted, zero orders/trades/errors; PAPER NOT READY / DO NOT TRADE. Old policy approval triggered BUILD_REVISION_MISMATCH, collector heartbeat was absent, and canonical coverage remained insufficient. No production migration, new database/server/collector/account, runtime cutover or 24-hour session was performed. Full A-to-Paper execution acceptance remains incomplete.

Final affected regression for this follow-up: 220 passed, one fresh-database provisioning fixture skipped. The existing test database was used without creating a server/database; the separate genuine-market diagnostic used a temporary schema and removed it afterward. Tests cover endpoint and redirect rejection, credential transport/redaction, configured environment propagation, bounded catalog timeouts and exact fact identifiers. Earlier broader counts describe their earlier commits, not this final code.


## Archived coverage acceptance follow-up (2026-10-04 Asia/Shanghai; superseded 2026-10-06)

The source-coverage blockers below describe the earlier implementation. News, exchange-announcement and macro-calendar completeness ceased to be V2 admission requirements on 2026-10-06; the remaining market-data, risk and execution gates are unchanged.

The existing collector now honors `UNIVERSE_LIMIT` (1..200, default 200).
Bitget UTA spot REST parses `execId/price/size/ts/side`; publicTrade WS parses
`i/p/v/T/S`. REST requests and responses are capped at 100 rows. Raw side is
still UNKNOWN aggressor direction, and the default source contract stays pending.
The canonical Phase1 market loader now retains the selected persisted Instrument
rows, including source, raw metadata, original clocks, tick/lot and quantity limits.
Missing raw specifications still leave V2 instrument metadata unavailable.

Genuine BTC/ETH spot REST/WS samples parsed successfully. One existing collector
ran for 120.86 seconds against the existing quant database and stopped normally;
price, Klines, perpetual flow and heartbeat updated. One existing engine-owned
Phase2 runtime cycle stored 6 OI, 6 funding and 2 cross-market snapshots with no
errors. These are bounded public-data diagnostics, not a production cutover.

Final core regression: 2151 passed, 6 role-DSN skips, 1 fresh-server fixture
deselected. Original V1 session evidence hash is unchanged. No Paper runner,
production migration, exact-commit approval or 24-hour session was created.

Coverage is NOT PASSED: Bitget local OI units remain unconfirmed, current funding
lacks a source event clock, Bitget flow aggressor semantics and complete-stream
attestations are missing, and the spot worker is not bound to the original
collector supervisor. Both flow tables lack coverage_status. Coinalyze funding
periods and complete news/macro/unlock risk coverage are still unverified. AI must
also complete within the actual candidate deadline. BTC/ETH remain Stage D and
no real admitted A execution was demonstrated. UNKNOWN/PARTIAL facts are never
upgraded merely because transport or collector heartbeat succeeds.

## Archived native coverage repair follow-up (2026-10-04 Asia/Shanghai; local OI/flow details remain current)

`BITGET_SBE_FLOW_ENABLED=true` enables one worker inside the original Collector's
Phase7 lifecycle even with RPC sources disabled. It subscribes to BTC/ETH spot
and USDT-futures publicTrade using the public UTA SBE endpoint. The decoder
accepts only the live-verified intro XML schema 1/version 4, root 16 and entry 40;
the incompatible trade-guide example is rejected. The official trade contract
defines side 0 as taker buyer and side 1 as taker seller. Legacy JSON side remains
UNKNOWN. When SBE is enabled, the original Bitget JSON trade hydration and live
route are suppressed; other exchange routes retain their original behavior.

Five-minute buckets deduplicate execution IDs, cap trades at 20,000 per window
and retained windows at 20, and reject unknown schema, conflicting IDs, future
events, late events and disconnects. After a 30-second closure grace, a window
requires subscription before its opening and exact base-volume equality with
the native closed five-minute candle. Flow and a digest-bound proof are written
atomically to the existing flow and market_observations tables. V2 verifies the
proof's category, symbol, source, window, receipt clock and row values rather
than trusting AVAILABLE or coverage_ratio alone. No new table/migration is
needed. REST reconciliation uses the original Collector admission controller;
the worker and its reconciliation task stop with their owner. SBE counters are
included in original runtime diagnostics. Its flag defaults to false; enabling
this feed does not enable Paper execution or approve the current strategy.

One bounded original Collector diagnostic ran for 661.09 seconds. All four
streams reconciled the 2026-10-03T17:15Z--17:20Z window exactly: BTC spot
6.292155/255 trades, ETH spot 4.284/184, BTC perpetual 20.2693/455, and ETH
perpetual 648.05/357. The first mid-window subscription correctly stayed PARTIAL.
These are historical window receipts, not proof of present freshness after the
diagnostic stops, continuous 24-hour coverage, or an admitted trade.

Local funding binds a documented period to the native ticker's actual source
clock only when symbol, endpoint, rate and bounded fetch clocks agree; the next
funding time is never treated as an observation timestamp. Fetch completion is
used for freshness checks. External Coinalyze funding now binds selected Binance
and Bybit market catalog entries to public native funding-period metadata, with
no API key sent to those supplemental hosts. All selected periods must verify;
missing periods remain PARTIAL. BTC/ETH genuine probes verified the selected
markets' eight-hour normalization; selection does not establish whole-market
coverage or a weighted average. Research transport bounds are ten seconds for
catalog and calls without a candidate deadline, five for other deadline-bound
calls, each capped by remaining candidate time; native period supplements use
at most five seconds. None extends the runtime's overall deadline.

The OI read now retains the endpoint and closest 15-minute baseline within
30 seconds, validates common units/endpoints and reports actual elapsed time.
It does not infer the unconfirmed native Bitget OI unit. The existing closed-bar
gap recovery also checks every 30 seconds on a healthy socket, since forming-bar
pushes alone did not advance stored closed candles. It uses the original bounded
recovery queue and workers; a 120.66-second diagnostic verified closed bars.

Historical acceptance status as of 2026-10-04 (superseded for the retired event sources): full data acceptance remained NOT PASSED because of native local OI unit attestation,
complete news/unlock/exchange-risk coverage, and fresh complete macro coverage
are unresolved. Actual GNews results and XOOMAR's 32 stale, limited-scope calendar
events cannot prove absence of risk. AI still needs to meet the real deadline.
No production V2 migration, exact-commit policy approval, Paper fill or 24-hour
session was created. Original V1 evidence remains unchanged. Final regression
counts and precise probe clocks are recorded in the accompanying acceptance
report; earlier counts above belong to their earlier versions.

## Archived public research receipt and scoped-calendar repair (2026-10-04; retired 2026-10-06)

This follow-up supersedes the earlier macro blocker above. The fixed keyless
XOOMAR calendar is queried explicitly from seven days before to sixty days after
the actual check. All eight intended US release families must have a future
release, every row must validate its agency, importance and publication schema,
and nextReleaseAt is checked against the next HIGH-importance event. A real HTTP
200 or digest/ETag-bound 304 is required on every assessment. The original
updatedAt remains source_event_time; a newly calculated next-24-hour risk flag
uses the actual assessment time. The assessment retains its existing 600-second
age limit, with a separate seven-day published-schedule version limit. It is
complete only for scheduled US releases, never global or unscheduled event risk.
The real bounded diagnostic returned 58 rows and verified both HTTP200 and304;
both BTC/ETH scoped assessments were usable, with no events in that 24h horizon.
The original source version remained 2026-10-02T14:30:17.798Z.

GNews now uses separate asset and systemic queries, fixed publication intervals,
bounded pagination, unique HTTPS article identities and exact stable totals.
Failures preserve successful partial pages; changed totals, duplicate/shifted
pages, schema errors, quotas and page caps stay PARTIAL. There is one bounded
retry for transient responses, and independent provider jobs share the original
candidate deadline. Keyless native common announcements use the documented
cursor, at most five ten-row pages. These utilities do not use Classic derivative
data or forward research API keys. Bounded titles, source URLs, descriptions,
publication clocks and coverage checks now reach the analyst as untrusted,
digest-bound context. Exhausting a search index or fetching announcement titles
cannot establish absence of incidents, unresolved older events or unlock risk.
The real ETH search indexes exhausted 70 asset and12 systemic results; the BTC
index shifted during pagination. Both native announcement probes hit the50-row
cap. EVENT_COVERAGE therefore remains PARTIAL, with no risk-free scalar.

Native Bitget OI now uses its dedicated UTA v3 open-interest route when the ticker
unit is unconfirmed, preserving its actual event clock and endpoint. Conversion
requires an explicit source unit and matching native ticker/mark-price clocks
within30 seconds. Real BTC/ETH responses still omit the unit, and the actual
Coinalyze catalog contains no Bitget markets. UNKNOWN units are not inferred from
magnitude, Classic contracts, another venue or a catalog with no matching market.
The existing engine-owned Phase2 single-cycle diagnostic stored6 OI,6 funding
and2 snapshots without errors; unknown local OI still correctly blocks entries.

The canonical research factory now records genuine repeatable-read revalidation
of unchanged usable native facts, binding their original values, source IDs and
clocks. It records the actual completed view-capture time and rereads quotes
following external lookups. A revalidation never advances original HTTP/source
clocks, rescues stale windows, invents units or admits an old execution quote.
Optional context/revalidation fields are omitted when absent, preserving legacy
canonical digests. Tests exercise tampering, expired windows, unknown units,
external receipts, calendar304 validation and runtime quote rereads.

Full data acceptance remains NOT PASSED: explicit native local OI units/history
and complete asset/systemic/announcement event-risk coverage remain unresolved.
The original collector is stopped after bounded diagnostics; historical complete
SBE windows do not imply current price/flow freshness. Current benchmark and
short-timeframe bars also depend on restarting that original lifecycle. No extra
collector, server, database, production migration, approval, Paper runner, private
order or24-hour acceptance was created. Three local keys and original V1 evidence
remain unchanged. Exact final commit, final regression counts and evidence clocks
are in V2_数据采集修复验收.md; earlier results above are historical.