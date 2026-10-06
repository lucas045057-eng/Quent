# Quant Core + NautilusTrader v1.231.0 integration implementation plan

Status: executable plan, 2026-09-28. Design: docs/superpowers/specs/2026-09-28-nautilus-v1-integration-design.md. The existing Phase9 plan docs/superpowers/plans/2026-09-27-phase9-evidence-chain.md remains authoritative for Task1–9 and its exact signatures, files, commands, test and final acceptance requirements. This plan adds prerequisite Core repairs and subsequent local backtest/Paper integration. Worktree: /home/lucas045057/projects/quant-integration-nautilus-v1, branch integration/nautilus-v1 from clean 2f9cbab. Never mutate the original checkout, its existing containers, real exchange accounts or credentials.

## Global rules

1. First use verified git status/branch/HEAD; stop for unknown user modifications. Do not reset --hard or clean -fdx.
2. RED test, observe failure, minimum GREEN change, affected regression, then commit at each milestone. Do not skip or xfail blockers. For an unexpected failure, inspect root cause before patching.
3. Before each Phase9 Task1–9, run the exact compatibility gate in the worktree. Four evidence artifacts are ignored/generated: copy the existing Task0 artifacts to the isolated worktree initially, then regenerate all four whenever a protected path changes. Never change the closure commit resolver, marker or digest rules.
4. Required integration validation uses disposable loopback PostgreSQL only; do not touch quant-engine, quant-postgres or agentops. Real Jev and real exchange credentials are unnecessary. Never start live trading.
5. After each milestone record command, exit code, test counts, git commit and remaining limitations. No self-declared PASS. Source/runtime verification uses the pinned dependency. Commit only reviewed files. No push/PR/merge/tag.
6. Test fixture policy approval may be fixture-only and cannot activate non-fixture runtime. If the Phase9 approved policy cannot be truthfully supplied under the existing plan, preserve fail-closed behavior and report the exact approval blocker rather than forging approved_by.

## M0 — Baseline, isolated environment and design

- Verify this worktree's branch and source commit, old checkout clean and protected tree digest.
- Copy only ignored Phase9 compatibility evidence into this worktree, run the gate and verify exit 0.
- Read Python/WSL architecture, Docker version and pinned Nautilus v1 requires-python/wheel compatibility before installation.
- Commit this integration Spec and Plan after one documented self-review of each. No product code in M0.
- Gate: clean tracked tree after commit, old checkout unchanged, gate true.

## M1 — Core identity and OI quality prerequisites

- Add tests/quant_phase9/test_core_symbol_boundary.py: begin with real Phase1 symbols rows and Phase3 producer, assert Phase9 reads Phase2 aggregate, Phase3 flow/CVD/gap and Phase4 liquidation via one validated canonical identity; bad/ambiguous/offline/mismatched contracts fail closed. RED on current query.
- Add tests/quant_phase9/test_oi_projection_quality.py: run actual normalize_open_interest for each valid unit/method, persist as the Phase2 adapter does, project with select_phase2, assert AVAILABLE+VALID where justified and PARTIAL/UNKNOWN for unknown/inconsistent cases. RED on current VERIFIED string check.
- Create a small framework-independent core instrument identity boundary from Phase1 metadata and explicit venue symbols. Change protected Phase9 Phase2/3 projection sources minimally; do not alter DecisionCandidateV1 or Phase1–8 status meanings.
- Run focused GREEN tests plus source, snapshot, Phase2 normalization, Phase3 flow and liquidation regression. Inspect database fixture behavior. Commit.
- Since protected source changed, run four mapped compatibility suites, regenerate ignored artifacts and rerun aggregate gate against the new protected tree digest. Preserve source_commit=original closure anchor. Record results.

## M2 — Early Nautilus feasibility spike

- Verify Python 3.12–3.14/Linux x86_64 and pinned v1 wheel availability. Install only into an isolated worktree virtual environment, pinned exactly 1.231.0, no v2/prerelease and no credential setup. Record exact version.
- Tests first define exact approved quantity and InstrumentId mapping, custom typed data with provenance/status, fee/latency settings and ExecutionResult round trip; then make minimal BTC/ETH LONG/SHORT historical replay pass using small deterministic data.
- Measure process RSS/peak RSS and container/cgroup value in the actual pilot shape. If peak >384 MiB, record exact value; attempt bounded reduction without weakening cases. A cap violation cannot be hidden as PASS.
- This spike does not claim full Phase9, funding, Paper or real strategy acceptance. Commit only pinned setup, spike and tests after GREEN.

## M3 — Existing Phase9 Tasks1–5

Execute the original approved plan sequentially with its exact test-first, verification and commit steps:
- Task1 policy.py and typed manifest/approval boundary. Human approval semantics preserved; no fabricated production approval.
- Task2 evidence.py/validator.py, 64 item cap, provenance and missing semantics.
- Task3 patterns.py, six directional structures/three patterns, no voting, policy-required MATCHED.
- Task4 decision.py, exact frozen candidate, Python gate, Jev non-trader, append-only status.
- Task5 persistence.py, final transaction/immutable references/Jev ordering.
Run compatibility gate as the first executable command of each task. Regenerate mapped evidence if a protected path changes, then rerun gate. Each task has one focused commit and test results.

## M4 — Existing Phase9 Tasks6A, 6B, 7, 8, 9

