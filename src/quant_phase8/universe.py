"""Deterministic, bounded option-universe selection for Phase 8."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType

from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    OptionInstrument,
    OptionMetricValue,
    OptionType,
    TimestampSemantics,
    UnitStatus,
)


_CURRENCIES = ("BTC", "ETH")
_SIDES = (OptionType.CALL, OptionType.PUT)


@dataclass(frozen=True, slots=True)
class ExpiryCoverage:
    eligible_count: int
    selected_count: int
    call_selected_count: int
    put_selected_count: int
    status: DataStatus


@dataclass(frozen=True, slots=True)
class UnderlyingCoverage:
    status: DataStatus
    reason: str | None
    eligible_count: int | None
    selected_count: int
    expiry: Mapping[str, ExpiryCoverage]
    price_time_basis: str | None = None
    price_data_age_seconds: int | None = None

    @property
    def coverage_ratio(self) -> Decimal | None:
        if self.eligible_count is None:
            return None
        if self.eligible_count == 0:
            return Decimal("0")
        return Decimal(self.selected_count) / Decimal(self.eligible_count)


@dataclass(frozen=True, slots=True)
class UniverseSelection(Mapping[str, tuple[OptionInstrument, ...]]):
    """Selected instruments with explicit per-underlying quality and coverage."""

    selected: Mapping[str, tuple[OptionInstrument, ...]]
    coverage: Mapping[str, UnderlyingCoverage]
    reasons: Mapping[str, Mapping[str, str]]

    def __getitem__(self, underlying: str) -> tuple[OptionInstrument, ...]:
        return self.selected[underlying]

    def __iter__(self) -> Iterator[str]:
        return iter(self.selected)

    def __len__(self) -> int:
        return len(self.selected)

    @property
    def status(self) -> DataStatus:
        statuses = {item.status for item in self.coverage.values()}
        if DataStatus.ERROR in statuses:
            return DataStatus.ERROR
        if DataStatus.PARTIAL in statuses:
            return DataStatus.PARTIAL
        if len(statuses) > 1:
            return DataStatus.PARTIAL
        if DataStatus.STALE in statuses:
            return DataStatus.STALE
        if statuses and statuses == {DataStatus.NOT_AVAILABLE}:
            return DataStatus.NOT_AVAILABLE
        return DataStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class FullChainValidation:
    status: DataStatus
    records: tuple[OptionInstrument, ...]
    reason: str | None
    rejected_count: int
    counts_by_underlying: Mapping[str, int]


@dataclass(slots=True)
class _CurrencyPlan:
    eligible: tuple[OptionInstrument, ...]
    per_expiry: dict[datetime, tuple[OptionInstrument, ...]]
    selected: list[OptionInstrument]
    reasons: dict[str, str]
    wing_sequence: tuple[OptionInstrument, ...]
    wing_cursor: int
    status: DataStatus
    reason: str | None
    price_time_basis: str
    price_data_age_seconds: int


def validate_full_chain_snapshot(
    instruments: Sequence[OptionInstrument], settings: Phase8Settings
) -> FullChainValidation:
    """Accept every full-chain row or reject the complete cycle; never truncate."""
    rows = tuple(instruments)
    counts = {currency: 0 for currency in _CURRENCIES}
    seen: set[str] = set()
    for instrument in rows:
        if not isinstance(instrument, OptionInstrument):
            raise TypeError("full-chain snapshot must contain canonical OptionInstrument rows")
        if instrument.symbol in seen:
            return FullChainValidation(
                DataStatus.ERROR, (), "DUPLICATE_SYMBOL", len(rows), MappingProxyType(counts)
            )
        seen.add(instrument.symbol)
        counts[instrument.underlying] += 1
    over_per_underlying = any(
        count > settings.max_full_chain_records_per_underlying for count in counts.values()
    )
    over_total = len(rows) > settings.max_full_chain_records_total
    if over_per_underlying or over_total:
        reason = "PER_UNDERLYING_CAP" if over_per_underlying else "TOTAL_CAP"
        return FullChainValidation(
            DataStatus.PARTIAL, (), reason, len(rows), MappingProxyType(counts)
        )
    return FullChainValidation(
        DataStatus.AVAILABLE, rows, None, 0, MappingProxyType(counts)
    )


def _source_price_state(
    metric: OptionMetricValue | None,
    *,
    as_of: datetime,
    max_source_age_seconds: int,
    max_fetch_age_seconds: int,
) -> tuple[Decimal | None, DataStatus, str | None, str | None, int | None]:
    if metric is None or metric.status is not DataStatus.AVAILABLE or metric.value is None:
        return None, DataStatus.NOT_AVAILABLE, "UNDERLYING_PRICE_NOT_AVAILABLE", None, None
    if metric.metric != "underlying_price" or metric.value <= 0 or metric.unit_status not in {
        UnitStatus.VERIFIED, UnitStatus.SOURCE_NATIVE_UNVERIFIED
    }:
        return None, DataStatus.NOT_AVAILABLE, "UNDERLYING_PRICE_CONTRACT_INVALID", None, None
    timestamp = None
    time_basis = None
    if metric.timestamp_semantics is TimestampSemantics.VERIFIED:
        timestamp = metric.field_last_updated_at or metric.exchange_timestamp
        if timestamp is not None:
            time_basis = "EXCHANGE_TIMESTAMP"
            max_age_seconds = max_source_age_seconds
    if timestamp is None and metric.fetched_at is not None:
        # REST book summaries intentionally expose no reliable event timestamp.
        # Their receive time remains explicit and is judged against the 90-min chain budget.
        timestamp = metric.fetched_at
        time_basis = "FETCHED_AT"
        max_age_seconds = max_fetch_age_seconds
    if timestamp is None:
        return None, DataStatus.NOT_AVAILABLE, "UNDERLYING_PRICE_TIMESTAMP_MISSING", None, None
    age = (as_of - timestamp).total_seconds()
    if age < 0:
        return None, DataStatus.NOT_AVAILABLE, "UNDERLYING_PRICE_FUTURE_TIMESTAMP", time_basis, 0
    if age > max_age_seconds:
        return None, DataStatus.STALE, "UNDERLYING_PRICE_STALE", time_basis, int(age)
    return metric.value, DataStatus.AVAILABLE, None, time_basis, int(age)


def _time_valid_instrument(
    instrument: OptionInstrument, *, underlying: str, as_of: datetime, settings: Phase8Settings
) -> bool:
    expiry_limit = as_of + timedelta(days=settings.expiry_days)
    return (
        instrument.underlying == underlying
        and instrument.status is DataStatus.AVAILABLE
        and instrument.is_active
        and instrument.instrument_state == "open"
        and instrument.expires_at > as_of
        and instrument.expires_at <= expiry_limit
    )


def _eligible_instruments(
    instruments: Sequence[OptionInstrument],
    *,
    underlying: str,
    price: Decimal,
    as_of: datetime,
    settings: Phase8Settings,
) -> tuple[OptionInstrument, ...]:
    boundary = Decimal(str(settings.max_abs_moneyness_pct))
    eligible = []
    for instrument in instruments:
        if not _time_valid_instrument(
            instrument, underlying=underlying, as_of=as_of, settings=settings
        ):
            continue
        moneyness_pct = abs((instrument.strike - price) / price) * Decimal("100")
        if moneyness_pct <= boundary:
            eligible.append(instrument)
    return tuple(sorted(eligible, key=lambda item: (
        item.expires_at, item.option_type.value, item.strike, item.symbol
    )))


def _atm_reservations(
    instruments: Sequence[OptionInstrument], price: Decimal
) -> tuple[list[OptionInstrument], bool]:
    groups: dict[datetime, list[OptionInstrument]] = {}
    for instrument in instruments:
        groups.setdefault(instrument.expires_at, []).append(instrument)
    reserved: list[OptionInstrument] = []
    missing_side = False
    for expiry in sorted(groups):
        expiry_rows = groups[expiry]
        for side in _SIDES:
            choices = [row for row in expiry_rows if row.option_type is side]
            if not choices:
                missing_side = True
                continue
            chosen = min(choices, key=lambda item: (
                abs(item.strike - price), item.strike, item.symbol
            ))
            reserved.append(chosen)
    return reserved, missing_side


def _wing_sequence(
    instruments_by_expiry: Mapping[datetime, Sequence[OptionInstrument]],
    *,
    price: Decimal,
    reserved_symbols: set[str],
) -> tuple[OptionInstrument, ...]:
    buckets: list[list[OptionInstrument]] = []
    for expiry in sorted(instruments_by_expiry):
        expiry_rows = instruments_by_expiry[expiry]
        for side in _SIDES:
            if side is OptionType.CALL:
                rows = [
                    item for item in expiry_rows
                    if item.option_type is side and item.strike > price
                ]
            else:
                rows = [
                    item for item in expiry_rows
                    if item.option_type is side and item.strike < price
                ]
            rows = [item for item in rows if item.symbol not in reserved_symbols]
            rows.sort(key=lambda item: (
                abs((item.strike / price).ln()), item.strike, item.symbol
            ))
            buckets.append(rows)

    sequence: list[OptionInstrument] = []
    cursors = [0] * len(buckets)
    while True:
        found = False
        for bucket_index, bucket in enumerate(buckets):
            cursor = cursors[bucket_index]
            if cursor < len(bucket):
                sequence.append(bucket[cursor])
                cursors[bucket_index] += 1
                found = True
        if not found:
            break
    return tuple(sequence)


def _coverage_for(
    *,
    status: DataStatus,
    reason: str | None,
    eligible: Sequence[OptionInstrument] | None,
    selected: Sequence[OptionInstrument],
    price_time_basis: str | None = None,
    price_data_age_seconds: int | None = None,
) -> UnderlyingCoverage:
    eligible_by_expiry: dict[datetime, int] = {}
    selected_by_expiry: dict[datetime, dict[OptionType, int]] = {}
    if eligible is not None:
        for instrument in eligible:
            eligible_by_expiry[instrument.expires_at] = eligible_by_expiry.get(instrument.expires_at, 0) + 1
    for instrument in selected:
        counts = selected_by_expiry.setdefault(instrument.expires_at, {OptionType.CALL: 0, OptionType.PUT: 0})
        counts[instrument.option_type] += 1
    expiry_keys = sorted(set(eligible_by_expiry) | set(selected_by_expiry))
    expiry_coverage = {}
    for expiry in expiry_keys:
        side_counts = selected_by_expiry.get(expiry, {OptionType.CALL: 0, OptionType.PUT: 0})
        expiry_coverage[expiry.isoformat()] = ExpiryCoverage(
            eligible_count=eligible_by_expiry.get(expiry, 0),
            selected_count=sum(side_counts.values()),
            call_selected_count=side_counts[OptionType.CALL],
            put_selected_count=side_counts[OptionType.PUT],
            status=(DataStatus.AVAILABLE if side_counts[OptionType.CALL] and side_counts[OptionType.PUT]
                    else DataStatus.PARTIAL),
        )
    return UnderlyingCoverage(
        status=status,
        reason=reason,
        eligible_count=None if eligible is None else len(eligible),
        selected_count=len(selected),
        expiry=MappingProxyType(expiry_coverage),
        price_time_basis=price_time_basis,
        price_data_age_seconds=price_data_age_seconds,
    )


def _stale_previous_selection(
    symbols_or_instruments: Sequence[str | OptionInstrument] | None,
    *,
    underlying: str,
    catalog: Mapping[str, OptionInstrument],
    as_of: datetime,
    settings: Phase8Settings,
) -> tuple[OptionInstrument, ...] | None:
    if not symbols_or_instruments:
        return None
    symbols = [
        item.symbol if isinstance(item, OptionInstrument) else item
        for item in symbols_or_instruments
    ]
    if (
        any(not isinstance(symbol, str) or not symbol for symbol in symbols)
        or len(symbols) != len(set(symbols))
        or len(symbols) > settings.max_ticker_instruments_per_currency
    ):
        return None
    instruments = [catalog.get(symbol) for symbol in symbols]
    if any(item is None for item in instruments):
        return None
    result = tuple(item for item in instruments if item is not None)
    if not all(_time_valid_instrument(
        item, underlying=underlying, as_of=as_of, settings=settings
    ) for item in result):
        return None
    return result


def select_ticker_universe(
    instruments: Sequence[OptionInstrument],
    underlying_prices: Mapping[str, OptionMetricValue],
    as_of: datetime,
    settings: Phase8Settings,
    *,
    previous_selection: Mapping[str, Sequence[str | OptionInstrument]] | None = None,
) -> UniverseSelection:
    """Select a deterministic expiry-stratified ticker subset for BTC and ETH.

    Price evidence must be a canonical, unit-verified source metric with a
    fresh source timestamp. Stale/missing prices never produce a new selection.
    An existing selection is retained only while each member remains listed,
    active, open, unexpired, within the horizon, and under configured caps.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware UTC")
    as_of = as_of.astimezone(timezone.utc)
    rows = tuple(instruments)
    chain_check = validate_full_chain_snapshot(rows, settings)
    empty_selected = {currency: () for currency in _CURRENCIES}
    empty_coverage = {
        currency: _coverage_for(
            status=DataStatus.PARTIAL if chain_check.status is not DataStatus.AVAILABLE else DataStatus.NOT_AVAILABLE,
            reason=chain_check.reason or "NO_ELIGIBLE_OPTIONS",
            eligible=() if chain_check.status is DataStatus.AVAILABLE else None,
            selected=(),
        )
        for currency in _CURRENCIES
    }
    empty_reasons: dict[str, dict[str, str]] = {currency: {} for currency in _CURRENCIES}
    if chain_check.status is not DataStatus.AVAILABLE:
        return UniverseSelection(
            MappingProxyType(empty_selected),
            MappingProxyType(empty_coverage),
            MappingProxyType({key: MappingProxyType(value) for key, value in empty_reasons.items()}),
        )
    catalog = {item.symbol: item for item in chain_check.records}
    if len(rows) != len(catalog):
        raise ValueError("instrument catalog contains duplicate symbols")

    coverage: dict[str, UnderlyingCoverage] = {}
    selected: dict[str, list[OptionInstrument]] = {currency: [] for currency in _CURRENCIES}
    reasons: dict[str, dict[str, str]] = {currency: {} for currency in _CURRENCIES}
    plans: dict[str, _CurrencyPlan] = {}
    stale_currencies: set[str] = set()

    for currency in _CURRENCIES:
        price, price_status, price_reason, price_time_basis, price_data_age = _source_price_state(
            underlying_prices.get(currency),
            as_of=as_of,
            max_source_age_seconds=settings.markprice_stale_after_seconds,
            max_fetch_age_seconds=settings.chain_stale_after_seconds,
        )
        if price is None:
            previous = _stale_previous_selection(
                (previous_selection or {}).get(currency),
                underlying=currency,
                catalog=catalog,
                as_of=as_of,
                settings=settings,
            )
            if previous is not None:
                selected[currency].extend(previous)
                stale_currencies.add(currency)
                for item in previous:
                    reasons[currency][item.symbol] = "RETAINED_PRIOR_SELECTION"
                coverage[currency] = _coverage_for(
                    status=DataStatus.STALE,
                    reason=f"{price_reason}_RETAINED_PRIOR_SELECTION",
                    eligible=None,
                    selected=previous,
                    price_time_basis=price_time_basis,
                    price_data_age_seconds=price_data_age,
                )
            else:
                reason = price_reason
                output_status = (
                    DataStatus.STALE if price_status is DataStatus.STALE else DataStatus.NOT_AVAILABLE
                )
                if (previous_selection or {}).get(currency):
                    reason = "PREVIOUS_SELECTION_INVALID"
                    output_status = DataStatus.NOT_AVAILABLE
                coverage[currency] = _coverage_for(
                    status=output_status,
                    reason=reason,
                    eligible=None,
                    selected=(),
                    price_time_basis=price_time_basis,
                    price_data_age_seconds=price_data_age,
                )
            continue

        eligible = _eligible_instruments(
            chain_check.records,
            underlying=currency,
            price=price,
            as_of=as_of,
            settings=settings,
        )
        groups: dict[datetime, tuple[OptionInstrument, ...]] = {}
        for instrument in eligible:
            groups.setdefault(instrument.expires_at, tuple())
            groups[instrument.expires_at] = (*groups[instrument.expires_at], instrument)
        if not eligible:
            coverage[currency] = _coverage_for(
                status=DataStatus.NOT_AVAILABLE,
                reason="NO_ELIGIBLE_OPTIONS",
                eligible=eligible,
                selected=(),
                price_time_basis=price_time_basis,
                price_data_age_seconds=price_data_age,
            )
            continue

        reservations, missing_side = _atm_reservations(eligible, price)
        if len(reservations) > settings.max_ticker_instruments_per_currency:
            coverage[currency] = _coverage_for(
                status=DataStatus.PARTIAL,
                reason="ATM_RESERVATIONS_EXCEED_PER_UNDERLYING_CAP",
                eligible=eligible,
                selected=(),
                price_time_basis=price_time_basis,
                price_data_age_seconds=price_data_age,
            )
            continue
        plan_reason = "ATM_SIDE_UNAVAILABLE" if missing_side else None
        reserved_symbols = {item.symbol for item in reservations}
        plans[currency] = _CurrencyPlan(
            eligible=eligible,
            per_expiry=groups,
            selected=list(reservations),
            reasons={
                item.symbol: f"ATM_{item.option_type.value.upper()}" for item in reservations
            },
            wing_sequence=_wing_sequence(
                groups, price=price, reserved_symbols=reserved_symbols
            ),
            wing_cursor=0,
            status=DataStatus.PARTIAL if missing_side else DataStatus.AVAILABLE,
            reason=plan_reason,
            price_time_basis=price_time_basis or "UNKNOWN",
            price_data_age_seconds=price_data_age if price_data_age is not None else 0,
        )

    stale_count = sum(len(selected[currency]) for currency in stale_currencies)
    if stale_count > settings.max_ticker_subscriptions_total:
        for currency in stale_currencies:
            selected[currency].clear()
            coverage[currency] = _coverage_for(
                status=DataStatus.NOT_AVAILABLE,
                reason="PRIOR_SELECTION_EXCEEDS_TOTAL_CAP",
                eligible=None,
                selected=(),
            )
            reasons[currency].clear()
        stale_currencies.clear()

    reservation_count = sum(len(plan.selected) for plan in plans.values())
    if stale_count + reservation_count > settings.max_ticker_subscriptions_total:
        for currency, plan in plans.items():
            coverage[currency] = _coverage_for(
                status=DataStatus.PARTIAL,
                reason="ATM_RESERVATIONS_EXCEED_TOTAL_CAP",
                eligible=plan.eligible,
                selected=(),
                price_time_basis=plan.price_time_basis,
                price_data_age_seconds=plan.price_data_age_seconds,
            )
        plans.clear()
    else:
        for currency, plan in plans.items():
            selected[currency].extend(plan.selected)
            reasons[currency].update(plan.reasons)

        available_slots = settings.max_ticker_subscriptions_total - sum(
            len(items) for items in selected.values()
        )
        while available_slots > 0:
            added = False
            for currency in _CURRENCIES:
                plan = plans.get(currency)
                if plan is None or len(selected[currency]) >= settings.max_ticker_instruments_per_currency:
                    continue
                while plan.wing_cursor < len(plan.wing_sequence):
                    candidate = plan.wing_sequence[plan.wing_cursor]
                    plan.wing_cursor += 1
                    if candidate.symbol not in reasons[currency]:
                        selected[currency].append(candidate)
                        reasons[currency][candidate.symbol] = f"OTM_{candidate.option_type.value.upper()}_WING"
                        available_slots -= 1
                        added = True
                        break
            if not added:
                break

        for currency, plan in plans.items():
            current = tuple(selected[currency])
            if len(current) < len(plan.eligible) and plan.reason is None:
                status = DataStatus.PARTIAL
                reason = "BOUNDED_TICKER_SAMPLE"
            else:
                status = plan.status
                reason = plan.reason
            coverage[currency] = _coverage_for(
                status=status,
                reason=reason,
                eligible=plan.eligible,
                selected=current,
                price_time_basis=plan.price_time_basis,
                price_data_age_seconds=plan.price_data_age_seconds,
            )

    for currency in _CURRENCIES:
        coverage.setdefault(currency, _coverage_for(
            status=DataStatus.NOT_AVAILABLE,
            reason="NO_ELIGIBLE_OPTIONS",
            eligible=(),
            selected=(),
        ))
        if coverage[currency].status is DataStatus.PARTIAL and not selected[currency]:
            reasons[currency].clear()

    frozen_selected = {key: tuple(value) for key, value in selected.items()}
    return UniverseSelection(
        MappingProxyType(frozen_selected),
        MappingProxyType(coverage),
        MappingProxyType({key: MappingProxyType(value) for key, value in reasons.items()}),
    )
