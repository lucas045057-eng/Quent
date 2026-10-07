# PHASE4_BASELINE_AUDIT

Date: 2026-09-21

## Baseline verification

- Accepted Phase 3 branch: `phase3`
- Phase 4 branch: `phase4`
- `git rev-parse phase3`: `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`
- `git rev-parse phase4`: `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`
- `git merge-base phase3 phase4`: `8ad95fa2370d5bae4eef600ffc60ebe2abf69386`
- Working tree at audit: clean

The previously reported `...693d7` suffix was a report typo. The actual
accepted baseline is `...69386`; no history rewrite or reset was required.

## Existing architecture

- Phase 1, Phase 2, and Phase 3 remain additive and are not rewritten.
- The runtime uses the existing `quant-collector` and `quant-engine` services.
- PostgreSQL remains the existing Quant database; no new service is introduced.
- Existing resource ceilings remain PostgreSQL 768 MiB, collector 256 MiB,
  and engine 384 MiB.
- `TRADING_MODE=paper` remains mandatory. No private API, order route,
  position route, or live executor exists in the accepted baseline.

## Existing data and reliability boundaries

- Migrations `001` through `008` are applied by the idempotent migration
  runner and must not be modified.
- Phase 2 canonical OI/Funding contracts already use explicit exchange and
  processed timestamps, status, source endpoint, raw payload, and raw
  reference fields.
- Phase 3 canonical trade contracts already preserve exchange timestamps,
  received timestamps, processed timestamps, source channel, side semantics,
  bounded deduplication, queue pressure, reconnects, gaps, and context-only
  Stage1 enrichment.
- Existing health events and scheduler loops are reused by Phase 4.
- The existing deployment does not contain Phase 4 models, adapters,
  persistence, retention, or enrichment tables. These are the only new
  subsystem boundaries required by this phase.

## Regression baseline

The local offline run completed with:

```text
229 passed, 7 skipped
```

The skips are the explicitly gated live exchange probes and PostgreSQL DSN
integration tests. The accepted Phase 3 runtime report recorded its separate
Jakarta runtime gate as `234 passed, 2 skipped`; Phase 4 acceptance must rerun
the live and database gates rather than treat local skips as passes.

## Audit result

`PHASE4_BASELINE_HEAD_VERIFIED`

Phase 4 is approved as an additive extension from the verified Phase 3 HEAD.
