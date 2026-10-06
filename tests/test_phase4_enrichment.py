from datetime import datetime, timezone

from quant_phase1.contracts import DataStatus as Phase1Status
from quant_phase1.stage1 import Stage1Result
from quant_phase4.contracts import CoverageSemantics
from quant_phase4.cross_exchange import build_phase4_context
from tests.test_phase4_cross_exchange import liquidation
from quant_phase4.enrichment import Phase4EnrichmentState, enrich_stage1_phase4


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def result():
    return Stage1Result("BTCUSDT", "A", "HIGH_CONFIDENCE", Phase1Status.AVAILABLE, ("price",), {}, "BULLISH")


def test_available_enrichment_preserves_stage1_and_is_context_only():
    context = build_phase4_context([], [], [], NOW)
    enriched = enrich_stage1_phase4(result(), context, NOW)

    assert enriched.state is Phase4EnrichmentState.UNAVAILABLE
    assert enriched.phase1_result == result()
    assert enriched.symbol == "BTCUSDT"
    assert enriched.classification == result().classification
    assert enriched.context_only is True
    assert not hasattr(enriched, "ai_evidence")
    assert not hasattr(enriched, "order")


def test_partial_enrichment_does_not_change_stage1_fields():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS)],
        [], [], NOW,
    )
    enriched = enrich_stage1_phase4(result(), context, NOW)

    assert enriched.state is Phase4EnrichmentState.PARTIAL
    assert enriched.phase1_result == result()
    assert enriched.phase1_result.category == "A"
    assert enriched.phase1_result.reason == "HIGH_CONFIDENCE"


def test_all_unavailable_is_typed_and_processed_at_is_deterministic():
    context = build_phase4_context([], [], [], NOW)
    first = enrich_stage1_phase4(result(), context, NOW)
    second = enrich_stage1_phase4(result(), context, NOW)

    assert first.state is Phase4EnrichmentState.UNAVAILABLE
    assert first.reason == second.reason == "PHASE4_NOT_AVAILABLE"
    assert first.processed_at == second.processed_at == NOW
