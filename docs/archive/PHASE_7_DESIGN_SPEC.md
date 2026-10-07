# PHASE 7 DESIGN SPECIFICATION

**Status:** Design only; no Phase 7 implementation has started

**Branch/base:** `phase7` / `18797b7b6abd890ad50abbb614990388cdf3e0ce`

**Operating mode:** `TRADING_MODE=paper`

**Role:** deterministic public-data and context layer, not a trading decision
layer

## 1. Objective and non-goals

Phase 7 describes what happened on selected public chains and public spot
markets. It produces normalized events and descriptive context for future
consumers. It never decides whether to buy or sell and never produces an
execution instruction.

Explicit non-goals:

- Phase 8 options;
- Phase 9 evidence chain or Stage2 decision;
- RiskEngine, position sizing, leverage, execution or live trading;
- AI/LLM calls, AI credential requirements or AI-derived labels;
- private exchange APIs, account balances, user positions, orders, deposits or
  withdrawals;
- a full-chain warehouse, full historical backfill, arbitrary token discovery,
  bridge consensus, or a new resident daemon;
- changing Phase 1–5 eligibility or deterministic semantics.

The Phase 7 vocabulary excludes trade-decision labels and execution fields.
The output contracts contain no decision, side recommendation, entry/exit,
leverage, position-size, stop or take-profit field.

## 2. Frozen V1 scope

### 2.1 Chains

Phase 7 V1 supports exactly:

1. Bitcoin mainnet;
2. Ethereum mainnet.

No L2, sidechain, Solana, Tron, BNB Chain, Arbitrum, Base, or additional
chain is included in V1. Adding one is a design change, not a configuration
toggle.

### 2.2 Assets

- Bitcoin native BTC;
- Ethereum native ETH for top-level transaction-value observations only;
- Ethereum ERC-20 USDT and USDC using a versioned contract allowlist;
- no arbitrary same-symbol token discovery;
- no balance polling.

The USDT/USDC contract registry is configuration data with chain, contract,
decimals, issuer/source reference, effective period and version. Symbol alone
is never an asset identity. If a registry entry is missing or stale, the asset
is `NOT_AVAILABLE`.

### 2.3 Public spot exchanges

V1 designs two bounded public spot adapters for BTCUSDT and ETHUSDT:

| Exchange | Primary feed | Directional side | V1 status |
|---|---|---|---|
| Binance Spot | Public `aggTrade` stream with REST bounded backfill | Directional only when the documented buyer-maker flag is present and valid; `m=true` means the buyer is maker, therefore the aggressor is sell, and `m=false` means aggressor buy | Approved design source; runtime contract test still required |
| Bitget UTA v3 Spot | Public `publicTrade` stream with bounded REST backfill | Total volume is usable; the raw side is evidence only and canonical direction remains `UNKNOWN` until a separate official semantic contract test passes | Approved non-directional design source; no silent upgrade to CVD |

Spot flow is limited to BTCUSDT and ETHUSDT in V1. It does not include
derivatives symbols or any account stream. Bitget UTA v3 uses `topic=publicTrade`
and `instType=spot`; the v2 Classic `channel=trade` schema is forbidden in
this adapter.

## 3. Architecture and ownership

```text
public chain source / public spot source
        -> source adapter + bounded transport
        -> deterministic normalization + dedup + finality
        -> PostgreSQL canonical events / checkpoints
        -> engine aggregation (whale, exchange flow, spot, stablecoin)
        -> Phase 7 context + additive Stage1 enrichment
```

The collector owns transport, source schema validation, timestamp capture,
bounded queues, source health and canonical event handoff. The engine owns
windowing, label classification, USD valuation joins, coverage calculation,
aggregate context and persistence. PostgreSQL owns durable canonical rows,
checkpoints, idempotency and retention.

There is no Phase 7 daemon. Chain polling and spot streaming are tasks inside
the existing collector. Phase 7 engine work is an isolated hook after the
existing deterministic Phase 1–5 work. Phase 6 AI remains a separate optional
hook and is not a dependency.

## 4. Source audit and semantic decisions

### 4.1 Bitcoin source contract

The normative protocol source is a read-only Bitcoin Core-compatible JSON-RPC
endpoint configured by the operator. The adapter uses only public chain data:

- `getblockchaininfo` for chain/head/IBD state;
- `getblockhash` for height-to-hash resolution;
- `getblock` with bounded verbosity for block header and transaction data;
- `getrawtransaction` with a block hash where transaction detail is needed.

Authentication, if required by the endpoint, is a data-provider read-only
credential injected through the environment/secret mechanism. It is never an
exchange credential. Phase 7 does not deploy Bitcoin Core or a node container.
If no endpoint is configured, the chain remains `NOT_AVAILABLE`.

Bitcoin is a UTXO ledger. A V1 event is normally one output identity
`chain=BITCOIN, tx_hash, vout_index`. The output amount is satoshis. Input
addresses may be multiple, absent, or non-attributable; the contract therefore
does not assume one sender. Exchange inflow/outflow requires a complete enough
input/output label view and otherwise remains `PARTIAL` or `NOT_AVAILABLE`.

### 4.2 Ethereum source contract

The normative protocol source is a read-only Ethereum JSON-RPC endpoint. The
adapter uses:

- `eth_chainId` to verify Ethereum mainnet;
- `eth_blockNumber` for the head;
- `eth_getBlockByNumber` for block hash and timestamp;
- `eth_getLogs` for allowlisted ERC-20 Transfer logs;
- `eth_getTransactionReceipt` for receipt/log verification.

The `safe` and `finalized` block tags are preferred where the endpoint supports
them. No explorer scraper is a required source. Provider authentication and
limits are source configuration; no default provider or API registration is
performed in the design phase.

ERC-20 events use the standard `Transfer(address,address,uint256)` log. Token
decimals are obtained from the versioned asset registry/contract metadata
contract and are never inferred from the symbol. Native ETH top-level
transaction value can be observed from full transaction blocks; internal ETH
calls require trace data and are outside V1, so native ETH flow coverage is
explicitly partial.

### 4.3 Binance Spot source contract

The directional source is the official public Spot WebSocket stream
`<symbol>@aggTrade`, with a bounded REST `/api/v3/aggTrades` backfill. The
adapter stores event time, trade time, aggregate trade ID, first/last trade IDs,
price, quantity and buyer-maker flag. A combined stream is not required for V1.
The stream is public and does not require an API key. REST rate limits and
weights are read from the exchange's published `exchangeInfo`/response
metadata; the local limiter uses a configurable safety fraction and never
assumes unlimited capacity. A 24-hour connection lifetime and reconnect
checkpoint are part of the source contract.

The side mapping is deterministic and source-specific:

```text
m = false -> buyer is taker -> canonical aggressor BUY
m = true  -> buyer is maker -> canonical aggressor SELL
missing/invalid m -> UNKNOWN
```

### 4.4 Bitget UTA v3 Spot source contract

The public WebSocket endpoint is `wss://ws.bitget.com/v3/ws/public` with:

```json
{
  "op": "subscribe",
  "args": [{
    "instType": "spot",
    "topic": "publicTrade",
    "symbol": "BTCUSDT"
  }]
}
```

The candidate REST backfill is the public market-fills endpoint with
`category=SPOT`. It must pass a future live contract test before implementation
freezes the response schema. There is no automatic fallback to a v2 endpoint.
The v2 Classic `channel=trade`/`instId` subscription is prohibited.

The existing Phase 3 Bitget adapter treats `side` as unconfirmed aggressor
semantics. Phase 7 follows that decision: volume and trade count may be
aggregated, but buy/sell volume, delta and CVD remain `NOT_AVAILABLE` unless a
separate official source-semantic review changes the adapter contract.

### 4.5 Source evaluation matrix

