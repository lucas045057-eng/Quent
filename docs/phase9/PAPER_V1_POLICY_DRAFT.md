# Paper V1 Policy Draft (For Strategy Owner Review Only)

Status: **DRAFT — NOT APPROVED — NOT ENABLED**

No production approval artifact is created by this document. The current frozen manifest still has `enabled_patterns=[]` and digest `5311c7f9d386029c335082580f266a6cbb4d5ade6cb2e9afd08c68fb3adaba83`.

Every strategy value without direct, reviewed evidence in the current code/data is marked `NEEDS_STRATEGY_OWNER_DECISION`. This draft does not set thresholds, directions, TTL values, or approval references.

## Supported pattern/timeframe combinations

The implementation recognizes these pattern/timeframe pairs. Recognition is not activation or a trading recommendation.

| Pattern | Timeframe | LONG eligibility | SHORT eligibility | Required evidence | Pattern freshness | Coverage | Hard vetoes | Confidence requirement | Phase9 candidate TTL | ExecutionIntent TTL | Revalidation | Material changes | Degradation behavior |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `TREND_CONTINUATION` | `1H` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION`  NEEDS_STRATEGY_OWNER_DECISION  NEEDS_STRATEGY_OWNER_DECISION | NEEDS_STRATEGY_OWNER_DECISION |
| `TREND_CONTINUATION` | `4H` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION`  NEEDS_STRATEGY_OWNER_DECISION  NEEDS_STRATEGY_OWNER_DECISION | NEEDS_STRATEGY_OWNER_DECISION |
| `BREAKOUT_CONFIRMATION` | `15m` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION`  NEEDS_STRATEGY_OWNER_DECISION  NEEDS_STRATEGY_OWNER_DECISION | NEEDS_STRATEGY_OWNER_DECISION |
| `BREAKOUT_CONFIRMATION` | `1H` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` |
| `LIQUIDATION_REVERSAL` | `15m` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` |
| `LIQUIDATION_REVERSAL` | `1H` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` | `NEEDS_STRATEGY_OWNER_DECISION` |

## Required decisions and distinct time bounds

These are separate clocks and must not be copied from one field into another:

| Boundary | Current representation | Draft status |
|---|---|---|
| Market data freshness | Existing source-specific Phase1/Phase2 freshness policies; retain current code values | Threshold changes are outside this draft; pattern-specific evidence freshness rules are `NEEDS_STRATEGY_OWNER_DECISION` |
| EvaluationSnapshot/source validity | Snapshot `as_of`, evaluation window, evidence freshness and source coverage | Required validity/coverage interpretation per pattern is `NEEDS_STRATEGY_OWNER_DECISION` |
| Stage1 candidate intake expiry | New explicit `stage1_candidate_ttl_seconds`; absent value leaves Phase9 intake deferred | Value is `NEEDS_STRATEGY_OWNER_DECISION`; never infer from A/B or market movement |
| Phase9 DecisionCandidate validity | Existing per-pattern/timeframe `TTLRuleV1` | Each `ttl_rule_id` and duration is `NEEDS_STRATEGY_OWNER_DECISION` |
| ExecutionIntent validity | Existing `RiskPolicyV1.intent_ttl_seconds` | Value is `NEEDS_STRATEGY_OWNER_DECISION`; absent or invalid value means `NO_TRADE_BY_MISSING_TTL` |

## Evidence and veto selection

- **Required, supporting, contradicting and hard-conflict predicates:** `NEEDS_STRATEGY_OWNER_DECISION` for each pattern/timeframe/direction.
- **Coverage status and minimum ratios:** `NEEDS_STRATEGY_OWNER_DECISION`; current Phase9 coverage machinery must remain fail-closed for missing required evidence.
- **Hard-veto selection and precedence:** `NEEDS_STRATEGY_OWNER_DECISION`. Existing code defines veto codes including `CORE_MARKET_DATA_INVALID`, `STAGE1_INVALIDATED`, `CORE_DIRECTIONAL_EVIDENCE_MISSING`, `DATA_CORRUPTION_OR_PROVENANCE_INVALID`, `EXTREME_REGIME_CONFLICT`, and `EVIDENCE_TOO_STALE`; this list is not an automatic selection.
- **Confidence band/ceiling and JEV conflict classes:** `NEEDS_STRATEGY_OWNER_DECISION`.
- **Revalidation triggers, material-change fields, and unavailable-evidence behavior:** `NEEDS_STRATEGY_OWNER_DECISION`.

## Approval and activation gate

Before any future approval producer invocation, the strategy owner must decide all fields above, review the frozen policy content and digest, choose explicit enabled pattern/timeframe/direction rows, and provide `approved_by` and the exact source commit. The existing approval producer binds that approval to the full manifest digest and commit. Until those decisions and a matching approval exist, Phase9 stays disabled for execution and the outcome is `NO_TRADE_BY_POLICY_DISABLED` or `NO_TRADE_BY_SYSTEM_NOT_READY` as appropriate.
