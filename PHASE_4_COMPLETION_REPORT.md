# Phase 4 Completion Report

Status: `PHASE4_CODE_COMPLETE_RUNTIME_PENDING`

This report records only the verified local evidence for the final global
rollup-recovery fix. Jakarta Runtime Acceptance remains pending. No Jakarta
connection, deployment, external migration, service restart, or Phase 5 work
was performed.

The prior local evidence source remains recorded for traceability: requested
plan commit `0e63dcc` is not reachable, and reachable evidence commit
`808293bff50aeb02b10e2e0ff6d5fbe60b44b9c6` is retained. Those history facts do
not expand the verified scope of this report.

## Verified fix

- The global rebuild marker now retains the bounded retry helper's advanced
  source range instead of restarting every retry at the first chunk.
- The marker durably retains its active exchange/symbol/timeframe cursor and
  the overall rebuild range. When one timeframe completes, the overall range
  is restored for the next timeframe; when one source scope completes, the
  cursor advances to the next scope.
- Global fallback scope discovery is keyset-paginated with bounded pages and
  is consumed one scope at a time. It does not fetch and materialize all
  retained 1-minute scopes.
- The regression covers two source scopes, multiple bounded chunks, all
  required rollup timeframes, cursor advancement, and completion without
  missing retained rollup identities.

## Verification evidence

Observed local checks for this fix round:

- Focused Phase 4 tests: `214 passed, 1 skipped`.
- Phase 1–3 regression selection: `221 passed, 2 skipped`.
- Full local pytest suite: `444 passed, 11 skipped`.
- `git diff --check`: clean.

The PostgreSQL-gated and live-contract tests remain explicit skips when their
environment variables are absent. Those skips are not runtime acceptance.

## Boundary

Only local code and deterministic test evidence are covered here. Runtime
acceptance against PostgreSQL/Jakarta, external migration, deployment, and
Phase 5 remain pending and are outside this fix.
