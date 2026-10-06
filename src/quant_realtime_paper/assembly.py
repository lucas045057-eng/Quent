"""Fail-closed construction of the REAL_PUBLIC_DATA Local Paper runtime."""
from __future__ import annotations
from strategies.providers import RESEARCH_PROVIDERS

from datetime import datetime, timezone
from dataclasses import replace
from contextlib import ExitStack

from quant_nautilus.owned_runtime import OwnedLocalPaperRuntime
from decimal import Decimal
import json
import re
from pathlib import Path
from typing import Mapping

from quant_data_layer.freshness import bitget_price_source_clock_skew_tolerance
from quant_execution.contracts import (
    AccountSnapshotV1, InstrumentSpecV1, QuoteV1,
)
from quant_execution.persistence import ExecutionStore
from quant_execution.paper_v1 import PaperCostsV1
from quant_nautilus.realtime_paper_adapter import NautilusLocalPaperExecutionAdapter
from quant_phase1.contracts import Ticker
from quant_phase1.repositories import Phase1Repository
from quant_phase9.canonical import canonical_json
from quant_phase9.config import load_phase9_runtime_config
from quant_phase9.policy import load_approved_policy_manifest
from quant_realtime_paper.config import RuntimeConfig
from quant_realtime_paper.runtime import RealtimePaperMonitor
from quant_realtime_paper.phase9_bridge import load_active_phase9_projection
from quant_realtime_paper.wiring import RealtimePaperExecutionPipeline


_SHA1 = re.compile(r"^[0-9a-f]{40}$")



def _resolve_path(root: Path, value: str | None, default: str | None = None) -> Path | None:
    raw = value if value is not None else default
    if not raw:
        return None
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _account_from_payload(payload: object) -> AccountSnapshotV1:
    if not isinstance(payload, dict):
        raise ValueError("persisted paper account payload is invalid")
    value = dict(payload)
    for name in ("equity", "available_balance", "exposure", "reserved_risk"):
        value[name] = Decimal(str(value[name]))
    if isinstance(value.get("as_of"), str):
        value["as_of"] = datetime.fromisoformat(value["as_of"].replace("Z", "+00:00"))
    return AccountSnapshotV1(**value)


def reconcile_initial_paper_account(connection, profile, *, now):
    """Verify an explicitly funded, never-traded local account before first risk.

    This is a new local reconciliation clock, not a refresh of a venue receipt.
    Accounts with execution history must use native checkpoint recovery instead.
    """
    account_id=profile['account_id']
    with connection.transaction():
        row=connection.execute('SELECT venue,mode,payload,lease_expires_at FROM execution_accounts WHERE account_id=%s FOR UPDATE',(account_id,)).fetchone()
        if row is None or row[:2]!=('BITGET_PAPER','PAPER'):raise ValueError('PAPER_ACCOUNT_UNAVAILABLE')
        account=_account_from_payload(row[2])
        if account.account_id!=account_id or account.venue!='BITGET_PAPER' or account.mode!='PAPER' or account.as_of>now:
            raise ValueError('PAPER_ACCOUNT_BINDING_MISMATCH')
        if row[3] is not None and row[3]>now:raise ValueError('PAPER_ACCOUNT_OWNED')
        if connection.execute('SELECT 1 FROM execution_intents WHERE account_id=%s LIMIT 1',(account_id,)).fetchone():
            return account
        funding=profile.get('initial_funding')
        if not isinstance(funding,dict) or funding.get('basis')!='EXPLICIT_HUMAN_PAPER_FUNDING':raise ValueError('INITIAL_PAPER_FUNDING_UNVERIFIED')
        amount=Decimal(funding['amount_usdt']);funded_at=datetime.fromisoformat(funding['funded_at'])
        if not amount.is_finite() or amount<=0 or funded_at.tzinfo is None or not funded_at<=account.as_of<=now:
            raise ValueError('INITIAL_PAPER_FUNDING_INVALID')
        if (account.equity!=amount or account.available_balance!=amount or account.exposure!=0 or account.reserved_risk!=0
            or account.open_intents!=0 or account.reconciliation_status!='RECONCILED'):
            raise ValueError('INITIAL_PAPER_ACCOUNT_CHANGED')
        if connection.execute('SELECT 1 FROM execution_reservations WHERE account_id=%s LIMIT 1',(account_id,)).fetchone() or connection.execute('SELECT 1 FROM execution_local_events WHERE account_id=%s LIMIT 1',(account_id,)).fetchone():
            raise ValueError('INITIAL_PAPER_HISTORY_UNRESOLVED')
        positions=_recovery_positions(connection,account_id)
        if positions:raise ValueError('INITIAL_PAPER_POSITION_NOT_FLAT')
        rows=connection.execute('SELECT canonical_symbol,payload FROM execution_positions WHERE account_id=%s',(account_id,)).fetchall()
        expected=set(funding['canonical_symbols'])
        if not expected or len(rows)!=len(expected) or {s for s,_ in rows}!=expected:raise ValueError('INITIAL_PAPER_POSITIONS_UNVERIFIED')
        for symbol,p in rows:
            if (p['account_id']!=account_id or p['venue']!='BITGET_PAPER' or p['mode']!='PAPER' or p['canonical_symbol']!=symbol
                or p['side']!='FLAT' or Decimal(p['quantity'])!=0 or p['reconciliation_status']!='RECONCILED'
                or datetime.fromisoformat(p['as_of'])!=funded_at or p['source_order_ids'] or p['source_fill_ids']
                or Decimal(p['equity'])!=amount or Decimal(p['available_balance'])!=amount or Decimal(p['margin'])!=0
                or Decimal(p['fees'])!=0 or Decimal(p['realized_trade_pnl'])!=0
                or Decimal(p['funding_cash'] or '0')!=0 or Decimal(p['unrealized_pnl'] or '0')!=0):
                raise ValueError('INITIAL_PAPER_POSITION_CHANGED')
        verified=replace(account,as_of=now)
        ExecutionStore(connection).record_account(verified)
        return verified