| Source | Kind | Auth | Historical coverage | Latency | Limits | V1 decision |
|---|---|---|---|---|---|---|
| Bitcoin Core-compatible RPC | official protocol / operator endpoint | endpoint-dependent read-only auth | endpoint-dependent; bounded only | block-driven | endpoint/provider-specific | normative BTC source; no default endpoint means `NOT_AVAILABLE` |
| Ethereum JSON-RPC | official protocol / operator endpoint | optional provider auth | endpoint-dependent; `eth_getLogs` range bounded | block-driven | provider-specific | normative ETH source; no default endpoint means `NOT_AVAILABLE` |
| Binance Spot WebSocket/REST | official public exchange API | none for market data | bounded REST backfill | real time | published request weights and stream limits | directional spot source |
| Bitget UTA v3 Spot WebSocket/REST | official public exchange API | none for market data | bounded REST backfill | real time | published public API limits | non-directional spot source |
| Explorer/scraper, random free API, social feed | third-party/unbounded | unknown or ad hoc | unclear | unclear | unclear | not approved |

No source fallback may hide a primary-source failure. A fallback, if approved
later, must have a fixed order, bounded attempts, its own source ID and
provenance, and must emit the primary failure as health evidence.

### 4.5.1 Executable safety limits

The following are Phase 7 local safety caps for the future implementation.
They are not claims about a provider's published quota. Provider limits must
be discovered from official documentation or configured endpoint metadata; the
local limiter remains below that limit. There is no fallback when a source is
not available.

| Source ID | Endpoint/method | Auth | Local cap | Timeout | Retry/backoff | Catch-up/backfill | Max pages/events/bytes | Cursor | Queue/concurrency | Fallback |
|---|---|---|---|---|---|---|---|---|---|---|
| `btc_core_rpc` | configured JSON-RPC: `getblockchaininfo`, `getblockhash`, `getblock`, `getrawtransaction` | endpoint-specific read-only auth | 1 request/s | connect 2s, total 8s | 2 retries, 0.5s/1.5s | 12 blocks catch-up, 144 blocks backfill | 10,000 events / 8 MiB | height + block hash | 256 events/8 MiB, 1 worker | none |
| `ethereum_rpc` | configured JSON-RPC: `eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`, `eth_getLogs`, `eth_getTransactionReceipt` | provider-specific read-only auth | 1 request/s | connect 2s, total 8s | 2 retries, 0.5s/1.5s | 120 blocks catch-up, 2,400 blocks backfill | `eth_getLogs` range 1,000 / 20,000 logs / 16 MiB | block + hash + log cursor | 512 events/16 MiB, 1 worker | none |
| `binance_spot` | public WS `<symbol>@aggTrade`; REST `/api/v3/aggTrades` | none | 80% of discovered published limit, or 5 requests/s until discovered | connect 1s, total 5s | 2 retries, 0.25s/1s | 120 seconds | 3 pages / 3,000 trades / 8 MiB | last aggregate-trade ID plus first/last IDs | 2,048 events/8 MiB, 1 worker | none |
| `bitget_spot_uta_v3` | `wss://ws.bitget.com/v3/ws/public`, topic `publicTrade`; candidate REST `/api/v3/market/fills?category=SPOT` | none | **PENDING_CONTRACT**; proposed local 2 requests/s only after verification | connect 1s, total 5s | 2 retries, 0.5s/1.5s | 120 seconds | 3 pages / 1,000 trades / 4 MiB | trade ID + event time when offered | 1,024 events/4 MiB, 1 worker | none |

All caps, retry counts, queue sizes and byte limits are configuration values
with the listed defaults. A provider 429 or 5xx cannot be converted into
availability by increasing timeout or retrying indefinitely. The Bitget REST
candidate remains `PENDING_CONTRACT` until a later official contract test
passes; no v2 endpoint is a fallback.

### 4.6 Required source-audit record

Before any adapter implementation is enabled, its source registry entry and
contract test record must include all of the following:

| Audit field | Required decision |
|---|---|
| official/third-party | Protocol/API owner and documentation URL |
| authentication | None, or read-only data-provider credential; never exchange trading credentials |
| cost/licensing | Public/free, provider plan, and applicable ToS/licence review |
| rate limit | Published limit or provider-configured limit; local limiter and bounded retry |
| historical coverage | Exact bounded backfill range and pagination/cursor semantics |
| real-time latency | Block-driven or stream-driven expectation and ingestion grace |
| supported chains/assets | Frozen allowlist; unsupported items return `NOT_AVAILABLE` |
| token transfers | Event/log support, decimals source and contract allowlist |
| address labels | Dataset owner/version, effective time, confidence and coverage denominator |
| pagination | Cursor/page ordering, maximum pages and gap detection |
| reorg/finality | Hash, confirmation/finalized semantics and replacement behavior |
| current WSL availability | Must be verified by a later opt-in contract test; design phase makes no network PASS claim |

The 2026-09-22 design audit researched official protocol/API semantics but did
not call any chain, explorer, or spot endpoint. Therefore current WSL network
availability, live rate-limit headers, and provider-specific licensing status
remain runtime/contract-test gates, not fabricated design evidence.

## 5. Chain capability matrix

`NOT_AVAILABLE` means the capability is not implemented or not semantically
complete in V1; it is not a zero value.

| Capability | Bitcoin mainnet | Ethereum mainnet | V1 rule |
|---|---|---|---|
| Block/transaction data | AVAILABLE when RPC configured | AVAILABLE when RPC configured | Source health and checkpoint required |
| Native transfer | AVAILABLE as UTXO outputs | PARTIAL: top-level transaction value only | No internal ETH trace inference |
| Token transfer | NOT_AVAILABLE | AVAILABLE for allowlisted ERC-20 logs | No arbitrary token discovery |
| USDT/USDC transfer | NOT_AVAILABLE in V1 | AVAILABLE for versioned allowlisted contracts | Ordinary, mint and burn remain separate |
| Address balance | NOT_AVAILABLE | NOT_AVAILABLE | No balance polling or account state |
| Exchange address labels | PARTIAL, snapshot-dependent | PARTIAL, snapshot-dependent | Unknown labels never guessed |
| Block timestamp | AVAILABLE | AVAILABLE | Retain source block reference |
| Finality | Confirmation-depth based | `safe`/`finalized` or configured depth | Chain-specific, no universal confirmation count |
| Reorg detection | block hash/active-chain checks | block hash/finality-window checks | Mark replacement, do not silently delete |
| Fee/gas | BTC fee from transaction inputs/outputs when complete | gas fields from receipt/transaction | Descriptive only |
| Bridge semantics | NOT_AVAILABLE | NOT_AVAILABLE | No V1 bridge aggregation |

## 6. Canonical asset identity and amount model

`CanonicalAssetId` is:

```text
chain + asset_kind + contract_address_or_NATIVE + asset_registry_version
```

The canonical record also stores display symbol and exchange aliases, but these
are not identity keys. Contract addresses are normalized to checksum/lowercase
according to the chain adapter and stored in a canonical form.

Amounts are never floating point:

| Field | Meaning |
|---|---|
| `amount_raw` | Decimal string of the chain integer (`satoshi`, EVM `uint256`) |
| `decimals` | Versioned token precision; null only for invalid/unresolved asset |
| `amount_normalized` | Deterministic `amount_raw / 10**decimals` decimal |
| `amount_usd` | Nullable valuation using an event-time canonical price |
| `valuation_price` / `valuation_exchange_timestamp` / `valuation_fetched_at` | The exact price evidence and acquisition clocks used |

Any conversion failure is `ERROR` or `NOT_AVAILABLE`; AI never performs unit
conversion.

`amount_raw` must contain only non-negative decimal digits; it is canonicalized
without loss of precision before storage. BTC uses exactly 8 decimals. EVM
decimals come only from the versioned asset registry and must be an integer in
the range 0–255. Normalization uses exact decimal arithmetic with no rounding;
the design target is `NUMERIC(120,36)` for normalized amounts and USD values.
Overflow, a negative value, a non-decimal string, or an unresolved decimals
registry entry produces `ERROR`/`NOT_AVAILABLE` and is never coerced to zero.

