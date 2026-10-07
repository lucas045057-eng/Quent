# Data Layer V1 — Failure Matrix and Restart Acceptance

**Result:** PASS

**Scope:** Data Layer V1 Task 7 only; no production database, ECS, private API, orders, or Phase 9.

**Runtime mode:** TRADING_MODE=paper

**Test boundary:** deterministic fixtures and disposable local Docker resources; external network disabled for the restart cycles.

## Failure and recovery matrix

| Scenario | Deterministic evidence | Expected safety behavior | Result |
|---|---|---|---|
| Provider timeout and transient transport errors | tests/test_phase7_sources.py::test_transport_errors_retry_then_report_exhaustion; tests/test_phase8_deribit_rest_transport.py::test_timeout_is_bounded_and_never_falls_back_to_another_endpoint | Retry is finite; timeout/failure remains visible; no endpoint fallback | PASS |
| HTTP 429 / Retry-After | tests/test_phase7_sources.py::test_retryable_429_is_finite_and_honors_retry_after; bounded Ethereum 429 tests in Phase 7 runtime; bounded retry tests in Phase 8 REST transport | Honor bounded Retry-After, stop at configured attempts, fail closed when over budget | PASS |
| WebSocket disconnect, gap, reconnect, and resubscribe | tests/test_phase7_runtime_integration.py::test_binance_websocket_reconnects_and_resubscribes_after_transport_failure; Phase 3 recovery/gap tests; Phase 8 reconnect/reseed tests | Health degrades on disconnect, subscriptions are re-established, gaps remain explicit | PASS |
| Parser/source contract mismatch | Phase 3 adapter schema rejection; tests/test_phase7_runtime_integration.py::test_bitcoin_source_probe_parser_pass_fails_on_contract_mismatch | Invalid or incomplete source data cannot become accepted canonical data | PASS |
| Database admission failure / outage fence | tests/test_data_layer_db_admission.py::test_admission_failure_is_fail_closed_and_does_not_run_writer; test_admission_session_loss_fence_prevents_overlapping_business_writer | No business writer runs without a valid admission slot; prevent overlapping writers | PASS |
| Transaction, event-chunk, or checkpoint failure | tests/test_phase7_persistence.py::test_middle_chunk_failure_rolls_back_rows_and_leaves_cursor_unchanged; test_checkpoint_failure_rolls_back_all_transfer_chunks; DB-admission rollback cases | Atomic rollback; cursor does not advance; admission slot is released | PASS |
| Queue/admission saturation and fairness | tests/test_data_layer_admission.py::test_active_and_pending_items_and_bytes_stay_within_configured_bounds; test_light_control_progresses_while_heavy_work_is_admitted; tests/test_phase6_runtime_integration.py::test_runtime_queue_full_is_bounded_persisted_and_does_not_call_provider | Enforce configured bounds; safe work can progress; overload is explicit/deferred | PASS |
| Cancellation and shutdown during owned work | tests/test_data_layer_admission.py::test_shutdown_cancels_queued_work_and_drains_owned_active_work; Phase 6 owned-writer shutdown tests; Phase 3 blocked-receiver cancellation test | Cancel queued work, drain owned active work, release permits, do not orphan tasks | PASS |
| Phase 6 provider absent | tests/test_phase6_runtime_integration.py::test_no_provider_degrades_without_queueing_or_fake_available | NOT_CONFIGURED / NOT_AVAILABLE; no fabricated success and no provider call | PASS |
| Phase 8 stale WebSocket state and reseed | tests/test_phase8_runtime_integration.py::test_runtime_orders_lifecycle_catalog_markprice_summary_universe_ticker_and_shutdown; tests/test_phase8_deribit_ws_runtime.py::test_disconnect_invalidates_state_and_reconnect_resubscribes; test_reconnect_attempts_are_bounded_until_a_trusted_seed_arrives | Invalidate untrusted state, reconnect within budget, require a trusted reseed | PASS |
| Bitcoin and Ethereum finality / reorg semantics | tests/test_phase7_bitcoin.py::test_reorg_or_block_hash_replacement_is_stale_and_not_available; tests/test_phase7_ethereum.py::test_safe_and_latest_have_different_finality_semantics; test_block_hash_replacement_is_reorged_and_not_available | Replacement is explicit; finality tags retain chain-specific meaning | PASS |
| Bounded reorg lookup | tests/test_data_layer_failure_matrix.py::test_bitcoin_and_ethereum_reorg_reconciliation_window_is_bounded_and_stale; test_reorg_lookup_beyond_bounded_block_range_fails_before_database_access; test_reorg_event_lookup_reads_only_one_sentinel_beyond_cap_then_fails_closed | Reconcile only bounded window; reject >2,400-block range before query and fail closed above 10,000 events using one sentinel row | PASS |

