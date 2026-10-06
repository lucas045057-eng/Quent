from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide


def _trade(**overrides):
    values = {
        "exchange": "bybit",
        "exchange_symbol": "BTCUSDT",
        "canonical_symbol": "BTC-USDT-PERP",
        "trade_id": "exec-1",
        "price": Decimal("100.00"),
        "quantity_base": Decimal("0.25"),
        "notional_usd": Decimal("25.00"),
        "aggressor_side": TradeSide.BUY,
        "raw_side": "Buy",
        "raw_side_semantics": "EXCHANGE_PROVIDED_TAKER_SIDE",
        "side_source": SideSource.EXCHANGE_PROVIDED,
        "exchange_timestamp": datetime(2026, 9, 20, 1, 2, 3, 4000, tzinfo=timezone.utc),
        "received_at": datetime(2026, 9, 20, 1, 2, 3, 5000, tzinfo=timezone.utc),
        "processed_at": datetime(2026, 9, 20, 1, 2, 3, 6000, tzinfo=timezone.utc),
        "source_channel": "publicTrade.BTCUSDT",
        "status": FlowStatus.AVAILABLE,
        "raw_payload": {"T": 1789866123004, "i": "exec-1"},
        "raw_reference": None,
    }
    values.update(overrides)
    return CanonicalTrade(**values)


def test_canonical_trade_preserves_identity_semantics_and_serializes_utc_fields():
    trade = _trade()

    record = trade.to_record()

    assert trade.trade_id == "exec-1"
    assert trade.aggressor_side is TradeSide.BUY
    assert trade.side_source is SideSource.EXCHANGE_PROVIDED
    assert record["exchange_timestamp"].endswith("+00:00")
    assert record["price"] == "100.00"
    assert record["raw_payload"]["i"] == "exec-1"


def test_canonical_trade_supports_all_runtime_statuses():
    for status in ("AVAILABLE", "STALE", "PARTIAL", "NOT_AVAILABLE", "ERROR"):
        assert _trade(status=FlowStatus(status)).status.value == status


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", Decimal("0")),
        ("quantity_base", Decimal("-1")),
        ("exchange_timestamp", datetime(2026, 9, 20, 1, 2, 3)),
    ],
)
def test_canonical_trade_rejects_invalid_numeric_or_non_utc_timestamps(field, value):
    with pytest.raises(ValueError):
        _trade(**{field: value})


def test_unknown_aggressor_side_is_allowed_only_with_unknown_source():
    trade = _trade(
        aggressor_side=TradeSide.UNKNOWN,
        raw_side="buy",
        raw_side_semantics="TRADE_SIDE_UNCONFIRMED_AGGRESSOR",
        side_source=SideSource.UNKNOWN,
    )

    assert trade.aggressor_side is TradeSide.UNKNOWN
    assert trade.side_source is SideSource.UNKNOWN


def test_inferred_side_is_rejected_by_initial_phase3_contract():
    with pytest.raises(ValueError):
        _trade(aggressor_side=TradeSide.BUY, side_source=SideSource.INFERRED)