## 7. On-chain transfer contract

The canonical contract is `OnChainTransferEvent` with these fields:

| Field | Contract |
|---|---|
| `event_id` | Stable adapter identity; not arrival-time based |
| `chain` | `BITCOIN` or `ETHEREUM` |
| `block_number`, `block_hash` | Required for confirmed/finalized rows |
| `tx_hash`, `tx_index` | Required when source provides them |
| `event_index` | EVM `log_index`; BTC `vout_index` mapped with an explicit index kind |
| `event_index_kind` | `LOG_INDEX`, `VOUT_INDEX` or `TX_VALUE` |
| `event_time` | Block timestamp, UTC |
| `observed_at` | Source observation time, UTC |
| `fetched_at` | Response acquisition time, UTC |
| `processed_at` | Canonicalization/persistence time, UTC |
| `asset_id` / `contract_address` | Explicit canonical asset identity |
| `from_address`, `to_address` | Nullable endpoint; no fabricated sender |
| `from_address_set` | Bounded set/reference for UTXO inputs where available |
| `amount_raw`, `decimals`, `amount_normalized` | Unit-safe amount fields |
| `amount_usd` | Nullable, timestamp-safe valuation |
| `source`, `source_version`, `source_reference`, `source_hash` | Provenance |
| `status` | `AVAILABLE`, `PARTIAL`, `STALE`, `NOT_AVAILABLE`, `ERROR` |
| `finality_status` | `OBSERVED`, `PENDING`, `CONFIRMED`, `FINALIZED`, `REORGED` |
| `schema_version`, `normalization_version` | Contract versions |
| `raw_payload` | Adapter-only transient payload; never persisted as a warehouse |
| `raw_reference` | Optional bounded reference/hash/TTL record; nullable by default |
| `details` | Bounded chain-specific metadata; no raw warehouse |

An ERC-20 EVM identity is `(chain, tx_hash, log_index, contract_address)`.
Native ETH top-level transaction value uses `(chain, tx_hash, tx_index,
NATIVE)` and `event_index_kind=TX_VALUE`; it has no fabricated log index and no
contract address. A Bitcoin identity is `(chain, tx_hash, vout_index)`. A
transaction hash alone is never a deduplication key. The persistence contract
must reject `LOG_INDEX` without a contract, `TX_VALUE` for a non-native asset,
`TX_VALUE` with a contract address, or `VOUT_INDEX` outside Bitcoin.

## 8. Finality and reorg model

### 8.1 Ethereum

- `OBSERVED`: block seen at the provider head;
- `CONFIRMED`: block is older than configured Ethereum confirmation depth;
- `FINALIZED`: provider returns the block under the `finalized` tag and hash;
- `REORGED`: stored block hash no longer belongs to the canonical chain;
- `PENDING`: only for source-provided pending records, not required for V1.

The preferred aggregate input is `FINALIZED`. If `finalized` is unsupported,
the aggregate may use `CONFIRMED` and must expose that downgrade in coverage
and provenance. Native internal calls are not reconstructed.

### 8.2 Bitcoin

- `OBSERVED`: block/transaction seen but not safely confirmed;
- `CONFIRMED`: configured Bitcoin confirmation threshold reached;
- `FINALIZED`: configured deeper threshold reached, if the source supports the
  distinction;
- `REORGED`: `confirmations=-1`, block hash mismatch, or active-chain check
  failure.

Bitcoin confirmation depths are chain-specific configuration, not a universal
six-confirmation rule. Aggregates default to the configured finalized depth.

### 8.3 Reorg handling

The checkpoint stores height and block hash. On restart or each head cycle, the
adapter verifies the checkpoint and scans a bounded reorg window. Changed hashes
mark affected events `REORGED`; replacement events use their new block hash and
identity. No row is silently deleted. `REORGED` events are persisted with
`status=STALE` and reason `REORGED_EVENT`, are excluded from every flow,
whale, spot-comparison and stablecoin aggregate, and any replacement is a
separate event under its new block hash/identity. A reorged row never remains
`AVAILABLE`.

## 9. USD valuation

Valuation reuses existing canonical market prices in `market_snapshots` or
`market_observations`. It requires a non-null `exchange_timestamp` and selects
the latest price whose `exchange_timestamp <= event_time` and whose skew is
within configurable `PHASE7_MAX_PRICE_SKEW_SECONDS` (proposed default 300
seconds). `snapshot_timestamp`, source `fetched_at` and `processed_at` are transport
or system clocks and must never substitute for the price event time. Native
BTC and ETH map explicitly to `BTCUSDT` and `ETHUSDT`, quote `USDT`, with
`market_kind=SPOT` (or an explicitly versioned mark-price policy). The selected
price source, exchange, exchange timestamp, valuation fetch timestamp, skew and
price are persisted; a future price, current latest price, or fetched-after-
event price is forbidden. `valuation_fetched_at` must be no later than
`event_time`.

If no compatible price exists, the normalized token/native amount remains
`AVAILABLE` but `amount_usd` is `NOT_AVAILABLE`, with a valuation reason. The
current latest price is never applied to an old event without a timestamp
match.

## 10. Address labels and exchange flow

### 10.1 Taxonomy

V1 taxonomy is intentionally small:

```text
KNOWN_EXCHANGE
KNOWN_PROTOCOL
KNOWN_TREASURY
KNOWN_BRIDGE
KNOWN_BURN
KNOWN_EXTERNAL
UNKNOWN
```

The label record includes chain, normalized address, category, source ID,
source version, label version, confidence/quality, observed/updated time,
effective interval and snapshot hash. A third-party label is evidence, not a
chain-native fact.

### 10.2 Coverage

The only approved V1 label input is an operator-reviewed, versioned snapshot
file containing source URLs, snapshot hash, reviewer, effective interval and
coverage denominator. No automatic third-party label API is used. The proposed
minimum `PHASE7_MIN_LABEL_COVERAGE` is `0.90`: below it is `NOT_AVAILABLE`,
from 0.90 to below 1.0 is `PARTIAL`, and 1.0 with no conflict is
`AVAILABLE`. Missing snapshots and conflicting labels never become an
exchange label through inference.

Every flow aggregate exposes `known_address_count`, `labeled_address_count`,
`label_coverage_ratio`, `source_count`, `source_quality` and
`coverage_status`. The denominator is the address set actually observed by the
source, not a claim that all exchange wallets were discovered.

### 10.3 Exchange inflow/outflow

Classify only when the relevant label snapshot is valid:

```text
KNOWN_EXTERNAL -> KNOWN_EXCHANGE = exchange inflow
KNOWN_EXCHANGE -> KNOWN_EXTERNAL = exchange outflow
KNOWN_EXCHANGE -> KNOWN_EXCHANGE = internal; exclude from net flow
KNOWN_EXCHANGE -> KNOWN_PROTOCOL/TREASURY/BRIDGE = classified separately;
                                    exclude from user net flow by default
```

`KNOWN_EXTERNAL` is assigned only when the approved label snapshot explicitly
classifies the endpoint as external/non-exchange. `UNKNOWN` is never treated as
external. If one side is unknown, the raw transfer is retained but exchange
flow is `PARTIAL` or `NOT_AVAILABLE` according to coverage thresholds. No
address is called an exchange because it is large.

## 11. Whale transfer context

Whale is a configuration-driven classification, not a universal dollar rule.
Each `(chain, asset_id, threshold_version)` has optional absolute USD tiers and
optional relative/percentile tiers. A tier is evaluable only when the relevant
USD valuation and configuration version are available. Missing valuation means
`threshold_not_evaluable`, not a non-whale zero.

`WhaleTransferContext` contains:

