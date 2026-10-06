from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3BasisAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5BasisAdapter
from quant_phase4.adapters.hyperliquid_public import HyperliquidPublicBasisAdapter
from quant_phase4.adapters.base import AdapterSchemaError
from quant_phase4.basis import compute_basis
from quant_phase4.contracts import BasisType, DataStatus, ReasonCode


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
SKEW = timedelta(seconds=5)


def _basis(*, basis_type: BasisType = BasisType.MARK_INDEX, reference: str = "100", offset: timedelta = timedelta()):
    return compute_basis(
        Decimal("101"), Decimal(reference), basis_type=basis_type,
        perpetual_timestamp=NOW, reference_timestamp=NOW + offset,
        max_timestamp_skew=SKEW, source="test", received_at=NOW, fetched_at=NOW,
    )


def test_compute_basis_mark_index_uses_index_formula():
    observation = _basis()

    assert observation.basis_type is BasisType.MARK_INDEX
    assert observation.absolute_basis == Decimal("1")
    assert observation.basis_bps == Decimal("100")
    assert observation.basis_pct == Decimal("1")
    assert observation.status is DataStatus.AVAILABLE


def test_compute_basis_mark_oracle_keeps_type_separate():
    observation = _basis(basis_type=BasisType.MARK_ORACLE)

    assert observation.basis_type is BasisType.MARK_ORACLE
    assert observation.reference_price == Decimal("100")


@pytest.mark.parametrize("reference", ["0", "NaN", "Infinity"])
def test_compute_basis_rejects_invalid_reference_price(reference: str):
    with pytest.raises(ValueError):
        _basis(reference=reference)


@pytest.mark.parametrize("offset", [timedelta(seconds=4), timedelta(seconds=5)])
def test_compute_basis_allows_skew_at_or_below_limit(offset: timedelta):
    assert _basis(offset=offset).status is DataStatus.AVAILABLE


def test_compute_basis_marks_skew_above_limit_stale_with_reason():
    observation = _basis(offset=timedelta(seconds=6))

    assert observation.status is DataStatus.STALE
    assert observation.reason_code is ReasonCode.UNCONFIRMED_SEMANTICS
    assert observation.absolute_basis is None


def test_compute_basis_rejects_non_utc_timestamps():
    with pytest.raises(ValueError):
        compute_basis(
            Decimal("101"), Decimal("100"), basis_type=BasisType.MARK_INDEX,
            perpetual_timestamp=datetime(2026, 9, 21, 12), reference_timestamp=NOW,
            max_timestamp_skew=SKEW, source="test", received_at=NOW, fetched_at=NOW,
        )


def test_bitget_uta_v3_parser_emits_canonical_mark_index_row():
    rows = BitgetUTA3BasisAdapter(canonical_symbol="BTC-USDT-PERP").parse_tickers(
        {"code": "00000", "data": [{"symbol": "BTCUSDT", "markPrice": "101", "indexPrice": "100", "ts": "1789992000000"}]}, NOW
    )

    assert len(rows) == 1
    assert rows[0].exchange == "bitget"
    assert rows[0].basis_type is BasisType.MARK_INDEX
    assert rows[0].status is DataStatus.AVAILABLE
    assert rows[0].raw_payload["markPrice"] == "101"


def test_bybit_v5_parser_propagates_source_status_without_fallback():
    rows = BybitV5BasisAdapter(canonical_symbol="BTC-USDT-PERP").parse_tickers(
        {"retCode": 0, "time": "1789992000000", "result": {"list": [{"symbol": "BTCUSDT", "markPrice": "101", "indexPrice": "100"}]}}, NOW
    )

    assert rows[0].exchange == "bybit"
    assert rows[0].source_endpoint == "/v5/market/tickers"
    assert rows[0].status is DataStatus.AVAILABLE


def test_hyperliquid_without_explicit_exchange_timestamp_is_typed_unavailable():
    rows = HyperliquidPublicBasisAdapter(canonical_symbol="BTC-USDT-PERP").parse_meta_and_asset_contexts(
        [{"universe": [{"name": "BTC"}]}, [{"markPx": "101", "oraclePx": "100"}]], NOW
    )

    assert len(rows) == 1
    assert rows[0].basis_type is BasisType.MARK_ORACLE
    assert rows[0].status is DataStatus.NOT_AVAILABLE
    assert rows[0].reason_code is ReasonCode.UNCONFIRMED_SEMANTICS


def test_mixed_basis_types_are_not_interchangeable():
    mark_index = _basis(basis_type=BasisType.MARK_INDEX)
    mark_oracle = _basis(basis_type=BasisType.MARK_ORACLE)

    assert mark_index.basis_type is not mark_oracle.basis_type


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (
            BitgetUTA3BasisAdapter().parse_tickers,
            {"code": "00000", "data": [{"symbol": "BTCUSDT", "markPrice": "101", "indexPrice": "100", "ts": "1789992000000"}]},
        ),
        (
            BybitV5BasisAdapter().parse_tickers,
            {"retCode": 0, "time": "1789992000000", "result": {"list": [{"symbol": "BTCUSDT", "markPrice": "101", "indexPrice": "100"}]}},
        ),
        (
            HyperliquidPublicBasisAdapter().parse_meta_and_asset_contexts,
            [{"universe": [{"name": "BTC"}]}, [{"markPx": "101", "oraclePx": "100"}]],
        ),
    ],
)
@pytest.mark.parametrize("received_at", [datetime(2026, 9, 21, 12), datetime(2026, 9, 21, 20, tzinfo=timezone(timedelta(hours=8)))])
def test_public_basis_parsers_reject_naive_and_non_utc_received_at(parser, payload, received_at):
    with pytest.raises(AdapterSchemaError, match="received_at"):
        parser(payload, received_at)


@pytest.mark.parametrize("field", ["markPx", "oraclePx"])
@pytest.mark.parametrize("value", [None, "0", "-1", "NaN", "Infinity"])
def test_hyperliquid_rejects_missing_or_invalid_mark_and_oracle_values(field: str, value: object):
    context = {"markPx": "101", "oraclePx": "100"}
    context[field] = value

    with pytest.raises(AdapterSchemaError, match=field):
        HyperliquidPublicBasisAdapter().parse_meta_and_asset_contexts(
            [{"universe": [{"name": "BTC"}]}, [context]], NOW
        )
