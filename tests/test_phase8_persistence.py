from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import os
from uuid import uuid4

import pytest

from quant_phase1.db import apply_migrations
from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    InstrumentEventType,
    ObservationKind,
    OptionContextMetric,
    OptionContextSnapshot,
    OptionInstrument,
    OptionInstrumentEvent,
    OptionMarketObservation,
    OptionMetricValue,
    OptionType,
    Provenance,
    TimestampSemantics,
    UnitStatus,
)
from quant_phase8.persistence import Phase8Repository


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 12, 25, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def phase8_postgres():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured for isolated Phase 8 persistence tests")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "Phase 8 tests require a loopback PostgreSQL host"
    assert info.get("dbname") == "quant_phase8_test", "Phase 8 tests require the disposable quant_phase8_test database"
    schema = f"phase8_persistence_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection = psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public")
    try:
        apply_migrations(connection)
        yield Phase8Repository(connection), connection
    finally:
        connection.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _instrument(symbol: str, *, underlying: str = "BTC", active: bool = True, state: str = "open") -> OptionInstrument:
    return OptionInstrument(
        exchange="DERIBIT", source="deribit", symbol=symbol, underlying=underlying,
        option_type=OptionType.CALL, strike=Decimal("100000.00"), expires_at=EXPIRY,
        instrument_created_at=NOW - timedelta(days=2), instrument_state=state, is_active=active,
        price_index=f"{underlying.lower()}_usd", base_currency=underlying,
        quote_currency="USD", settlement_currency=underlying,
        exchange_timestamp=None, fetched_at=NOW, processed_at=NOW,
        provider_instrument_id=123, timestamp_semantics=TimestampSemantics.NOT_PROVIDED,
        raw_reference="sha256:instrument-fixture",
    )


def _observation(
    symbol: str,
    kind: ObservationKind,
    *,
    captured_at: datetime = NOW,
    exact_value: Decimal = Decimal("123.450000000000000001"),
    source_timestamp: datetime | None = NOW,
) -> OptionMarketObservation:
    is_rest = kind is ObservationKind.REST_CHAIN_SUMMARY
    received_at = None if is_rest else captured_at
    fetched_at = captured_at if is_rest else None
    semantics = TimestampSemantics.NOT_PROVIDED if is_rest else TimestampSemantics.VERIFIED
    capture_fields = {"fetched_at": fetched_at, "received_at": received_at}
    metric_source_timestamp = None if is_rest else source_timestamp
    common_metric = dict(
        source="deribit", exchange="DERIBIT", processed_at=captured_at,
        exchange_timestamp=metric_source_timestamp,
        field_last_updated_at=None if is_rest else source_timestamp,
        timestamp_semantics=semantics, **capture_fields,
    )
    metrics = {
        "exact_value": OptionMetricValue(
            metric="exact_value", value=exact_value, source_field="data.mark_price",
            source_method_or_channel="ticker", unit_code="BTC", unit_status=UnitStatus.VERIFIED,
            status=DataStatus.AVAILABLE, provenance=Provenance.SOURCE_PROVIDED,
            raw_reference="sha256:metric-fixture", **common_metric,
        ),
        "mark_iv": OptionMetricValue(
            metric="mark_iv", value=None, source_field="data.mark_iv",
            source_method_or_channel="ticker", unit_status=UnitStatus.SOURCE_NATIVE_UNVERIFIED,
            status=DataStatus.NOT_AVAILABLE, provenance=Provenance.SOURCE_PROVIDED,
            quality_reason="SOURCE_NULL", raw_reference="sha256:metric-fixture", **common_metric,
        ),
    }
    return OptionMarketObservation(
        exchange="DERIBIT", source="deribit", symbol=symbol, underlying="BTC",
        observation_kind=kind, metrics=metrics,
        exchange_timestamp=None if is_rest else source_timestamp,
        fetched_at=fetched_at, received_at=received_at, processed_at=captured_at,
        status=DataStatus.AVAILABLE, price_index="btc_usd", underlying_index="index_price",
        quote_currency="USD", raw_reference="sha256:observation-fixture",
    )