def _stage1_projection_matches(stage1_result, projection: object) -> bool:
    if not isinstance(projection, dict):
        return False
    expected = {
        "symbol": stage1_result.symbol,
        "category": stage1_result.category,
        "classification": stage1_result.classification,
        "status": stage1_result.status.value,
        "reason": stage1_result.reason,
        "reason_codes": list(stage1_result.reason_codes),
        "inputs_used": list(stage1_result.inputs_used),
        "indicators": stage1_result.indicators,
        "structure": stage1_result.structure,
        "key_metrics": stage1_result.key_metrics,
    }
    if getattr(stage1_result,'strategy_version',None)=='QUANT_PAPER_V2':
        # Receipt identity changes with every quote. Current Stage A semantics
        # are rechecked here; the durable analysis and all source clocks are
        # verified separately before risk and again immediately before submit.
        expected.pop('inputs_used')
        expected['key_metrics']={'direction':stage1_result.key_metrics.get('direction')}
        projection=dict(projection,key_metrics={'direction':projection.get('key_metrics',{}).get('direction')})
    return all(canonical_json(projection.get(key)) == canonical_json(value) for key,value in expected.items())


def _latest_market_batch(config: RuntimeConfig, *, symbols: tuple[str, ...] | None = None):
    # Execution and position management consume their own bound symbols. The
    # full observation/screening loader and per-candle window remain unchanged.
    if symbols is not None:
        symbols = tuple(dict.fromkeys(symbols))
        if not symbols or not set(symbols) <= set(config.symbols):
            raise ValueError("EXECUTION_READ_SCOPE_INVALID")
    if not config.dsn:
        raise RuntimeError("CANONICAL_DATABASE_DSN_REQUIRED")
    import psycopg

    with psycopg.connect(
        config.dsn, connect_timeout=3, autocommit=True,
        options="-c default_transaction_read_only=on -c statement_timeout=4000 -c lock_timeout=1000",
    ) as connection:
        return Phase1Repository(connection).load_latest_market_batch(
            limit=max(200, len(config.symbols)) if symbols is None else len(symbols),
            candle_limit=100, requested_symbols=symbols,
        )


def _native_instrument(connection,intent,batch):
    if intent.strategy_profile=='QUANT_PAPER_V2':
        from nautilus_trader.model.instruments import CryptoPerpetual
        row=connection.execute("SELECT payload FROM execution_local_events WHERE intent_id=%s AND event_kind='CONFIG' ORDER BY seq LIMIT 1",(intent.intent_id,)).fetchone()
        if row and row[0].get('instrument'):return CryptoPerpetual.from_dict(row[0]['instrument'])
        from strategies.market_view import build_market_view
        from quant_nautilus.instruments import build_public_instrument
        market=next((m for m in build_market_view(batch,as_of=batch.collected_at).symbols if m.symbol==intent.core_symbol),None) if batch else None
        if market is None or market.instrument is None:raise ValueError('PUBLIC_INSTRUMENT_METADATA_UNAVAILABLE')
        return build_public_instrument(market.instrument)
    # Historical V1 decoding only; never an instrument for new V2 intents.
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    if intent.core_symbol=='BTCUSDT':return TestInstrumentProvider.btcusdt_perp_binance()
    if intent.core_symbol=='ETHUSDT':return TestInstrumentProvider.ethusdt_perp_binance()
    raise ValueError('LEGACY_INSTRUMENT_UNAVAILABLE')


