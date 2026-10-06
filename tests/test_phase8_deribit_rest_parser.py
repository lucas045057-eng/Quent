from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from quant_phase8 import contracts
from quant_phase8.adapters import deribit_rest


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 25, 16, 30, 1, tzinfo=timezone.utc)


def _load(name):
    return contracts.loads_decimal_json((FIXTURES / name).read_text(encoding="utf-8"))


def _require_parser(name):
    parser = getattr(deribit_rest, name, None)
    assert callable(parser), f"REST adapter must expose {name}"
    return parser


def _index_names():
    return _require_parser("parse_index_price_names_response")(_load("rest_index_names.json"))


def _instruments(currency="BTC", *, payload=None, max_records=2048):
    payload = payload or _load(f"rest_instruments_{currency.lower()}.json")
    return _require_parser("parse_instruments_response")(
        payload,
        requested_currency=currency,
        supported_index_names=_index_names(),
        fetched_at=T0,
        processed_at=T1,
        max_records=max_records,
    )


def test_index_name_and_instrument_parser_api_is_available():
    assert callable(getattr(deribit_rest, "parse_index_price_names_response", None)), (
        "adapter must parse the official supported index-name response"
    )
    assert callable(getattr(deribit_rest, "parse_instruments_response", None)), (
        "adapter must parse and validate the official instrument response"
    )
    names = _index_names()
    assert {"btc_usd", "eth_usd"}.issubset(names)


def test_index_name_parser_rejects_extended_object_schema_and_jsonrpc_errors():
    with pytest.raises(ValueError, match="string list"):
        _require_parser("parse_index_price_names_response")(
            {"jsonrpc": "2.0", "id": 1, "result": [{"name": "btc_usd"}]}
        )
    with pytest.raises(ValueError, match="returned an error"):
        _require_parser("parse_index_price_names_response")(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "failure"}}
        )


def test_rest_fixture_manifest_hashes_and_synthetic_origin_are_valid():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["origin"].startswith("synthetic minimized examples")
    assert manifest["source_capture_timestamp_utc"] is None
    assert manifest["contains_credentials_or_private_data"] is False
    for filename, expected_hash in manifest["fixtures"].items():
        actual_hash = hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest()
        assert actual_hash == expected_hash


def test_instruments_map_exact_decimals_utc_and_dynamic_price_index():
    rows = _instruments("ETH")

    assert len(rows) == 2
    assert rows[0].symbol == "ETH-30OCT26-4000-C"
    assert rows[0].strike.as_tuple().exponent == -3
    assert rows[0].strike == Decimal("4000.125")
    assert rows[0].price_index == "eth_usd"
    assert rows[0].expires_at == datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc)
    assert rows[0].exchange_timestamp is None
    assert rows[0].fetched_at == T0
    assert rows[0].instrument_created_at is not None
    assert rows[0].instrument_created_at != rows[0].fetched_at


@pytest.mark.parametrize(
    ("mutate", "currency", "message"),
    [
        (lambda p: p["result"][0].update({"base_currency": "ETH"}), "BTC", "currency"),
        (lambda p: p["result"][1].update({"instrument_name": p["result"][0]["instrument_name"]}), "BTC", "duplicate"),
        (lambda p: p["result"][0].update({"kind": "future"}), "BTC", "kind"),
        (lambda p: p["result"][0].update({"expiration_timestamp": "bad-time"}), "BTC", "timestamp"),
        (lambda p: p["result"][0].update({"price_index": "unlisted_index"}), "BTC", "index"),
    ],
)
def test_instrument_contract_mismatches_fail_closed(mutate, currency, message):
    payload = deepcopy(_load(f"rest_instruments_{currency.lower()}.json"))
    mutate(payload)

    with pytest.raises(ValueError, match=message):
        _instruments(currency, payload=payload)


def test_instrument_row_cap_rejects_whole_response_without_truncating():
    with pytest.raises(ValueError, match="cap"):
        _instruments("BTC", max_records=1)


