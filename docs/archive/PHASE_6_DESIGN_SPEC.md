# PHASE 6 DESIGN SPECIFICATION

**Status:** Design only — implementation has not started

**Branch/base:** `phase6` / `610c8afa389581f49b33cfd88cde6d6865ed59ab`

**Operating mode:** `TRADING_MODE=paper`

**Design gate:** This specification must pass the separate review recorded in
`PHASE_6_DESIGN_REVIEW.md` before any implementation begins.

## 1. Objective

Phase 6 adds a bounded external-context layer for News, Macro, and Token
Unlock/Supply Events, plus provider-neutral AI assistance for text/event
processing. It produces normalized facts, provenance, classifications,
extractions, and summaries. It does not produce trading decisions.

AI may assist with:

- event type and entity classification;
- time and impact extraction;
- duplicate assistance and bounded summaries;
- structured context generation from already-normalized, allowlisted data.

AI may not control exchanges, read secrets, execute shell commands, browse
arbitrary URLs, change Stage1 eligibility, or create an order-capable path.

## 2. Hard scope and non-goals

### In scope for a future implementation

- audited, bounded source registry for News, Macro, and Unlock events;
- canonical event contracts, normalization, deterministic fingerprints, and
  type-specific freshness;
- additive persistence for normalized events, AI results, usage, and prompt
  versions;
- provider-neutral AI Gateway and `AISafeContext` allowlist;
- strict structured-output validation, redaction, retries, fallbacks, and
  cost accounting;
- context-only outputs that can be consumed after existing Phase 5/Stage1
  processing;
- offline tests, contract tests, security tests, and resource/replay tests.

### Explicitly out of scope

- private exchange APIs, API keys, secrets, passphrases, authenticated routes;
- order, position, risk-budget, leverage, stop, take-profit, or live executor;
- BUY/SELL/LONG/SHORT or any equivalent action label;
- changing Stage1 A/B/C/D category, eligibility, or reason;
- Evidence Chain, Stage2, Phase 7+, on-chain/whale/options/order-book strategy;
- real AI provider calls during design or offline tests;
- any automatic source fallback that bypasses the source allowlist;
- whole HTML pages, full prompts, full AI responses, or credentials in durable
  storage or logs;
- a News/AI daemon, broker, Redis, Kafka, or additional container without an
  explicit architecture-change decision.

## 3. Architecture and ownership

```text
approved public/licensed source registry
        -> bounded source fetcher / cache
        -> source adapter (version-specific)
        -> canonical event normalizer + fingerprint
        -> PostgreSQL phase6 event tables
        -> AISafeContext builder (allowlist only)
        -> provider-neutral AI Gateway (optional, fail-soft)
        -> validated AI extraction/summary tables
        -> context-only engine output after Stage1 / Phase5
```

Ownership is strict:

| Component | Owns | Must not own |
|---|---|---|
| Source registry/fetcher | allowlisted source identity, bounded retrieval, fetch status | AI decisions or arbitrary URL traversal |
| Source adapter | source-specific parsing and provenance | canonical strategy fields or provider routing |
| Normalizer | canonical event contract, status, time, fingerprint | AI truth decisions |
| PostgreSQL | durable normalized context, usage, versions, idempotency | secrets or unbounded raw content |
| `AISafeContext` builder | explicit field selection and redaction | generic serialization of runtime objects |
| AI Gateway | provider-neutral request/response, validation, usage, failure taxonomy | source fetching, shell, exchange access, orders |
| Engine | bounded orchestration and context-only persistence | Stage1 eligibility or trading actions |

The existing `quant-engine` remains the preferred runtime host. A separate
service is an architecture change and is not part of this design gate.

## 4. Source audit and allowlist

No source is approved merely because a URL exists. Before implementation, each
candidate receives an audited registry entry containing:

| Field | Requirement |
|---|---|
| `source_id` | stable internal identifier, never a display name alone |
| `source_type` | `OFFICIAL`, `RSS`, `PUBLIC_API`, `LICENSED`, `EXCHANGE`, `PROJECT`, or `THIRD_PARTY` |
| owner/domain | verified ownership and HTTPS requirement |
| endpoint/feed | exact path/feed; no arbitrary user-supplied URL |
| terms/limits | license, robots/terms, rate limit, retention restriction |
| parser/version | versioned parser and schema contract |
| coverage | event types, symbols/regions, expected latency |
| availability policy | `AVAILABLE`, `PARTIAL`, `STALE`, `NOT_AVAILABLE`, or `ERROR` behavior |
| raw policy | bounded excerpt/hash/reference only |

