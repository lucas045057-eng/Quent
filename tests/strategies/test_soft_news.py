"""Regression coverage for retiring news/calendar inputs from V2 admission."""
from datetime import timedelta

from strategies.analysis.deep_analyzer import AnalysisPolicyV2
from strategies.contracts import AnalysisRequest
from strategies.evidence.evidence_chain import MarketHypothesis
from strategies.execution.execution_policy import ExecutionPolicyV2
from tests.strategies.test_deep_analyzer import NOW, complete_snapshot
from tests.strategies.test_evidence_chain import observation


def test_legacy_news_settings_and_requirements_are_read_without_restoring_gates():
    candidate = complete_snapshot().candidate
    request = AnalysisRequest.model_validate({
        "candidate": candidate.model_dump(mode="json"),
        "requested_at": NOW,
        "deadline": NOW + timedelta(seconds=15),
        "screening_digest": "a" * 64,
        "news_mode": "HARD",
        "required_kinds": (
            "PRICE", "SPOT_FLOW", "EVENT_COVERAGE",
            "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE",
        ),
    })
    assert request.required_kinds == ("PRICE", "SPOT_FLOW")

    analysis = AnalysisPolicyV2.model_validate({
        "provider_name": "fixture", "model": "fixture", "news_mode": "SOFT",
    })
    execution = ExecutionPolicyV2.model_validate({
        "allowed_symbols": ("SOLUSDT",), "data_source": "SYNTHETIC_FIXTURE",
        "news_mode": "HARD",
    })
    hypothesis = MarketHypothesis.model_validate({
        "direction": "LONG", "timeframe": "15m", "structure": "TREND_CONTINUATION",
        "news_mode": "HARD",
    })
    assert not hasattr(analysis, "news_mode")
    assert not hasattr(execution, "news_mode")
    assert not hasattr(hypothesis, "news_mode")
    assert not set(("EVENT_COVERAGE", "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE")) & set(hypothesis.required_kinds)


def test_retired_sources_are_not_sent_to_analysis_prompt():
    from strategies.analysis.prompts import analysis_prompt
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis

    snap = complete_snapshot()
    old_facts = tuple(
        observation(kind, "1", "RISK_FLAG", "LEGACY_SOURCE")
        for kind in ("EVENT_COVERAGE", "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE")
    )
    snap = snap.model_copy(update={"observations": (*snap.observations, *old_facts)})
    prompt = analysis_prompt(snap, AnalysisPolicyV2(provider_name="fixture", model="fixture"))
    assert all(kind not in prompt.untrusted_data for kind in
        ("EVENT_COVERAGE", "EXCHANGE_EVENT_COVERAGE", "MACRO_COVERAGE"))
    chain = build_evidence_chain(snap, hypothesis=MarketHypothesis(
        direction="LONG", timeframe="15m", structure="TREND_CONTINUATION"))
    assert chain.confirmed and chain.advisories == ()
