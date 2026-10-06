from datetime import datetime, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.persistence import stage1_result_json, retention_delete_statements
from quant_phase1.stage1 import Stage1Result


def test_stage1_result_serialization_preserves_decimal_as_text_and_status():
    result = Stage1Result(
        "BTCUSDT", "A", "ok", DataStatus.AVAILABLE, ("price",), {"atr": Decimal("1.2")}, "BULLISH",
        ("STRUCTURE_ALIGNED",), {"spread_ratio": Decimal("0.001")}, None, datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    payload = stage1_result_json(result)
    assert payload["symbol"] == "BTCUSDT"
    assert payload["status"] == "AVAILABLE"
    assert payload["indicators"]["atr"] == "1.2"
    assert payload["classification"] == "DEEP_ANALYSIS"
    assert payload["reason_codes"] == ["STRUCTURE_ALIGNED"]
    assert payload["key_metrics"]["spread_ratio"] == "0.001"


def test_retention_cleanup_is_configured_per_timeframe():
    statements = retention_delete_statements({"5m": 30, "15m": 90, "1H": 180, "4H": 365})
    assert len(statements) == 4
    assert "interval = %s" in statements[0][0]
    assert [params[1] for _, params in statements] == [30, 90, 180, 365]
