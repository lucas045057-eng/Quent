from __future__ import annotations

from decimal import Decimal

from quant_phase6.ai import AIResponse, AIUsage, StrictSchema
from quant_phase6.offline_eval import OfflineCase, OfflineEvalHarness


def test_offline_eval_reports_schema_fidelity_entities_latency_tokens_and_cost():
    schema = StrictSchema(
        required=("event_type", "evidence_refs", "entities"),
        allowed=("event_type", "evidence_refs", "entities"),
        enums={"event_type": ("SECURITY", "GENERAL_MARKET_CONTEXT")},
    )
    cases = (
        OfflineCase(
            "good",
            {"source_refs": ["hash-1"], "entities": ["BTC"]},
            expected_event_type="SECURITY",
            expected_entities=("BTC",),
            expected_evidence_refs=("hash-1",),
        ),
        OfflineCase(
            "unsupported",
            {"source_refs": ["hash-2"], "entities": ["ETH"]},
            expected_event_type="SECURITY",
            expected_entities=("ETH",),
            expected_evidence_refs=("hash-2",),
        ),
        OfflineCase("invalid", {"source_refs": [], "entities": []}),
    )

    def evaluate(case):
        if case.case_id == "invalid":
            output = {"event_type": "SECURITY", "evidence_refs": []}
        elif case.case_id == "unsupported":
            output = {"event_type": "GENERAL_MARKET_CONTEXT", "evidence_refs": ["not-in-input"], "entities": ["ETH"]}
        else:
            output = {"event_type": "SECURITY", "evidence_refs": ["hash-1"], "entities": ["BTC"]}
        return AIResponse(
            provider="fixture", model="fixture-v1", structured_output=output,
            usage=AIUsage(total_tokens=7, estimated_cost=Decimal("0.02"), latency_ms=4),
        )

    report = OfflineEvalHarness(schema, evaluate).run(cases)
    assert report.total_cases == 3
    assert report.schema_compliant == 2
    assert report.entity_matches == 2
    assert report.source_fidelity == 1
    assert report.unsupported_claims == 2
    assert report.average_latency_ms == 4
    assert report.total_tokens == 21
    assert report.estimated_cost == Decimal("0.06")