def _recovery_positions(connection,account_id, *, native_runtime=None):
    from quant_execution.persistence import checkpoint_positions,ReservationRejected
    from quant_execution.persistence import latest_position_rows
    rows=latest_position_rows(connection,account_id)
    checkpoints=native_runtime.verified_checkpoint_positions() if native_runtime is not None else None
    if checkpoints is None:checkpoints=checkpoint_positions(connection,account_id)
    groups={};selected=[]
    for symbol,at,payload in rows:groups.setdefault(symbol,[]).append((at,payload))
    for symbol,values in groups.items():
        latest=values[0][0];same=[payload for at,payload in values if at==latest]
        checkpoint=checkpoints.get(symbol)
        if checkpoint in same:chosen=checkpoint
        elif len({canonical_json(payload) for payload in same})==1:chosen=same[0]
        else:raise ReservationRejected('PAPER_POSITION_AMBIGUOUS')
        if chosen.get('side')!='FLAT':selected.append(chosen)
    return tuple(selected)


def manage_persisted_positions(config: RuntimeConfig, env: Mapping[str,str], now: datetime, *, execution_connection=None, native_runtime=None):
    """Restore all active Paper plans even when entry approval/research is blocked."""
    from quant_execution.contracts import intent_from_json
    import psycopg
    if not (env.get('TRADING_MODE','').lower()=='paper' and env.get('PAPER_ONLY','').lower()=='true' and env.get('LIVE_ALLOWED','').lower()=='false'):
        raise ValueError('PAPER_ONLY_CONFIGURATION_INVALID')
    if not config.dsn:raise ValueError('CANONICAL_DATABASE_DSN_REQUIRED')
    path=_resolve_path(config.project_root,env.get('QUANT_REALTIME_PAPER_EXECUTION_PROFILE_PATH'),str(config.execution_profile_path) if config.execution_profile_path else None)
    if path is None:raise ValueError('RECOVERY_ACCOUNT_BINDING_UNAVAILABLE')
    profile=json.loads(path.read_text());account_id=profile.get('account_id');venue=profile.get('venue')
    if not account_id or not venue:raise ValueError('RECOVERY_ACCOUNT_BINDING_INVALID')
    with ExitStack() as stack:
        conn=execution_connection if execution_connection is not None else stack.enter_context(
            psycopg.connect(config.dsn,connect_timeout=3,autocommit=True))
        ExecutionStore(conn).assert_ready()
        active=_recovery_positions(conn,account_id,native_runtime=native_runtime)
        if not active:return ()
        active_symbols=tuple(position['canonical_symbol'].replace('-USDT-PERP','USDT')
            for position in active)
        results=[]
        rows=conn.execute("SELECT i.payload FROM execution_intents i JOIN execution_submission_states s USING(intent_id) WHERE i.account_id=%s AND i.mode='PAPER' AND s.status IN ('FILLED','PARTIALLY_FILLED','ACCEPTED','CANCELLED') ORDER BY i.valid_until DESC,i.intent_id DESC",(account_id,)).fetchall()
        if native_runtime is not None:
            first=active[0]
            candidates=[intent_from_json(canonical_json(p)) for (p,) in rows
                if p.get('canonical_symbol')==first['canonical_symbol']
                and p.get('client_order_id') in first.get('source_order_ids',[])]
            if len(candidates)!=1:raise ValueError('ACTIVE_POSITION_INTENT_AMBIGUOUS')
            target=candidates[0]
            native_runtime.prepare(target.intent_id,instrument=_native_instrument(conn,target,None))
            active=_recovery_positions(conn,account_id,native_runtime=native_runtime)
            if not active:return ()
            active_symbols=tuple(p['canonical_symbol'].replace('-USDT-PERP','USDT') for p in active)
        # Quote capture follows any full native restoration; keep exact source
        # clocks and revalidate the same strict five-second bound at command.
        batch=_latest_market_batch(config, symbols=active_symbols)
        quote_times={t.symbol:t.processed_at for t in batch.tickers}
        for position in sorted(active,key=lambda p:quote_times.get(p['canonical_symbol'].replace('-USDT-PERP','USDT'),now)):
            candidates=[intent_from_json(canonical_json(p)) for (p,) in rows if p.get('canonical_symbol')==position['canonical_symbol'] and p.get('client_order_id') in position.get('source_order_ids',[])]
            if len(candidates)!=1:raise ValueError('ACTIVE_POSITION_INTENT_AMBIGUOUS')
            intent=candidates[0]
            if intent.account_id!=account_id or intent.venue!=venue or intent.created_at>now:raise ValueError('RECOVERY_INTENT_BINDING_INVALID')
            quote=next((t for t in batch.tickers if t.symbol==intent.core_symbol),None) if batch else None
            if quote is None:raise ValueError('POSITION_MANAGEMENT_QUOTE_UNAVAILABLE')
            adapter=NautilusLocalPaperExecutionAdapter(conn,intent,instrument=_native_instrument(conn,intent,batch),quote=quote,data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=native_runtime)
            results.append(adapter.manage_position(now=datetime.now(timezone.utc)))
        return tuple(results)