The initial implementation must select a small allowlist rather than crawl the
web. Official or licensed sources receive priority. Third-party sources are
advisory and cannot override an official conflict without a recorded
provenance/conflict state. Exchange/project announcements remain external
context, not authenticated exchange data.

Fetches are limited by host, path, response bytes, redirect count, timeout,
concurrency, and per-source rate budget. Redirects must remain on the approved
host allowlist. A source outage becomes a typed status, never a silent source
substitution.

## 5. Common event contract

Every normalized event uses a common envelope. All times are UTC
`TIMESTAMPTZ` values when persisted.

| Field | Type/meaning | Required rule |
|---|---|---|
| `event_id` | deterministic string | derived from source identity, event type/time/entities/content hash; AI cannot invent it |
| `source` | stable `source_id` | must exist in the audited registry |
| `source_type` | controlled enum | agrees with registry |
| `source_ref` | provider-native ID or bounded reference | not a secret or signed URL |
| `url` | approved HTTPS URL or null | host/path allowlisted; no arbitrary fetch from AI output |
| `published_at` | source publication time | may be null with explicit reason |
| `observed_at` | domain/source observation time | source time, not local receipt time |
| `event_at` | time the event applies to | may be a bounded interval or null when unknown |
| `fetched_at` | local fetch completion time | always UTC for fetched rows |
| `processed_at` | local normalization/AI completion time | always UTC |
| `event_type` | controlled taxonomy | unknown types become `NOT_AVAILABLE`/review, not guessed |
| `entities` | bounded canonical entity references | no raw arbitrary object |
| `symbols` | validated canonical symbols | empty is valid only with explicit non-symbol scope |
| `summary` | normalized bounded text | source text is untrusted data; no instructions |
| `importance` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`, `NOT_AVAILABLE` | context label only, not a trade signal |
| `sentiment` | optional controlled descriptive label | never used as an order direction |
| `status` | Phase 6 status enum | status must explain missing/stale/error fields |
| `confidence` | bounded extraction confidence | not probability of truth; source provenance remains authoritative |
| `content_hash` | SHA-256 of canonical bounded content | enables deterministic dedup/cache keys |
| `parser_version` | parser contract version | required for replay |
| `raw_reference` | URI/key/hash/byte metadata | reference only; no permanent full HTML |
| `provenance` | source/content/time/parser/model metadata | required for all derived fields |

The raw payload, if temporarily needed for parsing, is bounded, redacted, and
subject to cache expiry. A durable row stores `raw_reference` and hashes rather
than a full page or response.

## 6. News contract

News events add:

- `headline` and bounded `summary`;
- `event_type` such as `LISTING`, `DELISTING`, `PROTOCOL_UPDATE`, `SECURITY`,
  `PARTNERSHIP`, `REGULATION`, `EXCHANGE_ANNOUNCEMENT`, or
  `GENERAL_MARKET_CONTEXT`;
- canonical entities and symbols;
- `importance`, optional descriptive `sentiment`, and `impact_horizon`;
- source conflict and duplicate references.

The event is `AVAILABLE` only when source identity, event type, time semantics,
and provenance meet the source contract. Missing publication/event time is not
filled from fetch time; it is explicit `PARTIAL` or `NOT_AVAILABLE` according
to the field's requiredness. A news headline is never an instruction.

## 7. Macro contract

Macro events use the following fields:

| Field | Rule |
|---|---|
| `macro_event_type` | controlled release/calendar taxonomy |
| `region` | canonical region/country code |
| `scheduled_at` / `released_at` | UTC source times, separately retained |
| `actual`, `forecast`, `previous` | numeric values or null; never zero-filled |
| `unit` | required whenever a numeric value exists |
| `source`/`source_ref` | audited provenance |
| `status`/`freshness` | explicit Phase 6 status and reason |
| `surprise` | deterministic `actual - forecast` only when units, scale, and release are compatible |

If units, revisions, or release identity are incompatible, `surprise` is
`NOT_AVAILABLE`. Macro context is descriptive and cannot change Stage1.

## 8. Token Unlock/Supply Event contract

Unlock events use:

- canonical `symbol`/`asset`;
- `event_at` and optional bounded event interval;
- `amount` with explicit unit;
- optional notional `value` with valuation timestamp and currency;
- `circulating_supply_ref` describing source and as-of time;
- `unlock_pct` only when numerator/denominator use compatible units and the
  supply reference is present;
- `recipient_category` from a controlled taxonomy;
- source, provenance, status, and freshness.

No percentage is inferred from a headline, rounded display, or missing supply
reference. If the denominator is absent or incompatible, `unlock_pct` is null
and the row remains `PARTIAL` or `NOT_AVAILABLE` with a reason.

## 9. Status, freshness, and provenance

### 9.1 Status vocabulary

Phase 6 event and AI-derived rows use:

```text
AVAILABLE | PARTIAL | STALE | NOT_AVAILABLE | ERROR
```

Precedence is:

1. persistence/contract failure -> `ERROR`;
2. required source is beyond its type-specific freshness window -> `STALE`;
3. required field/source absent or below coverage -> `NOT_AVAILABLE`;
4. usable bounded result with missing optional fields -> `PARTIAL`;
5. all required fields and provenance valid -> `AVAILABLE`.

An AI result cannot promote a source row from `NOT_AVAILABLE` to `AVAILABLE`.
AI extraction is marked `PARTIAL` or `NOT_AVAILABLE` if it lacks source-backed
fields. Confidence is displayed separately from status.

### 9.2 Type-specific freshness

Freshness is configuration, not one universal timeout:

- News: measured from source `published_at`/`observed_at`, with source-class
  and event-type windows; late publication is not mistaken for live data.
- Macro: scheduled/released calendar semantics; a future scheduled event is
  not stale, while a released value beyond its configured revision window is
  stale until refreshed.
- Unlock: event-time proximity and source update cadence; a historical event
  remains valid as a historical fact but is not current future supply context.
- AI: result freshness is bounded by input content hash, prompt version,
  schema version, and model policy; changed inputs invalidate cache.

Each row records `freshness_checked_at`/`processed_at` and the applicable
policy version. Stale or missing context is visible and never converted to a
neutral numeric value.

### 9.3 Provenance

Every derived field can be traced to:

```text
source_id, source_ref, url, published/observed/event/fetched/processed times,
content_hash, parser_version, model_id (if any), prompt_version,
schema_version, extraction_version
```

Raw references are bounded and may be expired. Secret-bearing headers,
cookies, signed URLs, credentials, and unredacted provider responses are never
provenance fields.

## 10. Deterministic deduplication and event identity

The normalizer computes a canonical fingerprint from source, normalized event
type, normalized entities/symbols, event-time representation, and content hash.
Whitespace, tracking parameters, and provider-specific ordering are normalized
before hashing. The fingerprint is stable across retries and process restarts.

AI may suggest duplicate candidates, but only the deterministic fingerprint and
bounded source comparison can decide persistence identity. A duplicate keeps
the earliest stable identity and records later observations/revisions without
creating an unbounded copy.

## 11. Provider-neutral AI Gateway

The design exposes contracts, not an implementation:

```text
AIProvider
AIRequest
AIResponse
Usage
StructuredOutput
AIError
```

Required semantics:

- provider-neutral model/provider IDs; no domain import of OpenAI, DeepSeek,
  Anthropic, or a relay;
- request includes `prompt_version`, `schema_version`, `model_policy`, safe
  context hash, timeout, and token/output budgets;
- response includes validated structured data, provider/model metadata,
  usage, latency, request correlation ID, and redacted error state;
- errors distinguish timeout, rate limit, authentication/configuration,
  transport, provider rejection, invalid JSON, schema failure, budget, and
  policy block;
- the gateway has no shell, file, browser, exchange, database-secret, or
  arbitrary-URL tools;
- cheap/primary/future provider routing is configuration-driven. No provider
  name or endpoint is hard-coded in domain logic.

No provider key, endpoint credential, or SDK is added in the design branch.

## 12. `AISafeContext` and secret boundary

`AISafeContext` is an explicit immutable DTO assembled field by field. It is
not `model_dump()`/`vars()`/JSON serialization of a runtime object.

### Allowed fields

- normalized event IDs, event types, symbols, canonical entities, bounded
  summaries/headlines;
- source type/ID, non-sensitive URL/reference, event/published/observed times;
- status, freshness reason, bounded descriptive importance/sentiment;
- compatible macro values and unlock values with their units and provenance;
- selected Phase 5 context fields explicitly marked context-only;
- content hash, parser/schema versions, and bounded evidence references.

### Forbidden fields

- Bitget/API keys, secrets, passphrases, auth headers, cookies, sessions, JWTs,
  passwords, SSH keys, database DSNs/passwords, private keys, withdrawal
  credentials, provider tokens, signed URLs, or raw request headers;
- full database rows, connection objects, file paths, environment mappings,
  exception objects containing secrets, arbitrary raw HTML, or whole payloads;
- order/position/risk/executor controls or anything that could authorize a
  trade.

The builder rejects unknown fields, recursively redacts known secret patterns,
caps string/list/object sizes, and records only a safe-context hash for
telemetry. Tests must prove that adding a secret-looking field to an input
object cannot reach the AI request.

## 13. Prompt-injection and untrusted-content policy

External News, Macro, Unlock, and provider responses are untrusted **DATA**.
They are passed in a delimited data section after system/developer policy, with
explicit instructions to treat embedded commands, links, role claims, and
tool requests as text. The model cannot:

- execute shell or Python;
- read files, environment variables, database connections, or secrets;
- call arbitrary URLs or exchange APIs;
- change routing, risk, Stage1 eligibility, or runtime configuration.

Prompt templates are versioned and reviewed. User/source text is never
concatenated into system instructions. A prompt-injection detection result is
logged as a bounded policy reason and does not cause a retry storm.

## 14. Structured output and hallucination controls

AI outputs must be strict JSON validated against a versioned JSON Schema or
Pydantic model. The validator rejects:

- unknown keys when the schema is closed;
- invalid enum/status/unit/time values;
- fields without source-backed evidence references;
- incompatible numeric units or invented percentages;
- confidence outside `[0,1]` or presented as truth probability;
- output size over the configured bound.

Parse/schema failure produces `ERROR` for the AI attempt and a bounded
deterministic fallback. It never produces a plausible-looking `AVAILABLE`
event. Deterministic normalization remains the authority for identity, time,
units, and source status.

## 15. Failure, fallback, cache, rate, and cost policy

AI is optional and fail-soft:

- source normalization and Phase 1-5 processing continue when AI is down;
- deterministic labels/summaries may be used only when their contract is
  explicitly defined, otherwise the AI field is `NOT_AVAILABLE`;
- retries are bounded with exponential backoff and jitter for retryable errors;
- concurrency, queue length, input bytes, output tokens, and per-source fetch
  rate are bounded;
- no unbounded in-memory queue or retry loop is allowed;
- provider routing supports a cheap model, a primary model, and a future
  provider through configuration and policy, not domain conditionals;
- daily and monthly soft/hard usage budgets are configuration-driven;
  soft limits degrade to deterministic mode, hard limits reject new AI work;
- usage records include provider/model, request/response token counts when
  supplied, estimated cost, latency, status, cache hit, and budget decision;
- cache keys include input content hash, normalized context hash,
  prompt/schema versions, provider/model policy, and temperature/parameters;
- cache values are bounded, redacted, expire by policy, and never become a
  permanent copy of full prompts/responses.

## 16. Prompt and schema versioning

Every AI request and persisted result carries:

```text
prompt_version
schema_version
model_policy_version
parser_version
normalization_version
```

Version changes invalidate affected cache keys. Old results remain replayable
or are explicitly marked superseded; they are not silently reinterpreted.

## 17. Persistence design for Migration 011 (design only)

Migration 011 is additive and idempotent. The following logical tables are
planned; no SQL is created in this phase:

| Logical table | Purpose | Key safety |
|---|---|---|
| `phase6_source_registry` | audited source allowlist, parser, limits, policy version | unique source/version |
| `phase6_news_events` | normalized News envelope and bounded references | unique deterministic event fingerprint |
| `phase6_macro_events` | normalized macro release and compatible values | source event/revision identity |
| `phase6_unlock_events` | normalized supply/unlock facts | symbol/event/source fingerprint |
| `phase6_ai_analyses` | validated summary/classification result | input hash + prompt/schema/model version |
| `phase6_ai_extractions` | structured field-level provenance and evidence refs | analysis/result identity |
| `phase6_ai_usage` | usage, latency, cost, cache, budget, error class | request/correlation identity |
| `phase6_prompt_versions` | reviewed prompt/schema metadata and status | immutable version identity |

Each table requires bounded columns, UTC timestamps, explicit status/reason,
provenance/content hashes, natural-key uniqueness, and indexed retention
timestamps. Foreign keys must reference existing canonical symbols only where
the event truly has a symbol. Non-symbol macro/global events must not be
forced into `symbols`.

The final implementation may reduce or combine tables after contract review;
it must not mechanically create all logical tables without a usage need.
Migration 011 must pass empty-database, repeat-application, invalid-schema,
rollback-on-error, and old-migration-integrity tests. It must not alter
migrations 001-010.

## 18. Retention and resource policy

Retention is configuration, with conservative defaults for the existing
768/256/384 MiB ceilings. Suggested initial defaults are:

```text
PHASE6_NEWS_RETENTION_DAYS=30
PHASE6_MACRO_RETENTION_DAYS=730
PHASE6_UNLOCK_RETENTION_DAYS=730
PHASE6_AI_ANALYSIS_RETENTION_DAYS=30
PHASE6_AI_USAGE_RETENTION_DAYS=365
PHASE6_RAW_CACHE_TTL_HOURS=24
PHASE6_MAX_RAW_BYTES=262144
PHASE6_MAX_AI_INPUT_BYTES=65536
PHASE6_MAX_AI_OUTPUT_BYTES=32768
```

These are design defaults, not code or deployment changes. Cleanup must be
indexed, batched, observable, and safe to resume. Full HTML, prompts, and
responses are not durable by default. PostgreSQL growth must be measured as
`MEASURED_SAMPLE`; any longer projection must be labeled `PROJECTED_ESTIMATE`.

No new container is necessary for the first implementation. A bounded engine
worker or scheduled engine hook is preferred. A separate queue/cache/AI
service requires `ARCHITECTURE_CHANGE_REQUIRED` and a new resource review.

## 19. Stage1 and Phase5 boundary

The allowed flow is:

```text
canonical source data -> Stage1 immutable result -> Phase5 context
                      -> Phase6 external context / AI enrichment
