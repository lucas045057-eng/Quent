# Phase 2 Data Source Audit

Date: 2026-09-20  
Branch: `phase2`  
Scope: public official REST/WebSocket documentation only; no private API, order, position, or live-trading capability.

## Decision summary

| Exchange | Product scope | OI | Funding | Phase 2 status | Decision |
|---|---|---|---|---|---|
| Bitget | UTA USDT perpetuals | v3 ticker field available; unit requires contract verification | v3 ticker/current-fund-rate available; interval semantics require contract verification | `IMPLEMENTABLE_WITH_CONTRACT_TEST` | Keep the Phase 1 UTA v3 adapter. Do not import Classic v2 fields into business code. Historical funding uses local accumulation until a v3 historical endpoint is contract-verified. |
| Binance | USDⓈ-M perpetuals | current and historical endpoints available; current-unit interpretation must be verified | history and funding-info endpoints available | `IMPLEMENTABLE_WITH_CONTRACT_TEST` | Use USDⓈ-M `/fapi` only. Do not use COIN-M `/dapi` data. |
| Bybit | V5 linear USDT perpetuals | historical endpoint explicitly states linear OI is base-asset quantity | funding history and per-instrument funding interval available | `IMPLEMENTABLE` | First non-Bitget adapter candidate. Preserve raw OI and convert base quantity to USD with the verified mark price. |
| OKX | V5 `SWAP`, USDT-margined instruments only | public endpoint exists; exact response unit fields require live schema verification | current and historical public funding endpoints exist; settlement cadence is dynamic | `IMPLEMENTABLE_WITH_CONTRACT_TEST` | Implement only after schema probe confirms `oi`/`oiCcy`/`oiUsd` and funding semantics. |
| Hyperliquid | first perpetual DEX, crypto perps only | `metaAndAssetCtxs` supplies OI; contract spec is one unit of underlying | current context and funding history available; funding is paid hourly | `IMPLEMENTABLE` | Use explicit `coin` metadata mapping. Exclude HIP-3/non-crypto assets from the normal CEX comparison pool. |
| MEXC | USDT perpetuals | public ticker exposes `holdVol`, but official docs do not define it as market OI and no reliable public OI history contract was found | current and historical funding endpoints exist | `NOT_AVAILABLE` | Do not implement in the initial Phase 2 runtime. Re-audit only after a stable official OI schema and rate-limit contract exists. |

## Source matrix

