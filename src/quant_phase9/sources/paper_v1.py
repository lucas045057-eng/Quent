"""Bounded Paper V1 bundles on the existing repeatable-read source connection.

Historical clocks/digests remain in each bundle. Stream coverage is inherited,
never inferred from ratio=1 or a successful query. Missing health is UNKNOWN.
"""
from datetime import timedelta
from decimal import Decimal

from . import read_rows, make_projection, context_evaluation_id, status_pair
from quant_phase9.contracts import (
    SourcePhaseV1, PolicyDataStatusV1 as Status, EvidenceFreshnessV1 as Fresh,
    EvidenceQualityV1 as Quality, PolicyCoverageStatusV1 as Coverage,
)
from quant_phase9.paper_v1 import PROFILE, timestamp
from .phase5 import _json as normalize_phase5_json


def bundle_projection(rows, *, source_type, payload, as_of, require_current_freshness=False):
    if not rows: raise ValueError('bundle needs at least one source, including explicit missing sources')
    latest=max(rows,key=lambda r:r.observed_at or r.available_at or as_of-timedelta(days=36500))
    valid=all(r.availability_status==Status.AVAILABLE and r.quality_status==Quality.VALID for r in rows)
    if require_current_freshness:
        valid &= all(r.freshness_status==Fresh.FRESH for r in rows)
    coverage=Coverage.UNKNOWN if any(r.coverage_status==Coverage.UNKNOWN for r in rows) else (
        Coverage.PARTIAL if any(r.coverage_status in {Coverage.PARTIAL,Coverage.NOT_AVAILABLE} for r in rows) else None)
    return make_projection(evaluation_id=latest.evaluation_id,source_phase=latest.source_phase,source_type=source_type,
        source_ref=f'paper_v1:{source_type}',symbol=latest.symbol,market=latest.market,
        event_time=latest.event_time,observed_at=latest.observed_at,captured_at=latest.captured_at,
        processed_at=latest.processed_at,available_at=latest.available_at,
        availability_status=Status.AVAILABLE if valid else Status.PARTIAL,
        freshness=latest.freshness_status,quality=Quality.VALID if valid else Quality.PARTIAL,
        coverage=coverage,payload=dict(payload,sources=[dict(ref=r.source_ref,digest=r.canonical_digest,
            observed_at=r.observed_at,available_at=r.available_at,status=r.availability_status,
            freshness=r.freshness_status,quality=r.quality_status,coverage=r.coverage_status) for r in rows]))


def read_event_risk(connection, *, symbol, now):
    """Existing health records must explicitly attest calendar/security coverage."""
    rows=read_rows(connection,"""SELECT component,status,checked_at,details FROM system_health
        WHERE component IN ('phase6-macro-ingestion','phase6-news-ingestion') AND checked_at<=%s""",(now,))
    by={r['component']:r for r in rows}
    macro_health=by.get('phase6-macro-ingestion'); security_health=by.get('phase6-news-ingestion')
    health=None
    if macro_health and security_health:
        a,b=macro_health.get('details') or {},security_health.get('details') or {}
        health=dict(status='AVAILABLE' if macro_health['status']==security_health['status']=='AVAILABLE' else 'UNKNOWN',
            checked_at=min(macro_health['checked_at'],security_health['checked_at']),
            calendar_complete=a.get('calendar_complete') is True,security_complete=b.get('security_complete') is True,
            coverage_start=None,coverage_end=None,sources=[macro_health,security_health])
        starts=[timestamp(d.get('coverage_start')) for d in (a,b)]
        ends=[timestamp(d.get('coverage_end')) for d in (a,b)]
        if all(t is not None for t in starts+ends):
            health.update(coverage_start=max(starts),coverage_end=min(ends))
    # Limit+1 => a bounded overflow is UNKNOWN rather than truncating away risk.
    macro=read_rows(connection,"""SELECT scheduled_at,importance,observed_at AS known_at,status
        FROM phase6_macro_events WHERE observed_at<=%s AND fetched_at<=%s AND processed_at<=%s
        AND scheduled_at BETWEEN %s AND %s ORDER BY scheduled_at LIMIT 65""",
        (now,now,now,now-timedelta(minutes=30),now+timedelta(hours=1)))
    security=read_rows(connection,"""SELECT event_type,observed_at AS known_at,status
        FROM phase6_news_events WHERE observed_at<=%s AND fetched_at<=%s AND processed_at<=%s
        AND (symbols @> %s::jsonb OR symbols='[]'::jsonb)
        AND upper(event_type) IN ('SECURITY_EXPLOIT','EXPLOIT','HACK','DELISTING')
        ORDER BY observed_at DESC LIMIT 65""",(now,now,now,__import__('json').dumps([symbol])))
    if len(macro)>64 or len(security)>64 or any(r['status']!='AVAILABLE' for r in macro+security):
        health=None
    return dict(health=health,macro=macro,security=security)


