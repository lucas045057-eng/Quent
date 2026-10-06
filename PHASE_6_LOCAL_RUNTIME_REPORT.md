# Phase 6 Local Runtime Acceptance Report

## Final status

`PHASE6_AI_PROVIDER_CREDENTIAL_REQUIRED`

This runtime acceptance stopped before the live AI gate because no approved AI
provider was configured through the local environment or container secret
mechanism. No credential was requested, created, printed, persisted, or sent.

## 1. Execution boundary

- Runtime scope: WSL2 local only (`/home/lucas045057/projects/quant`).
- Remote ECS, remote PostgreSQL, SSH, deployment, and Jakarta were not accessed.
- Phase 7 was not started.
- `TRADING_MODE=paper` was verified in both running Quant containers.
- No private API, order API, position API, live executor, or real order was used.

## 2. Preflight

- Branch: `phase6`
- HEAD before this report: `eddef49c768648b5768833e8d494961c286a000b`
- Working tree before this report: clean
- `quant-postgres`: running, image `postgres:16-alpine`, OOMKilled=false,
  restart policy `unless-stopped`, memory limit 768 MiB.
- `quant-collector`: running, image `quant-phase5:f55a21b-local`,
  OOMKilled=false, restart policy `unless-stopped`, memory limit 256 MiB.
- `quant-engine`: running, image `quant-phase5:f55a21b-local`,
  OOMKilled=false, restart policy `unless-stopped`, memory limit 384 MiB.
- `docker compose ps`: unavailable because this Docker CLI does not provide the
  `docker compose` subcommand. The actual containers were inspected directly.
- Notice: the running collector and engine images are Phase 5 images; a Phase 6
  runtime image was not deployed during this acceptance.

## 3. PostgreSQL migration gate

- PostgreSQL: 16.15
- Database: `quant`
- `current_database()`: `quant`
- Time zone: `UTC`
- Existing database migration run 1: `011_phase6_external_context.sql`
- Existing database migration run 2: `[]` (0 new migrations)
- Existing history: 001–008, 009 repair/main entries, 010, 011.
- Fresh isolated database run 1: all migrations 001–011 applied successfully.
- Fresh isolated database run 2: `[]` (0 new migrations).
- Fresh isolated database was removed after validation.
- Migrations 001–010 were not modified in the working tree.

## 4. Phase 1–5 regression evidence

Previously completed code-phase evidence retained for this runtime decision:

- Full suite: 590 passed, 12 skipped.
- Phase 6 focused suite: 61 passed, 1 skipped.
- PostgreSQL repository/persistence integration suites passed in isolated local
  databases.
- This round did not claim a new full runtime regression result because the
  mandatory AI credential gate blocked the remainder of acceptance.

## 5. Phase 6 live sources

- Approved live News/Macro/Unlock source gate: not run.
- Source registry path: not configured.
- No new unreviewed source, scraper, crawler, social firehose, or unknown API
  was introduced.
- No event was fabricated; `NO_NEW_EVENT_OBSERVED` is not asserted because the
  live source gate was not entered.
- Parse, timestamps, source identity, provenance, hash, freshness, dedup, and
  persistence live checks: not run.

## 6. AI credential boundary

- `PHASE6_AI_PRIMARY_PROVIDER`: unset.
- `PHASE6_AI_FALLBACK_PROVIDER`: unset.
- Container environment inspection found no configured AI provider/key name.
- A minimal live provider probe was not attempted.
- Provider/model/relay type: not run.
- Input/output token counts, latency, cache, cost, queue, rate, fallback, and
  budget live measurements: not run.
- Safe-context, prompt-injection, strict-schema, and factual-fidelity runtime
  gates: not run. Their offline/code test evidence remains in the Phase 6 code
  reports and tests.
- No key was exposed in terminal output, logs, Git, fixtures, or this report.

## 7. Recovery and stability gates

The following were intentionally not started after the credential blocker was
confirmed:

- controlled collector restart;
- controlled engine restart;
- controlled PostgreSQL outage/restore;
- AI outage/fallback injection;
- post-recovery duplicate-burst verification;
- 30+ minute stability observation;
- live Phase 1–5 continuity verification during the Phase 6 runtime.

## 8. Resource sample

Single `docker stats --no-stream` sample before stopping:

| Container | CPU | Memory | Limit | OOM |
|---|---:|---:|---:|---|
| quant-postgres | 1.76% | 456 MiB | 768 MiB | false |
| quant-collector | 18.07% | 203.3 MiB | 256 MiB | false |
| quant-engine | 0.00% | 310.4 MiB | 384 MiB | false |

Database size sample: 5,142,912,023 bytes (about 4,905 MiB).

This is a measured point-in-time sample, not a long-term growth estimate.

## 9. Skips and known limitations

- `PHASE6_AI_PROVIDER_CREDENTIAL_REQUIRED`: blocking; no approved provider
  credential/configuration is present.
- Live source and AI acceptance gates: skipped because of the credential gate.
- Restart, outage recovery, and 30+ minute stability gates: skipped because the
  acceptance must stop at the credential gate.
- `docker compose ps`: unavailable with the installed Docker CLI; direct
  container inspection was used and recorded.
- Running images are Phase 5 images, so this round does not claim a deployed
  Phase 6 service runtime.

No Phase 7 work was performed.