| Exchange | Data type | Endpoint / method | Auth | Rate limit | Symbol / contract | Raw field and unit | Timestamp semantics | History | WS | Fallback / limitation |
|---|---|---|---|---|---|---|---|---|---|---|
| Bitget | instruments | `GET /api/v3/market/instruments?category=USDT-FUTURES` | Public | 20/sec/IP | `BTCUSDT`; UTA USDT futures | `data[].symbol`; live response uses `fundInterval` in hours and `quantityMultiplier`; contract metadata must be read before normalization | `requestTime` is response metadata; instrument rows have no event timestamp | Current metadata only | No dependency | v3 only; no silent Classic v2 fallback |
| Bitget | ticker OI/Funding | `GET /api/v3/market/tickers?category=USDT-FUTURES` | Public | 20/sec/IP | `category=USDT-FUTURES` | `openInterest`, `fundingRate`, `markPrice`, `indexPrice`; OI unit not stated in the market-data page | `ts` is system-generated data timestamp | Current snapshot | UTA ticker channel | Raw values remain usable even when normalization is `NOT_AVAILABLE` |
| Bitget | current funding | `GET /api/v3/market/current-fund-rate` | Public | 20/sec/IP market-data budget; contract-tested | UTA futures symbol | `fundingRate`; live `fundingRateInterval="8"` means 8 hours; `nextUpdate` is the next event | `requestTime` is response metadata; `nextUpdate` is stored as `next_funding_time`, not current `exchange_timestamp` | Current only | Ticker channel has `nextFundingTime` | Do not use `/api/v3/account/open-interest-limit`; it is account/limit semantics |
| Bitget | funding history | Classic documentation exposes `GET /api/v2/mix/market/history-fund-rate` | Public | 20/sec/IP | Classic product type | `fundingRate`, `fundingTime` | Settlement time | Yes, documented | No dependency | Not used by the UTA business adapter; local accumulation until a v3 history contract is verified |
| Binance | current OI | `GET https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT` | Public | IP weight 1 | USDⓈ-M `BTCUSDT`, `contractType=PERPETUAL` via exchange info | `openInterest`; docs do not explicitly label current-unit semantics | `time` is transaction time | Current only | Public market streams are optional, not required for first adapter | Current unit must be confirmed against instrument/contract response before USD normalization |
| Binance | OI history | `GET /futures/data/openInterestHist?symbol=&period=&limit=` | Public | IP 1000 requests/5min; weight 0 in current docs | USDⓈ-M `PERPETUAL` | `sumOpenInterest`, `sumOpenInterestValue`; documented value field is retained separately | `timestamp` is period end | Latest month | No dependency | `5m/15m/1h/4h` available; 24h is composed locally from stored observations |
| Binance | funding | `GET /fapi/v1/fundingRate`; `GET /fapi/v1/fundingInfo` | Public | Shared 500 requests/5min/IP for funding endpoints | USDⓈ-M | `fundingRate`, `fundingTime`, `markPrice`; `fundingIntervalHours` in funding-info for adjusted symbols | Funding time is funding event time; response time is separate | History + current rate info | Optional | Never assume 8h; read funding interval metadata |
| Bybit | OI history | `GET /v5/market/open-interest` | Public | 600 HTTP requests/5s/IP global default | `category=linear`, `symbol=BTCUSDT` | `openInterest`; official docs: linear value is base asset, inverse value is USD | `timestamp` in ms | Symbol launch time onward, paginated, max 200/page | Optional | Preserve `category` and unit exactly |
| Bybit | funding | `GET /v5/market/funding/history`; `GET /v5/market/tickers` | Public | 600 HTTP requests/5s/IP global default | Linear perpetual | `fundingRate`, `fundingRateTimestamp`; ticker also has current funding, mark/index price | Funding timestamp is event/settlement time | History, max 200/page | Public ticker channel available | `fundingInterval` is read from `/v5/market/instruments-info`, not hard-coded |
| Bybit | instruments | `GET /v5/market/instruments-info?category=linear` | Public | 600 HTTP requests/5s/IP global default | Explicit `contractType`, base/quote/settle coins | `fundingInterval` minutes, `settleCoin`, filters | Launch/delivery timestamps are metadata timestamps | Current metadata with cursor | No dependency | Paginate; more than 500 linear symbols exist |
| OKX | OI | `GET /api/v5/public/open-interest?instType=SWAP&instId=` | Public | Official docs define public REST limits per endpoint/IP; exact endpoint limit requires probe | `BTC-USDT-SWAP`; only `SWAP` and USDT settlement for this project | Expected fields include raw OI plus currency/notional fields; exact schema is `NEED_VERIFICATION` until live contract test | `ts` / response timestamps must be separated from exchange observation time | Current endpoint; historical availability `NEED_VERIFICATION` | Public OI channel exists but is not required initially | Never merge `BTC-USD-SWAP` with `BTC-USDT-SWAP` |
| OKX | funding | `GET /api/v5/public/funding-rate`; `GET /api/v5/public/funding-rate-history` | Public | Endpoint-specific public IP limit; verify from response/docs at contract test | `instType=SWAP` | `fundingRate`, `nextFundingRate`, `fundingTime`, `nextFundingTime`, `settFundingRate`, `settState`, `method` | Funding and next-funding timestamps are separate event semantics | Recent history; exact maximum range requires probe | Public funding-rate channel available | Funding settlement frequency can change dynamically; read current fields |
| OKX | instruments | `GET /api/v5/public/instruments?instType=SWAP` | Public | Endpoint-specific public IP limit | `instId`, `instType`, `ctVal`, `ctMult`, `ctType`, `settleCcy` | Contract multiplier and settlement currency metadata | Metadata response timestamp | Current metadata | No dependency | Filter to USDT SWAP and retain instrument identity |
| Hyperliquid | metadata + current OI/Funding | `POST https://api.hyperliquid.xyz/info` body `{"type":"metaAndAssetCtxs"}` | Public | Official info endpoint weight rules; exact current weight must be verified from current docs/runtime | `universe[].name`, e.g. `BTC`; first perp DEX only | Context `openInterest`, `funding`, `markPx`, `oraclePx`, `dayNtlVlm`; OI is underlying units per contract specification | No per-asset exchange timestamp in the context; use request receipt timestamp and mark source timestamp as `NEED_VERIFICATION` | Current snapshot | Public subscription exists | Normalize OI with `raw_oi × markPx`; preserve oracle separately |
| Hyperliquid | funding history | `POST /info` body `{"type":"fundingHistory","coin":"ETH","startTime":...}` | Public | Official info endpoint rules | `coin` plus first perp DEX | `fundingRate`, `premium`, `time` | `time` is event timestamp | Historical endpoint | Public WS available | Funding is paid hourly; the formula is 8h-equivalent but must not be treated as an 8h settlement interval |
| MEXC | funding | `GET /api/v1/contract/funding_rate/{symbol}` and `/history` | Public | 20/2s for current and market endpoints in docs | `BTC_USDT`, USDT swap | `fundingRate`, `collectCycle`, `nextSettleTime`, history `settleTime` | Timestamp fields are documented | Yes | Public funding stream | OI remains unavailable |
| MEXC | ticker OI candidate | WS `sub.ticker` / REST ticker equivalent | Public | Legacy docs | `BTC_USDT` | `holdVol` is called “hold volume”, not market OI; no reliable unit contract | `timestamp` / `ts` | No reliable OI history | Yes | `NOT_AVAILABLE`, raw field may be retained only as unpromoted payload |

