"""Deterministic, coverage-aware, context-only options calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Mapping, Sequence

from .config import Phase8Settings
from .contracts import (
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
from .freshness import SelectedContextInput, select_context_inputs


_CALCULATION_DECIMAL_PRECISION = 34


def _decimal_sum(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = _CALCULATION_DECIMAL_PRECISION
        return sum(values, Decimal(0))


def _decimal_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _CALCULATION_DECIMAL_PRECISION
        return numerator / denominator


def _decimal_mean(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _CALCULATION_DECIMAL_PRECISION
        return (left + right) / Decimal(2)


def _decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _CALCULATION_DECIMAL_PRECISION
        return left - right


@dataclass(frozen=True, slots=True)
class OptionsContextInputs:
    underlying: str
    instruments: tuple[OptionInstrument, ...]
    observations: tuple[OptionMarketObservation, ...]
    ticker_universe_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.underlying not in {"BTC", "ETH"}:
            raise ValueError("context underlying must be BTC or ETH")
        if any(not isinstance(item, OptionInstrument) or item.underlying != self.underlying for item in self.instruments):
            raise ValueError("context instruments must be canonical and match the underlying")
        if len({item.symbol for item in self.instruments}) != len(self.instruments):
            raise ValueError("context instrument symbols must be unique")
        if any(
            not isinstance(item, OptionMarketObservation) or item.underlying != self.underlying
            for item in self.observations
        ):
            raise ValueError("context observations must be canonical and match the underlying")
        symbols = tuple(self.ticker_universe_symbols)
        if any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
            raise ValueError("ticker universe symbols must be non-empty strings")
        if len(set(symbols)) != len(symbols):
            raise ValueError("ticker universe symbols must be unique")
        if not set(symbols).issubset({item.symbol for item in self.instruments}):
            raise ValueError("ticker universe must be a subset of the instrument catalog")
        object.__setattr__(self, "ticker_universe_symbols", symbols)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _selected_by_family(
    selected: Sequence[SelectedContextInput], *, family: str
) -> dict[tuple[str, str], SelectedContextInput]:
    if family == "REST":
        kinds = {ObservationKind.REST_CHAIN_SUMMARY}
    elif family == "MARKPRICE":
        kinds = {ObservationKind.WS_MARKPRICE_SNAPSHOT, ObservationKind.WS_MARKPRICE_CHANGE}
    else:
        kinds = {
            ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT,
            ObservationKind.WS_INCREMENTAL_TICKER_CHANGE,
        }
    result: dict[tuple[str, str], SelectedContextInput] = {}
    for item in selected:
        if item.observation_kind not in kinds:
            continue
        key = (item.symbol, item.metric_name)
        current = result.get(key)
        source_time = item.freshness.source_timestamp
        order_time = (
            source_time
            if source_time is not None and item.metric.timestamp_semantics is TimestampSemantics.VERIFIED
            else item.capture_timestamp
        )
        current_source = current.freshness.source_timestamp if current else None
        current_order = (
            current_source
            if current and current_source is not None
            and current.metric.timestamp_semantics is TimestampSemantics.VERIFIED
            else current.capture_timestamp if current else None
        )
        if current is None or (order_time, item.capture_timestamp) > (current_order, current.capture_timestamp):
            result[key] = item
    return result


def _available(item: SelectedContextInput | None) -> bool:
    return bool(
        item is not None
        and item.metric.value is not None
        and item.metric.status in {DataStatus.AVAILABLE, DataStatus.PARTIAL}
        and item.freshness.status in {DataStatus.AVAILABLE, DataStatus.PARTIAL}
    )


def _source_quality(items: Sequence[SelectedContextInput]) -> tuple[DataStatus, str | None]:
    if any(item.freshness.status is DataStatus.PARTIAL for item in items):
        reasons = sorted({item.freshness.reason_code for item in items if item.freshness.reason_code})
        return DataStatus.PARTIAL, reasons[0] if reasons else "SOURCE_TIME_QUALITY_DEGRADED"
    return DataStatus.AVAILABLE, None


def _make_metric(
    name: str,
    value: Decimal | None,
    *,
    inputs: Sequence[SelectedContextInput] = (),
    expected: int = 0,
    available: int = 0,
    status: DataStatus,
    reason: str | None,
    unit_code: str | None = None,
    unit_status: UnitStatus = UnitStatus.NOT_APPLICABLE,
) -> OptionContextMetric:
    source_timestamps = tuple(sorted({
        item.freshness.source_timestamp for item in inputs
        if item.freshness.source_timestamp is not None
    }))
    rest_captures = [item.capture_timestamp for item in inputs if item.observation_kind is ObservationKind.REST_CHAIN_SUMMARY]
    ws_captures = [item.capture_timestamp for item in inputs if item.observation_kind is not ObservationKind.REST_CHAIN_SUMMARY]
    ages = [item.freshness.data_age_seconds for item in inputs if item.freshness.data_age_seconds is not None]
    input_ids = tuple(sorted({
        item.observation.observation_id for item in inputs if item.observation.observation_id is not None
    }))
    return OptionContextMetric(
        metric=name,
        value=value,
        source_timestamps=source_timestamps,
        source_fetched_at=min(rest_captures) if rest_captures else None,
        source_received_at=min(ws_captures) if ws_captures else None,
        data_age_seconds=max(ages) if ages else None,
        coverage_expected=expected,
        coverage_available=available,
        status=status,
        quality_reason=reason,
        provenance=Provenance.COMPUTED,
        unit_code=unit_code,
        unit_status=unit_status,
        input_observation_ids=input_ids,
    )


def _not_available(name: str, reason: str, *, expected: int = 0, available: int = 0,
                   inputs: Sequence[SelectedContextInput] = ()) -> OptionContextMetric:
    return _make_metric(
        name, None, inputs=inputs, expected=expected, available=available,
        status=DataStatus.NOT_AVAILABLE, reason=reason,
    )


def _coverage_ok(available: int, expected: int, threshold: float) -> bool:
    return expected > 0 and _decimal_ratio(Decimal(available), Decimal(expected)) >= Decimal(str(threshold))


def _valid_chain_item(item: SelectedContextInput | None, underlying: str) -> bool:
    return bool(
        _available(item)
        and item.metric.unit_status is UnitStatus.VERIFIED
        and item.metric.unit_code == underlying
        and item.metric.value is not None
        and item.metric.value >= 0
    )


def _rest_cycle(
    rest: Mapping[tuple[str, str], SelectedContextInput]
) -> dict[tuple[str, str], SelectedContextInput]:
    if not rest:
        return {}
    latest_capture = max(item.capture_timestamp for item in rest.values())
    return {key: item for key, item in rest.items() if item.capture_timestamp == latest_capture}


def _chain_ratio(
    name: str,
    metric_name: str,
    *,
    instruments: Sequence[OptionInstrument],
    rest: Mapping[tuple[str, str], SelectedContextInput],
    underlying: str,
    threshold: float,
) -> OptionContextMetric:
    expected_by_side = {
        side: tuple(item for item in instruments if item.option_type is side)
        for side in (OptionType.CALL, OptionType.PUT)
    }
    selected_by_side: dict[OptionType, list[SelectedContextInput]] = {side: [] for side in expected_by_side}
    total_expected = sum(len(items) for items in expected_by_side.values())
    total_available = 0
    unit_unverified = False
    side_values: dict[OptionType, list[Decimal]] = {side: [] for side in expected_by_side}
    all_source_items: list[SelectedContextInput] = []
    for side, rows in expected_by_side.items():
        for instrument in rows:
            item = rest.get((instrument.symbol, metric_name))
            if item is None:
                continue
            all_source_items.append(item)
            if item.metric.value is not None and (
                item.metric.unit_status is not UnitStatus.VERIFIED or item.metric.unit_code != underlying
            ):
                unit_unverified = True
            if _valid_chain_item(item, underlying):
                side_values[side].append(item.metric.value or Decimal(0))
                selected_by_side[side].append(item)
        total_available += len(selected_by_side[side])
    if total_expected == 0:
        return _not_available(name, "NO_EXPECTED_INSTRUMENTS")
    side_coverage = [
        _coverage_ok(len(selected_by_side[side]), len(rows), threshold)
        for side, rows in expected_by_side.items()
    ]
    if not all(side_coverage):
        reason = "UNIT_UNVERIFIED" if unit_unverified else "COVERAGE_BELOW_THRESHOLD"
        return _not_available(name, reason, expected=total_expected, available=total_available, inputs=all_source_items)
    sums = {side: _decimal_sum(values) for side, values in side_values.items()}
    call_total = sums[OptionType.CALL]
    if call_total <= 0:
        return _not_available(
            name, "ZERO_CALL_DENOMINATOR", expected=total_expected,
            available=total_available, inputs=all_source_items,
        )
    put_total = sums[OptionType.PUT]
    value = _decimal_ratio(put_total, call_total)
    source_inputs = selected_by_side[OptionType.CALL] + selected_by_side[OptionType.PUT]
    quality_status, quality_reason = _source_quality(source_inputs)
    complete = total_available == total_expected
    status = DataStatus.AVAILABLE if complete and quality_status is DataStatus.AVAILABLE else DataStatus.PARTIAL
    reason = quality_reason if quality_status is DataStatus.PARTIAL else (None if complete else "MISSING_CHAIN_ROWS")
    return _make_metric(
        name, value, inputs=source_inputs, expected=total_expected, available=total_available,
        status=status, reason=reason, unit_code="ratio", unit_status=UnitStatus.VERIFIED,
    )


def _concentrations(
    *,
    instruments: Sequence[OptionInstrument],
    rest: Mapping[tuple[str, str], SelectedContextInput],
    settings: Phase8Settings,
    underlying: str,
) -> dict[str, OptionContextMetric]:
    name_base = "expiry_concentration"
    output: dict[str, OptionContextMetric] = {}
    expected = len(instruments)
    rows = [rest.get((item.symbol, "open_interest")) for item in instruments]
    valid_rows = [item for item in rows if _valid_chain_item(item, underlying)]
    unit_unverified = any(
        item is not None and item.metric.value is not None
        and (item.metric.unit_status is not UnitStatus.VERIFIED or item.metric.unit_code != underlying)
        for item in rows
    )
    coverage_ok = _coverage_ok(len(valid_rows), expected, settings.min_chain_coverage)
    all_inputs = [item for item in rows if item is not None]
    total_oi = _decimal_sum([item.metric.value or Decimal(0) for item in valid_rows])
    if not coverage_ok:
        reason = "UNIT_UNVERIFIED" if unit_unverified else "COVERAGE_BELOW_THRESHOLD"
        unavailable = _not_available(name_base, reason, expected=expected, available=len(valid_rows), inputs=all_inputs)
        output[name_base] = unavailable
        output["strike_concentration"] = _not_available(
            "strike_concentration", reason, expected=expected, available=len(valid_rows), inputs=all_inputs
        )
        return output
    if total_oi <= 0:
        for name in (name_base, "strike_concentration"):
            output[name] = _not_available(
                name, "ZERO_TOTAL_OPEN_INTEREST", expected=expected, available=len(valid_rows), inputs=all_inputs
            )
        return output

    expiry_values: dict[datetime, list[Decimal]] = {}
    strike_values: dict[Decimal, list[Decimal]] = {}
    source_for_expiry: dict[datetime, list[SelectedContextInput]] = {}
    source_for_strike: dict[Decimal, list[SelectedContextInput]] = {}
    by_symbol = {item.symbol: item for item in instruments}
    for item in valid_rows:
        instrument = by_symbol[item.symbol]
        value = item.metric.value or Decimal(0)
        expiry_values.setdefault(instrument.expires_at, []).append(value)
        strike_values.setdefault(instrument.strike, []).append(value)
        source_for_expiry.setdefault(instrument.expires_at, []).append(item)
        source_for_strike.setdefault(instrument.strike, []).append(item)

    quality_status, quality_reason = _source_quality(valid_rows)
    overall_status = DataStatus.AVAILABLE if len(valid_rows) == expected and quality_status is DataStatus.AVAILABLE else DataStatus.PARTIAL
    overall_reason = quality_reason if quality_status is DataStatus.PARTIAL else (None if len(valid_rows) == expected else "MISSING_CHAIN_ROWS")
    expiry_totals = {expiry: _decimal_sum(values) for expiry, values in expiry_values.items()}
    strike_totals = {strike: _decimal_sum(values) for strike, values in strike_values.items()}
    expiry_shares = sorted(
        ((expiry, _decimal_ratio(total, total_oi)) for expiry, total in expiry_totals.items()),
        key=lambda row: (-row[1], row[0]),
    )
    strike_shares = sorted(
        ((strike, _decimal_ratio(total, total_oi)) for strike, total in strike_totals.items()),
        key=lambda row: (-row[1], row[0]),
    )
    output[name_base] = _make_metric(
        name_base, expiry_shares[0][1], inputs=valid_rows, expected=expected, available=len(valid_rows),
        status=overall_status, reason=overall_reason, unit_code="ratio", unit_status=UnitStatus.VERIFIED,
    )
    output["strike_concentration"] = _make_metric(
        "strike_concentration", strike_shares[0][1], inputs=valid_rows, expected=expected,
        available=len(valid_rows), status=overall_status, reason=overall_reason,
        unit_code="ratio", unit_status=UnitStatus.VERIFIED,
    )
    for expiry, share in expiry_shares[:settings.concentration_top_n]:
        node_name = f"expiry_concentration:{expiry.isoformat()}"
        output[node_name] = _make_metric(
            node_name, share, inputs=source_for_expiry[expiry], expected=expected, available=len(valid_rows),
            status=overall_status, reason=overall_reason, unit_code="ratio", unit_status=UnitStatus.VERIFIED,
        )
    for strike, share in strike_shares[:settings.concentration_top_n]:
        strike_label = format(strike.normalize(), "f")
        node_name = f"strike_concentration:{strike_label}"
        output[node_name] = _make_metric(
            node_name, share, inputs=source_for_strike[strike], expected=expected, available=len(valid_rows),
            status=overall_status, reason=overall_reason, unit_code="ratio", unit_status=UnitStatus.VERIFIED,
        )
    return output


def _verified_price(
    instrument: OptionInstrument,
    rest: Mapping[tuple[str, str], SelectedContextInput],
    ticker: Mapping[tuple[str, str], SelectedContextInput],
) -> SelectedContextInput | None:
    candidates = [
        item for item in (rest.get((instrument.symbol, "underlying_price")), ticker.get((instrument.symbol, "underlying_price")))
        if _available(item) and item is not None and item.metric.value is not None
        and item.metric.value > 0 and item.metric.unit_status is UnitStatus.VERIFIED
        and item.metric.unit_code == instrument.quote_currency
    ]
    return max(candidates, key=lambda item: (item.freshness.source_timestamp or item.capture_timestamp, item.capture_timestamp)) if candidates else None


def _iv_node_for_expiry(
    expiry: datetime,
    instruments: Sequence[OptionInstrument],
    mark: Mapping[tuple[str, str], SelectedContextInput],
    rest: Mapping[tuple[str, str], SelectedContextInput],
    ticker: Mapping[tuple[str, str], SelectedContextInput],
    *,
    settings: Phase8Settings,
) -> tuple[Decimal | None, DataStatus, str | None, int, int, list[SelectedContextInput], str | None]:
    expiry_rows = [item for item in instruments if item.expires_at == expiry]
    iv_rows = [mark.get((item.symbol, "iv")) for item in expiry_rows]
    present = [item for item in iv_rows if item is not None]
    valid_iv = [
        item for item in present if _available(item) and item.metric.unit_status is UnitStatus.VERIFIED
        and item.metric.unit_code is not None and item.metric.value is not None
    ]
    if not expiry_rows or len(valid_iv) / len(expiry_rows) < settings.min_ticker_coverage:
        if any(item.metric.value is not None and item.metric.unit_status is not UnitStatus.VERIFIED for item in present):
            return None, DataStatus.NOT_AVAILABLE, "UNIT_UNVERIFIED", len(expiry_rows), len(valid_iv), present, None
        return None, DataStatus.NOT_AVAILABLE, "MARKPRICE_IV_NOT_AVAILABLE", len(expiry_rows), len(valid_iv), present, None
    units = {item.metric.unit_code for item in valid_iv}
    if len(units) != 1:
        return None, DataStatus.NOT_AVAILABLE, "IV_UNIT_MISMATCH", len(expiry_rows), len(valid_iv), valid_iv, None

    price_items = [_verified_price(item, rest, ticker) for item in expiry_rows]
    price_items = [item for item in price_items if item is not None]
    if not price_items:
        return None, DataStatus.NOT_AVAILABLE, "UNDERLYING_PRICE_NOT_AVAILABLE", len(expiry_rows), len(valid_iv), valid_iv, next(iter(units))
    price_item = max(price_items, key=lambda item: (item.freshness.source_timestamp or item.capture_timestamp, item.capture_timestamp))
    spot = price_item.metric.value
    assert spot is not None
    calls: dict[Decimal, SelectedContextInput] = {}
    puts: dict[Decimal, SelectedContextInput] = {}
    for instrument in expiry_rows:
        item = mark.get((instrument.symbol, "iv"))
        if item is None or item not in valid_iv:
            continue
        (calls if instrument.option_type is OptionType.CALL else puts)[instrument.strike] = item
    pairs: list[tuple[Decimal, Decimal, SelectedContextInput, SelectedContextInput]] = []
    for strike in sorted(set(calls).intersection(puts)):
        call, put = calls[strike], puts[strike]
        call_ts, put_ts = call.freshness.source_timestamp, put.freshness.source_timestamp
        if call_ts is None or put_ts is None:
            continue
        if abs((call_ts - put_ts).total_seconds()) > settings.max_source_skew_seconds:
            continue
        if call.metric.unit_code != put.metric.unit_code:
            continue
        assert call.metric.value is not None and put.metric.value is not None
        pairs.append((strike, _decimal_mean(call.metric.value, put.metric.value), call, put))
    if not pairs:
        return None, DataStatus.NOT_AVAILABLE, "PAIRED_ATM_IV_NOT_AVAILABLE", len(expiry_rows), len(valid_iv), valid_iv + [price_item], next(iter(units))
    nearest = min(pairs, key=lambda row: (abs(row[0] - spot), row[0]))
    used = [nearest[2], nearest[3], price_item]
    quality_status, quality_reason = _source_quality(used)
    return nearest[1], quality_status, quality_reason, len(expiry_rows), len(valid_iv), used, next(iter(units))


def _skew_metric(
    instruments: Sequence[OptionInstrument],
    mark: Mapping[tuple[str, str], SelectedContextInput],
    rest: Mapping[tuple[str, str], SelectedContextInput],
    ticker: Mapping[tuple[str, str], SelectedContextInput],
    *,
    settings: Phase8Settings,
) -> OptionContextMetric:
    fallback_reason = "SKEW_INPUTS_NOT_AVAILABLE"
    expiries = sorted({item.expires_at for item in instruments})
    for expiry in expiries:
        rows = [item for item in instruments if item.expires_at == expiry]
        candidates = [(instrument, mark.get((instrument.symbol, "iv"))) for instrument in rows]
        present = [(instrument, item) for instrument, item in candidates if item is not None]
        valid = [(instrument, item) for instrument, item in present if (
            _available(item) and item is not None and item.metric.value is not None
            and item.metric.unit_status is UnitStatus.VERIFIED and item.metric.unit_code
        )]
        if len(valid) < settings.skew_min_contracts:
            if any(item.metric.value is not None and item.metric.unit_status is not UnitStatus.VERIFIED for _, item in present):
                fallback_reason = "UNIT_UNVERIFIED"
            continue
        units = {item.metric.unit_code for _, item in valid}
        if len(units) != 1:
            fallback_reason = "IV_UNIT_MISMATCH"
            continue
        pairs: dict[Decimal, dict[OptionType, SelectedContextInput]] = {}
        for instrument, item in valid:
            pairs.setdefault(instrument.strike, {})[instrument.option_type] = item
        points: list[tuple[Decimal, Decimal, list[SelectedContextInput]]] = []
        for strike, sides in pairs.items():
            if OptionType.CALL not in sides or OptionType.PUT not in sides:
                continue
            call, put = sides[OptionType.CALL], sides[OptionType.PUT]
            if call.metric.unit_code != put.metric.unit_code:
                continue
            if call.freshness.source_timestamp is None or put.freshness.source_timestamp is None:
                continue
            if abs((call.freshness.source_timestamp - put.freshness.source_timestamp).total_seconds()) > settings.max_source_skew_seconds:
                continue
            assert call.metric.value is not None and put.metric.value is not None
            points.append((strike, _decimal_mean(call.metric.value, put.metric.value), [call, put]))
        if len(units) != 1 or len(points) < settings.skew_min_distinct_strikes:
            fallback_reason = "INSUFFICIENT_PAIRED_STRIKES"
            continue
        all_pairs = [item for _, _, pair in points for item in pair]
        coverage_expected = len(rows)
        if not _coverage_ok(len({item.symbol for item in all_pairs}), coverage_expected, settings.min_ticker_coverage):
            fallback_reason = "COVERAGE_BELOW_THRESHOLD"
            continue
        price_candidates = [_verified_price(instrument, rest, ticker) for instrument in rows]
        price_candidates = [item for item in price_candidates if item is not None]
        if not price_candidates:
            fallback_reason = "UNDERLYING_PRICE_NOT_AVAILABLE"
            continue
        price_item = max(price_candidates, key=lambda item: (item.freshness.source_timestamp or item.capture_timestamp, item.capture_timestamp))
        spot = price_item.metric.value
        assert spot is not None and spot > 0
        with localcontext() as context:
            context.prec = 34
            xy = [(strike / spot).ln() for strike, _, _ in points]
            ys = [value for _, value, _ in points]
            mean_x = sum(xy, Decimal(0)) / Decimal(len(xy))
            mean_y = sum(ys, Decimal(0)) / Decimal(len(ys))
            denominator = sum(((x - mean_x) ** 2 for x in xy), Decimal(0))
            if denominator == 0:
                fallback_reason = "SKEW_STRIKES_NOT_DISTINCT"
                continue
            slope = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xy, ys)), Decimal(0)) / denominator
        used = [item for _, _, pair in points for item in pair] + [price_item]
        quality_status, quality_reason = _source_quality(used)
        return _make_metric(
            "iv_skew", slope, inputs=used, expected=coverage_expected,
            available=len({item.symbol for item in all_pairs}),
            status=quality_status, reason=quality_reason,
            unit_code=f"{next(iter(units))}/log_moneyness", unit_status=UnitStatus.VERIFIED,
        )
    return _not_available("iv_skew", fallback_reason, expected=len(instruments), available=0)


def _optional_rr_metrics(
    instruments: Sequence[OptionInstrument],
    mark: Mapping[tuple[str, str], SelectedContextInput],
    ticker: Mapping[tuple[str, str], SelectedContextInput],
    rest: Mapping[tuple[str, str], SelectedContextInput],
    ticker_universe_symbols: Sequence[str],
    *,
    settings: Phase8Settings,
) -> tuple[OptionContextMetric, OptionContextMetric]:
    if settings.rr_delta_tolerance is None:
        return (
            _not_available("risk_reversal_25d", "DELTA_TOLERANCE_NOT_CONFIGURED"),
            _not_available("butterfly_25d", "DELTA_TOLERANCE_NOT_CONFIGURED"),
        )
    ticker_symbols = set(ticker_universe_symbols) or {
        symbol for symbol, metric_name in ticker if metric_name == "delta"
    }
    expected_ticker_rows = len(ticker_symbols)
    covered_ticker_symbols = {
        symbol for symbol in ticker_symbols
        if _available(ticker.get((symbol, "delta")))
        and _available(mark.get((symbol, "iv")))
        and ticker[(symbol, "delta")].metric.unit_status is UnitStatus.VERIFIED
        and mark[(symbol, "iv")].metric.unit_status is UnitStatus.VERIFIED
    }
    if not _coverage_ok(len(covered_ticker_symbols), expected_ticker_rows, settings.min_ticker_coverage):
        return (
            _not_available("risk_reversal_25d", "TICKER_COVERAGE_BELOW_THRESHOLD",
                           expected=expected_ticker_rows, available=len(covered_ticker_symbols)),
            _not_available("butterfly_25d", "TICKER_COVERAGE_BELOW_THRESHOLD",
                           expected=expected_ticker_rows, available=len(covered_ticker_symbols)),
        )
    # No unit scaling is performed. Only verified canonical delta and IV values
    # may enter the optional 25-delta context.
    by_expiry: dict[datetime, dict[OptionType, list[tuple[OptionInstrument, SelectedContextInput, SelectedContextInput]]]] = {}
    for instrument in instruments:
        delta = ticker.get((instrument.symbol, "delta"))
        iv = mark.get((instrument.symbol, "iv"))
        if not (_available(delta) and _available(iv)) or delta is None or iv is None:
            continue
        if (
            delta.metric.unit_status is not UnitStatus.VERIFIED
            or delta.metric.value is None
            or not delta.metric.unit_code
            or iv.metric.unit_status is not UnitStatus.VERIFIED
            or iv.metric.value is None
            or not iv.metric.unit_code
        ):
            continue
        by_expiry.setdefault(instrument.expires_at, {}).setdefault(instrument.option_type, []).append((instrument, delta, iv))
    for expiry in sorted(by_expiry):
        sides = by_expiry[expiry]
        selected: dict[OptionType, tuple[OptionInstrument, SelectedContextInput, SelectedContextInput]] = {}
        for side, target in ((OptionType.CALL, Decimal("0.25")), (OptionType.PUT, Decimal("-0.25"))):
            candidates = sides.get(side, [])
            candidates = [row for row in candidates if abs((row[1].metric.value or Decimal(0)) - target) <= Decimal(str(settings.rr_delta_tolerance))]
            if candidates:
                selected[side] = min(candidates, key=lambda row: (abs((row[1].metric.value or Decimal(0)) - target), row[0].strike))
        atm, atm_status, atm_reason, _, _, atm_inputs, unit = _iv_node_for_expiry(
            expiry, instruments, mark, rest, ticker, settings=settings
        )
        if len(selected) != 2 or atm is None:
            continue
        call, put = selected[OptionType.CALL], selected[OptionType.PUT]
        call_iv, put_iv = call[2].metric, put[2].metric
        if (
            call_iv.unit_code != put_iv.unit_code or call_iv.unit_code != unit
            or call[1].metric.unit_code != put[1].metric.unit_code
        ):
            continue
        if call[1].freshness.source_timestamp is None or put[1].freshness.source_timestamp is None:
            continue
        event_times = [
            item.freshness.source_timestamp
            for item in (call[1], call[2], put[1], put[2], *atm_inputs)
        ]
        event_times = [timestamp for timestamp in event_times if timestamp is not None]
        if len(event_times) < 4 or (max(event_times) - min(event_times)).total_seconds() > settings.max_source_skew_seconds:
            continue
        source_inputs = [call[1], call[2], put[1], put[2], *atm_inputs]
        quality_status, quality_reason = _source_quality(source_inputs)
        status = DataStatus.PARTIAL if atm_status is DataStatus.PARTIAL or quality_status is DataStatus.PARTIAL else DataStatus.AVAILABLE
        reason = quality_reason or atm_reason if status is DataStatus.PARTIAL else None
        return (
            _make_metric("risk_reversal_25d", _decimal_difference(call_iv.value, put_iv.value), inputs=source_inputs,
                         expected=expected_ticker_rows, available=len(covered_ticker_symbols), status=status, reason=reason,
                         unit_code=unit, unit_status=UnitStatus.VERIFIED),
            _make_metric("butterfly_25d", _decimal_difference(_decimal_mean(call_iv.value, put_iv.value), atm),
                         inputs=source_inputs, expected=expected_ticker_rows, available=len(covered_ticker_symbols), status=status, reason=reason,
                         unit_code=unit, unit_status=UnitStatus.VERIFIED),
        )
    return (
        _not_available("risk_reversal_25d", "VERIFIED_25_DELTA_INPUTS_NOT_AVAILABLE"),
        _not_available("butterfly_25d", "VERIFIED_25_DELTA_INPUTS_NOT_AVAILABLE"),
    )


def calculate_options_context(
    inputs: OptionsContextInputs,
    *,
    as_of: datetime,
    settings: Phase8Settings,
    processed_at: datetime | None = None,
) -> OptionContextSnapshot:
    """Calculate only non-actionable context metrics for one underlying."""
    if not isinstance(inputs, OptionsContextInputs):
        raise TypeError("inputs must be OptionsContextInputs")
    if not isinstance(settings, Phase8Settings):
        raise TypeError("settings must be validated Phase8Settings")
    as_of = _utc(as_of)
    processed_at = as_of if processed_at is None else _utc(processed_at)
    instruments = tuple(
        item for item in inputs.instruments
        if item.is_active and item.instrument_state == "open" and item.status is DataStatus.AVAILABLE
    )
    if len(inputs.ticker_universe_symbols) > settings.max_ticker_instruments_per_currency:
        raise ValueError("ticker universe exceeds the configured per-underlying cap")
    selected = select_context_inputs(inputs.observations, as_of=as_of, settings=settings)
    rest_all = _selected_by_family(selected, family="REST")
    rest = _rest_cycle(rest_all)
    mark = _selected_by_family(selected, family="MARKPRICE")
    ticker = _selected_by_family(selected, family="TICKER")

    metrics: dict[str, OptionContextMetric] = {}
    metrics["put_call_oi_ratio"] = _chain_ratio(
        "put_call_oi_ratio", "open_interest", instruments=instruments, rest=rest,
        underlying=inputs.underlying, threshold=settings.min_chain_coverage,
    )
    metrics["put_call_volume_ratio"] = _chain_ratio(
        "put_call_volume_ratio", "volume_24h", instruments=instruments, rest=rest,
        underlying=inputs.underlying, threshold=settings.min_chain_coverage,
    )
    metrics.update(_concentrations(
        instruments=instruments, rest=rest, settings=settings, underlying=inputs.underlying,
    ))

    expiry_nodes: list[tuple[datetime, Decimal, list[SelectedContextInput], DataStatus, str | None, int, int, str | None]] = []
    for expiry in sorted({item.expires_at for item in instruments}):
        value, status, reason, expected, available, source_inputs, unit = _iv_node_for_expiry(
            expiry, instruments, mark, rest, ticker, settings=settings
        )
        node_name = f"atm_iv:{expiry.isoformat()}"
        metrics[node_name] = _make_metric(
            node_name, value, inputs=source_inputs, expected=expected, available=available,
            status=status, reason=reason, unit_code=unit,
            unit_status=UnitStatus.VERIFIED if value is not None else UnitStatus.UNKNOWN,
        )
        if value is not None:
            expiry_nodes.append((expiry, value, source_inputs, status, reason, expected, available, unit))
    if expiry_nodes:
        first = expiry_nodes[0]
        metrics["atm_iv"] = _make_metric(
            "atm_iv", first[1], inputs=first[2], expected=first[5], available=first[6],
            status=first[3], reason=first[4], unit_code=first[7], unit_status=UnitStatus.VERIFIED,
        )
    else:
        reason = next((metric.quality_reason for name, metric in metrics.items() if name.startswith("atm_iv:") and metric.quality_reason), "MARKPRICE_IV_NOT_AVAILABLE")
        metrics["atm_iv"] = _not_available("atm_iv", reason)
    if len(expiry_nodes) >= 2:
        term_status = (
            DataStatus.PARTIAL if any(node[3] is DataStatus.PARTIAL for node in expiry_nodes)
            else DataStatus.AVAILABLE
        )
        metrics["iv_term_structure"] = _make_metric(
            "iv_term_structure", Decimal(len(expiry_nodes)),
            inputs=[item for node in expiry_nodes for item in node[2]],
            expected=len({item.expires_at for item in instruments}), available=len(expiry_nodes),
            status=term_status if len(expiry_nodes) == len({item.expires_at for item in instruments}) else DataStatus.PARTIAL,
            reason="MISSING_EXPIRY_NODES" if len(expiry_nodes) != len({item.expires_at for item in instruments}) else next((node[4] for node in expiry_nodes if node[4]), None),
            unit_code="expiry_nodes", unit_status=UnitStatus.VERIFIED,
        )
    else:
        iv_reasons = [metric.quality_reason for name, metric in metrics.items() if name.startswith("atm_iv:") and metric.quality_reason]
        metrics["iv_term_structure"] = _not_available(
            "iv_term_structure", iv_reasons[0] if iv_reasons else "INSUFFICIENT_EXPIRY_NODES",
            expected=len({item.expires_at for item in instruments}), available=len(expiry_nodes),
        )
    metrics["iv_skew"] = _skew_metric(instruments, mark, rest, ticker, settings=settings)
    rr, butterfly = _optional_rr_metrics(
        instruments, mark, ticker, rest, inputs.ticker_universe_symbols, settings=settings
    )
    metrics[rr.metric] = rr
    metrics[butterfly.metric] = butterfly

    return OptionContextSnapshot(
        underlying=inputs.underlying,
        context_timestamp=as_of,
        processed_at=processed_at,
        calculation_version="phase8.options-context.v1",
        metrics=MappingProxyType(dict(sorted(metrics.items()))),
    )
