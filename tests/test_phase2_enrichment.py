from datetime import datetime, timezone

from quant_phase1.contracts import DataStatus as Phase1Status
from quant_phase1.stage1 import Stage1Result
from quant_phase2.contracts import CrossExchangeSnapshot, DataStatus
from quant_phase2.enrichment import enrich_stage1_candidate


def test_derivative_enrichment_does_not_change_stage1_classification() -> None:
    result = Stage1Result("BTCUSDT", "B", "WAIT_FOR_TRIGGER", Phase1Status.AVAILABLE, (), {}, None)
    snapshot = CrossExchangeSnapshot("BTC-USDT-PERP", datetime.now(timezone.utc), status=DataStatus.AVAILABLE)
    enriched = enrich_stage1_candidate(result, snapshot)
    assert enriched.classification == "WAIT_TRIGGER"
    assert enriched.context_only is True


def test_missing_derivative_context_is_not_a_phase1_failure() -> None:
    result = Stage1Result("BTCUSDT", "A", "HIGH_CONFIDENCE", Phase1Status.AVAILABLE, (), {}, None)
    enriched = enrich_stage1_candidate(result, None)
    assert enriched.classification == "DEEP_ANALYSIS"
    assert enriched.derivative_status is DataStatus.NOT_AVAILABLE
