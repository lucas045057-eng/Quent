from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase3.adapters.base import (
    AdapterSchemaError,
    PublicTradeAdapter,
    decimal_field,
    epoch_milliseconds,
    require_mapping,
)
from quant_phase3.capabilities import BYBIT_CAPABILITIES


class StubPublicAdapter:
    exchange = "stub"
    capabilities = BYBIT_CAPABILITIES

    def subscription(self, symbol):
        return {"symbol": symbol}

    def parse_ws_message(self, payload, received_at):
        return ()

    def parse_recent_trades(self, payload, received_at):
        return ()

    def identity_key(self, trade):
        return (trade.exchange, trade.trade_id)


def test_public_adapter_protocol_is_exchange_neutral():
    adapter = StubPublicAdapter()

    assert isinstance(adapter, PublicTradeAdapter)
    assert adapter.capabilities.supports_public_trade_stream is True
    assert adapter.subscription("BTCUSDT") == {"symbol": "BTCUSDT"}


def test_common_helpers_validate_payloads_and_normalize_public_values():
    received = datetime(2026, 9, 20, 1, 2, 3, tzinfo=timezone.utc)

    assert require_mapping({"data": []}, "payload")["data"] == []
    assert decimal_field("1.25", "price") == Decimal("1.25")
    assert epoch_milliseconds(1789866123004, "time").tzinfo is timezone.utc
    assert received.utcoffset() == epoch_milliseconds(1789866123004, "time").utcoffset()


@pytest.mark.parametrize(
    "value,field",
    [([], "payload"), (None, "payload"), ("bad", "price"), ("", "time")],
)
def test_common_helpers_fail_closed_on_malformed_exchange_payload(value, field):
    with pytest.raises(AdapterSchemaError):
        if field == "payload":
            require_mapping(value, field)
        elif field == "price":
            decimal_field(value, field)
        else:
            epoch_milliseconds(value, field)


def test_adapter_identity_is_the_only_dedup_identity_boundary():
    adapter = StubPublicAdapter()

    assert adapter.identity_key(type("Trade", (), {"exchange": "stub", "trade_id": "id-1"})()) == (
        "stub",
        "id-1",
    )
