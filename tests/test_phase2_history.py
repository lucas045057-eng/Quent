from datetime import datetime, timezone
from decimal import Decimal

from quant_phase2.contracts import DataStatus, OIObservation
from quant_phase2.normalization import compute_oi_change


def test_persisted_oi_history_can_supply_a_window_after_restart() -> None:
    now = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    rows = []
    for minutes, value in ((10, "120"), (0, "100")):
        rows.append(OIObservation(
            symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange="bybit", contract_type="PERPETUAL",
            margin_asset="USDT", settle_asset="USDT", raw_open_interest=Decimal(value), raw_unit="BASE_ASSET",
            open_interest_base=Decimal(value), open_interest_quote=None, open_interest_usd=Decimal(value),
            mark_price=Decimal("1"), normalization_method="test", exchange_timestamp=now.replace(minute=minutes),
            fetched_at=now, processed_at=now, status=DataStatus.AVAILABLE, source_endpoint="test", raw_payload={}
        ))
    result = compute_oi_change(rows, requested_window_seconds=600, canonical_symbol="BTC-USDT-PERP")
    assert result.status is DataStatus.AVAILABLE
    assert result.change_absolute_usd == Decimal("20")
