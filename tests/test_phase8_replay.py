from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from quant_phase1.db import apply_migrations
from quant_phase8.adapters.deribit_rest import (
    parse_book_summary_response,
    parse_index_price_names_response,
    parse_instruments_response,
)
from quant_phase8.adapters.deribit_ws import (
    apply_incremental_ticker,
    derive_markprice_channels,
    parse_incremental_ticker_notification,
    parse_instrument_creation_notification,
    parse_instrument_state_notification,
    parse_markprice_notification,
    merge_markprice_state,
)
from quant_phase8.config import Phase8Settings
from quant_phase8.context import OptionsContextInputs, calculate_options_context
from quant_phase8.contracts import (
    DataStatus,
    ObservationKind,
    OptionInstrument,
    OptionMarketObservation,
    OptionType,
    Provenance,
    UnitStatus,
)
from quant_phase8.persistence import Phase8Repository
from quant_phase8.universe import select_ticker_universe


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)
REST_CAPTURE = T0 + timedelta(minutes=5)
WS_SEED_RECEIVE = T0 + timedelta(minutes=10)
CONTEXT_TIME = T0 + timedelta(minutes=15)


def test_replay_manifest_pins_small_synthetic_multi_expiry_fixture():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))

    replay_fixture = manifest["replay_fixture"]
    filename = replay_fixture["filename"]
    fixture_path = FIXTURES / filename
    raw = fixture_path.read_bytes()

    assert filename == "replay_chain.json"
    assert replay_fixture["origin"] == "synthetic"
    assert len(raw) <= 32 * 1024
    assert hashlib.sha256(raw).hexdigest() == replay_fixture["sha256"]
    assert manifest["contains_credentials_or_private_data"] is False
    assert manifest["source_capture_timestamp_utc"] is None

    fixture = json.loads(raw)
    for currency in ("BTC", "ETH"):
        instruments = fixture["instruments"][currency]
        expiries = {row["expiration_timestamp"] for row in instruments}
        option_types = {row["option_type"] for row in instruments}
        strikes = {row["strike"] for row in instruments}
        assert len(expiries) >= 2
        assert option_types == {"call", "put"}
        assert len(strikes) >= 3


def test_manifest_pins_every_fixture_and_references_only_local_replay_templates():
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for filename, expected_hash in manifest["fixtures"].items():
        payload = (FIXTURES / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_hash
        assert len(payload) <= 32 * 1024
        lowered = payload.lower()
        for forbidden in (b"api_key", b"password", b"authorization", b"private_key", b"access_token"):
            assert forbidden not in lowered
    assert set(manifest["replay_source_templates"]).issubset(manifest["fixtures"])


@pytest.fixture
def replay_postgres():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured for isolated Phase 8 replay")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "replay requires loopback PostgreSQL"
    assert info.get("dbname") == "quant_phase8_test", "replay requires the disposable quant_phase8_test database"
    schema = f"phase8_replay_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection = psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public")
    try:
        apply_migrations(connection)
        yield connection, Phase8Repository(connection, settings=_settings())
    finally:
        connection.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _settings():
    return Phase8Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE8_OPTIONS_ENABLED": "1",
        "PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "6",
        "PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "12",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "16",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "32",
    })


def _fixture(name):
    from quant_phase8.contracts import loads_decimal_json

    return loads_decimal_json((FIXTURES / name).read_text(encoding="utf-8"))


def _catalog_and_indexes(settings):
    replay = _fixture("replay_chain.json")
    index_names = parse_index_price_names_response(_fixture("rest_index_names.json"))
    catalog = []
    for currency in ("BTC", "ETH"):
        payload = _fixture(f"rest_instruments_{currency.lower()}.json")
        payload["result"].extend(replay["instruments"][currency])
        catalog.extend(parse_instruments_response(
            payload,
            requested_currency=currency,
            supported_index_names=index_names,
            fetched_at=REST_CAPTURE,
            processed_at=REST_CAPTURE + timedelta(seconds=1),
            max_records=settings.max_full_chain_records_per_underlying,
        ))
    return replay, index_names, tuple(catalog)


def _summary_cycles(replay, catalog, settings):
    result = {}
    for currency in ("BTC", "ETH"):
        payload = _fixture(f"rest_book_summary_{currency.lower()}.json")
        payload["result"].extend(replay["book_summary"][currency])
        instruments = tuple(item for item in catalog if item.underlying == currency)
        result[currency] = parse_book_summary_response(
            payload,
            requested_currency=currency,
            instrument_catalog=instruments,
            fetched_at=REST_CAPTURE,
            processed_at=REST_CAPTURE + timedelta(seconds=1),
            max_records=settings.max_full_chain_records_per_underlying,
        )
    return result


