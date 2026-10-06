from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from quant_phase8 import contracts


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=10)
T2 = T0 + timedelta(seconds=20)

try:
    deribit_ws = importlib.import_module("quant_phase8.adapters.deribit_ws")
except ModuleNotFoundError:
    deribit_ws = None


def _api(name):
    function = getattr(deribit_ws, name, None) if deribit_ws is not None else None
    assert callable(function), f"WebSocket adapter must expose {name}"
    return function


def _load(name):
    return contracts.loads_decimal_json((FIXTURES / name).read_text(encoding="utf-8"))


def _instrument(symbol, underlying, option_type, price_index):
    return contracts.OptionInstrument(
        exchange="DERIBIT",
        source="deribit",
        symbol=symbol,
        underlying=underlying,
        option_type=option_type,
        strike=Decimal("100000" if underlying == "BTC" else "4000"),
        expires_at=datetime(2026, 10, 30, 10, 40, tzinfo=timezone.utc),
        instrument_created_at=datetime(2026, 5, 28, 20, 26, 40, tzinfo=timezone.utc),
        instrument_state="open",
        is_active=True,
        price_index=price_index,
        base_currency=underlying,
        quote_currency=underlying,
        settlement_currency=underlying,
        exchange_timestamp=None,
        fetched_at=T0,
        processed_at=T1,
    )


def _catalog():
    return {
        "BTC-30OCT26-100000-C": _instrument(
            "BTC-30OCT26-100000-C", "BTC", "call", "btc_usd"
        ),
        "BTC-30OCT26-100000-P": _instrument(
            "BTC-30OCT26-100000-P", "BTC", "put", "btc_usd"
        ),
        "ETH-30OCT26-4000-C": _instrument(
            "ETH-30OCT26-4000-C", "ETH", "call", "eth_usd"
        ),
    }


def test_ws_fixture_manifest_hashes_and_synthetic_origin_are_valid():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["origin"].startswith("synthetic minimized examples")
    assert manifest["source_capture_timestamp_utc"] is None
    assert manifest["contains_credentials_or_private_data"] is False
    for filename, expected_hash in manifest["fixtures"].items():
        actual_hash = hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest()
        assert actual_hash == expected_hash


def test_subscription_builders_include_lifecycle_and_only_bounded_tickers():
    lifecycle = _api("build_lifecycle_channels")(["BTC", "ETH"])
    indexes = _api("derive_markprice_channels")(
        tuple(_catalog().values()), {"btc_usd", "eth_usd"}, max_channels=4
    )
    tickers = _api("derive_ticker_channels")(
        ["BTC-30OCT26-100000-C", "BTC-30OCT26-100000-P"], max_subscriptions=2
    )
    channels = (*lifecycle, *indexes, *tickers)

    assert lifecycle == (
        "instrument.creation.option.BTC",
        "instrument.state.option.BTC",
        "instrument.creation.option.ETH",
        "instrument.state.option.ETH",
    )
    assert indexes == ("markprice.options.btc_usd", "markprice.options.eth_usd")
    assert tickers == (
        "incremental_ticker.BTC-30OCT26-100000-C",
        "incremental_ticker.BTC-30OCT26-100000-P",
    )
    assert not any(channel.startswith("ticker.") for channel in channels)
    assert len(tickers) == 2


def test_channel_derivation_rejects_unsupported_index_and_overflow_without_truncation():
    with pytest.raises(ValueError, match="supported index"):
        _api("derive_markprice_channels")(
            tuple(_catalog().values()), {"btc_usd"}, max_channels=4
        )
    with pytest.raises(ValueError, match="cap"):
        _api("derive_ticker_channels")(
            ["BTC-30OCT26-100000-C", "BTC-30OCT26-100000-P"], max_subscriptions=1
        )


def test_subscribe_request_uses_public_jsonrpc_schema():
    request = _api("build_subscribe_request")(
        ["instrument.creation.option.BTC", "markprice.options.btc_usd"], request_id=17
    )

    assert request == {
        "jsonrpc": "2.0",
        "id": 17,
        "method": "public/subscribe",
        "params": {"channels": ["instrument.creation.option.BTC", "markprice.options.btc_usd"]},
    }


def test_lifecycle_notifications_keep_source_and_local_times_separate():
    instrument, creation = _api("parse_instrument_creation_notification")(
        _load("ws_instrument_creation.json"),
        supported_index_names={"btc_usd"},
        received_at=T1,
        processed_at=T2,
    )
    state = _api("parse_instrument_state_notification")(
        _load("ws_instrument_state.json"), received_at=T1, processed_at=T2
    )

    assert instrument.symbol == "BTC-30OCT26-100000-C"
    assert instrument.instrument_created_at == datetime(2026, 5, 28, 20, 26, 40, tzinfo=timezone.utc)
    assert instrument.exchange_timestamp == T0
    assert instrument.fetched_at == T1
    assert creation.event_type is contracts.InstrumentEventType.CREATION
    assert creation.exchange_timestamp == T0
    assert creation.received_at == T1
    assert creation.processed_at == T2
    assert state.event_type is contracts.InstrumentEventType.STATE
    assert state.instrument_state == "locked"
    assert state.exchange_timestamp == T0 + timedelta(seconds=1)
    assert state.received_at == T1


