# Local Paper account freshness

Before risk admission, a stale, explicitly human-funded BITGET_PAPER account
with no intent history is reconciled under the existing account row lock.
The execution profile's initial_funding identifies amount_usdt, funded_at,
canonical_symbols and EXPLICIT_HUMAN_PAPER_FUNDING. All initial position rows
must match that funding and remain reconciled, flat, fee/fill/order-free. Changed
balances, missing/ambiguous positions, local journals/reservations, future clocks
or an active owner reject verification. Only the account's local reconciliation
clock advances; initial positions, amount and all market source clocks stay intact.

Accounts with intent history are never treated as newly funded. Stale historical
accounts must reconstruct the original shared Sandbox and native checkpoint
through the existing adapter, including when the last position is already flat.
Unknown journals, ownership or quotes still block risk. The original five-second
account and quote limits remain in place. No venue credentials or order routes
are added, and no new collector, database, account-refresh daemon or Paper engine
is used.

Monitor cycles persist their structured readiness flags and reason codes in the
original session ledger, so formal acceptance can report the actual no-trade
classification. The original acceptance rules are unchanged. Startup, a funded
account, fixture fills or a running 24-hour process are not a full acceptance pass.

The V2 assembly supplies the monitor's risk-readiness callback from the same
RiskConfigLoader used for entry rechecks and per-intent risk inputs. The monitor
must not require a separate legacy RiskPolicyV1 file for this deployment. Invalid,
missing, or rolled-back V2 revisions, exceptions, and non-boolean success keep
risk blocked; the account and quote freshness limits are unchanged. Regression
checks exercise dispatch with a real V2 config and rejection before dispatch
with an invalid config. The existing formal acceptance rule remains unchanged.

The 2026-10-06 deployment is configured for all 478 symbols in
config/strategy_policy_v2_full_market.json's allowed_symbols; runtime and
Phase2 screening retain that scope. The full-market user-confirmed OI contract
matches the same symbol set. This configured scope does not mean every symbol
has usable fresh evidence: unavailable instruments or other required data keep
their existing fail-closed gates. The earlier BTCUSDT/ETHUSDT two-symbol
workflow acceptance remains a historical result only. A zero-A result in any
single cycle cannot establish that every Bitget perpetual contract failed
screening.