def _ticker_observation(instrument, metrics, *, processed_at):
    source_times = [item.exchange_timestamp for item in metrics.values() if item.exchange_timestamp]
    receive_times = [item.received_at for item in metrics.values() if item.received_at]
    statuses = {item.status for item in metrics.values()}
    return OptionMarketObservation(
        exchange=instrument.exchange,
        source=instrument.source,
        symbol=instrument.symbol,
        underlying=instrument.underlying,
        observation_kind=ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT,
        metrics=metrics,
        exchange_timestamp=max(source_times) if source_times else None,
        fetched_at=None,
        received_at=max(receive_times) if receive_times else None,
        processed_at=processed_at,
        status=DataStatus.AVAILABLE if statuses == {DataStatus.AVAILABLE} else DataStatus.PARTIAL,
        price_index=instrument.price_index,
        quote_currency=instrument.quote_currency,
    )


def _notification(name, channel, symbol, *, timestamp_ms=None):
    payload = _fixture(name)
    payload["params"]["channel"] = channel.format(symbol=symbol)
    data = payload["params"]["data"]
    rows = data if isinstance(data, list) else (data,)
    for row in rows:
        row["instrument_name"] = symbol
    if timestamp_ms is not None:
        for row in rows:
            row["timestamp"] = timestamp_ms
    return payload


