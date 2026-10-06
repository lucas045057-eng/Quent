# Phase9 policy manifest review

The committed `policies/phase9_policy_v1.json` is a DRAFT. Its typed content
and canonical SHA-256 are machine-checkable, but it enables no patterns. No
production approval artifact exists. The loader therefore raises
`FileNotFoundError`, and `policy_for` cannot return an activated production
pattern. This preserves `NOT_CONFIGURED` rather than inventing a trade signal.

Before an approval artifact can be created, the owner must review the
directional predicates and numeric thresholds, units/rounding, source
freshness/coverage, veto precedence, TTL, and conflict policy for all enabled
pattern/timeframe/direction combinations. Approval must identify the exact
manifest digest, version, UTC approval time, approver, and reviewed commit.
The approved Phase9 plan requires a human-approved artifact. The current
instruction to proceed autonomously does not supply those specific policy
parameters or an approver identity. Fixture approvals in tests have no
production effect.

The pinned Nautilus backtest spike is a fixture-driven execution check and
does not rely on this policy manifest. It cannot be used as evidence that a
production Phase9 decision is approved.
