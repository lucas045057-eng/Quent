from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)

def test_horizon_is_not_timeframe():
    from strategies.contracts import StructuredTradeThesis
    with pytest.raises(ValueError):
        StructuredTradeThesis(symbol="SOLUSDT", horizon="15m", timeframe="15m",
            generated_at=NOW, valid_until=NOW+timedelta(minutes=10),
            snapshot_digest="a"*64, bias="WAIT", market_structure="RANGE",
            confidence="INSUFFICIENT", no_trade_reason="NO_TRADE_BY_DATA")

def test_missing_evidence_never_becomes_neutral_or_zero():
    from strategies.contracts import MarketObservation, EvidenceNode
    observation=MarketObservation(symbol="SOLUSDT", kind="SPOT_CVD", provider="bitget",
        source_ref="spot:SOLUSDT", availability="UNAVAILABLE")
    assert observation.value is None
    with pytest.raises(ValueError):
        EvidenceNode(observation=observation, role="SUPPORTING", interpretation="buying")

def test_stale_evidence_cannot_support_confirmation():
    from strategies.contracts import MarketObservation, EvidenceNode
    observation=MarketObservation(symbol="SOLUSDT", kind="OI", provider="bitget",
        source_ref="oi:SOLUSDT", value=Decimal("100"), unit="BASE",
        observed_at=NOW, availability="AVAILABLE", freshness="STALE",
        quality="VALID", coverage="COMPLETE", source_group="bitget:OI")
    with pytest.raises(ValueError):
        EvidenceNode(observation=observation, role="SUPPORTING", interpretation="expanding")

def test_ambiguous_trigger_and_future_facts_are_rejected():
    from strategies.contracts import TriggerRule, MarketObservation
    with pytest.raises(ValueError):
        TriggerRule(side="LONG", price="100", operator="exec", required_kinds=("PRICE",))
    with pytest.raises(ValueError):
        MarketObservation(symbol="SOLUSDT", kind="PRICE", provider="bitget", source_ref="quote",
            value="NaN", unit="USDT", availability="AVAILABLE")

def test_analysis_request_requires_a_candidate():
    from strategies.contracts import AnalysisRequest, ScreeningCandidate
    candidate=ScreeningCandidate(symbol="SOLUSDT", category="B", reason_codes=("WAITING_FOR_TRIGGER",))
    with pytest.raises(ValueError):
        AnalysisRequest(candidate=candidate, requested_at=NOW, deadline=NOW+timedelta(seconds=60),
            screening_digest="a"*64)
