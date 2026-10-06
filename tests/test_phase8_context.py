from datetime import datetime, timedelta, timezone
from dataclasses import replace
import inspect
from decimal import Decimal, localcontext

from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    ObservationKind,
    OptionContextMetric,
    OptionContextSnapshot,
    OptionInstrument,
    OptionMarketObservation,
    OptionMetricValue,
    OptionType,
    Provenance,
    TimestampSemantics,
    UnitStatus,
)
from quant_phase8.context import OptionsContextInputs, calculate_options_context
from quant_phase8.persistence import _json_encode


UTC = timezone.utc
AS_OF = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REST_CAPTURE = AS_OF - timedelta(minutes=60)
EXPIRIES = (AS_OF + timedelta(days=10), AS_OF + timedelta(days=30))


def _settings(**overrides) -> Phase8Settings:
    values = {
        "TRADING_MODE": "paper",
        "PHASE8_OPTIONS_MIN_CHAIN_COVERAGE": "0.50",
        "PHASE8_OPTIONS_MIN_TICKER_COVERAGE": "0.50",
        "PHASE8_OPTIONS_CONCENTRATION_TOP_N": "5",
        "PHASE8_OPTIONS_SKEW_MIN_CONTRACTS": "6",
        "PHASE8_OPTIONS_SKEW_MIN_DISTINCT_STRIKES": "3",
    }
    values.update(overrides)
    return Phase8Settings.from_env(values)


def _instrument(expiry_index: int, strike: int, side: OptionType) -> OptionInstrument:
    expiry = EXPIRIES[expiry_index]
    suffix = "C" if side is OptionType.CALL else "P"
    symbol = f"BTC-{expiry:%y%m%d}-{strike}-{suffix}"
    return OptionInstrument(
        exchange="DERIBIT", source="deribit", symbol=symbol, underlying="BTC",
        option_type=side, strike=Decimal(str(strike)), expires_at=expiry,
        instrument_created_at=None, instrument_state="open", is_active=True,
        price_index="btc_usd", base_currency="BTC", quote_currency="USD",
        settlement_currency="BTC", exchange_timestamp=None, fetched_at=AS_OF,
        processed_at=AS_OF, timestamp_semantics=TimestampSemantics.NOT_PROVIDED,
        raw_reference=f"sha256:{symbol}",
    )


def _metric(
    name: str, value: Decimal | None, *, source_field: str, source_time: datetime | None,
    capture: datetime, unit_code: str | None, unit_status: UnitStatus,
    status: DataStatus | None = None, source_channel: str = "public/get_book_summary_by_currency",
    semantics: TimestampSemantics = TimestampSemantics.NOT_PROVIDED,
    observation_kind: ObservationKind = ObservationKind.REST_CHAIN_SUMMARY,
) -> OptionMetricValue:
    is_rest = observation_kind is ObservationKind.REST_CHAIN_SUMMARY
    return OptionMetricValue(
        metric=name, value=value, source_field=source_field, source_method_or_channel=source_channel,
        exchange_timestamp=source_time, field_last_updated_at=source_time,
        fetched_at=capture if is_rest else None, received_at=None if is_rest else capture,
        processed_at=capture, timestamp_semantics=semantics, unit_code=unit_code,
        unit_status=unit_status, status=status,
        provenance=Provenance.SOURCE_PROVIDED,
        quality_reason="EXPLICIT_NULL" if value is None else None,
        raw_reference=f"sha256:{name}:{source_field}",
    )


def _chain_observation(instrument: OptionInstrument, *, oi: Decimal | None, volume: Decimal | None,
                       mark_iv: Decimal | None = None, capture: datetime = REST_CAPTURE) -> OptionMarketObservation:
    return OptionMarketObservation(
        exchange="DERIBIT", source="deribit", symbol=instrument.symbol, underlying="BTC",
        observation_kind=ObservationKind.REST_CHAIN_SUMMARY,
        metrics={
            "open_interest": _metric(
                "open_interest", oi, source_field="open_interest", source_time=None, capture=capture,
                unit_code="BTC", unit_status=UnitStatus.VERIFIED,
                status=None if oi is not None else DataStatus.NOT_AVAILABLE,
            ),
            "volume_24h": _metric(
                "volume_24h", volume, source_field="volume", source_time=None, capture=capture,
                unit_code="BTC", unit_status=UnitStatus.VERIFIED,
                status=None if volume is not None else DataStatus.NOT_AVAILABLE,
            ),
            "mark_iv": _metric(
                "mark_iv", mark_iv, source_field="mark_iv", source_time=None, capture=capture,
                unit_code="native", unit_status=UnitStatus.VERIFIED if mark_iv is not None else UnitStatus.UNKNOWN,
                status=None if mark_iv is not None else DataStatus.NOT_AVAILABLE,
            ),
        },
        exchange_timestamp=None, fetched_at=capture, received_at=None, processed_at=capture,
        status=DataStatus.AVAILABLE, price_index="btc_usd", underlying_index="index_price",
        quote_currency="USD", observation_id=f"obs:{instrument.symbol}:rest",
        raw_reference="sha256:rest-summary",
    )


