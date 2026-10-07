# PHASE 6 BASELINE AUDIT

**Audit date:** 2026-09-22 (UTC)

**Design branch:** `phase6`

**Phase 5 accepted base:** `phase5` at `610c8afa389581f49b33cfd88cde6d6865ed59ab`

**Audit result:** `PHASE6_BASELINE_AUDIT_READY`

## 1. Audit scope and hard guardrails

This is a design-only audit for the local WSL2 repository at
`/home/lucas045057/projects/quant`. It does not connect to Jakarta, Hangzhou,
Singapore, Alibaba Cloud, an exchange, or a remote database.

The Phase 6 design surface is limited to external context and text/event
processing:

- bounded News, Macro, and Token Unlock/Supply Event ingestion;
- deterministic event normalization, provenance, deduplication, and freshness;
- an AI provider abstraction for classification, extraction, and summaries;
- an explicit `AISafeContext` allowlist and redaction boundary;
- bounded cost, reliability, cache, and persistence design;
- context-only enrichment consumed after existing Phase 5/Stage1 outputs.

The following are not authorized by this audit:

- Phase 6 feature code, source adapters, AI SDK installation, provider keys, or
  real AI calls;
- Migration 011 SQL or any database mutation;
- trading actions, signals, BUY/SELL/LONG/SHORT labels, position sizing,
  leverage, orders, stops, live execution, or private exchange APIs;
- Phase 7 or later work, Evidence Chain, or remote deployment.

## 2. Repository and Git baseline

The preflight was run in the requested WSL2 directory before branch creation:

| Check | Result |
|---|---|
| Preflight branch | `phase5` |
| Preflight HEAD | `610c8afa389581f49b33cfd88cde6d6865ed59ab` |
| Preflight worktree | clean |
| New design branch | `phase6` |
| New branch HEAD | same exact `610c8afa389581f49b33cfd88cde6d6865ed59ab` |
| Phase 5 report | `PHASE5_LOCAL_RUNTIME_ACCEPTED` |
| Host runtime report | `WSL2_QUANT_HOST_RUNTIME_STABLE` evidence retained |
| Phase 5 history | unchanged; no rewrite or reset |

The older Phase 5 audit contains historical base references from before the
final acceptance commits. This audit uses the verified final accepted HEAD
above as the authoritative Phase 6 parent.

## 3. Existing architecture and data surface

The current topology is intentionally small:

```text
public exchange adapters / collector
        -> canonical market data in PostgreSQL
        -> quant-engine deterministic Phase 1-5 processing
        -> context-only derived persistence
```

Existing reusable surfaces include:

- `symbols`, instruments, tickers, market snapshots, observations, and closed
  klines;
- point-in-time `universe_runs` and immutable Stage1 screening results;
- Phase 2 derivative observations, Phase 3 flow/CVD context, and Phase 4
  market context with explicit missing/stale/error semantics;
- Phase 5 deterministic market, breadth, relative-strength, sector, and
  context-only enrichment tables;
- the existing migration runner and exactly three local Compose services:
  PostgreSQL, `quant-collector`, and `quant-engine`.

Phase 6 must reuse the engine/PostgreSQL boundary. It must not create a
second raw warehouse, broker, queue, cache daemon, or standalone AI service
without a separately approved architecture change.

## 4. Existing schema and migration baseline

Migrations `001` through `010` exist, with `010_phase5_context.sql` providing
bounded deterministic Phase 5 context tables. The runner records migration
names in `schema_migrations`, uses a transaction/advisory lock, and is designed
for repeat application.

Phase 6 may design an additive, idempotent Migration 011, but this branch does
not create that SQL. Existing Phase 1-5 tables and migration files are frozen:

- no drop, truncate, rename, or destructive rewrite;
- no changes to Stage1 decision columns or existing source semantics;
- no role escalation or database-wide permission changes;
- future tables must use UTC-capable `TIMESTAMPTZ` fields and bounded JSON or
  references rather than unbounded raw payloads.

## 5. Current contracts, statuses, and time model

The canonical Phase 1 contract has `symbol`, `value`, source/exchange identity,
`exchange_timestamp`, `fetched_at`, `processed_at`, status, and raw payload or
reference. Its primary `DataStatus` is:

```text
AVAILABLE | STALE | NOT_AVAILABLE | ERROR
```

Phase 3 and Phase 5 use `PARTIAL` where incomplete-but-usable coverage is a
meaningful derived state. Phase 6 will not silently change the existing enum;
its design contract uses:

```text
AVAILABLE | PARTIAL | STALE | NOT_AVAILABLE | ERROR
```

`PARTIAL` means bounded usable coverage with explicitly recorded missing
fields, not an assertion that the event is complete. AI confidence is not a
status and is not a probability that an event is true.

All timestamps are UTC and retain their distinct meanings:

- `published_at` / `event_at`: source or domain time;
- `observed_at`: time the event was observed by the source adapter;
- `fetched_at`: time the system completed retrieval;
- `processed_at`: time normalization or AI processing completed.

The design never collapses these into one timestamp.

## 6. Dependency and capability audit

`pyproject.toml` currently contains only the existing HTTP/WebSocket,
Pydantic, PostgreSQL, and test dependencies. No OpenAI, Anthropic, DeepSeek,
LangChain, generic LLM SDK, News SDK, or scraper package is installed. No
Phase 6 module, provider client, event adapter, Migration 011, or real AI
configuration exists at this baseline.

This is an accepted design prerequisite, not an implementation gap to paper
over. The future implementation must not add a provider SDK until the provider
abstraction, secret policy, offline tests, and approval gate exist.

## 7. Runtime, resource, and safety baseline

The accepted local Compose topology and ceilings are:

| Service | Memory limit | Policy | Phase 6 implication |
|---|---:|---|---|
| `quant-postgres` | 768 MiB | `unless-stopped` | reuse; bounded tables/retention only |
| `quant-collector` | 256 MiB | `unless-stopped` | no News/AI daemon; acquisition remains bounded |
| `quant-engine` | 384 MiB | `unless-stopped` | context orchestration must be bounded and fail-soft |

`TRADING_MODE=paper` is the default and remains mandatory. No existing service
has a live executor. Phase 6 must not increase resource ceilings merely to
hide unbounded raw HTML, prompt, response, queue, or cache growth.

The Phase 5 acceptance recorded explicit missing/stale external data rather
than fabricating values. That rule carries forward: no synthetic News, Macro,
Unlock, or AI observations are permitted.

## 8. Known historical debts carried forward

The Phase 5 report retains external-source limitations, including the Bybit
access limitation and a later ticker/snapshot WebSocket reconnect limitation.
Those are not reopened by Phase 6 design. News/Macro/Unlock source selection
must start from a new source audit and must not silently use a degraded source
as a substitute for an approved one.

## 9. Baseline decision

The repository is suitable for a Phase 6 design specification. It is **not**
yet authorized for implementation. The required design gates are:

1. source allowlists and provenance contracts;
2. common event contracts and explicit status/freshness rules;
3. provider-neutral AI Gateway and strict `AISafeContext`;
4. redaction and prompt-injection controls;
5. cost, cache, retry, fallback, and degraded-mode limits;
6. additive Migration 011 design without SQL execution;
7. retention/resource limits and Stage1/Phase5 isolation;
8. a separate design-review pass with all blockers resolved.

The companion `PHASE_6_DESIGN_SPEC.md`, `PHASE_6_DESIGN_REVIEW.md`, and
`docs/superpowers/plans/2026-09-22-phase6-context.md` satisfy the design
documentation gate only. No feature implementation has begun.
