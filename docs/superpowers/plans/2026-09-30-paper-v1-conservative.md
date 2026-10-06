# Paper V1 Conservative implementation plan

Goal: implement the explicitly Owner-decided Paper V1 policy through existing typed and durable boundaries.
Architecture: profile-bound transformers -> EvidenceItem -> validator/predicates -> decision -> existing RiskPolicy -> digest-bound intent -> native Sandbox/journal -> monitor position management.
Tech Stack: Python 3.12, Decimal, Pydantic, pytest, PostgreSQL, pinned Nautilus 1.231.0.
Spec: docs/strategy/QUANT_PAPER_V1_STRATEGY_SPEC.md.
Global Constraints: no production approval, A1-A9, Docker build, 24H or Live; no V2; existing commit approval binding remains.
Review Focus: source identity and clock integrity, stale/core/aux scope, cost underestimation, target vs stop race, exits after expired intents, partial fills and restart duplicate exits, percentage-equity budget, no outbound order route.

### Task 1: Evidence and Conservative transformers
Files: src/quant_phase9/{contracts,policy,evidence,patterns,validator,snapshot,runtime}.py; src/quant_phase9/paper_v1.py; sources/paper_v1.py; tests/quant_phase9/test_paper_v1.py.
Interfaces: PaperV1Profile, BarV1, BreakoutSetupV1, numeric value/unit, profile selector and bundled canonical sources.
RED: parameterized transforms, numeric operators, future/partial/gap/conflict/core/aux/veto tests fail due missing implementation. GREEN: minimal transformers and existing consumer wiring. Expected: task suite + legacy Phase9 tests pass. Commit evidence/transforms.

### Task 2: Dynamic plan and risk boundary
Files: src/quant_execution/{contracts,risk}.py; src/quant_realtime_paper/{assembly,phase9_bridge,wiring}.py; tests/quant_execution/test_paper_v1_risk.py.
Interfaces: digest-bound target_price/max_hold_seconds/profile/setup identity, dynamic stop from immutable snapshot and real quote; cost config; canonical direction keys; current event recheck.
RED: LONG/SHORT costs/netR/chase/HIGH/equity/TTL/alias tests. GREEN: risk sizing and plan generation in existing assembly. Expected: focused execution/runtime suites pass. Commit risk/wiring.

### Task 3: Native exits and recovery
Files: src/quant_nautilus/{adapter,paper,realtime_paper_adapter,sandbox}.py; src/quant_realtime_paper/runtime.py; tests/quant_nautilus/test_paper_v1_exits.py.
Interfaces: native reduce-only exits, later real quotes and emergency exit command journal; first actual fill hold clock; monitor management before entry gates.
RED: target, maxhold, expired-intent, emergency, partial and repeated quote tests. GREEN: native implementation and durable replay. Expected: native and integration suites pass. Commit exits.

### Task 4: Final validation and review
Run entire ordinary pytest suite with isolated test DB guards (no acceptance harness); save full logs/counts/skips. Fresh whole-diff review. Important findings get RED/GREEN regression fixes and full suite. Record final HEAD, clean worktree, scope/diff/prod-approval checks. Stop before RC validation.