def _mark_observation(instrument: OptionInstrument, value: Decimal | None, *,
                      unit_status: UnitStatus = UnitStatus.SOURCE_NATIVE_UNVERIFIED,
                      unit_code: str | None = None, timestamp: datetime = AS_OF - timedelta(seconds=30),
                      status: DataStatus | None = None,
                      kind: ObservationKind = ObservationKind.WS_MARKPRICE_SNAPSHOT) -> OptionMarketObservation:
    return OptionMarketObservation(
        exchange="DERIBIT", source="deribit", symbol=instrument.symbol, underlying="BTC",
        observation_kind=kind,
        metrics={"iv": _metric(
            "iv", value, source_field="params.data[].iv", source_time=timestamp, capture=AS_OF - timedelta(seconds=20),
            unit_code=unit_code, unit_status=unit_status,
            status=status or (DataStatus.AVAILABLE if value is not None else DataStatus.NOT_AVAILABLE),
            source_channel="markprice.options.btc_usd", semantics=TimestampSemantics.VERIFIED,
            observation_kind=kind,
        )},
        exchange_timestamp=timestamp, fetched_at=None,
        received_at=AS_OF - timedelta(seconds=20), processed_at=AS_OF - timedelta(seconds=20),
        status=DataStatus.AVAILABLE, price_index="btc_usd", quote_currency="USD",
        observation_id=f"obs:{instrument.symbol}:mark", raw_reference="sha256:markprice",
    )


def _with_verified_underlying_price(observation: OptionMarketObservation, price: Decimal = Decimal("105")) -> OptionMarketObservation:
    metrics = dict(observation.metrics)
    metrics["underlying_price"] = _metric(
        "underlying_price", price, source_field="underlying_price", source_time=None,
        capture=observation.fetched_at or AS_OF, unit_code="USD", unit_status=UnitStatus.VERIFIED,
    )
    return replace(observation, metrics=metrics)


def _ticker_delta(instrument: OptionInstrument, value: Decimal) -> OptionMarketObservation:
    kind = ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT
    metric = _metric(
        "delta", value, source_field="data.greeks.delta", source_time=AS_OF - timedelta(seconds=20),
        capture=AS_OF - timedelta(seconds=15), unit_code="fraction", unit_status=UnitStatus.VERIFIED,
        source_channel=f"incremental_ticker.{instrument.symbol}", semantics=TimestampSemantics.VERIFIED,
        observation_kind=kind,
    )
    return OptionMarketObservation(
        exchange="DERIBIT", source="deribit", symbol=instrument.symbol, underlying="BTC",
        observation_kind=kind, metrics={"delta": metric}, exchange_timestamp=metric.exchange_timestamp,
        fetched_at=None, received_at=metric.received_at, processed_at=metric.processed_at,
        status=DataStatus.AVAILABLE, observation_id=f"obs:{instrument.symbol}:ticker",
    )


def _context(instruments, observations, *, as_of=AS_OF, processed_at=None) -> OptionContextSnapshot:
    return calculate_options_context(
        OptionsContextInputs("BTC", tuple(instruments), tuple(observations)),
        as_of=as_of, settings=_settings(), processed_at=processed_at,
    )


def _complete_chain():
    instruments = []
    observations = []
    oi_values = {
        (0, 100, OptionType.CALL): Decimal("10"),
        (0, 100, OptionType.PUT): Decimal("20"),
        (0, 110, OptionType.CALL): Decimal("10"),
        (0, 110, OptionType.PUT): Decimal("10"),
        (1, 100, OptionType.CALL): Decimal("20"),
        (1, 100, OptionType.PUT): Decimal("30"),
        (1, 110, OptionType.CALL): Decimal("20"),
        (1, 110, OptionType.PUT): Decimal("40"),
    }
    for (expiry_index, strike, side), oi in oi_values.items():
        instrument = _instrument(expiry_index, strike, side)
        instruments.append(instrument)
        observations.append(_chain_observation(instrument, oi=oi, volume=oi / 2))
    return tuple(instruments), tuple(observations)