def _event(
    symbol: str,
    identity: str | None,
    *,
    received_at: datetime = NOW,
    source_timestamp: datetime | None = NOW,
) -> OptionInstrumentEvent:
    return OptionInstrumentEvent(
        exchange="DERIBIT", source="deribit", symbol=symbol,
        event_type=InstrumentEventType.STATE, instrument_state="open",
        exchange_timestamp=source_timestamp, received_at=received_at, processed_at=received_at,
        status=DataStatus.AVAILABLE, raw_reference="sha256:event-fixture", event_identity=identity,
    )


def _context(timestamp: datetime = NOW, value: Decimal = Decimal("1.250000000000000001")) -> OptionContextSnapshot:
    metric = OptionContextMetric(
        metric="put_call_oi_ratio", value=value, source_timestamps=(timestamp - timedelta(minutes=2),),
        source_fetched_at=timestamp - timedelta(minutes=2), source_received_at=None,
        data_age_seconds=120, coverage_expected=100, coverage_available=98,
        status=DataStatus.AVAILABLE, quality_reason=None, provenance=Provenance.COMPUTED,
        unit_status=UnitStatus.VERIFIED, input_observation_ids=("observation-1",),
    )
    return OptionContextSnapshot(
        underlying="BTC", context_timestamp=timestamp, processed_at=timestamp,
        calculation_version="phase8.v1", metrics={metric.metric: metric},
    )


def test_instrument_upsert_and_reconciliation_soft_retire_without_deleting(phase8_postgres):
    repository, connection = phase8_postgres
    initial = _instrument("BTC-26DEC26-100000-C")
    assert repository.upsert_instruments((initial, _instrument("BTC-26DEC26-110000-C"))) == 2
    changed = replace(initial, strike=Decimal("105000.00"))
    assert repository.upsert_instruments((changed,)) == 1
    assert repository.retire_missing_instruments(
        exchange="DERIBIT", underlying="BTC", active_symbols=(changed.symbol,), processed_at=NOW + timedelta(seconds=1)
    ) == 1
    rows = connection.execute(
        "SELECT symbol, strike, is_active FROM phase8_option_instruments ORDER BY symbol"
    ).fetchall()
    assert rows == [
        ("BTC-26DEC26-100000-C", Decimal("105000.00"), True),
        ("BTC-26DEC26-110000-C", Decimal("100000.00"), False),
    ]


def test_empty_instrument_reconciliation_cannot_soft_retire_the_catalog(phase8_postgres):
    repository, connection = phase8_postgres
    instrument = _instrument("BTC-26DEC26-100000-C")
    repository.upsert_instruments((instrument,))
    with pytest.raises(ValueError, match="empty reconciliation"):
        repository.retire_missing_instruments(
            exchange="DERIBIT", underlying="BTC", active_symbols=(), processed_at=NOW
        )
    assert connection.execute(
        "SELECT is_active FROM phase8_option_instruments WHERE symbol = %s", (instrument.symbol,)
    ).fetchone() == (True,)


def test_lifecycle_event_insert_is_append_only_and_idempotent(phase8_postgres):
    repository, connection = phase8_postgres
    event = _event("BTC-26DEC26-100000-C", "event-identity-1")
    assert repository.append_instrument_events((event,)) == 1
    assert repository.append_instrument_events((event,)) == 0
    assert connection.execute(
        "SELECT count(*), min(timestamp_semantics) FROM phase8_option_instrument_events"
    ).fetchone() == (1, "UNVERIFIED")


