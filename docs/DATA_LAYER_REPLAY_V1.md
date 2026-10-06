# DATA_LAYER_REPLAY_V1

`DATA_LAYER_REPLAY_V1` composes a secret-free canonical scenario archive with the existing deterministic Phase 1–7 production-lifecycle replay and Phase 8 parser/repository replay. Its records are synthetic fixtures and must never be presented as live provider evidence.

## Frozen artifact

`tests/fixtures/data_layer_replay_v1/manifest.json` pins the source Git SHA, Collector/Engine/PostgreSQL image digests, migration version, paper-only config hash, compressed and decompressed fixture hashes and sizes, record count, seed, UTC as-of time, required phases, and repository-relative component hashes. The loader enforces:

- Phase 1 through Phase 8 coverage; the fixture builder tests additionally enforce coverage of the registered stream inventory;
- distinct source/event/fetch/process timestamps, explicit provenance and canonical identity;
- explicit missing values (`null` only with `NOT_AVAILABLE`), never synthetic zero substitution;
- no future timestamps, duplicate canonical identities, secret-shaped fields, or credential-bearing URLs;
- bounded compressed bytes (2 MiB), uncompressed bytes (16 MiB), and record count (20,000), derived with headroom from the measured 750,983-byte / 10,028,883-byte / 12,738-record artifact.

The fixture includes a 12,665-event Bitcoin block scenario, Ethereum block/log/receipt identity, Binance Spot cursor input, Phase 6 `NOT_CONFIGURED` AI, and Phase 8 BTC/ETH multi-expiry call/put examples with unknown-unit IV and sparse ticker updates. Fields not supplied remain `NOT_AVAILABLE`; sparse updates preserve prior state.

## Running the integrated replay

Build the application image from the manifest's pinned source commit and make sure both image IDs and the PostgreSQL image ID exactly match the manifest. Then run:

```sh
python scripts/run_data_layer_replay_v1.py \
  --app-image <pinned-local-app-image> \
  --postgres-image <pinned-local-postgres-image>
```

The runner validates the source component hashes and image package hashes before starting. It performs three sequential fresh database repetitions. Each repetition creates an owner-labeled Docker internal network and one PostgreSQL 16 container with a 768 MiB memory/swap cap, UTC timezone, no published host port, and ephemeral tmpfs data directory. Collector and Engine use the actual production lifecycle replay entrypoints under unchanged 256 MiB / 384 MiB caps. A short-lived Phase 8 replay/snapshot helper joins the same internal network under the 384 MiB engine cap. Containers have no external network egress, use paper mode, and run with private RPC disabled. No running project database or production container is reused.

The runner compares all public-table row counts, stable hashes of persisted business tables (excluding only volatile `created_at`/`updated_at` columns and explicitly volatile health-event tables), migration versions, Phase 7 event counts/cursors, Phase 8 context statuses and persisted hashes. Each fresh database must contain migrations 001–015, exactly 12,665 persisted events for the large Bitcoin block, and two idempotently persisted Phase 8 context rows. Exact owned test containers and internal networks are removed; no image, named volume, or unrelated resource is pruned.

The generated `DATA_LAYER_V1_REPLAY_REPORT.md` is fixture-only evidence. Short-replay memory samples and per-run database size are measured values, not a sustained runtime gate or long-term storage projection. Admission weights/slots and sustained thresholds remain provisional if this sample does not establish a representative saturation bound.
