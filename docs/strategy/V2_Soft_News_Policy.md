# Archived: optional news and exchange-risk policy (superseded 2026-10-06)

**Current status:** GNews, Bitget official announcements and Xoomar macro calendar are retired from V2 requests, caches and admission. Their absence does not assert a zero-risk flag. The historical policy description below is retained as an audit record only.

2026-10-04 operator instruction: skip GNews and change news admission from a
mandatory gate to a soft constraint. The saved local service config disables
GNews while preserving its key; `config/strategy_policy_v2.json` explicitly sets
`news_mode: SOFT`. Typed library defaults remain HARD for older explicit policy
users. HARD is still selectable. These source/policy changes do not create an
exact-version runtime approval or activate Paper execution.

SOFT removes EVENT_COVERAGE from mandatory refresh and hypothesis prerequisites.
An unavailable, missing, stale, ambiguous or risk-matching news observation stays
unavailable/uncertain/risky; no risk-free scalar is manufactured. Python emits
NEWS_UNVERIFIED_ADVISORY / NEWS_RISK_ADVISORY and lowers the displayed confidence
by one ordinal band at most. The minimum-confidence gate applies to confidence
before this deterministic news penalty, so HIGH -> MEDIUM due solely to missing
news does not recreate a hidden veto. The penalty and warnings are recomputed
from bound original inputs; tampering or mode disagreement fails admission.
Fresh recheck news warnings persist in result reason codes and durable artifacts.
Core market contradictions, provider failure, unsupported geometry and all
existing liquidity, trigger, account and execution limits remain mandatory.
No sizing or leverage limits change and news cannot replace directional proof.

The factory passes the execution policy's news mode into refresh and analysis;
prompt v2.0.2 explains optional news and keeps facts untrusted. Python retains
deterministic evidence selection and trade geometry. Ordinary news uncertainty
should be discussed in the summary rather than invented as a hard prerequisite.

SOFT independently requires EXCHANGE_EVENT_COVERAGE. The keyless bitget_official
provider collects the four registered native notice types without calling
GNews. Its separate `<symbol>.official.json` journal imports existing official
unresolved incidents, receipt windows, bodies and retry clocks from the previous
combined journal on first use. News-only incidents do not become exchange vetoes;
existing official incidents are not erased by the split. The original policy's
BTC/ETH scope, official full-body and exact-resolution proof, current tail and
age limits still apply. Native endpoints still forbid research credentials and
alternate paths; adding a provider name does not widen that transport allowlist.

Official assessments explicitly use BTC_ETH_BITGET_EXCHANGE_RISK_V1. Unknown
official risk/body/coverage or active official incidents remain blocking, as do
MACRO_COVERAGE and source freshness. Thus disabling GNews is not a claim that
official bodies, macro risk, live data or the full original news dataset passed.
Public helper traces/test fixtures are not actual Stage A/Paper/24-hour evidence.
