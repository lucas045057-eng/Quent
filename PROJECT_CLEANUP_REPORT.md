# Project Cleanup Report

Date: 2026-09-27 (Asia/Shanghai)

## Result

- Project: `/home/lucas045057/projects/quant`
- Branch: `data-layer-v1-hardening`
- Audit base HEAD: `be2187e7e6f83c745ea112278525576e9db8adc4`
- Initial worktree: clean; `git diff --check` passed.
- Cleanup changed no tracked source, configuration, schema, fixtures, or existing reports. No directories were moved.

## Storage

| Measurement | Before cleanup | After cleanup, before this report |
|---|---:|---:|
| `du -sh .` | 115M | 105M |
| `du -sb .` | 101,460,265 bytes | 92,230,765 bytes |

Reclaimed: **9,229,500 bytes** (about 8.80 MiB), exactly the logical size of the removed generated caches. The filesystem had 898G available and 1% inode use at inspection time.

## SAFE_DELETE completed

Removed 22 exact, ignored, regenerable targets: 21 Python `__pycache__` directories (containing only `.pyc`/`.pyo`) and `.pytest_cache`.

```text
scripts/__pycache__
src/quant_data_layer/__pycache__
src/quant_data_layer/replay/__pycache__
src/quant_phase1/__pycache__
src/quant_phase1/adapters/__pycache__
src/quant_phase1/adapters/bitget_v3/__pycache__
src/quant_phase1/entrypoints/__pycache__
src/quant_phase1/market/__pycache__
src/quant_phase2/__pycache__
src/quant_phase2/adapters/__pycache__
src/quant_phase3/__pycache__
src/quant_phase3/adapters/__pycache__
src/quant_phase4/__pycache__
src/quant_phase4/adapters/__pycache__
src/quant_phase5/__pycache__
src/quant_phase6/__pycache__
src/quant_phase7/__pycache__
src/quant_phase8/__pycache__
src/quant_phase8/adapters/__pycache__
tests/__pycache__
tests/contract/__pycache__
.pytest_cache
```

## Classification

- **KEEP:** `.git`; source, tests, scripts, migrations, compose files, deterministic fixtures, tracked documentation/reports, `.env.example`, ignored `.env.local`, `.venv`, and acceptance/replay evidence.
- **SAFE_DELETE:** the 22 cache targets listed above; no other item was deleted.
- **MANUAL_REVIEW:** `.venv/` (73M, actively used); `.superpowers/sdd/2026-09-21-phase4-metrics/` (small historical workspace with unresolved status markers). Neither was changed.
- **ARCHIVE_CANDIDATE:** `.superpowers/sdd/2026-09-25-phase8-options-context/` (36.9K; Task 16 is complete and no tracked Markdown reference was found). Kept in place for human review.
- **UNKNOWN:** none identified in the reviewed cleanup candidates.

The `.superpowers/sdd/2026-09-26-data-layer-v1-hardening/` workspace is **KEEP**: it contains Stage 8 report/sample evidence referenced by the acceptance specification. Existing Data Layer status, report, backlog, Phase 1–8 documents, and `outputs/docs` were not moved. No `.db`, `.sqlite`, or `.sqlite3` project files, `node_modules`, or root-level backup/temp candidates were found.

## Logs, duplicates, and Git hygiene

- Existing SDD test/run logs are ignored workspace artifacts. They were retained as historical task evidence; no tracked log was found.
- No duplicate-file groups were found among files at least 1 MiB.
- `.env.local` remains ignored (`.gitignore` rule verified). Its contents were not read. No environment or secret file was modified.
- Git objects: 2,473 loose objects, 17.45 MiB; zero garbage. No Git cleanup/GC was run.
- No `.gitignore` change was needed. No destructive Git command was run.

## Largest remaining space users

Top-level allocated sizes after cache removal (rounded): `.venv` 73M, `.git` 19M, `tests` 6.7M, `.superpowers` 3.0M, `src` 2.2M. The virtual environment is in use and retained.

Largest remaining files observed (all retained):

| Path | Bytes | Classification |
|---|---:|---|
| `tests/fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz` | 3,908,112 | KEEP, replay fixture |
| `.superpowers/sdd/2026-09-26-data-layer-v1-hardening/stage8/d3ae36332b9b/samples.jsonl` | 1,385,329 | KEEP, acceptance evidence |
| `tests/fixtures/data_layer_replay_v1/records.jsonl.gz` | 750,983 | KEEP, deterministic fixture |
| `tests/fixtures/replays/phase6-replay-v2-phase2.jsonl` | 395,584 | KEEP, replay fixture |
| `.superpowers/sdd/2026-09-26-data-layer-v1-hardening/stage8/eea539bbdd37/samples.jsonl` | 319,063 | KEEP, acceptance evidence |

## Docker observation (read-only)

`docker system df` showed images 8.483GB (82 total / 19 active), containers 65.14MB (92 total / 3 active), local volumes 48.89GB (78 total / 21 active), and build cache 0. These resources were not pruned or modified. In particular, Docker volumes and PostgreSQL data were left untouched.

## Minimal validation

- Import smoke for `quant_phase1`, `quant_phase7`, `quant_phase8`, and `quant_data_layer`: **PASS**.
- Config test discovery: **23 collected**.
- Focused config tests (`tests/test_config.py`, `tests/test_phase8_config.py`): **23 passed**.
- `git diff --check`: **PASS**.
- Post-test cache check: no project `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.pyright`, or `.hypothesis` directories reappeared.
- No full regression, public-source probe, Docker runtime, or Phase 9 work was run.

## Recommendation and readiness

No tracked documentation was reorganized because existing references and phase history should remain stable. The SDD Phase 8 workspace may be archived only after human review; the Phase 4 workspace needs manual status review. The project directory is ready for a separately authorized next phase from a cleanup/hygiene perspective: **`PHASE9_DIRECTORY_READY=true`**. This is not authorization to begin Phase 9.