def test_put_call_ratios_and_concentrations_use_exact_complete_chain():
    instruments, observations = _complete_chain()
    result = _context(instruments, observations)
    with localcontext() as context:
        context.prec = 34
        expected_oi_ratio = Decimal(100) / Decimal(60)
        expected_volume_ratio = Decimal(50) / Decimal(30)
    assert result.metrics["put_call_oi_ratio"].value == expected_oi_ratio
    assert result.metrics["put_call_volume_ratio"].value == expected_volume_ratio
    assert result.metrics["expiry_concentration:2026-10-06T12:00:00+00:00"].value == Decimal("0.3125")
    assert result.metrics["strike_concentration:100"].value == Decimal("0.5")
    assert result.metrics["put_call_oi_ratio"].status is DataStatus.PARTIAL
    assert result.metrics["put_call_oi_ratio"].provenance is Provenance.COMPUTED
    assert result.metrics["put_call_oi_ratio"].source_fetched_at == REST_CAPTURE
    assert result.metrics["put_call_oi_ratio"].data_age_seconds == 3600


def test_coverage_below_threshold_and_zero_denominator_are_not_available():
    instruments, observations = _complete_chain()
    missing_symbol = instruments[0].symbol
    sparse = tuple(row for row in observations if row.symbol != missing_symbol)
    result = calculate_options_context(
        OptionsContextInputs("BTC", instruments, sparse), as_of=AS_OF,
        settings=Phase8Settings.from_env({
            "TRADING_MODE": "paper", "PHASE8_OPTIONS_MIN_CHAIN_COVERAGE": "0.95",
        }),
    )
    assert result.metrics["put_call_oi_ratio"].status is DataStatus.NOT_AVAILABLE
    assert result.metrics["put_call_oi_ratio"].quality_reason == "COVERAGE_BELOW_THRESHOLD"

    zeros = tuple(_chain_observation(item, oi=Decimal(0), volume=Decimal(0)) for item in instruments)
    zero_result = _context(instruments, zeros)
    assert zero_result.metrics["put_call_oi_ratio"].value is None
    assert zero_result.metrics["put_call_oi_ratio"].quality_reason == "ZERO_CALL_DENOMINATOR"
    assert zero_result.metrics["put_call_volume_ratio"].quality_reason == "ZERO_CALL_DENOMINATOR"


def test_missing_and_unverified_mark_iv_never_becomes_zero_or_uses_rest_iv_fallback():
    instruments, observations = _complete_chain()
    mark = tuple(_mark_observation(item, Decimal("0.54")) for item in instruments)
    result = _context(instruments, observations + mark)
    atm = result.metrics["atm_iv"]
    assert atm.value is None
    assert atm.status is DataStatus.NOT_AVAILABLE
    assert atm.quality_reason == "UNIT_UNVERIFIED"
    assert result.metrics["iv_term_structure"].value is None
    assert result.metrics["iv_skew"].value is None
    assert not any("gex" in name.lower() for name in result.metrics)


def test_verified_paired_atm_iv_and_term_structure_nodes_are_calculated_without_rescaling():
    instruments, chain = _complete_chain()
    priced_chain = tuple(_with_verified_underlying_price(row) for row in chain)
    marks = []
    for instrument in instruments:
        value = Decimal("0.50") if instrument.option_type is OptionType.CALL else Decimal("0.60")
        if instrument.expires_at == EXPIRIES[1]:
            value += Decimal("0.20")
        marks.append(_mark_observation(
            instrument, value, unit_status=UnitStatus.VERIFIED, unit_code="fraction"
        ))
    result = _context(instruments, priced_chain + tuple(marks))
    assert result.metrics["atm_iv"].value == Decimal("0.55")
    assert result.metrics["atm_iv"].unit_code == "fraction"
    assert result.metrics["atm_iv"].status is DataStatus.PARTIAL
    assert result.metrics["iv_term_structure"].value == Decimal(2)
    assert result.metrics["atm_iv:2026-10-06T12:00:00+00:00"].value == Decimal("0.55")


def test_iv_skew_requires_paired_strikes_and_uses_decimal_log_moneyness():
    instruments = tuple(
        _instrument(0, strike, side)
        for strike in (90, 100, 110)
        for side in (OptionType.CALL, OptionType.PUT)
    )
    chain = tuple(
        _with_verified_underlying_price(_chain_observation(item, oi=Decimal(1), volume=Decimal(1)))
        for item in instruments
    )
    marks = []
    for instrument in instruments:
        level = {90: Decimal("0.4"), 100: Decimal("0.5"), 110: Decimal("0.7")}[int(instrument.strike)]
        if instrument.option_type is OptionType.PUT:
            level += Decimal("0.02")
        marks.append(_mark_observation(
            instrument, level, unit_status=UnitStatus.VERIFIED, unit_code="fraction"
        ))
    result = _context(instruments, chain + tuple(marks))
    skew = result.metrics["iv_skew"]
    assert skew.value is not None and skew.value > 0
    assert skew.unit_code == "fraction/log_moneyness"
    assert skew.coverage_available == 6


