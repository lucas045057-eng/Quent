"""Synthetic static-fact revalidation boundaries, preserving original clocks."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from decimal import Decimal as D
import pytest
from strategies.contracts import MarketObservation, AnalysisRequest, ScreeningCandidate
from strategies.canonical_validation import attest_canonical_read, valid_canonical_read
from strategies.refresh import refresh_candidate
from strategies.evidence.evidence_chain import _valid, MarketHypothesis

NOW=datetime(2026,10,3,18,tzinfo=timezone.utc)

def flow():
    return MarketObservation(symbol='BTCUSDT',kind='PERP_FLOW',provider='bitget',source_ref='canonical:PERP_FLOW:SYNTHETIC',
        source_group='BITGET_PERP_FLOW',value='.1',unit='RATIO',observed_at=NOW-timedelta(seconds=120),
        source_event_time=NOW-timedelta(seconds=120),fetched_at=NOW-timedelta(seconds=90),
        availability='AVAILABLE',quality='VALID',coverage='COMPLETE',freshness='FRESH')

def refreshed(o):
    req=AnalysisRequest(candidate=ScreeningCandidate(symbol='BTCUSDT',category='A',reason_codes=('SYNTHETIC_FIXTURE',)),
        requested_at=NOW,deadline=NOW+timedelta(seconds=15),screening_digest='SYNTHETIC',required_kinds=('PERP_FLOW',))
    class Source:
        data_source='SYNTHETIC_FIXTURE'
        def refresh(self,request):return (o,)
    return refresh_candidate(req,source=Source(),now=NOW+timedelta(seconds=2))

def test_actual_canonical_recheck_accepts_static_closed_fact_without_advancing_source_clocks():
    original=flow();checked=attest_canonical_read(original,checked_at=NOW+timedelta(seconds=1))
    assert checked.fetched_at==original.fetched_at and checked.observed_at==original.observed_at
    assert checked.source_event_time==original.source_event_time and checked.value==original.value
    assert refreshed(original).refresh_status=='UNAVAILABLE'
    snapshot=refreshed(checked)
    assert snapshot.refresh_status=='REFRESHED'
    assert _valid(snapshot.observations[0],snapshot,MarketHypothesis(direction='LONG',timeframe='15m',structure='TREND_CONTINUATION'))

@pytest.mark.parametrize('update',[
    {'value':D('9')}, {'unit':'USD'}, {'revalidated_at':NOW-timedelta(seconds=1)},
    {'revalidated_at':NOW+timedelta(seconds=3)}, {'source_ref':'different'},
])
def test_changed_fact_or_out_of_request_recheck_time_invalidates_attestation(update):
    o=attest_canonical_read(flow(),checked_at=NOW+timedelta(seconds=1)).model_copy(update=update)
    assert not valid_canonical_read(o,requested_at=NOW,captured_at=NOW+timedelta(seconds=2))
    assert refreshed(o).refresh_status=='UNAVAILABLE'

def test_new_recheck_does_not_rescue_expired_source_window_or_unknown_units():
    old=flow().model_copy(update={'observed_at':NOW-timedelta(hours=1)})
    checked=attest_canonical_read(old,checked_at=NOW+timedelta(seconds=1))
    assert refreshed(checked).refresh_status=='UNAVAILABLE'
    missing=flow().model_copy(update={'kind':'OI','value':None,'unit':None,'quality':'UNKNOWN','coverage':'UNKNOWN'})
    assert attest_canonical_read(missing,checked_at=NOW).revalidated_at is None

def test_external_research_receipt_cannot_claim_a_canonical_db_recheck():
    external=flow().model_copy(update={'provider':'legacy_feed','source_ref':'legacy://feed'})
    assert attest_canonical_read(external,checked_at=NOW).revalidated_at is None

def test_runtime_reloads_local_quotes_after_external_lookups(monkeypatch):
    import strategies.providers as providers
    from strategies.runtime import CanonicalResearchFactory
    from strategies.analysis.deep_analyzer import AnalysisPolicyV2
    now=datetime.now(timezone.utc);reads=[];lookups=[]
    old=flow().model_copy(update={'kind':'PRICE','value':D('100'),'unit':'USDT','observed_at':now,'fetched_at':now})
    new=old.model_copy(update={'value':D('101')})
    factory=CanonicalResearchFactory('SYNTHETIC',policy=object(),analysis_policy=AnalysisPolicyV2(),environ={})
    def view(at, *, symbols=None):
        assert symbols == ("BTCUSDT","ETHUSDT")
        reads.append(at)
        return SimpleNamespace(symbols=(SimpleNamespace(symbol='BTCUSDT',observations=(old if len(reads)==1 else new,)),))
    factory.view=view
    class Sources:
        def __init__(self,**kwargs):pass
        def fetch(self,*args,**kwargs):lookups.append(len(reads));return ()
    monkeypatch.setattr(providers,'PublicResearchSources',Sources)
    req=AnalysisRequest(candidate=ScreeningCandidate(symbol='BTCUSDT',category='A',reason_codes=('SYNTHETIC_FIXTURE',)),
        requested_at=now,deadline=now+timedelta(seconds=15),screening_digest='SYNTHETIC',required_kinds=('PRICE',))
    rows=factory.refresh(req)
    assert lookups==[1] and len(reads)==2 and rows[0].value==101
