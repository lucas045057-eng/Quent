"""Owner-decided Conservative Paper V1 semantics; pure, causal Decimal transforms.

This module supplies policy content, never an approval. Nothing here opens a
network client, chooses a position size or sends an order.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D
from collections.abc import Mapping, Sequence

from quant_features.core import exact_decimal


@dataclass(frozen=True, slots=True)
class PaperV1Profile:
    name: str = 'QUANT_PAPER_V1_CONSERVATIVE'
    breakout_lookback_bars: int = 20
    volume_lookback_bars: int = 20
    volume_ratio_min: D = D('1.25')
    breakout_buffer_atr: D = D('0.15')
    retest_band_atr: D = D('0.20')
    reclaim_buffer_atr: D = D('0.05')
    invalidation_buffer_atr: D = D('0.20')
    retest_timeout_bars: int = 4
    max_chase_atr: D = D('0.50')
    swing_left: int = 2
    swing_right: int = 2
    swing_lookback_1h_bars: int = 24
    stop_atr_buffer: D = D('0.25')
    min_net_R: D = D('1.5')
    flow_confirmation_windows: int = 2
    delta_ratio_min: D = D('0.05')
    oi_base_change_min: D = D('0.0025')
    funding_normalized_8h_long_max: D = D('0.0005')
    funding_normalized_8h_short_min: D = D('-0.0005')
    decision_ttl_seconds: int = 600
    execution_intent_ttl_seconds: int = 30
    revalidation_seconds: int = 60
    max_hold_seconds: int = 10800
    cooldown_seconds: int = 1800
    max_spread_bps: D = D('8')
    max_slippage_bps: D = D('10')
    risk_per_trade_equity_ratio: D = D('0.0025')
    max_position_notional_equity_ratio: D = D('0.10')

PROFILE = PaperV1Profile()
CORE_SOURCE_TYPES = frozenset({
    'PAPER_BREAKOUT', 'PAPER_PERP_FLOW', 'PAPER_SPOT_FLOW', 'PAPER_OI_CHANGE',
    'PAPER_FUNDING', 'PAPER_MARKET_CONTEXT', 'PAPER_EVENT_RISK',
})

def is_paper_v1(manifest) -> bool:
    return any(getattr(p, 'strategy_profile', None) == PROFILE.name
               for p in manifest.manifest.policy_content.enabled_patterns)

def timestamp(value):
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError: return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        return None
    return value


@dataclass(frozen=True, slots=True)
class BarV1:
    closed_at: datetime
    open: D
    high: D
    low: D
    close: D
    turnover: D

    def __post_init__(self):
        if timestamp(self.closed_at) is None or any(
                exact_decimal(v) is None or not isinstance(v, D) for v in
                (self.open, self.high, self.low, self.close, self.turnover)):
            raise ValueError('exact Decimal prices and UTC close required')
        if min(self.open,self.high,self.low,self.close)<=0 or self.turnover<0 or not (
                self.low <= min(self.open,self.close) <= max(self.open,self.close) <= self.high):
            raise ValueError('invalid OHLCV')

def parse_bars(rows):
    try:
        return tuple(BarV1(timestamp(r.get('bar_close_timestamp') or r.get('closed_at')),
                           *(exact_decimal(r.get(k)) for k in ('open','high','low','close','turnover')))
                     for r in rows)
    except (TypeError, ValueError, AttributeError):
        return ()

def continuous_bars(bars, *, seconds=900, as_of):
    return bool(bars) and all(b.closed_at<=as_of for b in bars) and all(
        b.closed_at-a.closed_at == timedelta(seconds=seconds) for a,b in zip(bars,bars[1:]))

def confirmed_pivots(bars: Sequence[BarV1], *, high: bool):
    result=[]
    for i in range(PROFILE.swing_left, len(bars)-PROFILE.swing_right):
        center=bars[i].high if high else bars[i].low
        neighbors=bars[i-PROFILE.swing_left:i]+bars[i+1:i+1+PROFILE.swing_right]
        if all(center>(b.high) if high else center<(b.low) for b in neighbors):
            result.append((bars[i].closed_at,center))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class BreakoutSetupV1:
    side: str
    level: D
    atr: D
    volume_ratio: D
    trigger_at: datetime
    retest_at: datetime
    stop_anchor: D

def breakout_retest(bars: Sequence[BarV1], side: str, *, as_of: datetime):
    bars=tuple(bars)
    if side not in {'LONG','SHORT'} or len(bars)<22 or not continuous_bars(bars,as_of=as_of):
        return None
    # Closed bars end at the most recent 15m boundary; forming bars never qualify.
    boundary=datetime.fromtimestamp(int(as_of.timestamp())//900*900,tz=as_of.tzinfo)
    if bars[-1].closed_at!=boundary: return None
    sign=1 if side=='LONG' else -1
    for i in range(max(20,len(bars)-5),len(bars)-1):
        prior=bars[i-20:i]
        level=max(b.high for b in prior) if sign==1 else min(b.low for b in prior)
        tr=[max(b.high-b.low,abs(b.high-bars[j-1].close),abs(b.low-bars[j-1].close))
            for j,b in enumerate(bars[i-14:i],start=i-14)]
        atr=sum(tr,D(0))/14
        baseline=sum(b.turnover for b in prior)/20
        if atr<=0 or baseline<=0: continue
        trigger=bars[i]
        ratio=trigger.turnover/baseline
        if sign*(trigger.close-level)<PROFILE.breakout_buffer_atr*atr or ratio<PROFILE.volume_ratio_min:
            continue
        retest=None
        for bar in bars[i+1:]:
            if sign*(bar.close-level)<-PROFILE.invalidation_buffer_atr*atr:
                retest=None; break
            distance=int((bar.closed_at-trigger.closed_at).total_seconds()/900)
            if distance>PROFILE.retest_timeout_bars and retest is None: break
            in_band=bar.low<=level+PROFILE.retest_band_atr*atr and bar.high>=level-PROFILE.retest_band_atr*atr
            if retest is None and in_band and sign*(bar.close-level)>=PROFILE.reclaim_buffer_atr*atr:
                retest=bar
        if retest is None: continue
        anchor=retest.low if sign==1 else retest.high
        swings=confirmed_pivots(bars[:i+1],high=sign==-1)
        if swings:
            anchor=min(anchor,swings[-1][1]) if sign==1 else max(anchor,swings[-1][1])
        return BreakoutSetupV1(side,level,atr,ratio,trigger.closed_at,retest.closed_at,anchor)
    return None

def flow_confirmation(rows, side, *, as_of):
    if side not in {'LONG','SHORT'} or len(rows)<3: return None
    rows=tuple(rows[-3:]); sign=1 if side=='LONG' else -1
    if len({(r.get('exchange'),r.get('session')) for r in rows})!=1 or not rows[0].get('session'):
        return None
    ratios=[]
    for i,r in enumerate(rows):
        start,end=timestamp(r.get('window_open')),timestamp(r.get('window_close'))
        if start is None or end is None or end-start!=timedelta(minutes=15) or end>as_of:
            return None
        if not r.get('coverage_complete') or r.get('unknown_trade_count')!=0 or r.get('gap') or r.get('reset'):
            return None
        total,delta,cvd=(exact_decimal(r.get(k)) for k in ('total_volume_base','delta_base','cvd'))
        if total is None or total<=0 or delta is None or abs(delta)>total or cvd is None: return None
        if i:
            if timestamp(rows[i-1]['window_close'])!=start: return None
            previous=exact_decimal(rows[i-1].get('cvd'))
            if cvd-previous!=delta or sign*delta/total<PROFILE.delta_ratio_min: return None
            ratios.append(sign*delta/total)
    boundary=datetime.fromtimestamp(int(as_of.timestamp())//900*900,tz=as_of.tzinfo)
    return min(ratios) if timestamp(rows[-1]['window_close'])==boundary else None

def oi_confirmation(rows, *, as_of):
    if len(rows)!=2 or len({r.get('exchange') for r in rows})!=1 or not rows[0].get('exchange'):
        return None
    start,end=(timestamp(r.get('at')) for r in rows)
    if start is None or end is None or end-start!=timedelta(minutes=15) or end>as_of or (as_of-end).total_seconds()>60:
        return None
    first,last=(exact_decimal(r.get('base')) for r in rows)
    if first is None or first<=0 or last is None or last<0 or any(not r.get('unit_verified') for r in rows): return None
    return last/first-1

def funding_confirmation(rate, interval, normalized, side):
    rate,normalized=exact_decimal(rate),exact_decimal(normalized)
    if side not in {'LONG','SHORT'} or rate is None or normalized is None or type(interval) is not int or interval<=0:
        return None
    if rate*D(28800)/interval!=normalized: return None
    if side=='LONG' and normalized>PROFILE.funding_normalized_8h_long_max: return None
    if side=='SHORT' and normalized<PROFILE.funding_normalized_8h_short_min: return None
    return normalized

def market_vetoes(side, benchmark_1h, benchmark_4h, volatility):
    aliases={'TREND_UP':'BULLISH','TREND_DOWN':'BEARISH'}
    benchmark_1h=aliases.get(benchmark_1h,benchmark_1h)
    benchmark_4h=aliases.get(benchmark_4h,benchmark_4h)
    if side not in {'LONG','SHORT'} or benchmark_1h not in {'BULLISH','BEARISH','RANGE','NEUTRAL'} or benchmark_4h not in {'BULLISH','BEARISH','RANGE','NEUTRAL'} or volatility not in {'LOW','NORMAL','HIGH','EXTREME'}:
        return ('MARKET_CONTEXT_UNKNOWN',)
    opposing='BEARISH' if side=='LONG' else 'BULLISH'
    veto=[]
    if benchmark_1h==benchmark_4h==opposing: veto.append('BENCHMARK_OPPOSITION')
    if volatility=='EXTREME': veto.append('EXTREME_VOLATILITY')
    return tuple(veto)

def event_risk(health, macro, security, *, now):
    if not isinstance(health,Mapping): return ('EVENT_RISK_UNKNOWN',)
    checked,start,end=(timestamp(health.get(k)) for k in ('checked_at','coverage_start','coverage_end'))
    if health.get('status')!='AVAILABLE' or checked is None or not 0<=(now-checked).total_seconds()<=60 or not health.get('calendar_complete') or not health.get('security_complete') or start is None or end is None or start>now or end<now+timedelta(seconds=PROFILE.max_hold_seconds):
        return ('EVENT_RISK_UNKNOWN',)
    veto=[]
    for e in macro:
        known,scheduled=timestamp(e.get('known_at')),timestamp(e.get('scheduled_at'))
        if known is None or known>now: return ('EVENT_RISK_UNKNOWN',)
        if str(e.get('importance')).upper()=='HIGH':
            if scheduled is None: return ('EVENT_RISK_UNKNOWN',)
            if scheduled-timedelta(hours=1)<=now<=scheduled+timedelta(minutes=30):
                veto.append('SCHEDULED_MACRO_BLACKOUT')
    for e in security:
        known=timestamp(e.get('known_at'))
        if known is None or known>now: return ('EVENT_RISK_UNKNOWN',)
        if str(e.get('event_type')).upper() in {'SECURITY_EXPLOIT','EXPLOIT','HACK','DELISTING'}:
            cleared=timestamp(e.get('cleared_at'))
            if cleared is None or cleared>now: veto.append('SECURITY_EVENT_VETO')
    return tuple(dict.fromkeys(veto))

def conservative_policy_content():
    """Reviewable manifest content, without an approval or runtime activation."""
    from .policy import PolicyContentV1
    content={name:[] for name in PolicyContentV1.model_fields}
    facts=[('setup','PHASE1','PAPER_BREAKOUT','PRICE_STRUCTURE'),
           ('perp','PHASE3','PAPER_PERP_FLOW','TRADE_FLOW'),
           ('spot','PHASE7','PAPER_SPOT_FLOW','ONCHAIN_SPOT_MACRO'),
           ('oi','PHASE2','PAPER_OI_CHANGE','OPEN_INTEREST_STRUCTURE'),
           ('funding','PHASE2','PAPER_FUNDING','FUNDING_BASIS_POSITIONING'),
           ('market','PHASE5','PAPER_MARKET_CONTEXT','MARKET_REGIME'),
           ('events','PHASE6','PAPER_EVENT_RISK','ONCHAIN_SPOT_MACRO')]
    for side in ('LONG','SHORT'):
        refs=[]
        for name,phase,source,category in facts:
            ref=name+'_'+side; refs.append(ref)
            threshold=PROFILE.oi_base_change_min if name=='oi' else None
            content['predicates'].append(dict(predicate_id=ref,source_phase=phase,source_type=source,evidence_type=category,
                accepted_semantic_codes=(('OI_BASE_CHANGE',) if name=='oi' else (name.upper()+'_'+side+'_CONFIRMED',)),
                operator='GTE' if name=='oi' else 'PRESENT',threshold=threshold,upper_threshold=None,
                unit='RATIO' if name=='oi' else None,missing_behavior='FAIL_CLOSED'))
        content['enabled_patterns'].append(dict(pattern_type='BREAKOUT_CONFIRMATION',timeframe='15m',direction=side,
            required_predicates=refs,supporting_predicates=[],contradicting_predicates=[],hard_conflict_predicates=[],
            freshness_rule_ids=[],coverage_rule_ids=[],confidence_ceiling='HIGH',jev_required_conflict_classes=[],
            ttl_rule_id='paper_ttl',revalidation_rule_id='paper_revalidation',approval_reference='OWNER_DECISION_2026_09_30',strategy_profile=PROFILE.name))
    content['ttl_rules']=[dict(rule_id='paper_ttl',pattern_type='BREAKOUT_CONFIRMATION',timeframe='15m',ttl_seconds=600)]
    content['revalidation_rules']=[dict(rule_id='paper_revalidation',pattern_type='BREAKOUT_CONFIRMATION',timeframe='15m',cadence_seconds=60,max_runs_per_minute=1)]
    return PolicyContentV1.model_validate(content)

def setup_from_snapshot(snapshot):
    sources=[s for s in snapshot.source_projections if s.source_type=='PAPER_BREAKOUT']
    if len(sources)!=1: return None
    bars=parse_bars(sources[0].canonical_payload.get('bars_15m',()))
    setups=[s for side in ('LONG','SHORT') if (s:=breakout_retest(bars,side,as_of=snapshot.as_of)) is not None]
    return setups[0] if len(setups)==1 else None

def paper_semantics(projection, snapshot):
    """One derived fact per core bundle, bound to the immutable setup direction."""
    from .contracts import EvidenceDirectionV1 as Direction, EvidenceStrengthV1 as Strength
    setup=setup_from_snapshot(snapshot)
    side=setup.side if setup is not None else None
    source=projection.source_type; p=projection.canonical_payload
    numeric=None; unit=None; name=None; valid=False
    if source=='PAPER_BREAKOUT':
        name='SETUP'; valid=setup is not None
        if valid: numeric=setup.volume_ratio; unit='RATIO'
    elif source in {'PAPER_PERP_FLOW','PAPER_SPOT_FLOW'}:
        name='PERP' if source=='PAPER_PERP_FLOW' else 'SPOT'
        values=[flow_confirmation(g,side,as_of=snapshot.as_of) for g in p.get('groups',(p.get('rows',()),))]
        numeric=min(values) if values and all(v is not None for v in values) else None
        valid=numeric is not None; unit='RATIO' if valid else None
    elif source=='PAPER_OI_CHANGE':
        values=[oi_confirmation(g,as_of=snapshot.as_of) for g in p.get('groups',(p.get('rows',()),))]
        numeric=min(values) if values and all(v is not None for v in values) else None
        return ('OI_BASE_CHANGE' if numeric is not None else 'OI_UNKNOWN',Direction.NEUTRAL,Strength.STRONG if numeric is not None else Strength.UNKNOWN,
                'Verified paired base OI change' if numeric is not None else 'Paired base OI unavailable',numeric,'RATIO' if numeric is not None else None)
    elif source=='PAPER_FUNDING':
        name='FUNDING'
        values=[funding_confirmation(v.get('rate'),v.get('interval'),v.get('normalized'),side) for v in p.get('values',(p,))]
        numeric=(max(values) if side=='LONG' else min(values)) if values and all(v is not None for v in values) else None
        valid=numeric is not None; unit='RATIO' if valid else None
    elif source=='PAPER_MARKET_CONTEXT':
        name='MARKET'; valid=not market_vetoes(side,p.get('benchmark_1h'),p.get('benchmark_4h'),p.get('volatility'))
    elif source=='PAPER_EVENT_RISK':
        name='EVENTS'; valid=not event_risk(p.get('health'),p.get('macro',()),p.get('security',()),now=snapshot.as_of)
    if name is None: return None
    code=f'{name}_{side}_CONFIRMED' if valid and side else f'{name}_UNKNOWN'
    direction=(Direction.BULLISH if side=='LONG' else Direction.BEARISH) if valid and side else Direction.UNKNOWN
    return code,direction,Strength.STRONG if valid else Strength.UNKNOWN,code,numeric,unit

def paper_vetoes(snapshot, *, now=None):
    veto=[]; setup=setup_from_snapshot(snapshot)
    if snapshot.symbol not in {'BTCUSDT','ETHUSDT'} or snapshot.timeframe!='15m': veto.append('PAPER_V1_SCOPE')
    if setup is None: veto.append('SETUP_UNKNOWN')
    for s in snapshot.source_projections:
        p=s.canonical_payload
        if s.source_type=='PAPER_MARKET_CONTEXT':
            veto.extend(market_vetoes(setup.side if setup else None,p.get('benchmark_1h'),p.get('benchmark_4h'),p.get('volatility')))
        elif s.source_type=='PAPER_EVENT_RISK':
            veto.extend(event_risk(p.get('health'),p.get('macro',()),p.get('security',()),now=now or snapshot.as_of))
    return tuple(dict.fromkeys(veto))