def test_instrument_creation_rejects_naive_local_timestamps():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _api("parse_instrument_creation_notification")(
            _load("ws_instrument_creation.json"),
            supported_index_names={"btc_usd"},
            received_at=datetime(2026, 9, 25, 16, 30),
            processed_at=T2,
        )


def test_markprice_seed_builds_full_index_state_and_sparse_change_preserves_omissions():
    catalog = _catalog()
    parse = _api("parse_markprice_notification")
    merge = _api("merge_markprice_state")
    seed = parse(
        _load("ws_markprice_seed.json"), instrument_catalog=catalog,
        received_at=T1, processed_at=T2, is_seed=True,
    )
    state = merge({}, seed, is_seed=True)
    assert set(state) == {"BTC-30OCT26-100000-C", "BTC-30OCT26-100000-P"}

    sparse = parse(
        _load("ws_markprice_change.json"), instrument_catalog=catalog,
        received_at=T1 + timedelta(seconds=5), processed_at=T2 + timedelta(seconds=5),
        is_seed=False,
    )
    assert set(sparse[0].metrics) == {"mark_price"}
    merged = merge(state, sparse, is_seed=False)
    call = merged["BTC-30OCT26-100000-C"]
    put = merged["BTC-30OCT26-100000-P"]

    assert call.metrics["mark_price"].value == Decimal("0.005112345678901234")
    assert call.metrics["mark_price"].exchange_timestamp == T0 + timedelta(seconds=5)
    assert call.metrics["mark_price"].received_at == T1 + timedelta(seconds=5)
    assert call.metrics["iv"].value == Decimal("57.1234")
    assert call.metrics["iv"].exchange_timestamp == T0
    assert call.metrics["iv"].received_at == T1
    assert put.metrics["mark_price"].value == Decimal("0.006012345678901234")


def test_markprice_duplicate_is_idempotent_and_older_field_cannot_roll_back():
    catalog = _catalog()
    parse = _api("parse_markprice_notification")
    merge = _api("merge_markprice_state")
    state = merge(
        {},
        parse(_load("ws_markprice_seed.json"), instrument_catalog=catalog,
              received_at=T1, processed_at=T2, is_seed=True),
        is_seed=True,
    )
    current = parse(
        _load("ws_markprice_change.json"), instrument_catalog=catalog,
        received_at=T1, processed_at=T2, is_seed=False,
    )
    once = merge(state, current, is_seed=False)
    twice = merge(once, current, is_seed=False)
    older = parse(
        _load("ws_markprice_out_of_order.json"), instrument_catalog=catalog,
        received_at=T1 + timedelta(seconds=6), processed_at=T2 + timedelta(seconds=6),
        is_seed=False,
    )
    after_older = merge(twice, older, is_seed=False)
    price = after_older["BTC-30OCT26-100000-C"].metrics["mark_price"]

    assert twice["BTC-30OCT26-100000-C"].metrics["mark_price"].value == Decimal("0.005112345678901234")
    assert price.value == Decimal("0.005112345678901234")
    assert price.exchange_timestamp == T0 + timedelta(seconds=5)


def test_incremental_ticker_snapshot_then_sparse_change_merges_fieldwise():
    parse = _api("parse_incremental_ticker_notification")
    apply = _api("apply_incremental_ticker")
    snapshot = parse(_load("ws_ticker_snapshot.json"), received_at=T1, processed_at=T2)
    state = apply({}, snapshot)
    change = parse(
        _load("ws_ticker_change.json"), received_at=T1 + timedelta(seconds=3),
        processed_at=T2 + timedelta(seconds=3),
    )
    assert change.is_snapshot is False
    assert set(change.metrics) == {"bid_iv", "delta"}
    merged = apply(state, change)

    assert merged["mark_iv"].value == Decimal("57.1234")
    assert merged["mark_iv"].exchange_timestamp == T0
    assert merged["mark_iv"].received_at == T1
    assert merged["bid_iv"].value is None
    assert merged["bid_iv"].status is contracts.DataStatus.NOT_AVAILABLE
    assert merged["bid_iv"].quality_reason == "EXPLICIT_NULL"
    assert merged["bid_iv"].exchange_timestamp == T0 + timedelta(seconds=3)
    assert merged["delta"].value == Decimal("0.502")
    assert merged["delta"].provenance is contracts.Provenance.SOURCE_PROVIDED


def test_incremental_ticker_duplicate_is_idempotent_and_out_of_order_change_is_ignored():
    parse = _api("parse_incremental_ticker_notification")
    apply = _api("apply_incremental_ticker")
    snapshot = parse(_load("ws_ticker_snapshot.json"), received_at=T1, processed_at=T2)
    state = apply({}, snapshot)
    change = parse(_load("ws_ticker_change.json"), received_at=T1, processed_at=T2)
    once = apply(state, change)
    twice = apply(once, change)
    older = parse(_load("ws_ticker_out_of_order.json"), received_at=T1, processed_at=T2)
    after_older = apply(twice, older)

    assert twice == once
    assert after_older["bid_iv"].value is None
    assert after_older["bid_iv"].exchange_timestamp == T0 + timedelta(seconds=3)
