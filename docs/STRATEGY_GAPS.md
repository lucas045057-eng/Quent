# Strategy Gaps (observed, 2026-09-29)

This is a record of missing behavior found in the current source. No strategy parameters are changed here.

| Gap | Source evidence | Consequence | Suggested experiment / next step |
|---|---|---|---|
| No active approved LONG/SHORT policy | `policies/phase9_policy_v1.json` has no enabled patterns; approval file is absent; Phase9 defaults disabled | No production LONG/SHORT pattern can match; automated orders must remain blocked | Have the strategy owner explicitly approve one frozen pattern policy before enabling Phase9 |
| Stage1 result has no execution TTL | `src/quant_phase1/service.py` persists candidates with `candidate_valid_until=None` | Phase9 candidate intake marks the event expired/ineligible | Define validity/expiry in a reviewed policy; do not infer a TTL in runtime code |
| Production pipeline stops before risk/intent | Search found `approve_intent()` only in `quant_nautilus/acceptance.py`; engine does not call it | No connected Stage1→Phase9→Risk→ExecutionIntent path | Integrate only after active approved policy and execution account/paper configuration exist |
| Current Paper is fixture-only | `quant_nautilus/paper.py` and `paper_acceptance.py` describe fixture-driven sandbox acceptance | Existing Paper success is not evidence of real-feed Paper readiness | Preserve fixture test; create separate real-data orchestration that reads existing canonical PostgreSQL |
| No collector running in audited Docker runtime | `docker ps` showed engine/PostgreSQL/agentops; no collector | Canonical ticker/Kline freshness cannot be assumed continuous | Start/recover the existing `quant-collector` service; do not add a second collector/data model |
| Stage1's listed volume input is not a volume confirmation | `evaluate_stage1()` does not use volume in its decision formula | No volume-confirmed breakout rule exists | Measure candidate outcomes by volume regime before proposing a rule change |
| Stage1 A/B are screening labels, not orders | A means high-confidence aligned trend; B means wait for trigger; neither carries side/entry/stop/target | Treating A as BUY would invent a strategy | Keep as analysis state until a separate approved Phase9 policy creates directional decisions |
| Several Phase9 evidence types are observation/context only | OI direction is explicitly unknown; funding/regime/options/other contexts have no approved directional predicate; evidence validator's hard veto output is empty in current path | These feeds do not independently authorize or veto directional entry today | Add reviewed semantic predicates only through versioned policy and replay evidence |
| Risk lacks some portfolio guardrails | No daily loss, drawdown, cooldown, explicit stop-generation or separate max-position controls found in `RiskPolicyV1` | Existing bounds do not equal complete portfolio risk management | Design and replay proposed controls separately; never weaken current risk to pass acceptance |
| Execution protection is stop-only | Adapter creates reduce-only stop protection after entry fills; no target/trailing/signal/timeout exit | No full exit policy beyond protective stop | Decide and test exit policy before any strategy is authorized to trade |
| 24h runner cannot be called passed from a short check | A 15–30 minute run only proves its own observed window | Formal 24h pass must use `REAL_PUBLIC_DATA` continuously for 24 hours | Store source provenance and runtime session; report short acceptance separately from long-duration pass |

## Invariants for readiness work

- `LIVE = DISABLED`; never instantiate or call a Live adapter.
- Reuse Phase1–8 collectors, canonical repositories, schemas, and Stage1 outputs. Do not add a second market-data/OI/funding/trade-flow model.
- A missing policy, collector heartbeat, required feed, freshness proof, database, Risk configuration, Paper runtime, or reconciliation status means `DO NOT TRADE`.
- `REAL_PUBLIC_DATA` and `SYNTHETIC/FIXTURE` must remain distinct in all session and acceptance records.
- No GPT call; Jev remains `NOT_CONFIGURED`.