def _blocked(config: RuntimeConfig, env: Mapping[str, str], reason: str) -> RealtimePaperMonitor:
    locked=(env.get("TRADING_MODE","").lower()=="paper" and env.get("PAPER_ONLY","").lower()=="true"
            and env.get("LIVE_ALLOWED","").lower()=="false")
    manager=(lambda now:manage_persisted_positions(config,env,now)) if locked else None
    return RealtimePaperMonitor(config, environ=env, execution_pipeline=None, startup_blocker=reason,
        position_manager=manager)


def _operating_public_ticker_ready(ticker, *, now, max_age_seconds):
    """Existing Stage1 freshness for a no-order native readiness probe.

    No QuoteTick or execution intent is emitted here. The original execution
    adapter and risk policy retain their separate strict five-second ceiling.
    """
    if (ticker is None or ticker.exchange != 'bitget'
            or ticker.source not in {'bitget_v3_rest', 'bitget_v3_ws'}
            or ticker.status.value != 'AVAILABLE'):
        return False
    exchange_age = (now - ticker.exchange_timestamp).total_seconds()
    local_ages = (
        (now - ticker.fetched_at).total_seconds(),
        (now - ticker.processed_at).total_seconds(),
    )
    skew_tolerance = bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", ticker.exchange, ticker.source,
    )
    return (
        -skew_tolerance <= exchange_age <= max_age_seconds
        and all(0 <= age <= max_age_seconds for age in local_ages)
    )