def test_sparse_markprice_change_merges_with_seed_instead_of_replacing_other_fields():
    instruments = (_instrument(0, 100, OptionType.CALL), _instrument(0, 100, OptionType.PUT))
    chain = tuple(
        _with_verified_underlying_price(_chain_observation(item, oi=Decimal(1), volume=Decimal(1)))
        for item in instruments
    )
    seed = (
        _mark_observation(instruments[0], Decimal("0.50"), unit_status=UnitStatus.VERIFIED, unit_code="fraction"),
        _mark_observation(instruments[1], Decimal("0.60"), unit_status=UnitStatus.VERIFIED, unit_code="fraction"),
    )
    sparse_change = _mark_observation(
        instruments[0], Decimal("0.70"), unit_status=UnitStatus.VERIFIED, unit_code="fraction",
        timestamp=AS_OF - timedelta(seconds=10), kind=ObservationKind.WS_MARKPRICE_CHANGE,
    )
    result = _context(instruments, chain + seed + (sparse_change,))
    assert result.metrics["atm_iv"].value == Decimal("0.65")


def test_iv_context_reports_missing_markprice_field_without_rest_summary_substitution():
    instruments, observations = _complete_chain()
    rest_with_iv = tuple(
        _chain_observation(item, oi=Decimal(1), volume=Decimal(1), mark_iv=Decimal("0.60"))
        for item in instruments
    )
    mark_missing = tuple(_mark_observation(item, None) for item in instruments)
    result = _context(instruments, rest_with_iv + mark_missing)
    assert result.metrics["atm_iv"].value is None
    assert result.metrics["atm_iv"].quality_reason == "MARKPRICE_IV_NOT_AVAILABLE"


def test_context_exposes_source_times_capture_age_coverage_reason_and_provenance():
    instruments, observations = _complete_chain()
    result = _context(instruments, observations)
    metric = result.metrics["put_call_oi_ratio"]
    assert result.context_timestamp == AS_OF
    assert metric.source_timestamps == ()
    assert metric.source_fetched_at == REST_CAPTURE
    assert metric.source_received_at is None
    assert metric.coverage_expected == 8
    assert metric.coverage_available == 8
    assert metric.data_age_seconds == 3600
    assert metric.status is DataStatus.PARTIAL
    assert metric.quality_reason == "SOURCE_TIME_NOT_PROVIDED"
    assert metric.provenance is Provenance.COMPUTED
    assert metric.input_observation_ids
    partial_coverage = OptionContextMetric(
        metric="coverage_probe", value=Decimal(1), source_timestamps=(),
        source_fetched_at=None, source_received_at=None, data_age_seconds=0,
        coverage_expected=3, coverage_available=2, status=DataStatus.PARTIAL,
        quality_reason=None, provenance=Provenance.COMPUTED,
    )
    coverage_ratio = partial_coverage.coverage_ratio
    with localcontext() as context:
        context.prec = 7
        assert partial_coverage.coverage_ratio == coverage_ratio

    processed_at = AS_OF + timedelta(milliseconds=25)
    processed = _context(instruments, observations, processed_at=processed_at)
    assert processed.context_timestamp == AS_OF
    assert processed.processed_at == processed_at


def test_optional_rr_butterfly_require_verified_delta_iv_and_configured_tolerance():
    instruments, observations = _complete_chain()
    result = _context(instruments, observations)
    assert result.metrics["risk_reversal_25d"].value is None
    assert result.metrics["risk_reversal_25d"].status is DataStatus.NOT_AVAILABLE
    assert result.metrics["risk_reversal_25d"].quality_reason == "DELTA_TOLERANCE_NOT_CONFIGURED"
    assert result.metrics["butterfly_25d"].value is None


