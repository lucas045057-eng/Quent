from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib

import pytest

from quant_phase8 import contracts
from quant_phase8.config import Phase8Settings


T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
PRICE = Decimal("100000")

try:
    universe = importlib.import_module("quant_phase8.universe")
except ModuleNotFoundError:
    universe = None


def _api(name):
    function = getattr(universe, name, None) if universe is not None else None
    assert callable(function), f"Phase 8 universe must expose {name}"
    return function


def _instrument(
    underlying: str,
    expiry_days: int,
    side: str,
    strike: int,
    *,
    index: int = 0,
    state: str = "open",
    active: bool = True,
    status: contracts.DataStatus = contracts.DataStatus.AVAILABLE,
):
    prefix = "BTC" if underlying == "BTC" else "ETH"
    quote = "BTC" if underlying == "BTC" else "ETH"
    expires = T0 + timedelta(days=expiry_days)
    symbol = f"{prefix}-{expires.strftime('%d%b%y').upper()}-{strike}-{side[0].upper()}-{index}"
    return contracts.OptionInstrument(
        exchange="DERIBIT",
        source="deribit",
        symbol=symbol,
        underlying=underlying,
        option_type=side,
        strike=Decimal(str(strike)),
        expires_at=expires,
        instrument_created_at=T0 - timedelta(days=120),
        instrument_state=state,
        is_active=active,
        price_index="btc_usd" if underlying == "BTC" else "eth_usd",
        base_currency=quote,
        quote_currency=quote,
        settlement_currency=quote,
        exchange_timestamp=T0,
        fetched_at=T0,
        processed_at=T0,
        status=status,
        timestamp_semantics=contracts.TimestampSemantics.VERIFIED,
    )


def _measured_shape(*, underlying="BTC", expiries=(7, 14, 30, 45, 60, 75, 89)):
    """Synthetic chain with measured-style expiry buckets and strike ladders."""
    strikes = range(80_000, 120_001, 2_000) if underlying == "BTC" else range(3_200, 4_801, 80)
    rows = []
    for expiry in expiries:
        for strike in strikes:
            for side in ("call", "put"):
                rows.append(_instrument(underlying, expiry, side, strike))
    return rows


def _price(value=PRICE, *, timestamp=T0, status=contracts.DataStatus.AVAILABLE):
    return contracts.OptionMetricValue(
        metric="underlying_price",
        value=value,
        source="deribit",
        exchange="DERIBIT",
        source_field="index_price",
        source_method_or_channel="public/get_index_price",
        exchange_timestamp=timestamp,
        field_last_updated_at=timestamp,
        received_at=timestamp,
        processed_at=timestamp,
        timestamp_semantics=contracts.TimestampSemantics.VERIFIED,
        unit_code="USD",
        unit_status=contracts.UnitStatus.VERIFIED,
        status=status,
        provenance=contracts.Provenance.SOURCE_PROVIDED,
    )


def _summary_price(value=PRICE, *, fetched_at=T0):
    return contracts.OptionMetricValue(
        metric="underlying_price",
        value=value,
        source="deribit",
        exchange="DERIBIT",
        source_field="underlying_price",
        source_method_or_channel="public/get_book_summary_by_currency",
        exchange_timestamp=None,
        field_last_updated_at=None,
        fetched_at=fetched_at,
        timestamp_semantics=contracts.TimestampSemantics.NOT_PROVIDED,
        unit_status=contracts.UnitStatus.SOURCE_NATIVE_UNVERIFIED,
        status=contracts.DataStatus.AVAILABLE,
        provenance=contracts.Provenance.SOURCE_PROVIDED,
    )