def _replay_once(repository, settings):
    replay, index_names, catalog = _catalog_and_indexes(settings)
    assert derive_markprice_channels(catalog, index_names, max_channels=settings.max_markprice_channels_total) == (
        "markprice.options.btc_usd", "markprice.options.eth_usd",
    )
    repository.upsert_instruments(catalog)

    summary_by_currency = _summary_cycles(replay, catalog, settings)
    for observations in summary_by_currency.values():
        repository.persist_full_chain_cycle(
            observations, max_records=settings.max_full_chain_records_per_underlying
        )
    prices = {
        currency: next(item.metrics["underlying_price"] for item in observations)
        for currency, observations in summary_by_currency.items()
    }
    selection = select_ticker_universe(catalog, prices, CONTEXT_TIME, settings)
    selected = tuple(
        item
        for currency in ("BTC", "ETH")
        for item in selection.selected[currency]
    )
    assert len(selected) == 12
    for currency in ("BTC", "ETH"):
        by_expiry = {}
        for item in selection.selected[currency]:
            by_expiry.setdefault(item.expires_at, set()).add(item.option_type)
        assert len(by_expiry) == 2
        assert all({OptionType.CALL, OptionType.PUT}.issubset(sides) for sides in by_expiry.values())

    catalog_by_symbol = {item.symbol: item for item in catalog}
    mark_state = {}
    seed_observations = []
    for currency, index_name in (("BTC", "btc_usd"), ("ETH", "eth_usd")):
        payload = {
            "jsonrpc": "2.0",
            "method": "subscription",
            "params": {"channel": f"markprice.options.{index_name}", "data": replay["markprice_seed"][currency]},
        }
        rows = parse_markprice_notification(
            payload,
            instrument_catalog=catalog_by_symbol,
            received_at=WS_SEED_RECEIVE,
            processed_at=WS_SEED_RECEIVE + timedelta(seconds=1),
            is_seed=True,
        )
        mark_state[index_name] = merge_markprice_state({}, rows, is_seed=True)
        seed_observations.extend(rows)
    repository.persist_observations(tuple(seed_observations))

    mark_symbol = "BTC-30OCT26-100000-C"
    mark_update = parse_markprice_notification(
        _notification("ws_markprice_change.json", "markprice.options.btc_usd", mark_symbol),
        instrument_catalog=catalog_by_symbol,
        received_at=CONTEXT_TIME - timedelta(minutes=2),
        processed_at=CONTEXT_TIME - timedelta(minutes=2) + timedelta(seconds=1),
        is_seed=False,
    )
    mark_state["btc_usd"] = merge_markprice_state(mark_state["btc_usd"], mark_update, is_seed=False)
    mark_state["btc_usd"] = merge_markprice_state(mark_state["btc_usd"], mark_update, is_seed=False)
    older_mark_update = parse_markprice_notification(
        _notification("ws_markprice_out_of_order.json", "markprice.options.btc_usd", mark_symbol),
        instrument_catalog=catalog_by_symbol,
        received_at=CONTEXT_TIME - timedelta(minutes=1),
        processed_at=CONTEXT_TIME - timedelta(minutes=1) + timedelta(seconds=1),
        is_seed=False,
    )
    mark_state["btc_usd"] = merge_markprice_state(mark_state["btc_usd"], older_mark_update, is_seed=False)
    assert mark_state["btc_usd"][mark_symbol].metrics["mark_price"].value == Decimal("0.005112345678901234")
    assert mark_state["btc_usd"][mark_symbol].metrics["iv"].value == Decimal("57.1234")

    ticker_state = {}
    ticker_initial_observations = []
    for instrument in selected:
        source_time = T0 + timedelta(minutes=13)
        payload = _notification(
            "ws_ticker_snapshot.json",
            "incremental_ticker.{symbol}",
            instrument.symbol,
            timestamp_ms=int(source_time.timestamp() * 1000),
        )
        payload["params"]["data"]["underlying_price"] = prices[instrument.underlying].value
        update = parse_incremental_ticker_notification(
            payload, received_at=source_time + timedelta(seconds=1), processed_at=source_time + timedelta(seconds=2)
        )
        ticker_state[instrument.symbol] = apply_incremental_ticker({}, update)
        ticker_initial_observations.append(_ticker_observation(
            instrument, ticker_state[instrument.symbol], processed_at=source_time + timedelta(seconds=2)
        ))
    repository.persist_observations(tuple(ticker_initial_observations))

    target = next(item for item in selected if item.underlying == "BTC" and item.option_type is OptionType.CALL)
    target_time = T0 + timedelta(minutes=14)
    ticker_change = parse_incremental_ticker_notification(
        _notification(
            "ws_ticker_change.json", "incremental_ticker.{symbol}", target.symbol,
            timestamp_ms=int(target_time.timestamp() * 1000),
        ),
        received_at=target_time + timedelta(seconds=1),
        processed_at=target_time + timedelta(seconds=2),
    )
    ticker_state[target.symbol] = apply_incremental_ticker(ticker_state[target.symbol], ticker_change)
    ticker_state[target.symbol] = apply_incremental_ticker(ticker_state[target.symbol], ticker_change)
    older_time = T0 + timedelta(minutes=13, seconds=30)
    older_ticker_change = parse_incremental_ticker_notification(
        _notification(
            "ws_ticker_out_of_order.json", "incremental_ticker.{symbol}", target.symbol,
            timestamp_ms=int(older_time.timestamp() * 1000),
        ),
        received_at=target_time + timedelta(seconds=3),
        processed_at=target_time + timedelta(seconds=4),
    )
    ticker_state[target.symbol] = apply_incremental_ticker(ticker_state[target.symbol], older_ticker_change)
    merged = ticker_state[target.symbol]
    assert merged["mark_iv"].value == Decimal("57.1234")
    assert merged["delta"].value == Decimal("0.502")
    assert merged["bid_iv"].value is None
    assert merged["bid_iv"].status is DataStatus.NOT_AVAILABLE
    assert merged["bid_iv"].exchange_timestamp == target_time

    ticker_snapshots = tuple(
        _ticker_observation(
            catalog_by_symbol[symbol], metrics,
            processed_at=max((item.processed_at for item in metrics.values() if item.processed_at), default=CONTEXT_TIME),
        )
        for symbol, metrics in sorted(ticker_state.items())
    )
    repository.persist_observations(ticker_snapshots)

    creation_payload = _fixture("ws_instrument_creation.json")
    lifecycle_symbol = "BTC-30OCT26-120000-C"
    creation_payload["params"]["data"]["instrument_name"] = lifecycle_symbol
    creation_payload["params"]["data"]["instrument_id"] = 8100299
    creation_payload["params"]["data"]["strike"] = Decimal("120000")
    created_instrument, created_event = parse_instrument_creation_notification(
        creation_payload,
        supported_index_names=index_names,
        received_at=CONTEXT_TIME,
        processed_at=CONTEXT_TIME + timedelta(seconds=1),
    )
    repository.upsert_instruments((created_instrument,))
    repository.append_instrument_events((created_event,))
    state_payload = _fixture("ws_instrument_state.json")
    state_payload["params"]["data"]["instrument_name"] = lifecycle_symbol
    lifecycle_event = parse_instrument_state_notification(
        state_payload, received_at=CONTEXT_TIME + timedelta(seconds=2),
        processed_at=CONTEXT_TIME + timedelta(seconds=3),
    )
    updated_lifecycle_instrument = replace(
        created_instrument,
        instrument_state=lifecycle_event.instrument_state,
        is_active=lifecycle_event.instrument_state == "open",
        exchange_timestamp=lifecycle_event.exchange_timestamp,
        fetched_at=lifecycle_event.received_at,
        processed_at=lifecycle_event.processed_at,
    )
    repository.upsert_instruments((updated_lifecycle_instrument,))
    repository.append_instrument_events((lifecycle_event,))

    mark_snapshots = tuple(
        replace(
            observation,
            observation_kind=ObservationKind.WS_MARKPRICE_SNAPSHOT,
            processed_at=CONTEXT_TIME,
        )
        for index in sorted(mark_state)
        for observation in mark_state[index].values()
    )
    repository.persist_observations(mark_snapshots)

    contexts = {}
    context_observations = (
        tuple(item for group in summary_by_currency.values() for item in group)
        + mark_snapshots
        + ticker_snapshots
    )
    for currency in ("BTC", "ETH"):
        context_instruments = tuple(
            item for item in catalog if item.underlying == currency and item.is_active
        )
        currency_tickers = tuple(item.symbol for item in selected if item.underlying == currency)
        snapshot = calculate_options_context(
            OptionsContextInputs(
                underlying=currency,
                instruments=context_instruments,
                observations=tuple(item for item in context_observations if item.underlying == currency),
                ticker_universe_symbols=currency_tickers,
            ),
            as_of=CONTEXT_TIME,
            settings=settings,
            processed_at=CONTEXT_TIME,
        )
        repository.persist_context(snapshot)
        contexts[currency] = snapshot
    return {
        "catalog": catalog,
        "selected": tuple(item.symbol for item in selected),
        "summary_by_currency": summary_by_currency,
        "mark_state": mark_state,
        "ticker_state": ticker_state,
        "contexts": contexts,
        "lifecycle_symbol": lifecycle_symbol,
        "target_ticker_symbol": target.symbol,
    }