- event reference and asset identity;
- chain, event time, finality and freshness;
- normalized amount and nullable USD amount;
- source and destination categories;
- direction (`INBOUND`, `OUTBOUND`, `PEER`, `UNKNOWN`), only descriptive;
- exchange involvement and label coverage;
- threshold version/tier;
- status, reason, coverage and provenance.

Windows are `1m`, `5m`, `15m`, `1H`, `4H`, bounded by configured queue,
event-count and memory limits. Windows expose large inflow/outflow counts,
USD values where available, exchange net flow, unknown transfer count and
coverage. They never produce a trade recommendation.

## 12. Spot flow contracts

`SpotFlowWindow` is separate from all perpetual flow tables and contains:

- `market_kind` (required literal `SPOT`), exchange, canonical symbol,
  timeframe, UTC start/end;
- buy/sell/unknown base volume;
- total base and quote volume;
- directional delta and CVD only when side semantics are confirmed;
- trade count and directional trade count;
- source cursor/sequence range;
- coverage, missing count and source count;
- freshness, status, reason, event-time bounds;
- provenance and normalization version.

For Bitget spot V1, `unknown_volume=total_volume`, directional fields are null,
and the window cannot become directional merely because the exchange sends a
field named `side`. For Binance, `m` drives directional mapping only when
schema validation succeeds.

`SpotPerpFlowContext` is a descriptive comparison containing spot volume,
perp volume, spot delta if available, perp delta if available, direction
agreement/divergence as data descriptors, coverage and statuses. It never maps
any combination to a trade action.

Cross-exchange spot aggregation is allowed only when canonical symbol mapping,
base/quote units, side semantics, timestamp alignment and source status all
pass. Otherwise the output is `PARTIAL` or `NOT_AVAILABLE`.

Spot uses a dedicated repository and table contract. The database must enforce
`market_kind='SPOT'`, and the adapter must never write spot rows into the
perpetual `trade_flow_windows` or any other derivative-flow table. Spot/perp
comparison is a read-only descriptive join with explicit `SPOT`/`PERPETUAL`
domain checks.

## 13. Stablecoin context

V1 stablecoin scope is Ethereum USDT and USDC allowlisted contracts. The
following categories are distinct:

```text
ORDINARY_TRANSFER
MINT
BURN
EXCHANGE_DEPOSIT
EXCHANGE_WITHDRAWAL
BRIDGE_TRANSFER
UNKNOWN
```

Mint requires a zero-address endpoint plus issuer/contract registry semantics;
burn requires the corresponding burn endpoint and registry semantics. A normal
Transfer log is not automatically a mint or burn. Bridge transfer aggregation
is disabled in V1 because source/destination leg correlation is not proven;
each leg remains separate and bridge context is `NOT_AVAILABLE`.

Stablecoin context includes chain, contract, category counts/amounts,
exchange coverage, event-time valuation (normally one USD reference only when
the configured market price is valid), finality, freshness, status, reason and
provenance. It does not mean “money entering the market.”

Stablecoin transfer events remain in the canonical event table but carry
`flow_domain=STABLECOIN` and `aggregation_scope=STABLECOIN`. V1 excludes them
from generic on-chain and whale flow by default; only the dedicated stablecoin
context consumes them. Bridge legs retain a `bridge_leg_id` and set
`aggregation_eligible=false`, so both legs are excluded from ordinary net flow,
whale flow and stablecoin net-flow totals by default. V1 has no bridge aggregate
and therefore cannot double-count a bridge transfer. The explicit configuration
`include_stablecoin_context` defaults to false for generic flow.

## 14. Freshness, coverage and data quality

Freshness is source-class-specific:

| Context | Freshness rule |
|---|---|
| Chain head | Chain/source expected head interval plus configured ingestion grace; head hash and cursor must advance. |
| Confirmed/finalized transfer | Freshness is tied to the latest finalized head, not wall-clock age of an old event. |
| Whale window | Window end plus per-timeframe grace; missing finality or valuation is visible in coverage. |
| Spot flow | Last received event/sequence versus exchange-specific expected cadence plus grace; reconnect/gap makes the window partial/stale. |
| Stablecoin context | Latest finalized chain head plus contract-log cursor; no single 30-minute rule. |

Aggregate coverage fields are mandatory:

```text
sample_count
source_count
available_count
missing_count
coverage_ratio
labeled_count
label_coverage_ratio
```

The status precedence is:

1. calculation/persistence failure -> `ERROR`;
2. required input stale or cursor cannot advance -> `STALE`;
3. required input absent or below minimum -> `NOT_AVAILABLE`;
4. usable but incomplete/unknown coverage -> `PARTIAL`;
5. complete configured evidence -> `AVAILABLE`.

Quality reasons distinguish `SOURCE_UNAVAILABLE`, `SOURCE_STALE`,
`LABEL_INCOMPLETE`, `PRICE_MISSING`, `SEMANTICS_UNCONFIRMED`,
`INSUFFICIENT_COVERAGE`, `REORG_RISK`, `PAGINATION_GAP`, `RATE_LIMITED`,
`CHECKPOINT_INVALID` and `SOURCE_CONFLICT`. These are not all collapsed into
`ERROR`.

## 15. Provenance and source conflict

Every event and important context stores:

- source/provider ID and source version;
- chain or exchange;
- endpoint/stream identifier;
- block/transaction/log or trade cursor reference;
- source event hash/reference;
- observed/fetched/processed UTC timestamps;
- parser, normalization and canonical schema versions;
- label snapshot version where applicable;
- price source/timestamp/skew where applicable.

If two sources disagree on block hash, amount, labels or side semantics, both
provenances are retained and the result is `PARTIAL` with a conflict reason.
V1 does not implement a consensus engine or silently choose a winner.

## 16. Polling, streaming, rate limits and recovery

Chain ingestion uses block polling, not one request per transaction and not an
unbounded historical scan. The source adapter follows this bounded cycle:

1. read head;
2. validate checkpoint hash;
3. process at most configured catch-up blocks;
4. fetch only allowlisted contracts/assets;
5. normalize/dedup/finality-classify;
6. commit rows and checkpoint atomically;
7. emit lag/gap/rate-limit health.

Spot ingestion uses WebSocket as primary and REST only for bounded reconnect
backfill. The maximum backfill time, event count, pages, retries and queue
capacity are configuration values. 429/5xx handling uses exponential backoff
with a finite retry count. No infinite retry loop is allowed.

Checkpoints include source ID, chain/exchange, cursor kind, last observed and
last finalized block/sequence, block hash where relevant, update time, parser
version and status. A restart never rescans all history.

Gap detection covers block-height gaps, parent-hash mismatch, log pagination
gaps, WebSocket disconnects, trade-sequence/ID gaps where the source provides
them, and queue drops. Unresolved gaps degrade coverage.

## 17. Persistence / Migration 012 design

Migration 012 is additive, idempotent and safe to rerun. The selected minimal
tables are:

1. `phase7_asset_registry` — canonical asset identity, decimals, aliases,
   effective versions and provenance;
2. `phase7_address_labels` — versioned chain/address taxonomy snapshots;
3. `phase7_onchain_transfer_events` — normalized BTC/EVM transfer events,
   finality, reorg state and provenance;
4. `phase7_onchain_flow_windows` — descriptive exchange/asset flow aggregates
   with coverage;
5. `phase7_whale_flow_windows` — thresholded whale context aggregates;
6. `phase7_spot_flow_windows` — exchange spot windows and side semantics;
7. `phase7_stablecoin_context` — USDT/USDC category-separated context;
8. `phase7_ingestion_checkpoints` — restart/reorg-safe cursors;
9. `stage1_phase7_context_enrichment` — additive context-only attachment to an
   existing screening run.

Existing `runtime_health_events` and `system_health` are reused; a separate
Phase 7 health table is not needed. Existing Phase 1–6 tables are not altered.

Recommended identity/index constraints:

- unique chain/event identity on transfer events;
- lookup by `(chain, block_number DESC)`, `(asset_id, event_time DESC)`,
  `(tx_hash, event_index)`;
