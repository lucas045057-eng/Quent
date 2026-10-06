"""Snapshot-bound Conservative trade geometry and explicit maximum-hold costs."""
from dataclasses import dataclass
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING

from quant_phase9.paper_v1 import PROFILE, setup_from_snapshot, parse_bars, continuous_bars, confirmed_pivots, paper_vetoes
from quant_phase9.canonical import canonical_sha256
from quant_execution.contracts import decimal


@dataclass(frozen=True, slots=True)
class PaperCostsV1:
    entry_fee_rate: D
    exit_fee_rate: D
    funding_cost_rate_max_hold: D
    def __post_init__(self):
        for v in (self.entry_fee_rate,self.exit_fee_rate,self.funding_cost_rate_max_hold):
            decimal(v,nonnegative=True)


@dataclass(frozen=True, slots=True)
class PaperTradePlanV1:
    snapshot_hash: str
    side: str
    stop_price: D
    target_price: D
    net_R: D
    risk_per_unit: D
    setup_id: str
    setup_trigger_at: object
    profile: str = PROFILE.name


def normalize_stop_key(value: str) -> str:
    if not isinstance(value,str) or value.count('|')!=2: raise ValueError('invalid stop direction key')
    pattern,timeframe,side=value.split('|')
    side={'BULLISH':'LONG','BEARISH':'SHORT','LONG':'LONG','SHORT':'SHORT'}.get(side)
    if not pattern or timeframe not in {'15m','1H','4H'} or side is None: raise ValueError('invalid stop direction key')
    return '|'.join((pattern,timeframe,side))


def build_trade_plan(snapshot,candidate,instrument,quote,costs,*,now):
    if costs is None or not isinstance(costs,PaperCostsV1): raise ValueError('PAPER_COSTS_REQUIRED')
    if (candidate.confidence_band!='HIGH' or not candidate.eligible or candidate.pattern_status!='MATCHED'
            or candidate.matched_pattern!='BREAKOUT_CONFIRMATION' or candidate.timeframe!='15m'
            or candidate.symbol not in {'BTCUSDT','ETHUSDT'} or snapshot.evaluation_id!=candidate.evaluation_id
            or snapshot.symbol!=candidate.symbol or snapshot.timeframe!=candidate.timeframe
            or snapshot.as_of>now or now>=candidate.valid_until or candidate.veto_reasons
            or candidate.degraded_evidence_ids or candidate.missing_evidence):
        raise ValueError('PAPER_V1_CANDIDATE_INVALID')
    if (now-snapshot.as_of).total_seconds()>PROFILE.revalidation_seconds or paper_vetoes(snapshot,now=now):
        raise ValueError('PAPER_V1_REVALIDATION_REQUIRED')
    setup=setup_from_snapshot(snapshot)
    side='LONG' if candidate.direction_bias=='BULLISH' else 'SHORT' if candidate.direction_bias=='BEARISH' else None
    if setup is None or setup.side!=side: raise ValueError('SETUP_DIRECTION_CONFLICT')
    from quant_phase9.paper_v1 import CORE_SOURCE_TYPES, paper_semantics
    from quant_phase9.canonical import canonical_sha256
    core=[s for s in snapshot.source_projections if s.source_type in CORE_SOURCE_TYPES]
    if {s.source_type for s in core}!=CORE_SOURCE_TYPES or len(core)!=len(CORE_SOURCE_TYPES):
        raise ValueError('CORE_EVIDENCE_MISSING')
    for s in core:
        fact=paper_semantics(s,snapshot)
        if (canonical_sha256(s.canonical_payload)!=s.canonical_digest or s.availability_status!='AVAILABLE'
                or s.freshness_status!='FRESH' or s.quality_status!='VALID'
                or s.coverage_status in {'PARTIAL','UNKNOWN','NOT_AVAILABLE'}
                or fact is None or fact[0].endswith('UNKNOWN')
                or (s.source_type=='PAPER_OI_CHANGE' and fact[4]<PROFILE.oi_base_change_min)):
            raise ValueError('CORE_EVIDENCE_INVALID')
    ref=quote.ask if side=='LONG' else quote.bid; sign=1 if side=='LONG' else -1
    if sign*(ref-setup.level)>PROFILE.max_chase_atr*setup.atr: raise ValueError('MAX_CHASE')
    if sign*(ref-setup.level)<PROFILE.reclaim_buffer_atr*setup.atr: raise ValueError('RECLAIM_LOST')
    price=next(s for s in snapshot.source_projections if s.source_type=='PAPER_BREAKOUT')
    hour=parse_bars(price.canonical_payload.get('bars_1h',()))[-24:]
    if len(hour)!=24 or not continuous_bars(hour,seconds=3600,as_of=snapshot.as_of): raise ValueError('TARGET_HISTORY_UNKNOWN')
    if (snapshot.as_of-hour[-1].closed_at).total_seconds()>=3600: raise ValueError('TARGET_HISTORY_STALE')
    targets=[v for _,v in confirmed_pivots(hour,high=side=='LONG') if sign*(v-ref)>0]
    if not targets: raise ValueError('TARGET_UNKNOWN')
    target=min(targets) if side=='LONG' else max(targets)
    stop=setup.stop_anchor-sign*PROFILE.stop_atr_buffer*setup.atr
    stop=(stop/instrument.price_tick).to_integral_value(rounding=ROUND_FLOOR if side=='LONG' else ROUND_CEILING)*instrument.price_tick
    target=(target/instrument.price_tick).to_integral_value(rounding=ROUND_FLOOR if side=='LONG' else ROUND_CEILING)*instrument.price_tick
    if stop<=0 or sign*(ref-stop)<=0 or sign*(target-ref)<=0: raise ValueError('PLAN_GEOMETRY_INVALID')
    slip=PROFILE.max_slippage_bps/10000
    worst_entry=ref*(1+sign*slip)
    worst_stop=stop*(1-sign*slip); worst_target=target*(1-sign*slip)
    funding=worst_entry*costs.funding_cost_rate_max_hold
    risk=sign*(worst_entry-worst_stop)+worst_entry*costs.entry_fee_rate+worst_stop*costs.exit_fee_rate+funding
    reward=sign*(worst_target-worst_entry)-worst_entry*costs.entry_fee_rate-worst_target*costs.exit_fee_rate-funding
    if risk<=0 or reward/risk<PROFILE.min_net_R: raise ValueError('MIN_NET_R')
    setup_id=str(canonical_sha256(dict(symbol=snapshot.symbol,side=side,level=setup.level,trigger_at=setup.trigger_at,retest_at=setup.retest_at)))
    return PaperTradePlanV1(str(snapshot.snapshot_digest),side,stop,target,reward/risk,risk,setup_id,setup.trigger_at)

def conservative_risk_policy(policy, account):
    from dataclasses import replace
    notional=account.equity*PROFILE.max_position_notional_equity_ratio
    risk=account.equity*PROFILE.risk_per_trade_equity_ratio
    return replace(policy,max_notional=min(policy.max_notional,notional),max_margin=min(policy.max_margin,notional),
        max_exposure=min(policy.max_exposure,notional),max_leverage=min(policy.max_leverage,D(1)),
        max_risk=min(policy.max_risk,risk),max_reserved_risk=min(policy.max_reserved_risk,risk),max_open_intents=1,
        max_spread_bps=min(policy.max_spread_bps,PROFILE.max_spread_bps),max_slippage_bps=min(policy.max_slippage_bps,PROFILE.max_slippage_bps),
        intent_ttl_seconds=min(policy.intent_ttl_seconds,PROFILE.execution_intent_ttl_seconds))
