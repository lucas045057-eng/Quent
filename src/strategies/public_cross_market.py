"""Fail-closed normalization of free Binance and Bybit public derivatives data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from strategies.contracts import MarketObservation


OI_PERIOD_SECONDS = 900
FUNDING_PERIOD_MS = 8 * 60 * 60 * 1000
FUNDING_ALIGNMENT_TOLERANCE = timedelta(seconds=1)
MAX_SOURCE_CLOCK_AGE_SECONDS = 120


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _time_ms(value):
    try:
        raw = int(value)
        if raw <= 0:
            return None
        return datetime.fromtimestamp(raw / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _list(value):
    if isinstance(value, list):
        return value
    # Windows PowerShell 5 serializes a top-level JSON array through its
    # Collection adapter as {value:[...],Count:n}; unwrap only that exact shape.
    if isinstance(value, dict) and isinstance(value.get("value"), list) and value.get("Count") == len(value["value"]):
        return value["value"]
    return []


def _unavailable(symbol, kind, fetched_at, reason):
    return MarketObservation(symbol=symbol, kind=kind, provider="public_cross_market",
        source_ref="public_cross_market:" + kind, source_group="PUBLIC_BINANCE_BYBIT:" + kind,
        observed_at=None, fetched_at=fetched_at, processed_at=fetched_at,
        availability="UNAVAILABLE", freshness="UNKNOWN", quality="UNKNOWN", coverage="UNKNOWN", reason=reason)


def _binance_rows(payload, symbol):
    rows = []
    for row in _list((payload.get("binance") or {}).get("open_interest_history")):
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            continue
        at = _time_ms(row.get("timestamp"))
        value = _number(row.get("sumOpenInterestValue"))
        if at is not None and value is not None and value > 0:
            rows.append((int(at.timestamp() * 1000), value))
    return dict(rows)


def _bybit_oi_rows(payload, symbol):
    section = (payload.get("bybit") or {}).get("open_interest") or {}
    result = section.get("result") or {}
    if section.get("retCode") != 0 or result.get("category") != "linear" or result.get("symbol") != symbol:
        return {}
    rows = {}
    for row in _list(result.get("list")):
        if not isinstance(row, dict):
            continue
        at = _time_ms(row.get("timestamp"))
        value = _number(row.get("openInterest"))
        if at is not None and value is not None and value > 0:
            rows[int(at.timestamp() * 1000)] = value
    return rows


def _bybit_mark_prices(payload, symbol):
    bybit = payload.get("bybit") or {}
    if bybit.get("mark_price_symbol") != symbol:
        return {}
    response = bybit.get("mark_price_klines") or {}
    result = response.get("result") or {}
    if response.get("retCode") != 0 or result.get("symbol") not in (None, symbol):
        return {}
    rows = {}
    for row in _list(result.get("list")):
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        start = _time_ms(row[0])
        close = _number(row[4])
        if start is not None and close is not None and close > 0:
            rows[int(start.timestamp() * 1000) + 60_000] = close
    return rows


def normalize_public_cross_oi(payload, *, symbol, fetched_at):
    """Aggregate aligned 15m Binance USDT value and Bybit base OI at mark close."""
    try:
        if not symbol.endswith("USDT") or not isinstance(payload, dict):
            raise ValueError
        binance = _binance_rows(payload, symbol)
        bybit = _bybit_oi_rows(payload, symbol)
        marks = _bybit_mark_prices(payload, symbol)
        boundary_ms = int(fetched_at.timestamp() * 1000) // (OI_PERIOD_SECONDS * 1000) * (OI_PERIOD_SECONDS * 1000)
        latest_closed_ms = boundary_ms - OI_PERIOD_SECONDS * 1000
        common = sorted(t for t in binance.keys() & bybit.keys() & marks.keys() if t <= latest_closed_ms)
        if len(common) < 2 or common[-1] - common[-2] != OI_PERIOD_SECONDS * 1000:
            raise ValueError
        values = [binance[t] + bybit[t] * marks[t] for t in common[-2:]]
        if min(values) <= 0:
            raise ValueError
        change = values[-1] / values[0] - Decimal(1)
        event_at = datetime.fromtimestamp(common[-1] / 1000, timezone.utc)
        age = (fetched_at - event_at).total_seconds()
        if not 0 <= age <= 3600:
            raise ValueError
        return MarketObservation(symbol=symbol, kind="CROSS_OI", provider="public_cross_market",
            source_ref=f"public_cross_market:CROSS_OI:{symbol}:{common[-2]}:{common[-1]}",
            source_group="PUBLIC_BINANCE_BYBIT_CROSS_OI", value=change, unit="CHANGE_RATIO",
            source_event_time=event_at, observed_at=event_at, fetched_at=fetched_at, processed_at=fetched_at,
            availability="AVAILABLE", freshness="FRESH", quality="VALID", coverage="COMPLETE",
            source_context={"venues": ["binance", "bybit"], "scope": "TWO_PUBLIC_VENUE_SUBSET_NOT_WHOLE_MARKET",
                "interval_seconds": OI_PERIOD_SECONDS, "binance_unit": "USDT_NOTIONAL",
                "bybit_unit": "BASE_OI_TIMES_MATCHED_ONE_MINUTE_MARK_CLOSE_USDT",
                "sample_times_ms": common[-2:], "binance_values": [str(binance[t]) for t in common[-2:]],
                "bybit_base_values": [str(bybit[t]) for t in common[-2:]],
                "bybit_mark_closes": [str(marks[t]) for t in common[-2:]]},
            reason="VERIFIED_TWO_VENUE_15M_OI_CHANGE;NOT_WHOLE_MARKET")
    except (ValueError, TypeError, KeyError, OverflowError):
        return _unavailable(symbol, "CROSS_OI", fetched_at, "PUBLIC_CROSS_OI_SCOPE_UNIT_OR_HISTORY_INCOMPLETE")


def _period_matches_eight_hours(left, right):
    return abs((right - left) - timedelta(hours=8)) <= FUNDING_ALIGNMENT_TOLERANCE


def _funding_history(rows, *, symbol, time_field):
    selected = []
    for row in _list(rows):
        if not isinstance(row, dict) or row.get("symbol") != symbol:
            continue
        at = _time_ms(row.get(time_field))
        if at is not None:
            selected.append(at)
    selected = sorted(set(selected))
    return selected[-2:] if len(selected) >= 2 else []


def normalize_public_cross_funding(payload, *, symbol, fetched_at):
    """Require verified 8h settlements and fresh, symbol-bound rates at both venues."""
    try:
        if not symbol.endswith("USDT") or not isinstance(payload, dict):
            raise ValueError
        binance = payload.get("binance") or {}
        premium = binance.get("premium_index") or {}
        b_clock = _time_ms(premium.get("time"))
        b_next = _time_ms(premium.get("nextFundingTime"))
        b_rate = _number(premium.get("lastFundingRate"))
        b_history = _funding_history(binance.get("funding_history"), symbol=symbol, time_field="fundingTime")
        if premium.get("symbol") != symbol or b_clock is None or b_next is None or b_rate is None or len(b_history) != 2:
            raise ValueError
        if not _period_matches_eight_hours(b_history[0],b_history[1]) or not _period_matches_eight_hours(b_history[1],b_next):
            raise ValueError
        b_age = (fetched_at - b_clock).total_seconds()
        if not 0 <= b_age <= MAX_SOURCE_CLOCK_AGE_SECONDS or b_history[1] > b_clock:
            raise ValueError

        bybit = payload.get("bybit") or {}
        tickers = bybit.get("tickers") or {}
        ticker_result = tickers.get("result") or {}
        rows = _list(ticker_result.get("list"))
        ticker = next((row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol), None)
        y_clock = _time_ms(tickers.get("time"))
        y_rate = _number(ticker.get("fundingRate")) if ticker else None
        y_interval_hour = _number(ticker.get("fundingIntervalHour")) if ticker else None
        y_next = _time_ms(ticker.get("nextFundingTime")) if ticker else None
        instruments = bybit.get("instruments") or {}
        instrument_result = instruments.get("result") or {}
        instrument_rows = _list(instrument_result.get("list"))
        instrument = next((row for row in instrument_rows if isinstance(row, dict) and row.get("symbol") == symbol), None)
        y_interval_min = _number(instrument.get("fundingInterval")) if instrument else None
        y_history = _funding_history((bybit.get("funding_history") or {}).get("result", {}).get("list"),
            symbol=symbol, time_field="fundingRateTimestamp")
        if (tickers.get("retCode") != 0 or ticker_result.get("category") != "linear" or y_clock is None
            or y_rate is None or y_interval_hour != 8 or y_interval_min != 480 or y_next is None or len(y_history) != 2
            or instruments.get("retCode") != 0 or (bybit.get("funding_history") or {}).get("retCode") != 0):
            raise ValueError
        if not _period_matches_eight_hours(y_history[0],y_history[1]) or not _period_matches_eight_hours(y_history[1],y_next):
            raise ValueError
        y_age = (fetched_at - y_clock).total_seconds()
        if not 0 <= y_age <= MAX_SOURCE_CLOCK_AGE_SECONDS or y_history[1] > y_clock:
            raise ValueError

        rates = {"binance": b_rate, "bybit": y_rate}
        selected = max(rates, key=lambda venue: abs(rates[venue]))
        event_at = datetime.fromtimestamp(min(b_clock.timestamp(), y_clock.timestamp()), timezone.utc)
        return MarketObservation(symbol=symbol, kind="CROSS_FUNDING", provider="public_cross_market",
            source_ref=f"public_cross_market:CROSS_FUNDING:{symbol}:{int(b_clock.timestamp()*1000)}:{int(y_clock.timestamp()*1000)}",
            source_group="PUBLIC_BINANCE_BYBIT_CROSS_FUNDING", value=rates[selected], unit="RATE_RATIO",
            source_event_time=event_at, observed_at=fetched_at, fetched_at=fetched_at, processed_at=fetched_at,
            availability="AVAILABLE", freshness="FRESH", quality="VALID", coverage="COMPLETE",
            source_context={"venues": ["binance", "bybit"], "scope": "TWO_PUBLIC_VENUE_SUBSET_NOT_WHOLE_MARKET",
                "native_interval_hours": {"binance": 8, "bybit": 8}, "rates": {key: str(value) for key, value in rates.items()},
                "source_clocks": {"binance": b_clock.isoformat(), "bybit": y_clock.isoformat()}, "selected_provider": selected},
            reason="VERIFIED_TWO_VENUE_8H_MAX_ABSOLUTE_RATE;NOT_WHOLE_MARKET")
    except (ValueError, TypeError, KeyError, OverflowError):
        return _unavailable(symbol, "CROSS_FUNDING", fetched_at, "PUBLIC_CROSS_FUNDING_SCOPE_CLOCK_OR_PERIOD_INCOMPLETE")