- unique `(chain, asset_id, timeframe, window_open, context_version)` for
  on-chain windows;
- unique `(exchange, symbol, timeframe, window_open, aggregation_version)` for
  spot windows;
- unique source/asset/address label version;
- one active checkpoint per source/chain or source/exchange;
- Stage1 enrichment unique by `(screening_run_id, symbol)`.

### 17.1 Migration 012 DDL contract (design only)

This is the complete logical contract for Migration 012; it is not SQL and no
migration file is created during the design phase. All tables use UTC-aware
timestamps, additive `CREATE TABLE IF NOT EXISTS` semantics, explicit
constraints and indexed batched retention. Migrations 001–011 remain
immutable. No `DROP`, destructive `ALTER`, role mutation or existing-table
rewrite is permitted.

| Table | Required columns and checks | Identity/index contract |
|---|---|---|
| `phase7_asset_registry` | `asset_id`, `chain`, `asset_kind`, `contract_address`, `symbol`, `decimals`, `registry_version`, effective interval, source/provenance, status; native assets require `contract_address=NULL`; EVM decimals 0–255; BTC decimals 8 | PK `asset_id`; partial unique native key `(chain,asset_kind,registry_version) WHERE contract_address IS NULL`; partial unique ERC20 key `(chain,asset_kind,contract_address,registry_version) WHERE contract_address IS NOT NULL`; lookup by `(chain, symbol, effective_from)` |
| `phase7_address_labels` | chain/address/category, source ID/version, label version, confidence, snapshot hash, effective interval, observed/updated UTC timestamps, status; category is frozen taxonomy; normalized address required | PK `(chain, address, label_version)`; unique `(chain,address,source_id,source_version,effective_from)`; interval and confidence checks |
| `phase7_onchain_transfer_events` | event identity, chain/block/tx fields, `event_index`, `event_index_kind`, asset/contract, endpoints, `amount_raw` digit string, decimals, `amount_normalized NUMERIC(120,36)`, nullable USD fields, event/observed/fetched/processed UTC clocks, finality/status/reason, provenance, bounded raw reference; `amount_usd` exact numeric; `REORGED` requires `status=STALE` and `reason=REORGED_EVENT` | conditional unique keys: EVM `LOG_INDEX` `(chain,tx_hash,event_index,contract_address)`; EVM native `TX_VALUE` `(chain,tx_hash,tx_index)`; BTC `VOUT_INDEX` `(chain,tx_hash,event_index)`; EVM `LOG_INDEX` requires contract; `TX_VALUE` requires native asset and null contract; BTC `VOUT_INDEX` only; indexes by block, asset/event time, tx identity |
| `phase7_onchain_flow_windows` | chain/asset/timeframe/window, `flow_domain`, `aggregation_scope`, `aggregation_eligible`, optional `bridge_leg_id`, inbound/outbound/net exact numerics, unknown counts, coverage and label coverage, status/reason, provenance, processed UTC | unique `(chain,asset_id,timeframe,window_open,context_version,aggregation_scope)`; indexes by asset/time and status; stablecoin excluded from generic scope by check/config |
| `phase7_whale_flow_windows` | same domain/eligibility/bridge fields, threshold version/tier, exact amount/USD totals, evaluability/unknown counts, coverage/status/reason/provenance | unique `(chain,asset_id,threshold_version,timeframe,window_open,aggregation_version,aggregation_scope)`; bridge and ineligible rows cannot contribute to totals |
| `phase7_spot_flow_windows` | required `market_kind='SPOT'`, exchange/symbol/timeframe/window, base/quote totals, buy/sell/unknown volumes, nullable delta/CVD, event/sequence bounds, coverage/status/reason, source/provenance UTC clocks | CHECK prevents non-SPOT; unique `(exchange,symbol,market_kind,timeframe,window_open,aggregation_version)`; dedicated repository/indexes; no FK or write path to perpetual flow tables |
| `phase7_stablecoin_context` | chain/asset/contract, category, `aggregation_scope='STABLECOIN'`, `bridge_leg_id`, `aggregation_eligible` default false for bridge legs, exact totals, finality/freshness/coverage/status/reason/provenance | unique `(chain,asset_id,category,timeframe,window_open,aggregation_version)`; indexed by contract/category/time; no bridge net aggregate |
| `phase7_ingestion_checkpoints` | source ID, chain/exchange, cursor kind/value, last observed/finalized cursor, block hash where applicable, parser/schema version, status, updated UTC, bounded error reason | one active row per source/chain or source/exchange; unique source scope; checkpoint update and event batch commit atomically |
| `stage1_phase7_context_enrichment` | screening run, symbol, optional context references/coverage, status/reason, created/processed UTC, normalization version; context-only fields and no decision fields | unique `(screening_run_id,symbol)`; additive FK/reference to existing screening result; absent Phase 7 context cannot delete or mutate Stage1 result |

For all event and aggregate numeric fields, raw integer amounts remain exact
decimal text, BTC uses 8 decimals, EVM precision comes from the registry, and
no rounding is permitted during normalization. The V1 allowlist (native ETH,
USDT and USDC) is required to have `decimals <= 36`; an asset registry entry
outside that storage precision is retained as `NOT_AVAILABLE` and cannot emit
a normalized event or aggregate. Raw payloads are not a bulk
warehouse: only a bounded `raw_reference`/hash/TTL pointer is allowed by
default. `raw_payload` may exist inside an adapter contract or sanitized test
fixture but is not persisted unbounded.

Migration idempotency requires a fresh database run, a second run with zero new
migrations, and a third run after restart with the same result. It must not
change roles, grants, extensions, or any Phase 1–6 table.

### 17.2 Column-level type, nullability and key contract

The following is the implementation-level contract behind section 17.1. It is
written as a reviewable schema specification, not executable SQL. Every listed
column is required unless marked `NULL`; every timestamp is `TIMESTAMPTZ` in
UTC; every exact amount is `NUMERIC(120,36)` except the raw integer text. The
implementation must not replace these rules with an untyped JSON column.

`phase7_asset_registry`:

| Column | Type/nullability | Constraint |
|---|---|---|
| `asset_id` | `TEXT NOT NULL` | PK; stable versioned identity |
| `chain` | `TEXT NOT NULL` | `BITCOIN` or `ETHEREUM` |
| `asset_kind` | `TEXT NOT NULL` | `NATIVE` or `ERC20`; BTC must be `NATIVE` |
| `contract_address` | `TEXT NULL` | null for native; required and normalized for ERC20 |
| `symbol` | `TEXT NOT NULL` | display/alias only, never identity |
| `decimals` | `SMALLINT NOT NULL` | BTC exactly 8; EVM 0..255 |
| `registry_version` | `TEXT NOT NULL` | immutable registry version |
| `effective_from`, `effective_to` | `TIMESTAMPTZ NOT NULL`, `TIMESTAMPTZ NULL` | `effective_to > effective_from` when present |
| `source_id`, `source_version`, `source_reference`, `snapshot_hash` | `TEXT NOT NULL`, `TEXT NOT NULL`, `TEXT NOT NULL`, `TEXT NOT NULL` | bounded provenance |
| `status`, `created_at`, `updated_at` | `TEXT NOT NULL`, `TIMESTAMPTZ NOT NULL`, `TIMESTAMPTZ NOT NULL` | status vocabulary and UTC clocks |

Its conditional uniqueness is mandatory: native rows use
`(chain,asset_kind,registry_version) WHERE contract_address IS NULL`; ERC-20
rows use `(chain,asset_kind,contract_address,registry_version) WHERE
contract_address IS NOT NULL`. A broad unique key containing nullable
`contract_address` is not sufficient.

`phase7_address_labels`:

| Column | Type/nullability | Constraint |
|---|---|---|
| `chain`, `address`, `category`, `source_id`, `source_version`, `label_version` | `TEXT NOT NULL` each | normalized address; category is frozen taxonomy |
| `confidence` | `NUMERIC(5,4) NOT NULL` | 0 <= confidence <= 1 |
| `snapshot_hash`, `source_reference` | `TEXT NOT NULL` each | operator-reviewed snapshot evidence |
| `effective_from`, `effective_to`, `observed_at`, `updated_at` | `TIMESTAMPTZ NOT NULL`, `TIMESTAMPTZ NULL`, `TIMESTAMPTZ NOT NULL`, `TIMESTAMPTZ NOT NULL` | valid interval and UTC |
| `status`, `reason` | `TEXT NOT NULL`, `TEXT NULL` | missing/conflict is explicit |
| key | — | PK `(chain,address,label_version)`; unique `(chain,address,source_id,source_version,effective_from)` |

`phase7_onchain_transfer_events`:

| Column | Type/nullability | Constraint |
|---|---|---|
| `event_id` | `TEXT NOT NULL` | PK; deterministic adapter identity |
| `chain`, `tx_hash`, `event_index_kind` | `TEXT NOT NULL`, `TEXT NOT NULL`, `TEXT NOT NULL` | chain is BTC/ETH; index kind is LOG_INDEX/VOUT_INDEX/TX_VALUE |
| `block_number`, `tx_index`, `event_index` | `BIGINT NOT NULL`, `INTEGER NULL`, `BIGINT NULL` | tx index required for EVM; event index required for LOG_INDEX/VOUT_INDEX and null for TX_VALUE |
| `block_hash` | `TEXT NOT NULL` | required for all persisted source events |
| `asset_id` | `TEXT NOT NULL` | FK/reference to asset registry version |
| `contract_address` | `TEXT NULL` | required only for ERC20 LOG_INDEX; null for BTC and native TX_VALUE |
| `from_address`, `to_address` | `TEXT NULL` each | null allowed when source cannot attribute endpoint |
| `from_address_set_ref` | `TEXT NULL` | bounded reference for BTC input set; not a raw warehouse |
| `amount_raw` | `TEXT NOT NULL` | regex decimal digits only, non-negative |
| `decimals` | `SMALLINT NOT NULL` | must match asset registry |
| `amount_normalized` | `NUMERIC(120,36) NOT NULL` | exact `amount_raw / 10**decimals`, no rounding |
| `amount_usd`, `valuation_price` | `NUMERIC(120,36) NULL` each | both null or both present |
| `valuation_exchange`, `valuation_source`, `valuation_reason` | `TEXT NULL` each | required when USD value is present or unavailable reason is recorded |
| `valuation_exchange_timestamp`, `valuation_fetched_at` | `TIMESTAMPTZ NULL` each | both required with USD value; each must be <= `event_time`; exchange timestamp is the market-event clock |
| `valuation_skew_seconds` | `INTEGER NULL` | non-negative and within configured cap when USD value present |
| `event_time`, `observed_at`, `fetched_at`, `processed_at` | `TIMESTAMPTZ NOT NULL` each | distinct UTC clocks; event time is chain time |
| `status`, `reason`, `finality_status` | `TEXT NOT NULL` each | status/finality vocabularies; REORGED implies STALE + REORGED_EVENT |
| `source_id`, `source_version`, `source_reference`, `source_hash`, `normalization_version`, `schema_version` | `TEXT NOT NULL` each | bounded provenance/versioning |
| `raw_reference`, `details` | `TEXT NULL`, `JSONB NULL` | bounded hash/TTL reference and bounded metadata only |
| conditional keys | — | EVM LOG_INDEX unique `(chain,tx_hash,event_index,contract_address)`; EVM TX_VALUE unique `(chain,tx_hash,tx_index)`; BTC VOUT_INDEX unique `(chain,tx_hash,event_index)` |

The transfer checks are mandatory: EVM `LOG_INDEX` requires an ERC20 asset,
non-null contract and non-null event index; EVM `TX_VALUE` requires native ETH,
null contract and non-null transaction index with null event index; BTC
`VOUT_INDEX` requires native BTC and non-null event index. No single broad unique
key may collapse these three identities.

`phase7_onchain_flow_windows` and `phase7_whale_flow_windows`:

| Column group | Type/nullability | Constraint |
|---|---|---|
| identity | `chain`, `asset_id`, `timeframe`, `window_open`, `window_close`, `context_version` all `NOT NULL` | close > open; timeframe is the frozen 1m/5m/15m/1H/4H set |
| domain | `flow_domain`, `aggregation_scope` `TEXT NOT NULL`; `aggregation_eligible` `BOOLEAN NOT NULL`; `bridge_leg_id TEXT NULL` | stablecoin/bridge domain checks; bridge leg defaults ineligible |
| amounts | inbound/outbound/net base and USD `NUMERIC(120,36) NOT NULL` | exact arithmetic; excluded/ineligible rows cannot contribute |
| coverage | sample/source/available/missing/labeled counts `INTEGER NOT NULL`; ratios `NUMERIC(5,4) NOT NULL` | counts >= 0; ratios 0..1; label coverage follows threshold |
| quality | status/reason/threshold_version/normalization_version `TEXT NOT NULL`; processed/source timestamps `TIMESTAMPTZ NOT NULL` | no hidden zero for missing |
| keys | — | flow unique `(chain,asset_id,timeframe,window_open,context_version,aggregation_scope)`; whale unique additionally includes `threshold_version` and `aggregation_version`; indexes `(asset_id,window_open)`, `(status,window_open)` |

`phase7_spot_flow_windows`:

| Column | Type/nullability | Constraint |
|---|---|---|
| `exchange`, `symbol`, `market_kind`, `timeframe`, `window_open`, `window_close`, `aggregation_version` | `TEXT/TIMESTAMPTZ NOT NULL` as applicable | `market_kind='SPOT'`; close > open; only BTCUSDT/ETHUSDT in V1 |
| base/quote volume fields | `NUMERIC(120,36) NOT NULL` | unknown volume is explicit, never dropped |
| `buy_volume`, `sell_volume`, `delta`, `cvd` | `NUMERIC(120,36) NULL` | null unless source direction is confirmed |
| trade/coverage fields | integer counts, ratios, source cursor bounds, status/reason, source/provenance and UTC clocks | non-negative counts, ratios 0..1, bounded cursor |
| key/index | — | unique `(exchange,symbol,market_kind,timeframe,window_open,aggregation_version)`; indexes `(symbol,window_open)`, `(status,window_open)`; dedicated repository only |

`phase7_stablecoin_context`:

| Column group | Type/nullability | Constraint |
|---|---|---|
| identity | chain, asset_id, contract_address, category, timeframe, window_open, window_close, aggregation_version | all non-null; `aggregation_scope='STABLECOIN'` |
| bridge/domain | `bridge_leg_id TEXT NULL`, `aggregation_eligible BOOLEAN NOT NULL DEFAULT FALSE`, `flow_domain TEXT NOT NULL` | bridge legs remain separate and ineligible; no bridge net aggregate |
| totals/quality | exact amount fields, category counts, coverage/finality/freshness/status/reason, provenance and UTC clocks | exact arithmetic; status cannot mask missing finality |
| key/index | — | unique `(chain,asset_id,category,timeframe,window_open,aggregation_version)`; indexes `(contract_address,window_open)`, `(category,window_open)` |

`phase7_ingestion_checkpoints`:

| Column | Type/nullability | Constraint |
|---|---|---|
| `source_id`, `scope_kind`, `scope_key`, `cursor_kind`, `cursor_value` | `TEXT NOT NULL` each | scope is `CHAIN` or `EXCHANGE`; cursor value is bounded canonical text |
| `last_observed_cursor`, `last_finalized_cursor`, `last_block_hash` | `TEXT NULL` each | finalized cursor cannot move backwards; block hash required for chain scopes |
| `parser_version`, `schema_version`, `status`, `reason`, `updated_at` | `TEXT NOT NULL` each | one active checkpoint per source/scope; UTC |
| key | — | unique `(source_id,scope_kind,scope_key)`; event batch and checkpoint update commit atomically |

