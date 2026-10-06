from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import pytest
from strategies.contracts import MarketObservation, AnalysisRequest, ScreeningCandidate
NOW=datetime(2026,10,3,tzinfo=timezone.utc)

def test_missing_event_timestamp_is_not_fetch_timestamp():
    from strategies.market_view import observation_from_projection
    o=observation_from_projection(symbol="SOLUSDT",kind="OI",provider="aggregator",
        payload={"value":"10","unit":"BASE","observed_at":NOW,"fetched_at":NOW,
            "availability":"AVAILABLE","freshness":"FRESH","quality":"VALID","coverage":"COMPLETE"},
        source_ref="agg:oi")
    assert o.source_event_time is None and o.fetched_at==NOW

def test_historical_retired_receipt_stays_readable_without_becoming_a_risk_fact():
    from strategies.providers import ProviderReceipt, receipt_observation
    receipt=ProviderReceipt(provider="gnews",symbol="BTCUSDT",kind="EVENT_COVERAGE",
        endpoint="https://gnews.io/api/v4/search",fetched_at=NOW,availability="AVAILABLE",
        coverage="COMPLETE",payload_json='{"risk_flag":0}')
    observation=receipt_observation(receipt)
    assert observation.kind=="EVENT_COVERAGE"
    assert observation.value is None and observation.unit is None and not observation.usable
    assert observation.reason=="LEGACY_EVENT_SOURCE_RETIRED"

def test_analysis_rejects_reused_screening_snapshot():
    from strategies.refresh import refresh_candidate
    request=AnalysisRequest(candidate=ScreeningCandidate(symbol="SOLUSDT",category="A",reason_codes=("OPPORTUNITY",)),
        requested_at=NOW,deadline=NOW+timedelta(seconds=60),screening_digest="a"*64,required_kinds=("PRICE",))
    old=MarketObservation(symbol="SOLUSDT",kind="PRICE",provider="bitget",source_ref="old",
        source_group="price",value="100",unit="USDT",observed_at=NOW-timedelta(seconds=1),
        fetched_at=NOW-timedelta(seconds=1),availability="AVAILABLE",freshness="FRESH",quality="VALID",coverage="COMPLETE")
    class Source:
        data_source="SYNTHETIC_FIXTURE"
        def refresh(self,request):return (old,)
    result=refresh_candidate(request,source=Source(),now=NOW)
    assert result.refresh_status=="UNAVAILABLE"
    assert result.observations[0].freshness=="STALE"

def test_altcoin_spot_flow_keeps_its_own_symbol():
    from strategies.providers import normalize_provider_observations
    raw={"observations":[{"symbol":"ETHUSDT","kind":"SPOT_FLOW","value":"0.1","unit":"RATIO",
        "observed_at":NOW.isoformat(),"fetched_at":NOW.isoformat(),"availability":"AVAILABLE",
        "freshness":"FRESH","quality":"VALID","coverage":"COMPLETE"}]}
    with pytest.raises(ValueError):
        normalize_provider_observations(raw,symbol="SOLUSDT",provider="bitget",required_kinds=("SPOT_FLOW",))