The in-window persistence atomicity test exercises the shared canonical replacement writer with Bitcoin fixtures. Ethereum parser/finality and bounded recovery-window behavior are independently covered. No live-chain reorg was induced.

## Process restart acceptance

The runner used three fresh, short-lived Collector processes against one disposable PostgreSQL instance. The WebSocket and Bitcoin JSON-RPC inputs were deterministic fakes; Docker networking was internal-only and external egress unavailable. This is restart/lifecycle evidence, not live-provider acceptance.

| Cycle | Migrations first / repeat | Durable cursor before → after | Cumulative events / unique IDs | WS connects / subscriptions / acks | Health recovered → final | Queue |
|---:|---:|---:|---:|---:|---|---|
| 1 | 15 / 0 | null → 862 | 12 / 12 | 2 / 4 / 2 | yes → STOPPED | clean |
| 2 | 0 / 0 | 862 → 874 | 24 / 24 | 2 / 4 / 2 | yes → STOPPED | clean |
| 3 | 0 / 0 | 874 → 886 | 36 / 36 | 2 / 4 / 2 | yes → STOPPED | clean |

All three cycles passed. Each process restored the durable cursor, advanced exactly 12 blocks, re-applied migrations idempotently, resumed subscriptions after a simulated disconnect, persisted unique events, recovered health, and exited with no owned tasks or queued work remaining. Collector and PostgreSQL caps remained 256 MiB and 768 MiB respectively; no persistent test volume or production container was used.

## Frozen replay and regression

- Frozen Task 6 replay was run at source SHA 9e8f0c9ab1ceb226d8c3129688b9be1a5883a60e using the pinned local images.
- Three replay outcomes matched with no differing paths; outcome SHA-256: 29de3262e8bbf1c0a43a35db23a5c3ead2e1120489f49dfd67a181aef54e6df3.
- Replay storage guard: 89.5% free before and after; inode use 1%. This short-run measurement is not a long-term storage projection.
- Focused Phase 1–8 deterministic regression: 256 passed, 17 skipped before DB provisioning (DB-gated cases skipped in that run).
- Task 7 failure-matrix tests: 13 passed.
- Final DB-enabled full regression: **1,256 passed, 8 skipped, 0 failed**. The eight skips were public live probes requiring explicit opt-in: Phase 2 (3), Phase 3 (2), Phase 4 (3). The known local Bitget live WebSocket contract file was excluded because of the previously established host network restriction. No private or order API tests were enabled.
- One existing aiohttp BasicAuth deprecation warning remains; it did not fail tests.

## Limits and interpretation

- Reorg/event lookup remains intentionally bounded at 2,400 blocks and 10,000 events; overflow fails closed. This is a known coverage limit requiring explicit policy if future chains need a wider window.
- The three process cycles and frozen replay use deterministic fixtures. They do not establish public-source availability, a 15-minute integrated run, 60-minute soak, sustained resource acceptance, or long-term retention growth.
- No migrations were added or changed in this task. No collector or engine was left running by the acceptance runner.

## Safety outcome

- Paper mode: PASS.
- External network disabled for process restart test: PASS.
- Private API, live trading, and order routes: not used.
- Existing project containers and databases: left running and untouched.
- Temporary test PostgreSQL and restart containers/network: removed by exact generated identity and owner-label checks.
