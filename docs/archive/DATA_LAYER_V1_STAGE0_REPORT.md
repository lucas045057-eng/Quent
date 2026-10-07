# Data Layer V1 Hardening — Stage 0 Report

- **Result:** `STAGE_0_PASS=true`
- **Captured at:** 2026-09-26T07:25:09Z
- **Branch / HEAD:** `data-layer-v1-hardening` / `978d8ddef625165eb6347982d6abe749bae5f2c8`
- **Frozen application baseline:** `73c157c7fcf259a9b7df3bfc535082afde0af343`

## Scope and baseline

Stage 0 ran before any hardening source changes. A tracked-source comparison confirmed `src/`, `tests/`, `migrations/`, and `scripts/` matched the frozen Phase 8 application baseline. HEAD differs from that baseline only in approved design documents. No Collector or Engine was launched by this test.

## Disposable PostgreSQL

- Image: `postgres:16.15-bookworm`; actual server: PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2).
- Image ID: `sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67`.
- Test container: `quant-dlv1-stage0-pg-20260926c` (ID `c7ddaeed894a6a99c80976ca86025566e35a3021670500f8e66683400b72e729`); memory and memory-swap caps: 768 MiB; data directory: 512 MiB tmpfs; no Docker volume mounts.
- Host binding was loopback-only with an ephemeral port. Authentication was trust only inside this disposable local test instance. The DSN value was not saved in evidence.
- Fresh database: `quant_phase8_test`; separate upgrade database: `quant_phase8_upgrade_test`.

## Migration results

Fresh database:
- Migrations 001–015 applied successfully: 15 SQL migrations.
- Schema validation passed; database timezone was UTC.
- The runner also records the existing Phase 4 repair marker, so `schema_migrations` contained 16 rows (015 plus 001–014 and the repair marker).
- A second runner invocation applied 0 migrations. Repeated execution remained a no-op.
- Fresh Phase 7 evidence snapshot showed empty health and Phase 7 data tables.

Independent upgrade path:
- Applied 001–014 to a separate empty database: 14 SQL migrations, 15 recorded rows including the Phase 4 repair marker.
- The Phase 8 context table was absent before 015.
- Applied 015 successfully; `phase8_option_context_snapshots` then existed.
- Repeated full migration application returned 0 migrations. Database timezone was UTC.

## Tests

Focused command (with `TEST_POSTGRES_DSN` set to the disposable loopback database):
`.venv/bin/pytest -q -ra --tb=short tests/test_migrations.py tests/test_phase8_migrations.py tests/test_phase8_persistence.py tests/test_phase8_replay.py tests/test_phase8_runtime_integration.py`

- **36 passed, 0 skipped, 0 failed** (2.70s).

Full database-enabled regression command:
`.venv/bin/pytest -q -ra --tb=short`

- **1094 passed, 8 skipped, 0 failed** (38.52s).
- Database-dependent skips: **0**.
- All 8 skips were opt-in public live contract probes:
  - `tests/contract/test_phase2_live.py::test_bitget_uta_v3_public_oi_and_funding_contract`
  - `tests/contract/test_phase2_live.py::test_bybit_v5_public_oi_funding_and_instrument_contract`
  - `tests/contract/test_phase2_live.py::test_hyperliquid_public_info_contract`
  - `tests/contract/test_phase3_public_live.py::test_official_public_rest_contracts`
  - `tests/contract/test_phase3_public_live.py::test_official_public_websocket_subscriptions_and_payloads`
  - `tests/contract/test_phase4_public_live.py::test_official_public_basis_schemas`
  - `tests/contract/test_phase4_public_live.py::test_official_public_long_short_schemas`
  - `tests/contract/test_phase4_public_live.py::test_official_public_liquidation_websocket_schemas`
- One existing warning: Phase 7 runtime test uses deprecated `aiohttp.BasicAuth`; no test failure.

Sanitized run logs and the run manifest are in the ignored SDD evidence directory:
`.superpowers/sdd/2026-09-26-data-layer-v1-hardening/`.

## Cleanup, storage, and non-interference

The disposable database container was stopped and removed by its exact name after evidence capture. It had no volume mounts, so all test databases and rows were discarded with the container. No prune or broad cleanup ran.

Storage before and after Stage 0: filesystem 1007 GiB total / 902 GiB available (about 89% free); inode use 1%. Docker image, volume, and build-cache cleanup was not performed.

Existing `quant-engine`, `quant-postgres`, and `agentops` containers were not targeted by any command. They were observed Up in snapshots. Their reported uptime reset between separate WSL/Docker observations; the cause is unverified and is an operational warning, not evidence that this Stage 0 test started them. No conclusion about their uninterrupted runtime is claimed.

No production database, .env.local value, credential, live provider, ECS, Collector, or Engine was accessed by the test workflow.

## Gate conclusion

The final-SHA database regression gate passes. DL-12 is closed. This report does **not** claim overall Data Layer V1 acceptance and does not authorize Phase 9. Stage 1 may begin only after this report/backlog checkpoint is committed.