class PaperV1SourceReader:
    def read(self,conn,*,candidate,timeframe,as_of):
        from .phase2 import select_phase2
        from .phase3 import select_phase3
        from .phase7 import select_phase7
        from quant_instruments import resolve_core_instrument
        eid=context_evaluation_id(candidate,timeframe,as_of)
        def standalone(phase,kind,payload,observed=None,valid=False,coverage=None):
            return make_projection(evaluation_id=eid,source_phase=SourcePhaseV1(phase),source_type=kind,
                source_ref=f'paper_v1:{kind}',symbol=candidate.symbol,market=candidate.market,
                event_time=observed,observed_at=observed,captured_at=None,processed_at=as_of,available_at=as_of,
                availability_status=Status.AVAILABLE if valid else Status.NOT_AVAILABLE,
                freshness=Fresh.FRESH if valid else Fresh.UNKNOWN,
                quality=Quality.VALID if valid else Quality.UNKNOWN,coverage=coverage,payload=payload)
        bars={}
        for interval,seconds,limit in [('15m',900,26),('1H',3600,24)]:
            rows=read_rows(conn,"""SELECT id,bar_open_timestamp,open,high,low,close,turnover,status,source,exchange,
                exchange_timestamp,fetched_at,processed_at FROM klines WHERE symbol=%s AND interval=%s
                AND bar_open_timestamp<=%s AND exchange_timestamp<=%s AND fetched_at<=%s AND processed_at<=%s
                ORDER BY bar_open_timestamp DESC,id DESC LIMIT %s""",
                (candidate.symbol,interval,as_of-timedelta(seconds=seconds),as_of,as_of,as_of,limit))
            bars[interval]=[dict(r,closed_at=r['bar_open_timestamp']+timedelta(seconds=seconds)) for r in reversed(rows)]
        price_payload=dict(bars_15m=bars['15m'],bars_1h=bars['1H'])
        current=bars['15m'][-1]['closed_at'] if bars['15m'] else None
        price_valid=all(bars.values()) and all(r['status']=='AVAILABLE' for v in bars.values() for r in v)
        price_valid &= all(len({(r['source'],r['exchange']) for r in v})==1 for v in bars.values())
        result=[standalone('PHASE1','PAPER_BREAKOUT',price_payload,current,price_valid)]
        canonical=resolve_core_instrument(conn,candidate.symbol).canonical_symbol
        # Capture up to two latest exact 15m endpoints per venue, without USD substitution.
        oi=read_rows(conn,"""WITH latest AS (SELECT DISTINCT ON (exchange) * FROM open_interest
            WHERE canonical_symbol=%s AND exchange_timestamp<=%s AND fetched_at<=%s AND processed_at<=%s
            ORDER BY exchange,exchange_timestamp DESC,id DESC)
            SELECT o.* FROM latest l JOIN open_interest o ON o.exchange=l.exchange AND o.canonical_symbol=l.canonical_symbol
            AND o.exchange_timestamp IN (l.exchange_timestamp,l.exchange_timestamp-interval '15 minutes')
            WHERE o.fetched_at<=%s AND o.processed_at<=%s ORDER BY o.exchange,o.exchange_timestamp LIMIT 33""",
            (canonical,as_of,as_of,as_of,as_of,as_of))
        from .phase2 import _valid_oi_normalization
        groups={}
        for r in oi:
            groups.setdefault(r['exchange'],[]).append(dict(exchange=r['exchange'],at=r['exchange_timestamp'],
                base=r['open_interest_base'],unit_verified=r['status']=='AVAILABLE' and _valid_oi_normalization(r)))
        result.append(standalone('PHASE2','PAPER_OI_CHANGE',dict(groups=list(groups.values())),
            max((r['exchange_timestamp'] for r in oi),default=None),bool(oi) and len(oi)<=32))
        derivatives=select_phase2(conn,candidate,'15m',as_of)
        funding=[r for r in derivatives if r.source_type=='FUNDING_RATE']
        latest_funding={}
        for r in funding:
            venue=r.canonical_payload.get('exchange')
            if venue not in latest_funding: latest_funding[venue]=r
        funding=list(latest_funding.values())
        values=[dict(rate=r.canonical_payload.get('funding_rate'),interval=r.canonical_payload.get('funding_interval_seconds'),normalized=r.canonical_payload.get('normalized_8h_rate')) for r in funding]
        result.append(bundle_projection(funding,source_type='PAPER_FUNDING',payload=dict(values=values),as_of=as_of,require_current_freshness=True) if funding else standalone('PHASE2','PAPER_FUNDING',{},valid=False))
        for phase,kind,raw in [('PHASE3','PAPER_PERP_FLOW',select_phase3(conn,candidate,'15m',as_of)),
                               ('PHASE7','PAPER_SPOT_FLOW',select_phase7(conn,candidate,'15m',as_of))]:
            expected='TRADE_FLOW_WINDOW' if phase=='PHASE3' else 'SPOT_FLOW_WINDOW'
            flows=[r for r in raw if r.source_type==expected]
            by={}
            cvds={}
            for r in raw:
                if r.source_type=='CVD_SNAPSHOT':
                    key=(r.canonical_payload.get('exchange'),timestamp(r.canonical_payload.get('window_end')))
                    cvds.setdefault(key,[]).append(r)
            for r in flows:
                p=r.canonical_payload; venue=p.get('exchange'); end=timestamp(p.get('window_close'))
                matching=cvds.get((venue,end),[])
                cvd=matching[0] if len(matching)==1 else None
                session=p.get('session')
                cvd_identity=(phase!='PHASE3' or cvd is not None and session
                    and cvd.canonical_payload.get('session')==session)
                complete=r.coverage_status==Coverage.SOURCE_DECLARED_COMPLETE and bool(cvd_identity)
                row=dict(exchange=venue,window_open=p.get('window_open'),window_close=p.get('window_close'),
                    total_volume_base=p.get('total_volume_base',p.get('base_volume')),delta_base=p.get('delta_base',p.get('delta')),
                    unknown_trade_count=p.get('unknown_trade_count',0 if p.get('unknown_volume')==0 else None),
                    cvd=cvd.canonical_payload.get('value') if cvd else p.get('cvd'),
                    session=session,coverage_complete=complete,
                    reset=bool(p.get('reset') or (cvd and cvd.canonical_payload.get('reset'))),
                    gap=any(g.source_type=='TRADE_GAP' and g.canonical_payload.get('exchange')==venue
                        and timestamp(g.canonical_payload.get('gap_start')) is not None
                        and timestamp(g.canonical_payload['gap_start'])<=end
                        and (timestamp(g.canonical_payload.get('gap_end')) or as_of)>=timestamp(p.get('window_open')) for g in raw))
                by.setdefault(venue,[]).append(row)
            groups=[sorted(v,key=lambda r:timestamp(r['window_close']))[-3:] for v in by.values()]
            # Only the needed windows affect bundle health; older research rows are not execution core.
            used=[r for r in flows if any(timestamp(r.canonical_payload.get('window_close'))==timestamp(v['window_close'])
                and r.canonical_payload.get('exchange')==v['exchange'] for group in groups for v in group)]
            # CVD state is a required input, including its own health and digest.
            used_cvd=[r for key,values in cvds.items() for r in values if any(
                key==(v['exchange'],timestamp(v['window_close'])) for group in groups for v in group)]
            used+=used_cvd
            result.append(bundle_projection(used,source_type=kind,payload=dict(groups=groups),as_of=as_of) if used else standalone(phase,kind,{},valid=False))
        benchmark='ETHUSDT' if candidate.symbol=='BTCUSDT' else 'BTCUSDT'
        context={}; good=True; observed=[]; context_sources=[]
        for interval in ('1H','4H'):
            rows=read_rows(conn,"""SELECT trend_state,volatility_state,context_timestamp,status,freshness_status,data_quality,missing_count
                FROM phase5_market_leader_context WHERE symbol=%s AND timeframe=%s AND context_timestamp<=%s AND processed_at<=%s
                ORDER BY context_timestamp DESC,id DESC LIMIT 1""",(benchmark,interval,as_of,as_of))
            r=rows[0] if rows else {}
            quality=normalize_phase5_json(r.get('data_quality'))
            context_sources.append(dict(r,data_quality=quality,timeframe=interval,benchmark=benchmark))
            context['benchmark_'+interval.lower()]=r.get('trend_state')
            good &= r.get('status')=='AVAILABLE' and r.get('freshness_status')=='AVAILABLE' and r.get('missing_count')==0
            good &= isinstance(quality,dict) and quality.get('source_status')=='AVAILABLE'
            if r.get('context_timestamp'): observed.append(r['context_timestamp'])
            if r.get('volatility_state')=='EXTREME': context['volatility']='EXTREME'
        regime=read_rows(conn,"""SELECT volatility_regime,volatility_status,context_timestamp FROM phase5_market_regime_snapshots
            WHERE timeframe='15m' AND context_timestamp<=%s AND processed_at<=%s ORDER BY context_timestamp DESC,id DESC LIMIT 1""",(as_of,as_of))
        if 'volatility' not in context: context['volatility']=regime[0]['volatility_regime'] if regime else None
        good &= bool(regime) and regime[0]['volatility_status']=='AVAILABLE'
        if regime:
            context_sources.append(dict(regime[0],timeframe='15m'))
            observed.append(regime[0]['context_timestamp'])
        context['sources']=context_sources
        good &= len(observed)==3 and all(0<=(as_of-t).total_seconds()<=60 for t in observed)
        result.append(standalone('PHASE5','PAPER_MARKET_CONTEXT',context,min(observed) if observed else None,good))
        events=read_event_risk(conn,symbol=candidate.symbol,now=as_of)
        h=events['health']; checked=timestamp(h.get('checked_at')) if h else None
        result.append(standalone('PHASE6','PAPER_EVENT_RISK',events,checked,bool(h) and h['status']=='AVAILABLE'))
        return tuple(result)
