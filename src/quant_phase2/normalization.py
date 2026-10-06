"""Explicit OI unit normalization and historical change calculations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from .contracts import DataStatus, FundingObservation, OIChange, OIObservation


@dataclass(frozen=True, slots=True)
class OINormalization:
    open_interest_base: Decimal | None
    open_interest_quote: Decimal | None
    open_interest_usd: Decimal | None
    method: str | None
    status: DataStatus


def normalize_open_interest(
    *, raw_value: Decimal | None, raw_unit: str, mark_price: Decimal | None,
    contract_multiplier: Decimal | None = None, quote_asset: str | None = None,
) -> OINormalization:
    if raw_value is None or raw_value < 0:
        return OINormalization(None, None, None, "missing_raw_value", DataStatus.NOT_AVAILABLE)
    if raw_unit == "BASE_ASSET":
        if mark_price is None or mark_price <= 0:
            return OINormalization(raw_value, None, None, "base_quantity_missing_mark_price", DataStatus.NOT_AVAILABLE)
        usd = raw_value * mark_price
        return OINormalization(raw_value, usd, usd, "base_quantity_times_mark_price", DataStatus.AVAILABLE)
    if raw_unit == "CONTRACTS":
        if contract_multiplier is None or contract_multiplier <= 0:
            return OINormalization(None, None, None, "contracts_missing_multiplier", DataStatus.NOT_AVAILABLE)
        base = raw_value * contract_multiplier
        if mark_price is None or mark_price <= 0:
            return OINormalization(base, None, None, "contracts_multiplier_missing_mark_price", DataStatus.NOT_AVAILABLE)
        usd = base * mark_price
        return OINormalization(base, usd, usd, "contracts_times_multiplier_times_mark_price", DataStatus.AVAILABLE)
    if raw_unit in {"USD_NOTIONAL", "QUOTE_NOTIONAL"}:
        if mark_price is None or mark_price <= 0:
            return OINormalization(None, raw_value, raw_value, "quote_notional_missing_mark_price", DataStatus.NOT_AVAILABLE)
        base = raw_value / mark_price
        return OINormalization(base, raw_value, raw_value, "exchange_reported_quote_notional", DataStatus.AVAILABLE)
    if raw_unit in {"UNKNOWN", "UNCONFIRMED", ""}:
        return OINormalization(None, None, None, "UNCONFIRMED_UNIT", DataStatus.NOT_AVAILABLE)
    return OINormalization(None, None, None, "UNRESOLVED_UNIT", DataStatus.NOT_AVAILABLE)


def apply_derivative_freshness(
    observations: Iterable[OIObservation], now: datetime, *, max_age_seconds: int
) -> list[OIObservation]:
    """Mark old observations stale without changing their raw or normalized values.

    Some public feeds do not publish an event timestamp. For those feeds the
    receipt timestamp is the only honest freshness clock; it is never relabeled
    as ``exchange_timestamp``.
    """
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    result: list[OIObservation] = []
    for row in observations:
        observed_at = row.exchange_timestamp or row.fetched_at
        age = (now - observed_at).total_seconds()
        if row.status is DataStatus.AVAILABLE and age < 0:
            result.append(replace(row, status=DataStatus.STALE))
        elif row.status is DataStatus.AVAILABLE and age > max_age_seconds:
            result.append(replace(row, status=DataStatus.STALE))
        else:
            result.append(row)
    return result


def apply_funding_freshness(
    observations: Iterable[FundingObservation], now: datetime, *, max_age_seconds: int
) -> list["FundingObservation"]:
    """Apply the funding freshness clock independently from OI."""
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    result = []
    for row in observations:
        observed_at = row.exchange_timestamp or row.fetched_at
        age = (now - observed_at).total_seconds()
        if row.status is DataStatus.AVAILABLE and age < 0:
            result.append(replace(row, status=DataStatus.STALE))
        elif row.status is DataStatus.AVAILABLE and age > max_age_seconds:
            result.append(replace(row, status=DataStatus.STALE))
        else:
            result.append(row)
    return result


def compute_oi_change(
    observations: Iterable[OIObservation], *, requested_window_seconds: int, canonical_symbol: str
) -> OIChange:
    rows = sorted(
        (row for row in observations if row.exchange_timestamp is not None and row.open_interest_usd is not None),
        key=lambda row: row.exchange_timestamp,
    )
    if len(rows) < 2:
        return OIChange(
            canonical_symbol, "unknown", requested_window_seconds, None, None, None, None, None, None,
            DataStatus.NOT_AVAILABLE, "at_least_two_fresh_normalized_observations_required",
        )
    new = rows[-1]
    target = new.exchange_timestamp.timestamp() - requested_window_seconds
    old = min(rows[:-1], key=lambda row: abs(row.exchange_timestamp.timestamp() - target))
    old_value = old.open_interest_usd
    new_value = new.open_interest_usd
    absolute = new_value - old_value
    percent = None if old_value == 0 else absolute / old_value * Decimal("100")
    status = DataStatus.AVAILABLE if old.status is DataStatus.AVAILABLE and new.status is DataStatus.AVAILABLE else DataStatus.STALE
    return OIChange(
        canonical_symbol=canonical_symbol,
        exchange=new.exchange,
        requested_window_seconds=requested_window_seconds,
        actual_old_timestamp=old.exchange_timestamp,
        actual_new_timestamp=new.exchange_timestamp,
        old_value_usd=old_value,
        new_value_usd=new_value,
        change_absolute_usd=absolute,
        change_percent=percent,
        status=status,
        reason=None if status is DataStatus.AVAILABLE else "source_not_fresh",
    )