def test_lifecycle_fallback_identity_does_not_depend_on_receive_time(phase8_postgres):
    repository, connection = phase8_postgres
    original = _event("BTC-26DEC26-100000-C", None)
    redelivery = _event(
        "BTC-26DEC26-100000-C", None, received_at=NOW + timedelta(seconds=5), source_timestamp=NOW
    )
    assert repository.append_instrument_events((original,)) == 1
    assert repository.append_instrument_events((redelivery,)) == 0
    assert connection.execute("SELECT count(*) FROM phase8_option_instrument_events").fetchone()[0] == 1


@pytest.mark.parametrize(
    "kind",
    [
        ObservationKind.REST_CHAIN_SUMMARY,
        ObservationKind.WS_MARKPRICE_SNAPSHOT,
        ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT,
    ],
)
def test_observation_dedup_preserves_decimal_null_and_field_provenance(phase8_postgres, kind):
    repository, connection = phase8_postgres
    observation = _observation("BTC-26DEC26-100000-C", kind)
    assert repository.persist_observations((observation,)) == 1
    assert repository.persist_observations((observation,)) == 0
    row = connection.execute(
        """SELECT metrics ->> 'exact_value', metrics -> 'mark_iv',
                  field_metadata -> 'mark_iv' ->> 'status',
                  field_metadata -> 'mark_iv' ->> 'unit_status',
                  field_metadata -> 'mark_iv' ->> 'provenance',
                  field_metadata -> 'mark_iv' ->> 'field_last_updated_at',
                  field_metadata -> 'mark_iv' ->> 'exchange_timestamp',
                  field_metadata -> 'mark_iv' ->> 'fetched_at',
                  field_metadata -> 'mark_iv' ->> 'received_at',
                  field_metadata -> 'mark_iv' ->> 'source_field',
                  field_metadata -> 'mark_iv' ->> 'raw_reference',
                  exchange_timestamp, fetched_at, received_at, processed_at
           FROM phase8_option_market_snapshots"""
    ).fetchone()
    assert Decimal(row[0]) == Decimal("123.450000000000000001")
    assert row[1] is None
    assert row[2:5] == ("NOT_AVAILABLE", "SOURCE_NATIVE_UNVERIFIED", "SOURCE_PROVIDED")
    assert row[9:11] == ("data.mark_iv", "sha256:metric-fixture")
    if kind is ObservationKind.REST_CHAIN_SUMMARY:
        assert row[5:9] == (None, None, NOW.isoformat(), None)
        assert row[11:] == (None, NOW, None, NOW)
    else:
        assert row[5:9] == (NOW.isoformat(), NOW.isoformat(), None, NOW.isoformat())
        assert row[11:] == (NOW, None, NOW, NOW)


def test_ticker_observation_persistence_enforces_configured_universe_caps(phase8_postgres):
    _, connection = phase8_postgres
    repository = Phase8Repository(connection, settings=Phase8Settings.from_env({
        "PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "2",
        "PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "4",
    }))
    rows = tuple(
        _observation(f"BTC-ticker-{index}", ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT)
        for index in range(3)
    )
    with pytest.raises(ValueError, match="per-underlying ticker persistence cap"):
        repository.persist_observations(rows)
    assert connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0] == 0


def test_full_chain_writes_enforce_per_underlying_cap_before_any_insert(phase8_postgres):
    _, connection = phase8_postgres
    settings = Phase8Settings.from_env({
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "1",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "2",
    })
    repository = Phase8Repository(connection, settings=settings)
    instruments = (
        _instrument("BTC-26DEC26-100000-C"),
        _instrument("BTC-26DEC26-110000-C"),
    )
    with pytest.raises(ValueError, match="per-underlying full-chain cap"):
        repository.upsert_instruments(instruments)
    assert connection.execute("SELECT count(*) FROM phase8_option_instruments").fetchone()[0] == 0

    observations = (
        _observation("BTC-26DEC26-100000-C", ObservationKind.REST_CHAIN_SUMMARY),
        _observation("BTC-26DEC26-110000-C", ObservationKind.REST_CHAIN_SUMMARY),
    )
    with pytest.raises(ValueError, match="per-underlying full-chain cap"):
        repository.persist_full_chain_cycle(observations, max_records=2)
    assert connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0] == 0