`stage1_phase7_context_enrichment` contains `screening_run_id BIGINT NOT NULL`
(matching the existing `screening_runs.id` type),
`symbol TEXT NOT NULL`, bounded context reference/status/reason/coverage JSON,
`normalization_version TEXT NOT NULL`, and created/processed UTC timestamps.
Its sole uniqueness rule is `(screening_run_id,symbol)`. It has no decision,
side, score, eligibility, order or execution columns and cannot update the
referenced Stage1 result.

To remove any grouped-field ambiguity, the remaining table columns are frozen
as follows. These are logical types and null rules for implementation review;
the migration author must materialize every item rather than collapsing a row
into a generic JSON document.

`phase7_onchain_flow_windows` exact columns:

```text
window_id BIGINT NOT NULL PRIMARY KEY
chain TEXT NOT NULL, asset_id TEXT NOT NULL, timeframe TEXT NOT NULL
window_open TIMESTAMPTZ NOT NULL, window_close TIMESTAMPTZ NOT NULL
context_version TEXT NOT NULL, flow_domain TEXT NOT NULL
aggregation_scope TEXT NOT NULL, aggregation_eligible BOOLEAN NOT NULL
bridge_leg_id TEXT NULL
inbound_amount NUMERIC(120,36) NOT NULL
outbound_amount NUMERIC(120,36) NOT NULL
net_amount NUMERIC(120,36) NOT NULL
inbound_amount_usd NUMERIC(120,36) NULL
outbound_amount_usd NUMERIC(120,36) NULL
net_amount_usd NUMERIC(120,36) NULL
exchange_inflow_amount NUMERIC(120,36) NULL
exchange_outflow_amount NUMERIC(120,36) NULL
unknown_transfer_count INTEGER NOT NULL DEFAULT 0
sample_count INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0
available_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0
labeled_count INTEGER NOT NULL DEFAULT 0
coverage_ratio NUMERIC(5,4) NOT NULL, label_coverage_ratio NUMERIC(5,4) NOT NULL
status TEXT NOT NULL, reason TEXT NOT NULL
source_version TEXT NOT NULL, normalization_version TEXT NOT NULL
source_reference TEXT NULL, processed_at TIMESTAMPTZ NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

Checks are `window_close > window_open`, all counts >= 0, ratios in [0,1],
`net_amount = inbound_amount - outbound_amount`, and
`aggregation_scope <> 'STABLECOIN'` when `flow_domain='GENERIC_ONCHAIN'`.
The unique key is `(chain,asset_id,timeframe,window_open,context_version,
aggregation_scope)`; indexes are `(asset_id,window_open DESC)` and
`(status,window_open DESC)`.

`phase7_whale_flow_windows` exact columns:

```text
window_id BIGINT NOT NULL PRIMARY KEY
chain TEXT NOT NULL, asset_id TEXT NOT NULL, timeframe TEXT NOT NULL
window_open TIMESTAMPTZ NOT NULL, window_close TIMESTAMPTZ NOT NULL
aggregation_version TEXT NOT NULL, flow_domain TEXT NOT NULL
aggregation_scope TEXT NOT NULL, aggregation_eligible BOOLEAN NOT NULL
bridge_leg_id TEXT NULL, threshold_version TEXT NOT NULL, threshold_tier TEXT NULL
large_inflow_count INTEGER NOT NULL DEFAULT 0, large_outflow_count INTEGER NOT NULL DEFAULT 0
large_inflow_amount NUMERIC(120,36) NOT NULL, large_outflow_amount NUMERIC(120,36) NOT NULL
large_inflow_usd NUMERIC(120,36) NULL, large_outflow_usd NUMERIC(120,36) NULL
threshold_not_evaluable_count INTEGER NOT NULL DEFAULT 0
unknown_transfer_count INTEGER NOT NULL DEFAULT 0
sample_count INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0
available_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0
coverage_ratio NUMERIC(5,4) NOT NULL, label_coverage_ratio NUMERIC(5,4) NOT NULL
status TEXT NOT NULL, reason TEXT NOT NULL
source_reference TEXT NULL, normalization_version TEXT NOT NULL
processed_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL
```

Checks mirror the flow-window count/ratio/time checks; an ineligible bridge row
must have zero aggregate contribution, and the unique key is
`(chain,asset_id,threshold_version,timeframe,window_open,aggregation_version,
aggregation_scope)`. Indexes are `(asset_id,window_open DESC)` and
`(threshold_version,window_open DESC)`.

`phase7_spot_flow_windows` exact columns:

```text
window_id BIGINT NOT NULL PRIMARY KEY
exchange TEXT NOT NULL, symbol TEXT NOT NULL, market_kind TEXT NOT NULL
timeframe TEXT NOT NULL, window_open TIMESTAMPTZ NOT NULL, window_close TIMESTAMPTZ NOT NULL
aggregation_version TEXT NOT NULL
base_volume NUMERIC(120,36) NOT NULL, quote_volume NUMERIC(120,36) NOT NULL
buy_volume NUMERIC(120,36) NULL, sell_volume NUMERIC(120,36) NULL
unknown_volume NUMERIC(120,36) NOT NULL
delta NUMERIC(120,36) NULL, cvd NUMERIC(120,36) NULL
trade_count INTEGER NOT NULL DEFAULT 0, directional_trade_count INTEGER NOT NULL DEFAULT 0
event_time_first TIMESTAMPTZ NULL, event_time_last TIMESTAMPTZ NULL
cursor_first TEXT NULL, cursor_last TEXT NULL
sample_count INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0
available_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0
coverage_ratio NUMERIC(5,4) NOT NULL
status TEXT NOT NULL, reason TEXT NOT NULL, source_reference TEXT NULL
normalization_version TEXT NOT NULL, processed_at TIMESTAMPTZ NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

Checks require `market_kind='SPOT'`, non-negative volumes/counts, close after
open, `buy_volume/sell_volume/delta/cvd` either all semantically supported or
null as specified by the source, and `unknown_volume <= base_volume`. The
unique key is `(exchange,symbol,market_kind,timeframe,window_open,
aggregation_version)`; indexes are `(symbol,window_open DESC)` and
`(status,window_open DESC)`. No column or repository in this table may point to
the perpetual flow table.

`phase7_stablecoin_context` exact columns:

```text
context_id BIGINT NOT NULL PRIMARY KEY
chain TEXT NOT NULL, asset_id TEXT NOT NULL, contract_address TEXT NOT NULL
category TEXT NOT NULL, timeframe TEXT NOT NULL
window_open TIMESTAMPTZ NOT NULL, window_close TIMESTAMPTZ NOT NULL
aggregation_version TEXT NOT NULL, flow_domain TEXT NOT NULL
aggregation_scope TEXT NOT NULL, bridge_leg_id TEXT NULL
aggregation_eligible BOOLEAN NOT NULL DEFAULT FALSE
transfer_count INTEGER NOT NULL DEFAULT 0
amount_normalized NUMERIC(120,36) NOT NULL
amount_usd NUMERIC(120,36) NULL
mint_count INTEGER NOT NULL DEFAULT 0, burn_count INTEGER NOT NULL DEFAULT 0
exchange_deposit_count INTEGER NOT NULL DEFAULT 0
exchange_withdrawal_count INTEGER NOT NULL DEFAULT 0
sample_count INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0
available_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0
coverage_ratio NUMERIC(5,4) NOT NULL, finality_status TEXT NOT NULL
freshness_status TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL
source_reference TEXT NULL, normalization_version TEXT NOT NULL
processed_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL
```