## Canonical implementation rules

1. Business code consumes only canonical observations. Adapters own exchange fields, symbols, units, timestamps, and normalization methods.
2. `raw_open_interest` is never overwritten by `open_interest_base`, `open_interest_quote`, or `open_interest_usd`.
3. A normalization result is `NOT_AVAILABLE` when the exchange contract size, multiplier, quote unit, or mark price is missing or ambiguous.
4. Current, predicted, and realized funding are separate fields. `funding_interval_seconds` is read from exchange metadata or a verified official response.
5. No raw OI/Funding observation with `STALE`, `ERROR`, or `NOT_AVAILABLE` status enters cross-exchange weighted calculations.
6. Missing values stay missing; they are never replaced with numeric zero.
7. Phase 2 initial runtime will target Bitget + Bybit + Hyperliquid. Binance and OKX remain behind contract-test gates until the ambiguous fields above are confirmed. MEXC remains `NOT_AVAILABLE`.
8. Phase 1 Stage1 remains unchanged and independent. Derivatives data is an enrichment context only; it cannot create an A result or a trading direction.

## Official references

- Bitget UTA market data: https://www.bitget.com/docs/catalog/market/market-data
- Bitget UTA ticker channel: https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel
- Bitget UTA/Classic separation: https://www.bitget.com/docs/classic/uta-api-upgrade-guide
- Binance USDⓈ-M market data: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- Bybit V5 Open Interest: https://bybit-exchange.github.io/docs/v5/market/open-interest
- Bybit V5 Funding History: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- Bybit V5 Instruments: https://bybit-exchange.github.io/docs/v5/market/instrument
- Bybit V5 rate limits: https://bybit-exchange.github.io/docs/v5/rate-limit
- OKX V5 API guide: https://www.okx.com/docs-v5/en/
- Hyperliquid perpetual info: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Hyperliquid contract specifications: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- Hyperliquid funding: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- MEXC contract API: https://mexcdevelop.github.io/apidocs/contract_v1_en/
