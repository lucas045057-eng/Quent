# Data Layer V1 Hardening — Final One-Shot Acceptance Report

**Date:** 2026-09-27
**Branch:** `data-layer-v1-hardening`
**Disposition:** Implementation and deterministic acceptance pass; live
acceptance blocked by environment.

```text
DATA_LAYER_V1_IMPLEMENTATION_COMPLETE=true
DATA_LAYER_V1_DETERMINISTIC_ACCEPTANCE_PASS=true
DATA_LAYER_V1_LIVE_ACCEPTANCE_PASS=false
LIVE_ACCEPTANCE_BLOCKED_BY_ENVIRONMENT=true
```

Overall Data Layer V1 acceptance is **not** granted.

## Deterministic evidence

- Stage 0–7 had previously passed.
- Focused acceptance/runtime tests rerun for this closeout: **71 passed**.
- Previous full regression evidence: **1287 passed, 8 skipped, 0 failed**.
- No production code, database schema, runtime configuration, or resource cap
  was changed in this closeout.

## Live gate and blocker

The only live blocker is Deribit host-side direct connectivity:

- The IPv4 DNS answer was a public address.
- The WSL direct IPv4 request reached the endpoint path but TLS timed out.
- The Windows direct connection timed out before establishing the connection.
- The DNS safety preflight additionally observed one non-public AAAA answer;
  the formal transport could therefore not be certified safe to proceed.
- The Phase 8 REST/catalog probe was not run.
- Stage 8 (900-second acceptance) and Stage 9 (60-minute soak) were
  intentionally not run.

This evidence is classified as an **environment blocker**, not a production
code defect. No further DNS, TLS, proxy, TUN, or Deribit investigation was
performed for this closeout.

## PostgreSQL resource handling

The earlier `FILE_CACHE_PRESSURE` result came from a non-formal diagnostic
container and is retained only as diagnostic evidence. It is not a formal
Stage 8/Stage 9 result and does not justify a PostgreSQL parameter or cap
change. Since Stage 8/9 did not run, no formal PostgreSQL resource conclusion
is made. The formal caps remain:

- PostgreSQL: 768 MiB
- Collector: 256 MiB
- Engine: 384 MiB

## Final disposition

The implementation and deterministic acceptance are complete; live
acceptance remains false due to the single external Deribit connectivity
blocker. This one-shot sprint is closed. Stage 8 and Stage 9 must not be
repeated, and Phase 9 must not start, without a new explicit instruction.