def _persisted_counts_and_digest(connection):
    queries = {
        "instruments": "SELECT exchange, symbol, underlying, option_type, strike::text, expires_at, instrument_state, is_active, source_fields::text FROM phase8_option_instruments ORDER BY exchange, symbol",
        "events": "SELECT exchange, symbol, event_type, event_identity, instrument_state, exchange_timestamp, received_at FROM phase8_option_instrument_events ORDER BY exchange, event_identity",
        "observations": "SELECT exchange, symbol, observation_kind, exchange_timestamp, fetched_at, received_at, metrics::text, field_metadata::text, payload_hash FROM phase8_option_market_snapshots ORDER BY exchange, symbol, observation_kind, exchange_timestamp NULLS FIRST, payload_hash",
        "contexts": "SELECT underlying, context_timestamp, calculation_version, metrics::text, status, context_only FROM phase8_option_context_snapshots ORDER BY underlying, context_timestamp, calculation_version",
    }
    rows = {name: connection.execute(statement).fetchall() for name, statement in queries.items()}
    counts = {name: len(values) for name, values in rows.items()}
    serialized = json.dumps(rows, default=str, sort_keys=True, separators=(",", ":"))
    return counts, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def test_deterministic_replay_persists_source_age_sparse_state_and_idempotent_rows(replay_postgres):
    connection, repository = replay_postgres
    settings = _settings()
    first = _replay_once(repository, settings)

    assert len(first["selected"]) == 12
    assert first["contexts"]["BTC"].metrics["atm_iv"].status is DataStatus.NOT_AVAILABLE
    assert first["contexts"]["BTC"].metrics["atm_iv"].quality_reason == "UNIT_UNVERIFIED"
    assert first["contexts"]["ETH"].metrics["put_call_oi_ratio"].data_age_seconds == 600
    assert first["contexts"]["BTC"].metrics["put_call_volume_ratio"].status is DataStatus.NOT_AVAILABLE
    assert first["ticker_state"][first["target_ticker_symbol"]]["gamma"].provenance is Provenance.SOURCE_PROVIDED
    assert len(first["mark_state"]["btc_usd"]) == 8
    assert len(first["mark_state"]["eth_usd"]) == 8

    missing_volume = connection.execute(
        """SELECT metrics ->> 'volume_24h', field_metadata -> 'volume_24h' ->> 'status'
           FROM phase8_option_market_snapshots
           WHERE symbol = 'BTC-27NOV26-100000-P' AND observation_kind = 'REST_CHAIN_SUMMARY'"""
    ).fetchone()
    assert missing_volume == (None, "NOT_AVAILABLE")
    assert connection.execute(
        "SELECT count(*) FROM phase8_option_instrument_events WHERE symbol = %s",
        (first["lifecycle_symbol"],),
    ).fetchone()[0] == 2
    assert connection.execute(
        """SELECT field_metadata -> 'bid_iv' ->> 'status', metrics ->> 'bid_iv'
           FROM phase8_option_market_snapshots
           WHERE symbol = %s AND observation_kind = 'WS_INCREMENTAL_TICKER_SNAPSHOT'
           ORDER BY exchange_timestamp DESC LIMIT 1""",
        (first["target_ticker_symbol"],),
    ).fetchone() == ("NOT_AVAILABLE", None)

    counts_after_first, digest_after_first = _persisted_counts_and_digest(connection)
    second = _replay_once(repository, settings)
    counts_after_second, digest_after_second = _persisted_counts_and_digest(connection)

    assert second["selected"] == first["selected"]
    assert counts_after_second == counts_after_first
    assert digest_after_second == digest_after_first