def test_full_chain_summary_preserves_exact_values_units_nulls_and_source_time():
    instruments = _instruments("BTC")
    observations = _require_parser("parse_book_summary_response")(
        _load("rest_book_summary_btc.json"),
        requested_currency="BTC",
        instrument_catalog=instruments,
        fetched_at=T0,
        processed_at=T1,
        max_records=2048,
    )

    assert len(observations) == 2
    call = observations[0]
    assert call.price_index == "btc_usd"
    assert call.underlying_index == "index_price"
    assert call.quote_currency == "BTC"
    assert call.metrics["volume_24h"].value == Decimal("0.123456789012345678901")
    assert call.metrics["volume_24h"].unit_code == "BTC"
    assert call.metrics["open_interest"].value == Decimal("12.34567890123456789")
    assert call.metrics["open_interest"].unit_code == "BTC"
    assert call.metrics["mark_iv"].value == Decimal("57.1234")
    assert call.metrics["mark_iv"].unit_status is contracts.UnitStatus.SOURCE_NATIVE_UNVERIFIED
    assert call.metrics["mark_iv"].provenance is contracts.Provenance.SOURCE_PROVIDED
    assert call.metrics["source_creation_timestamp_ms"].value == Decimal("1780000000000")
    assert call.metrics["source_creation_timestamp_ms"].source_field == "creation_timestamp"
    assert call.metrics["source_creation_timestamp_ms"].unit_code == "unix_ms"
    assert call.metrics["bid_price"].value is None
    assert call.metrics["bid_price"].quality_reason == "EXPLICIT_NULL"
    assert observations[1].metrics["bid_price"].value is None
    assert observations[1].metrics["bid_price"].quality_reason == "MISSING_FIELD"
    assert call.exchange_timestamp is None
    assert call.fetched_at == T0
    assert call.received_at is None


def test_summary_underlying_index_is_not_instrument_price_index_and_cap_rejects_whole_cycle():
    instruments = _instruments("BTC")
    payload = deepcopy(_load("rest_book_summary_btc.json"))
    payload["result"][0]["underlying_index"] = "index_price"
    observations = _require_parser("parse_book_summary_response")(
        payload, requested_currency="BTC", instrument_catalog=instruments,
        fetched_at=T0, processed_at=T1, max_records=2048,
    )
    assert observations[0].price_index == "btc_usd"
    assert observations[0].underlying_index == "index_price"

    with pytest.raises(ValueError, match="cap"):
        _require_parser("parse_book_summary_response")(
            _load("rest_book_summary_btc.json"), requested_currency="BTC",
            instrument_catalog=instruments, fetched_at=T0, processed_at=T1, max_records=1,
        )


def test_summary_rejects_unknown_instrument_instead_of_dropping_it():
    instruments = _instruments("ETH")
    payload = deepcopy(_load("rest_book_summary_eth.json"))
    payload["result"][0]["instrument_name"] = "ETH-UNKNOWN"

    with pytest.raises(ValueError, match="instrument"):
        _require_parser("parse_book_summary_response")(
            payload, requested_currency="ETH", instrument_catalog=instruments,
            fetched_at=T0, processed_at=T1, max_records=2048,
        )


def test_summary_missing_and_null_required_values_remain_partial_without_zero_fill():
    instruments = _instruments("BTC")
    payload = deepcopy(_load("rest_book_summary_btc.json"))
    payload["result"][0].pop("volume")
    payload["result"][1]["open_interest"] = None
    observations = _require_parser("parse_book_summary_response")(
        payload, requested_currency="BTC", instrument_catalog=instruments,
        fetched_at=T0, processed_at=T1, max_records=2048,
    )

    assert observations[0].status is contracts.DataStatus.PARTIAL
    assert observations[0].metrics["volume_24h"].value is None
    assert observations[0].metrics["volume_24h"].quality_reason == "MISSING_FIELD"
    assert observations[1].status is contracts.DataStatus.PARTIAL
    assert observations[1].metrics["open_interest"].value is None
    assert observations[1].metrics["open_interest"].quality_reason == "EXPLICIT_NULL"
    assert all(observation.metrics["volume_24h"].value != Decimal("0") for observation in observations)


def test_summary_rejects_binary_float_values_and_currency_mismatch():
    instruments = _instruments("BTC")
    float_payload = deepcopy(_load("rest_book_summary_btc.json"))
    float_payload["result"][0]["volume"] = 0.125
    with pytest.raises(ValueError, match="finite JSON number"):
        _require_parser("parse_book_summary_response")(
            float_payload, requested_currency="BTC", instrument_catalog=instruments,
            fetched_at=T0, processed_at=T1, max_records=2048,
        )

    currency_payload = deepcopy(_load("rest_book_summary_btc.json"))
    currency_payload["result"][0]["quote_currency"] = "ETH"
    with pytest.raises(ValueError, match="quote currency"):
        _require_parser("parse_book_summary_response")(
            currency_payload, requested_currency="BTC", instrument_catalog=instruments,
            fetched_at=T0, processed_at=T1, max_records=2048,
        )