```

Phase 6 may append context-only references after the existing result. It must
not be imported by the Stage1 eligibility evaluator or used to alter A/B/C/D,
universe membership, risk, or execution. Phase 5 remains deterministic and
must continue to work when Phase 6 is disabled or all Phase 6 statuses are
`NOT_AVAILABLE`.

No Evidence Chain is created. Provenance fields are for source traceability,
not a Phase 9 evidence system.

## 20. Security acceptance tests required before implementation acceptance

- static scan proves no private exchange API, order route, position API, live
  executor, or provider secret requirement;
- `TRADING_MODE=paper` remains mandatory and no live default path exists;
- forbidden-secret fixtures never enter `AISafeContext`, prompts, responses,
  exceptions, logs, or usage telemetry;
- prompt injection fixtures cannot call tools, URLs, shell, files, or secrets;
- provider/domain import scan proves adapters depend only on Gateway contracts;
- unknown structured fields, invalid units, fake percentages, and unsupported
  event types are rejected;
- source allowlist blocks unapproved hosts and redirects;
- AI outage, timeout, rate limit, invalid JSON, budget exhaustion, and cache
  corruption degrade without stopping Phase 1-5;
- restart/replay creates no duplicate event or AI rows;
- Stage1 output is byte/semantically identical with Phase 6 disabled/enabled;
- retention and resource tests remain within the existing service ceilings.

## 21. Design acceptance criteria

The Phase 6 design gate passes only when:

1. the baseline audit is accurate against the accepted Phase 5 HEAD;
2. all source/event/AI contracts are explicit and versioned;
3. `AISafeContext` is allowlist-based and secret-safe;
4. status, time, freshness, provenance, dedup, and fallback rules are
   deterministic;
5. Migration 011 is described without being created or executed;
6. costs, limits, retention, and degraded behavior are bounded;
7. Phase 1-5 and Stage1 boundaries are testable and unchanged;
8. a separate review finds no blocker;
9. the branch remains clean after committing design artifacts.

## 22. Known limits

- No source is approved until its terms, schema, freshness, and rate policy
  are audited during implementation.
- AI cannot validate the truth of an external event; it can only produce a
  bounded, provenance-linked interpretation.
- Macro revisions and unlock supply denominators can invalidate historical
  derived values; versioning and status must make that visible.
- External source outages and provider outages are expected degraded states,
  not reasons to fabricate availability.
- Long-term storage growth cannot be claimed from the current short runtime
  samples; acceptance must measure it again.