def test_verified_25_delta_risk_reversal_and_butterfly_obey_configured_tolerance():
    instruments = tuple(
        _instrument(0, strike, side)
        for strike, side in (
            (100, OptionType.CALL), (110, OptionType.CALL),
            (100, OptionType.PUT), (110, OptionType.PUT),
        )
    )
    chain = tuple(
        _with_verified_underlying_price(_chain_observation(item, oi=Decimal(1), volume=Decimal(1)))
        for item in instruments
    )
    deltas = {
        (100, OptionType.CALL): Decimal("0.10"),
        (110, OptionType.CALL): Decimal("0.25"),
        (100, OptionType.PUT): Decimal("-0.25"),
        (110, OptionType.PUT): Decimal("-0.10"),
    }
    marks = []
    ticker = []
    for instrument in instruments:
        iv = {
            (100, OptionType.CALL): Decimal("0.50"),
            (110, OptionType.CALL): Decimal("0.70"),
            (100, OptionType.PUT): Decimal("0.60"),
            (110, OptionType.PUT): Decimal("0.80"),
        }[(int(instrument.strike), instrument.option_type)]
        marks.append(_mark_observation(
            instrument, iv, unit_status=UnitStatus.VERIFIED, unit_code="fraction"
        ))
        ticker.append(_ticker_delta(instrument, deltas[(int(instrument.strike), instrument.option_type)]))
    settings = _settings(PHASE8_OPTIONS_RR_DELTA_TOLERANCE="0.01")
    result = calculate_options_context(
        OptionsContextInputs("BTC", instruments, chain + tuple(marks) + tuple(ticker)),
        as_of=AS_OF, settings=settings,
    )
    assert result.metrics["risk_reversal_25d"].value == Decimal("0.1")
    assert result.metrics["butterfly_25d"].value == Decimal("0.10")


def test_explicit_bounded_ticker_universe_enforces_rr_coverage():
    instruments = tuple(
        _instrument(0, strike, side)
        for strike, side in (
            (100, OptionType.CALL), (110, OptionType.CALL),
            (100, OptionType.PUT), (110, OptionType.PUT),
        )
    )
    chain = tuple(
        _with_verified_underlying_price(_chain_observation(item, oi=Decimal(1), volume=Decimal(1)))
        for item in instruments
    )
    deltas = (Decimal("0.10"), Decimal("0.25"), Decimal("-0.25"), Decimal("-0.10"))
    ticker = tuple(_ticker_delta(item, delta) for item, delta in zip(instruments[:3], deltas[:3]))
    marks = tuple(
        _mark_observation(item, Decimal("0.5"), unit_status=UnitStatus.VERIFIED, unit_code="fraction")
        for item in instruments
    )
    settings = _settings(
        PHASE8_OPTIONS_RR_DELTA_TOLERANCE="0.01",
        PHASE8_OPTIONS_MIN_TICKER_COVERAGE="0.90",
    )
    result = calculate_options_context(
        OptionsContextInputs(
            "BTC", instruments, chain + marks + ticker,
            ticker_universe_symbols=tuple(item.symbol for item in instruments),
        ),
        as_of=AS_OF, settings=settings,
    )
    assert result.metrics["risk_reversal_25d"].value is None
    assert result.metrics["risk_reversal_25d"].quality_reason == "TICKER_COVERAGE_BELOW_THRESHOLD"


def test_context_module_has_no_stage1_or_trade_action_surface():
    import quant_phase8.context as context_module

    source = inspect.getsource(context_module).lower()
    assert "quant_phase1" not in source
    assert not any(token in source for token in ("buy", "sell", "long", "short", "riskengine", "executor"))


def test_context_calculation_is_deterministic_and_context_only():
    instruments, observations = _complete_chain()
    first = _context(instruments, observations)
    second = _context(instruments, observations)
    assert first == second
    def serialize(snapshot):
        return _json_encode({
            "underlying": snapshot.underlying,
            "context_timestamp": snapshot.context_timestamp,
            "processed_at": snapshot.processed_at,
            "calculation_version": snapshot.calculation_version,
            "metrics": {
                name: {
                    "value": metric.value,
                    "status": metric.status,
                    "reason": metric.quality_reason,
                    "source_timestamps": metric.source_timestamps,
                    "fetched_at": metric.source_fetched_at,
                    "received_at": metric.source_received_at,
                    "data_age_seconds": metric.data_age_seconds,
                    "coverage_expected": metric.coverage_expected,
                    "coverage_available": metric.coverage_available,
                    "coverage_ratio": metric.coverage_ratio,
                    "provenance": metric.provenance,
                    "unit_code": metric.unit_code,
                    "unit_status": metric.unit_status,
                    "input_observation_ids": metric.input_observation_ids,
                }
                for name, metric in snapshot.metrics.items()
            },
        })
    assert serialize(first) == serialize(second)
    with localcontext() as context:
        context.prec = 7
        low_precision = _context(instruments, observations)
    assert first == low_precision
    assert all(metric.provenance is Provenance.COMPUTED for metric in first.metrics.values())
    assert not {"side", "trade_action", "buy", "sell", "long", "short", "gex"}.intersection(
        name.lower() for name in first.metrics
    )