Checks require `flow_domain='STABLECOIN'`, `aggregation_scope='STABLECOIN'`,
allowlisted USDT/USDC contract, valid category, non-negative counts/amount,
ratios in [0,1], and `aggregation_eligible=false` for `BRIDGE_TRANSFER`.
The unique key is `(chain,asset_id,category,timeframe,window_open,
aggregation_version)`; indexes are `(contract_address,window_open DESC)` and
`(category,window_open DESC)`.

`phase7_ingestion_checkpoints` exact columns are
`checkpoint_id BIGINT PRIMARY KEY`, `source_id TEXT NOT NULL`,
`scope_kind TEXT NOT NULL`, `scope_key TEXT NOT NULL`, `cursor_kind TEXT NOT
NULL`, `cursor_value TEXT NOT NULL`, `last_observed_cursor TEXT NULL`,
`last_finalized_cursor TEXT NULL`, `last_block_hash TEXT NULL`,
`parser_version TEXT NOT NULL`, `schema_version TEXT NOT NULL`,
`status TEXT NOT NULL`, `reason TEXT NULL`, `updated_at TIMESTAMPTZ NOT NULL`.
Checks restrict scope kind to CHAIN/EXCHANGE, require a block hash for CHAIN,
and forbid a finalized cursor moving backwards. The unique key is
`(source_id,scope_kind,scope_key)` with an index on `(status,updated_at)`.

`stage1_phase7_context_enrichment` exact columns are
`enrichment_id BIGINT PRIMARY KEY`, `screening_run_id BIGINT NOT NULL`,
`symbol TEXT NOT NULL`, `context_reference JSONB NULL`,
`coverage JSONB NULL`, `status TEXT NOT NULL`, `reason TEXT NOT NULL`,
`normalization_version TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL`,
`processed_at TIMESTAMPTZ NOT NULL`. JSON is bounded context references only;
no decision fields are allowed. The unique key is
`(screening_run_id,symbol)` and the FK/reference type is compatible with
`screening_runs.id BIGINT`.

V1 does not introduce table partitioning. Indexed batched cleanup is simpler
under the current resource ceiling. Partitioning is a future design change
only if measured event volume proves it necessary.

## 18. Retention and raw policy

All values are configuration defaults, not hardcoded behavior:

| Data | Default retention |
|---|---:|
| normalized on-chain transfer events | 90 days |
| on-chain flow windows | 365 days |
| whale flow windows | 365 days |
| spot flow windows | 30 days |
| stablecoin context | 365 days |
| address label snapshots | 730 days |
| checkpoints and gap evidence | 365 days |
| raw source references | none by default; if enabled, short-lived bounded TTL |

Configuration keys use the `PHASE7_*_RETENTION_DAYS` prefix. No raw chain or
spot warehouse is approved. Sanitized small fixtures may be committed for
parser/schema-drift tests; real bulk payloads may not be committed.

## 19. Stage1, Phase 5 and Phase 6 boundaries

Phase 7 consumes existing market prices and may read Phase 5 context for a
descriptive comparison, but it does not change BTC/ETH market context, regime,
relative strength or sector semantics.

On-chain, whale, spot or stablecoin absence does not remove a Stage1 candidate,
change its A/B/C/D classification, or turn missing into zero. The Phase 7 hook
persists only an additive enrichment row after the existing Stage1 result.

Phase 7 runs with AI disabled, AI credential absent or AI relay unavailable.
No on-chain raw data is sent to an LLM. Phase 6 remains an independent,
unresolved runtime debt.

## 20. Resource and reliability boundary

The design fits the existing ceilings only under these constraints:

- no additional container or daemon;
- only BTC/ETH and two spot symbols in V1;
- bounded block catch-up and log ranges;
- bounded public-trade queues and batch sizes;
- normalized events and aggregates only;
- per-class retention and indexed cleanup;
- no full-chain address-balance scan;
- no arbitrary token discovery;
- no infinite retries, pagination or in-memory windows.

The collector target is 256 MiB, engine 384 MiB and PostgreSQL 768 MiB. A
resource test must fail the gate if an implementation needs higher limits. A
queue drop or source gap emits a health event and degrades coverage; it does
not silently discard the evidence.

## 21. Security

- public data only;
- no exchange private API, account, balance, position, order, deposit or
  withdrawal endpoint;
- data-provider credentials, if later required, are read-only and injected via
  environment/secret storage;
- data-provider keys are separate from exchange trading keys;
- no API key, secret or token is written to Git, fixtures, reports or logs;
- public addresses are not sent to an AI provider;
- `TRADING_MODE=paper` remains the only default and no executor is introduced.

## 22. Testing and future runtime acceptance gate

Implementation must use test-first slices:

- contract fixtures for every approved adapter and schema drift rejection;
- canonical asset/amount/unit tests;
- BTC UTXO and EVM log semantic tests;
- finality, block-hash change and reorg fixtures;
- dedup identity and conflicting-source tests;
- label provenance, coverage and unknown-class tests;
- timestamp-safe USD valuation tests;
- Binance maker-flag and Bitget unknown-side tests;
- spot/perp separation and cross-exchange compatibility tests;
- checkpoint restart, bounded backfill, gap, 429 and reconnect tests;
- PostgreSQL Migration 012 fresh/repeat/integration tests;
- retention and resource tests;
- Stage1/Phase5 non-interference tests;
- security scan ensuring no private API, order, position, executor or AI import.

Live external tests are separate from deterministic CI and are not run in this
design phase. Future runtime acceptance must require live head/block
progression, cursor progression, at least one real normalized public event per
enabled source when observed, spot side semantics, finality/reorg fixtures,
dedup, freshness, coverage, DB/source outage recovery and a 30–60 minute
resource/stability observation. If a source has no event during the window, the
result is `NO_NEW_EVENT_OBSERVED`, never fabricated data.

## 23. Known limitations and design blockers

The following are explicit limitations, not hidden fallbacks:

- no default public Bitcoin/Ethereum provider is selected in the design phase;
  without an approved configured endpoint the chain outputs are
  `NOT_AVAILABLE`;
- EVM native internal ETH calls are outside V1 without trace-capable source;
- BTC sender attribution and exchange coverage are partial when input/output
  labels are incomplete;
- exchange label completeness is not guaranteed and is exposed in coverage;
- Bitget Spot direction remains unknown by default;
- bridge leg matching is disabled;
- no arbitrary token or account-balance coverage.

These limitations do not prevent the deterministic contract and persistence
design. They must remain visible in implementation and runtime acceptance.

## 24. Official semantic references

The design was based on primary/official references, including:

- Ethereum JSON-RPC and block tags: <https://ethereum.org/developers/docs/apis/json-rpc/>;
- EIP-1474 JSON-RPC specification: <https://eips.ethereum.org/EIPS/eip-1474>;
- ERC-20 decimals and Transfer event: <https://eips.ethereum.org/EIPS/eip-20>;
- Ethereum event logging: <https://ethereum.org/developers/tutorials/logging-events-smart-contracts>;
- Bitcoin Core `getblock`: <https://developer.bitcoin.org/reference/rpc/getblock.html>;
- Bitcoin Core `getblockchaininfo`: <https://developer.bitcoin.org/reference/rpc/getblockchaininfo.html>;
- Bitcoin Core `getrawtransaction`: <https://developer.bitcoin.org/reference/rpc/getrawtransaction.html>;
- Bitcoin UTXO/transaction model: <https://developer.bitcoin.org/devguide/transactions.html>;
- Bitget UTA v3 public ticker/spot schema: <https://www.bitget.com/docs/uta/websocket/public/Tickers-Channel>;
- Bitget UTA migration mapping, including v2/v3 trade subscriptions: <https://www.bitget.com/legacy-docs/classic/uta-api-upgrade-guide>;
- Bitget UTA public domains and limits: <https://www.bitget.com/docs/uta/quick-start>;
- Binance official Spot API documentation repository: <https://github.com/binance/binance-spot-api-docs>;
- Binance Spot REST limits: <https://developers.binance.com/en/docs/products/spot/rest-api>.