Follow exact approved plan and milestone commits:
- 6A: dedicated migration role, schema readiness, no DSN in logs.
- 6B: remove startup DDL from listed runtime/acceptance entrypoints; use read-only readiness. Protected service.py change requires compatibility suite regeneration.
- 7: bounded Engine, durable cursor, health and backpressure; no second unbounded service.
- 8: typed replay, parent Data Layer loader isolation, Fake/Recorded Jev.
- 9: formal safety/secret scan, 900-second minimum runtime acceptance, nine machine command records and C1 19-task completion proof.
No shortened runtime duration, required skip, or manual PASS field. Commit each task. Run affected Phase1–8/Data Layer regression plus Phase9 formal acceptance. If Task0 prerequisite repairs are separate commits, they do not substitute for the 19 required Phase9 task commits.

## M5 — Framework-neutral execution and Risk Policy

- Write failing tests for exact DecisionCandidate boundary, active/TTL/versions/hash, stale quote/account, unsupported mode, quantitative risk caps, concurrency, no upward resizing, stop/protection, idempotent intent digest and UNKNOWN submission.
- Add quant_execution domain-only contracts (ExecutionIntentV1, ExecutionResultV1, PositionSnapshotV1, ExecutionAdapter Protocol), deterministic canonical serialization and policy. No Nautilus import in Core packages.
- Add additive persistence migration for intent/reservation/order mapping/result/position audit. PostgreSQL integration tests use disposable DB; concurrent reservation and ambiguous send after crash must fail closed. Commit domain/contracts and persistence as separate milestones.
- Build Nautilus v1 adapter mapping with explicit instrument binding and capability checks; test real pinned runtime objects, quantity/fill/result mapping and unsupported operation rejection. Commit.

## M6 — Research and no-lookahead

- Test one shared feature projector for Phase9 source projection and ResearchFrame on PRICE/OI/FUNDING/TAKER/CVD/LIQUIDATION, preserving status/authority/provenance. Backward-only as-of and known_at; future candle/OI/funding/liquidation/news/options/on-chain hidden; null/UNKNOWN never zero.
- Create immutable Parquet export manifest plus PostgreSQL source reference, limited initial BTC/ETH data. A data coverage report distinguishes genuine historical point-in-time from retrospective or PIT_UNVERIFIED.
- Implement the five user-requested hypotheses only, with common eligible cohort, price-only control, chronological splits and fee/funding/spread/slippage model. Do not optimize for profit. Commit feature/data then research harness.
- Gate: repeated export/replay same hashes, no-lookahead and cost/funding attribution tests pass.

## M7 — Shared funding and cost ledger

- RED tests for long/short payment signs, held-position-at-boundary, missing rate/mark/position, paid/received cash, duplicate boundary/restart and separate trade/fee/funding PnL.
- Implement one pure FundingAccounting calculator and idempotent ledger; thin drivers for pinned v1 backtest and local Sandbox. Configure explicit fee/fill/latency. Verify account balance effect, report ledger and portfolio reconciliation in real pinned engine.
- Run BTC/ETH LONG/SHORT historical cases including trend and one other Phase9 pattern; fixture-driven cases must be labeled. Show gross/trading fees/funding/spread/slippage/net and unresolved cost flags. Commit.

## M8 — Local Paper and real process restart

- RED integration test launches the local Paper worker in a subprocess against isolated loopback PostgreSQL, produces accepted/filled or partial order, durable intent/order/result/position map, terminates it, then launches a fresh process and reconciles persisted simulated account/order/fill state.
- Local Paper uses no real exchange client/key; no network order route. Unknown intent/query-before-retry, one account owner, stale account fail-closed, partial protection and cancellation have explicit tests. If Sandbox state reconstruction needs a narrow persistent driver, implement only that bridge; do not write a generic portfolio engine.
- Run measured continuous Paper sample, record RSS/peak RSS and cgroup/container memory. No new counted container or cap increase. Commit implementation plus recovery instructions.
- Gate: real process restart and reconciliation pass, not just mocked repository tests.

## M9 — Documentation, whole-branch review and final verification

- Update Architecture, Integration, Research, Backtest, Paper, Operations and Recovery with exact commands for start/stop/health/audit/reconcile; no live instructions that might accidentally order.
- One whole-branch review after implementation. Fix Critical/Important findings in one concentrated round; record Minor. Re-run all affected tests, Phase9 compatibility and formal acceptance, pinned Nautilus BTC/ETH backtest and Paper/restart/resource gates after fixes.
- Report machine statuses exactly as required by user, including PARTIAL/BLOCKED if any gate fails, actual branch/HEAD/commits, skipped tests, result economics, funding status NATIVE_SUPPORTED/ADAPTER_IMPLEMENTED/BLOCKED, peak RSS and cap status, limitations and unverified items.
- Verify original checkout and named containers remain untouched; verify integration git status clean. Do not push, merge, tag or start Live.


## Single self-review (2026-09-28)

Checked ordering and evidence requirements against the approved nineteen-task Phase9 plan and the integration design. The first draft omitted Phase2 aggregate and Phase4 liquidation from the symbol regression scope; corrected in M1. The worktree lacks ignored Task0 artifacts by construction, so M0 explicitly copies them before the first gate and M1 regenerates them after protected-path changes. No Phase9 task is silently replaced by this plan; its exact preconditions, signatures, 900-second final acceptance and task commits remain authoritative. The plan measures real pinned framework behavior, real subprocess restart and RSS, and separates fixture-driven local acceptance from production strategy approval. Open substantive risk: the policy approval artifact's human provenance and possible Nautilus Sandbox restart persistence; both must be resolved by evidence, not a PASS label.