def test_full_chain_cap_and_database_failure_leave_no_partial_observation_rows(phase8_postgres):
    repository, connection = phase8_postgres
    first = _observation("BTC-26DEC26-100000-C", ObservationKind.REST_CHAIN_SUMMARY)
    second = _observation("BTC-26DEC26-110000-C", ObservationKind.REST_CHAIN_SUMMARY)
    with pytest.raises(ValueError, match="full-chain cycle exceeds"):
        repository.persist_full_chain_cycle((first, second), max_records=1)
    assert connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0] == 0

    oversized_numeric = _observation(
        "BTC-26DEC26-110000-C", ObservationKind.REST_CHAIN_SUMMARY,
        exact_value=Decimal("1e+131073"),
    )
    import psycopg
    with pytest.raises(psycopg.DataError):
        repository.persist_full_chain_cycle((first, oversized_numeric), max_records=2)
    assert connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0] == 0


def test_context_replay_key_is_immutable(phase8_postgres):
    repository, connection = phase8_postgres
    original = _context()
    conflicting_replay = _context(value=Decimal("9.99"))
    assert repository.persist_context(original) is True
    assert repository.persist_context(conflicting_replay) is False
    stored = connection.execute(
        "SELECT metrics -> 'put_call_oi_ratio' ->> 'value', input_observation_ids"
        " FROM phase8_option_context_snapshots"
    ).fetchone()
    assert Decimal(stored[0]) == Decimal("1.250000000000000001")
    assert stored[1] == ["observation-1"]


def test_retention_is_opt_in_bounded_and_scoped_to_phase8_tables(phase8_postgres):
    repository, connection = phase8_postgres
    old = NOW - timedelta(days=200)
    connection.execute("CREATE TABLE non_phase8_retention_guard (id INTEGER PRIMARY KEY, happened_at TIMESTAMPTZ NOT NULL)")
    connection.execute("INSERT INTO non_phase8_retention_guard VALUES (1, %s)", (old,))
    old_observations = tuple(
        _observation(f"BTC-old-{index}", ObservationKind.REST_CHAIN_SUMMARY, captured_at=old, source_timestamp=None)
        for index in range(3)
    )
    repository.persist_observations(old_observations)
    repository.persist_observations((_observation("BTC-current", ObservationKind.REST_CHAIN_SUMMARY),))
    repository.append_instrument_events(tuple(_event(f"BTC-old-{index}", f"old-event-{index}", received_at=old) for index in range(3)))
    for index in range(3):
        repository.persist_context(_context(timestamp=old + timedelta(seconds=index)))

    disabled = Phase8Settings.from_env({})
    assert disabled.retention_enforcement is False
    assert repository.cleanup_retention(disabled, now=NOW) == {}
    assert (
        connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0],
        connection.execute("SELECT count(*) FROM phase8_option_instrument_events").fetchone()[0],
        connection.execute("SELECT count(*) FROM phase8_option_context_snapshots").fetchone()[0],
    ) == (4, 3, 3)

    enabled = Phase8Settings.from_env({
        "PHASE8_OPTIONS_RETENTION_ENFORCEMENT": "true",
        "PHASE8_OPTIONS_RETENTION_DELETE_BATCH_ROWS": "2",
    })
    deleted = repository.cleanup_retention(enabled, now=NOW, max_batches=1)
    assert deleted == {
        "phase8_option_instrument_events": 2,
        "phase8_option_market_snapshots": 2,
        "phase8_option_context_snapshots": 2,
    }
    assert connection.execute("SELECT count(*) FROM phase8_option_market_snapshots").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM phase8_option_instrument_events").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM phase8_option_context_snapshots").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM non_phase8_retention_guard").fetchone()[0] == 1