def test_selector_is_deterministic_stratified_and_respects_v1_bounds():
    rows = _measured_shape()
    prices = {"BTC": _price(), "ETH": _price(Decimal("4000"))}
    rows.extend(_measured_shape(underlying="ETH"))
    settings = Phase8Settings.from_env({})

    first = _api("select_ticker_universe")(rows, prices, T0, settings)
    second = _api("select_ticker_universe")(list(reversed(rows)), prices, T0, settings)

    assert first["BTC"]
    assert len(first["BTC"]) <= 64
    assert len(first["ETH"]) <= 64
    assert len(first["BTC"]) + len(first["ETH"]) <= 128
    assert tuple(item.symbol for item in first["BTC"]) == tuple(item.symbol for item in second["BTC"])
    assert tuple(item.symbol for item in first["ETH"]) == tuple(item.symbol for item in second["ETH"])
    assert first.coverage["BTC"].eligible_count == 7 * 5 * 2
    assert first.coverage["BTC"].selected_count == len(first["BTC"])
    assert first.coverage["BTC"].status is contracts.DataStatus.PARTIAL

    by_expiry = {}
    for item in first["BTC"]:
        by_expiry.setdefault(item.expires_at, set()).add(item.option_type)
    assert len(by_expiry) == 7
    assert all({contracts.OptionType.CALL, contracts.OptionType.PUT} <= sides for sides in by_expiry.values())

    # Each eligible expiry receives its ATM pair before any expiry receives wings.
    assert all(
        first.reasons["BTC"][symbol] in {"ATM_CALL", "ATM_PUT"}
        for symbol in [item.symbol for item in first["BTC"][:14]]
    )

    tighter_total = _api("select_ticker_universe")(
        rows,
        prices,
        T0,
        Phase8Settings.from_env({"PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "64"}),
    )
    assert len(tighter_total["BTC"]) + len(tighter_total["ETH"]) == 64
    assert abs(len(tighter_total["BTC"]) - len(tighter_total["ETH"])) <= 1


def test_eligibility_enforces_expiry_moneyness_and_lifecycle_without_zero_fill():
    rows = [
        _instrument("BTC", 30, "call", 100_000),
        _instrument("BTC", 30, "put", 100_000),
        _instrument("BTC", 91, "call", 100_000),
        _instrument("BTC", 30, "call", 106_000),
        _instrument("BTC", 30, "put", 94_000),
        _instrument("BTC", 30, "call", 101_000, state="locked"),
        _instrument("BTC", 30, "put", 99_000, active=False),
        _instrument("BTC", 30, "call", 99_000, status=contracts.DataStatus.STALE),
    ]
    result = _api("select_ticker_universe")(
        rows, {"BTC": _price()}, T0, Phase8Settings.from_env({})
    )

    assert [item.symbol for item in result["BTC"]] == [rows[0].symbol, rows[1].symbol]
    assert result.coverage["BTC"].eligible_count == 2
    assert result.coverage["BTC"].status is contracts.DataStatus.AVAILABLE
    assert all(item.status is contracts.DataStatus.AVAILABLE for item in result["BTC"])


def test_summary_underlying_price_uses_fetched_at_not_invented_exchange_time():
    rows = _measured_shape(expiries=(7,))
    settings = Phase8Settings.from_env({})
    fresh = _api("select_ticker_universe")(
        rows,
        {"BTC": _summary_price(fetched_at=T0 - timedelta(seconds=3600))},
        T0,
        settings,
    )
    stale = _api("select_ticker_universe")(
        rows,
        {"BTC": _summary_price(fetched_at=T0 - timedelta(
            seconds=settings.chain_stale_after_seconds + 1
        ))},
        T0,
        settings,
    )

    assert fresh["BTC"]
    assert fresh.coverage["BTC"].price_time_basis == "FETCHED_AT"
    assert fresh.coverage["BTC"].price_data_age_seconds == 3600
    assert stale["BTC"] == ()
    assert stale.coverage["BTC"].status is contracts.DataStatus.STALE
    assert stale.coverage["BTC"].price_time_basis == "FETCHED_AT"


def test_stale_or_missing_price_never_creates_a_new_selection_and_can_only_retain_valid_prior_set():
    rows = _measured_shape(expiries=(7, 30))
    settings = Phase8Settings.from_env({})
    fresh = _api("select_ticker_universe")(rows, {"BTC": _price()}, T0, settings)
    prior = {"BTC": tuple(item.symbol for item in fresh["BTC"][:2])}
    stale_price = _price(timestamp=T0 - timedelta(seconds=settings.markprice_stale_after_seconds + 1))

    retained = _api("select_ticker_universe")(
        rows, {"BTC": stale_price}, T0, settings, previous_selection=prior
    )
    unavailable = _api("select_ticker_universe")(
        rows, {}, T0, settings
    )
    invalid_instruments = [
        _instrument("BTC", 30, "call", 100_000, state="locked"),
        _instrument("BTC", 30, "put", 100_000),
    ]
    invalid_prior = _api("select_ticker_universe")(
        invalid_instruments,
        {}, T0, settings,
        previous_selection={"BTC": (invalid_instruments[0].symbol, invalid_instruments[1].symbol)},
    )

    assert tuple(item.symbol for item in retained["BTC"]) == prior["BTC"]
    assert retained.coverage["BTC"].status is contracts.DataStatus.STALE
    assert retained.coverage["BTC"].reason == "UNDERLYING_PRICE_STALE_RETAINED_PRIOR_SELECTION"
    assert unavailable["BTC"] == ()
    assert unavailable.coverage["BTC"].status is contracts.DataStatus.NOT_AVAILABLE
    assert invalid_prior["BTC"] == ()
    assert invalid_prior.coverage["BTC"].status is contracts.DataStatus.NOT_AVAILABLE


def test_atm_reservations_over_per_underlying_cap_return_partial_without_selection():
    rows = _measured_shape(expiries=(7, 30))
    settings = Phase8Settings.from_env({
        "PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "2",
        "PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "2",
    })

    result = _api("select_ticker_universe")(
        rows, {"BTC": _price()}, T0, settings
    )

    assert result["BTC"] == ()
    assert result.coverage["BTC"].status is contracts.DataStatus.PARTIAL
    assert result.coverage["BTC"].reason == "ATM_RESERVATIONS_EXCEED_PER_UNDERLYING_CAP"


def test_atm_reservations_over_global_cap_fail_closed_for_both_underlyings():
    rows = _measured_shape(expiries=(7,)) + _measured_shape(underlying="ETH", expiries=(7,))
    settings = Phase8Settings.from_env({
        "PHASE8_OPTIONS_MAX_TICKER_INSTRUMENTS_PER_CURRENCY": "2",
        "PHASE8_OPTIONS_MAX_TICKER_SUBSCRIPTIONS_TOTAL": "2",
    })

    result = _api("select_ticker_universe")(
        rows,
        {"BTC": _price(), "ETH": _price(Decimal("4000"))},
        T0,
        settings,
    )

    assert result["BTC"] == ()
    assert result["ETH"] == ()
    assert result.coverage["BTC"].reason == "ATM_RESERVATIONS_EXCEED_TOTAL_CAP"
    assert result.coverage["ETH"].reason == "ATM_RESERVATIONS_EXCEED_TOTAL_CAP"


@pytest.mark.parametrize(
    ("rows", "overrides", "reason"),
    [
        (_measured_shape(expiries=(7,)), {"PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "2"}, "PER_UNDERLYING_CAP"),
        (
            _measured_shape(underlying="BTC", expiries=(7,))[:4]
            + _measured_shape(underlying="ETH", expiries=(7,))[:4],
            {"PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "7", "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "7"},
            "TOTAL_CAP",
        ),
    ],
)
def test_full_chain_overflow_rejects_the_whole_cycle_without_truncation(rows, overrides, reason):
    env = {
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "2048",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "4096",
        **overrides,
    }
    validation = _api("validate_full_chain_snapshot")(rows, Phase8Settings.from_env(env))

    assert validation.status is contracts.DataStatus.PARTIAL
    assert validation.records == ()
    assert validation.reason == reason
    assert validation.rejected_count == len(rows)