def build_default_runtime(
    config: RuntimeConfig | None = None,
    environ: Mapping[str, str] | None = None,
) -> RealtimePaperMonitor:
    """Build only from explicit approved inputs; missing inputs remain observable blocks."""
    env = dict(environ or {}) if environ is not None else __import__("os").environ.copy()
    cfg = config or RuntimeConfig.from_env(env)
    if not (
        env.get("TRADING_MODE", "").strip().lower() == "paper"
        and env.get("PAPER_ONLY", "").strip().lower() == "true"
        and env.get("LIVE_ALLOWED", "").strip().lower() == "false"
    ):
        return _blocked(cfg, env, "PAPER_ONLY_CONFIGURATION_INVALID")

    revision = (env.get("QUANT_BUILD_REVISION") or cfg.build_revision or "").strip()
    if not _SHA1.fullmatch(revision):
        return _blocked(cfg, env, "BUILD_REVISION_MISSING_OR_INVALID")
    manifest_path = _resolve_path(
        cfg.project_root, env.get("PHASE9_POLICY_MANIFEST_PATH"),
        str(cfg.policy_manifest_path) if cfg.policy_manifest_path else "policies/phase9_policy_v1.json",
    )
    approval_path = _resolve_path(
        cfg.project_root, env.get("PHASE9_POLICY_APPROVAL_PATH"),
        str(cfg.policy_approval_path) if cfg.policy_approval_path else "policies/phase9_policy_v1.approval.json",
    )
    assert manifest_path is not None and approval_path is not None
    if not approval_path.is_file():
        return _blocked(cfg, env, "APPROVAL_MISSING")
    if not manifest_path.is_file():
        return _blocked(cfg, env, "POLICY_MANIFEST_MISSING")
    env["PHASE9_POLICY_MANIFEST_PATH"] = str(manifest_path)
    env["PHASE9_POLICY_APPROVAL_PATH"] = str(approval_path)
    try:
        approved = load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit=revision,
        )
    except (OSError, ValueError, TypeError):
        approval_text = approval_path.read_text(encoding="utf-8") if approval_path.is_file() else ""
        if '"approved_commit"' in approval_text and revision not in approval_text:
            return _blocked(cfg, env, "BUILD_REVISION_MISMATCH")
        return _blocked(cfg, env, "POLICY_APPROVAL_INVALID")
    if approved.code_version != revision or approved.approval.approved_commit != revision:
        return _blocked(cfg, env, "BUILD_REVISION_MISMATCH")
    if not approved.manifest.policy_content.enabled_patterns:
        return _blocked(cfg, env, "POLICY_DISABLED")
    if any(
        "fixture" in pattern.approval_reference.lower()
        for pattern in approved.manifest.policy_content.enabled_patterns
    ):
        return _blocked(cfg, env, "POLICY_PROVENANCE_INVALID")

    try:
        phase9_config = load_phase9_runtime_config(env)
    except (TypeError, ValueError):
        return _blocked(cfg, env, "PHASE9_CONFIGURATION_INVALID")
    if not phase9_config.enabled:
        return _blocked(cfg, env, "PHASE9_DISABLED")
    if phase9_config.stage1_candidate_ttl_seconds is None:
        return _blocked(cfg, env, "INTAKE_TTL_NOT_CONFIGURED")

    from strategies.integration.policy_manifest import is_strategy_v2
    from strategies.runtime import CanonicalResearchFactory, execution_policy_from_env
    from quant_execution.risk_config import RiskConfigLoader, resolve_risk_policy
    from quant_execution.trade_plan import build_trade_plan as build_v2_trade_plan
    from strategies.integration.phase9_bridge import bound_inputs, build_v2_candidate
    from strategies.contracts import ExecutionPolicyResult
    from strategies.execution.execution_policy import evaluate_execution
    if not is_strategy_v2(approved):return _blocked(cfg,env,'LEGACY_STRATEGY_PRODUCER_DISABLED')
    try:
        strategy_policy=execution_policy_from_env(env)
        if {p.strategy_policy_digest for p in approved.manifest.policy_content.enabled_patterns}!={strategy_policy.digest}:
            return _blocked(cfg,env,'STRATEGY_POLICY_DIGEST_MISMATCH')
    except (OSError,ValueError):return _blocked(cfg,env,'STRATEGY_POLICY_UNAVAILABLE')
    risk_path=_resolve_path(cfg.project_root,env.get('QUANT_RISK_CONFIG_PATH'), 'config/risk_policy_v2.json')
    risk_loader=RiskConfigLoader(risk_path)
    if not risk_loader.poll().new_risk_allowed:return _blocked(cfg,env,'RISK_CONFIG_INVALID_OR_UNAVAILABLE')
    profile_path=_resolve_path(cfg.project_root,env.get('QUANT_REALTIME_PAPER_EXECUTION_PROFILE_PATH'),str(cfg.execution_profile_path) if cfg.execution_profile_path else None)
    if profile_path is None or not profile_path.is_file():return _blocked(cfg,env,'EXECUTION_PROFILE_UNAVAILABLE')
    try:
        raw_profile=json.loads(profile_path.read_text());account_id=raw_profile['account_id'];venue=raw_profile['venue']
        if not isinstance(account_id,str) or not account_id.strip() or venue!='BITGET_PAPER':raise ValueError('V2 Paper account binding required')
        raw_costs=raw_profile['strategy_costs']
        if set(raw_costs)!={'entry_fee_rate','exit_fee_rate','funding_cost_rate_max_hold'}:raise ValueError('Explicit costs required')
        costs=PaperCostsV1(**{k:Decimal(str(v)) for k,v in raw_costs.items()})
    except (OSError,ValueError,TypeError,KeyError):return _blocked(cfg,env,'EXECUTION_PROFILE_INVALID')
    if not set(cfg.symbols)<=set(strategy_policy.allowed_symbols):return _blocked(cfg,env,'SYMBOL_NOT_ALLOWED')
    env['QUANT_REALTIME_PAPER_EXECUTION_PROFILE_PATH']=str(profile_path)
    if not cfg.dsn:
        return _blocked(cfg, env, "CANONICAL_DATABASE_DSN_REQUIRED")

    import psycopg
    try:
        execution_connection = psycopg.connect(cfg.dsn, connect_timeout=3, autocommit=True)
        execution_store = ExecutionStore(execution_connection)
        execution_store.assert_ready()
    except Exception:
        try:
            execution_connection.close()
        except (UnboundLocalError, AttributeError):
            pass
        return _blocked(cfg, env, "EXECUTION_STORE_UNAVAILABLE")

    timeframes = tuple(sorted({pattern.timeframe for pattern in approved.manifest.policy_content.enabled_patterns}))
    generation = f"{approved.manifest_version}:{approved.manifest_digest}"
    latest_tickers: dict[str, Ticker] = {}

    def phase9_evaluate(stage1_result, now, _correlation_id):
        if stage1_result.category != "A" or stage1_result.status.value != "AVAILABLE":
            return None, None, "STAGE1_NOT_ELIGIBLE"
        event_rows = execution_connection.execute(
            """SELECT payload FROM outbox_events
                 WHERE event_type='phase9.stage1_candidate' AND payload->>'symbol'=%s
                 ORDER BY created_at DESC,event_id DESC LIMIT 64""",
            (stage1_result.symbol,),
        ).fetchall()
        candidate_id = None
        for (event_payload,) in event_rows:
            if event_payload.get('stage1_policy_version')!='QUANT_PAPER_V2':continue
            canonical_payload = event_payload.get("canonical_payload")
            projection = canonical_payload.get("stage1_projection") if isinstance(canonical_payload, dict) else None
            if _stage1_projection_matches(stage1_result, projection):
                raw_id = event_payload.get("stage1_candidate_id")
                if type(raw_id) is int and raw_id > 0:
                    candidate_id = raw_id
                    break
        if candidate_id is None:
            return None, None, "STAGE1_CANDIDATE_NOT_DURABLE"

        ready = []
        blocked_reasons = []
        for timeframe in timeframes:
            result = load_active_phase9_projection(
                execution_connection,
                stage1_candidate_id=candidate_id,
                policy_generation=generation,
                symbol=stage1_result.symbol,
                timeframe=timeframe,
                build_revision=revision,
                now=now, approved_policy=approved,
            )
            if result.ready:
                ready.append(result)
            else:
                blocked_reasons.append(result.reason_code)
        eligible = [item for item in ready if item.candidate and item.candidate.eligible]
        if len(eligible) == 1:
            result = eligible[0]
            return result.snapshot, result.candidate, result.effective_status
        if len(eligible) > 1:
            return None, None, "PHASE9_MULTIPLE_ELIGIBLE_TIMEFRAMES"
        if len(ready) == 1:
            result = ready[0]
            return result.snapshot, result.candidate, result.effective_status
        return None, None, blocked_reasons[0] if blocked_reasons else "PHASE9_RESULT_NOT_FOUND"

    research=CanonicalResearchFactory(cfg.dsn,policy=strategy_policy,environ=env)
    native_instruments={}
    native_runtime=OwnedLocalPaperRuntime(execution_connection,account_id)

    def research_binding(snapshot,candidate):
        raw=next(p.canonical_payload for p in snapshot.source_projections if p.source_type=='STRATEGY_V2_RESULT')
        result=ExecutionPolicyResult.model_validate(raw);inputs=bound_inputs(snapshot,result)
        decision,_,_,_,thesis=build_v2_candidate(snapshot,result,inputs)
        if decision.decision_id!=candidate.decision_id or not decision.eligible:raise ValueError('V2_DURABLE_BINDING_INVALID')
        return inputs,thesis

    def risk_inputs(snapshot,candidate,now):
        from quant_execution.persistence import active_execution_state
        from strategies.market_view import build_market_view
        from quant_nautilus.instruments import build_public_instrument
        from quant_execution.trade_plan import validate_execution_capabilities
        state=risk_loader.poll()
        if not state.new_risk_allowed:raise ValueError(state.reason)
        config=state.config
        validate_execution_capabilities(config,NautilusLocalPaperExecutionAdapter._CAPABILITIES)
        row=execution_connection.execute('SELECT venue,mode,payload FROM execution_accounts WHERE account_id=%s',(account_id,)).fetchone()
        if row is None or row[0]!=venue or row[1]!='PAPER':raise ValueError('PAPER_ACCOUNT_UNAVAILABLE')
        account=_account_from_payload(row[2])
        if not 0<=(now-account.as_of).total_seconds()<=5:
            history=execution_connection.execute('SELECT payload FROM execution_intents WHERE account_id=%s ORDER BY valid_until DESC,intent_id DESC LIMIT 1',(account_id,)).fetchone()
            if history:
                # Reconstruct the original shared Sandbox and its native journal,
                # including closed positions; never just advance a history clock.
                from quant_execution.contracts import intent_from_json
                intent=intent_from_json(canonical_json(history[0]))
                native_runtime.prepare(intent.intent_id,instrument=_native_instrument(execution_connection,intent,None))
                batch=_latest_market_batch(cfg, symbols=(intent.core_symbol,))
                ticker=next((t for t in batch.tickers if t.symbol==intent.core_symbol),None) if batch else None
                if ticker is None:raise ValueError('ACCOUNT_RECONCILIATION_QUOTE_UNAVAILABLE')
                NautilusLocalPaperExecutionAdapter(execution_connection,intent,instrument=_native_instrument(execution_connection,intent,batch),
                    quote=ticker,data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=native_runtime).manage_position(now=datetime.now(timezone.utc))
                account=_account_from_payload(execution_connection.execute('SELECT payload FROM execution_accounts WHERE account_id=%s',(account_id,)).fetchone()[0])
            else:account=reconcile_initial_paper_account(execution_connection,raw_profile,now=now)
            now=datetime.now(timezone.utc)
        if account.account_id!=account_id or account.venue!=venue:raise ValueError('PAPER_ACCOUNT_BINDING_MISMATCH')
        context=active_execution_state(execution_connection,account_id=account_id,now=now)
        if context.reconciliation_status!='RECONCILED':raise ValueError('PAPER_ACCOUNT_UNRECONCILED')
        inputs,thesis=research_binding(snapshot,candidate)
        batch=_latest_market_batch(cfg, symbols=(snapshot.symbol,))
        now=datetime.now(timezone.utc)
        market=next((m for m in build_market_view(batch,as_of=now).symbols if m.symbol==snapshot.symbol),None) if batch else None
        if market is None or market.instrument is None:raise ValueError('PUBLIC_INSTRUMENT_METADATA_UNAVAILABLE')
        meta=market.instrument
        if meta.max_quantity is None or meta.min_notional is None:raise ValueError('PUBLIC_INSTRUMENT_LIMITS_UNAVAILABLE')
        native=build_public_instrument(meta,costs=costs);native_instruments[snapshot.symbol]=native
        instrument=InstrumentSpecV1(core_symbol=meta.symbol,canonical_symbol=meta.symbol[:-4]+'-USDT-PERP',venue=venue,
            instrument_id=str(native.id),settlement_currency='USDT',quantity_step=meta.lot,min_quantity=meta.min_quantity,
            max_quantity=meta.max_quantity,price_tick=meta.tick,min_notional=meta.min_notional,tradable=True)
        ticker=next((t for t in batch.tickers if t.symbol==snapshot.symbol),None)
        if ticker is None:raise ValueError('REAL_PUBLIC_QUOTE_UNAVAILABLE')
        latest_tickers[snapshot.symbol]=ticker
        quote=QuoteV1(canonical_symbol=instrument.canonical_symbol,bid=ticker.bid_price,ask=ticker.ask_price,
            as_of=ticker.fetched_at,status=ticker.status.value,received_at=ticker.processed_at,
            source_as_of=ticker.exchange_timestamp,source=ticker.source,exchange=ticker.exchange)
        plan=build_v2_trade_plan(thesis,instrument,quote,costs=costs,config=config,now=now)
        if execution_connection.execute("SELECT 1 FROM execution_intents WHERE account_id=%s AND payload->>'setup_id'=%s LIMIT 1",(account_id,plan.setup_id)).fetchone():raise ValueError('SETUP_ALREADY_EXECUTED')
        policy=resolve_risk_policy(config,account,code_version=revision,decision_versions=('2.0.0',))
        return dict(instrument=instrument,quote=quote,account=account,policy=policy,stop_price=plan.stop_price,trade_plan=plan,now=now)

    def operational_preflight(now):
        from quant_nautilus.sandbox import SandboxSession
        from quant_nautilus.instruments import build_public_instrument
        from quant_execution.trade_plan import validate_execution_capabilities
        from quant_execution.contracts import intent_from_json
        from strategies.market_view import build_market_view
        state=risk_loader.poll()
        if not state.new_risk_allowed:raise ValueError('RISK_CONFIG_INVALID_OR_UNAVAILABLE')
        validate_execution_capabilities(state.config,NautilusLocalPaperExecutionAdapter._CAPABILITIES)
        execution_store.assert_ready()
        history=execution_connection.execute(
            'SELECT payload FROM execution_intents WHERE account_id=%s ORDER BY valid_until DESC,intent_id DESC LIMIT 1',
            (account_id,)).fetchone()
        if history:
            # Restore and reconcile real existing history using the same adapter,
            # never fabricate an intent solely to make an operational probe.
            intent=intent_from_json(canonical_json(history[0]))
            batch=Phase1Repository(execution_connection).load_latest_market_batch(
                limit=1,candle_limit=100,requested_symbols=(intent.core_symbol,))
            ticker=next((t for t in batch.tickers if t.symbol==intent.core_symbol),None) if batch else None
            if ticker is None:raise ValueError('ACCOUNT_RECONCILIATION_QUOTE_UNAVAILABLE')
            adapter=NautilusLocalPaperExecutionAdapter(execution_connection,intent,
                instrument=_native_instrument(execution_connection,intent,batch),quote=ticker,
                data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=native_runtime)
            adapter.preflight_existing_account()
            positions=adapter.reconcile()
            if not positions:raise ValueError('PAPER_ACCOUNT_UNRECONCILED')
            return {'paper_engine':True,'reconciliation':True}
        account=reconcile_initial_paper_account(execution_connection,raw_profile,now=now)
        # A proven never-traded flat account has no native replay inputs. Exercise
        # its actual funded balance on the existing native core, with no intent.
        batch=Phase1Repository(execution_connection).load_latest_market_batch(
            limit=1,candle_limit=100,requested_symbols=('BTCUSDT',))
        at=datetime.now(timezone.utc)
        view=build_market_view(batch,as_of=at) if batch else None
        market=next((m for m in view.symbols if m.symbol=='BTCUSDT'),None) if view else None
        ticker=next((t for t in batch.tickers if t.symbol=='BTCUSDT'),None) if batch else None
        if (market is None or market.instrument is None
                or not _operating_public_ticker_ready(ticker, now=at,
                    max_age_seconds=cfg.ticker_max_age_seconds)):
            raise ValueError('PAPER_OPERATIONAL_PUBLIC_INPUT_UNAVAILABLE')
        return SandboxSession.preflight_empty_account(
            build_public_instrument(market.instrument,costs=costs),
            starting_balance=account.equity,now=at,max_leverage=state.config.max_leverage)

    def adapter_factory(intent):
        ticker=latest_tickers.get(intent.core_symbol)
        if ticker is None:return None
        native=native_instruments.get(intent.core_symbol)
        if native is None:return None
        return NautilusLocalPaperExecutionAdapter(execution_connection,intent,instrument=native,quote=ticker,
            data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=native_runtime)

    def entry_recheck(snapshot,candidate,now):
        inputs,_=research_binding(snapshot,candidate)
        view=research.view(now).model_copy(update={'analysis_snapshots':(inputs.analysis,)})
        # A prior analysis receipt can be reused only inside its independent source freshness bound.
        external=tuple(o for o in inputs.analysis.observations if o.provider in RESEARCH_PROVIDERS)
        view=view.model_copy(update={'symbols':tuple(m.model_copy(update={'observations':(*m.observations,*external)}) if m.symbol==snapshot.symbol else m for m in view.symbols)})
        result=evaluate_execution(inputs.theses,view,policy=inputs.policy,now=now)
        if result.disposition!='PASS':raise ValueError(result.reason_codes[0])
        if not risk_loader.poll().new_risk_allowed:raise ValueError('RISK_CONFIG_INVALID_OR_UNAVAILABLE')

    pipeline=RealtimePaperExecutionPipeline(approved_policy=approved,current_revision=revision,
        phase9_evaluate=phase9_evaluate,risk_inputs=risk_inputs,execution_store=execution_store,
        adapter_factory=adapter_factory,environ=env,entry_recheck=entry_recheck,
        position_manager=lambda now:manage_persisted_positions(cfg,env,now,execution_connection=execution_connection,native_runtime=native_runtime),
        operational_preflight=operational_preflight)
    return RealtimePaperMonitor(cfg,environ=env,execution_pipeline=pipeline,resources=(execution_connection,native_runtime),
        risk_readiness=lambda: risk_loader.poll().new_risk_allowed)
